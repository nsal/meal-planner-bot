# Meal Planner Bot

A Telegram assistant that collects a household nutrition profile, generates a
seven-day meal plan through LiteLLM, confirms edits, builds groceries
asynchronously, and records cooked, skipped, or swapped meal outcomes.

## Architecture

```text
Telegram -> API Gateway HTTP API -> Bot Lambda -> DynamoDB
                                     |
                                     +-> asynchronous Planner Lambda
                                              |
                                              +-> LiteLLM provider
                                              +-> DynamoDB / Telegram
```

The bot Lambda authenticates every webhook before parsing it. The planner
Lambda handles plan generation and grocery finalization outside Telegram's
webhook deadline. Both functions run on Python 3.14 ARM64 and are built from
the locked `uv` environment.

## Prerequisites

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/)
- AWS credentials with CloudFormation, Lambda, API Gateway, DynamoDB, IAM,
  S3, and Secrets Manager access
- AWS SAM CLI (the commands below run it through `uvx`)
- A Telegram bot token from BotFather
- An API key for a LiteLLM-supported provider

Install and verify the project:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Configuration

Local development reads these variables from the environment or an ignored
`.env` file:

| Variable | Default | Purpose |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | required | BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | required for bot | Telegram webhook secret token |
| `LLM_API_KEY` | required | Provider credential used by LiteLLM |
| `LLM_MODEL` | `gpt-4o-mini` | LiteLLM model identifier |
| `DYNAMODB_TABLE_NAME` | `meal-planner` | DynamoDB table |
| `AWS_REGION` | `us-east-1` | AWS client region |
| `BOT_FUNCTION_TIMEOUT_SECONDS` | `30` | Bot Lambda deadline |
| `BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `5` | Bot Telegram HTTP timeout |
| `BOT_LLM_REQUEST_TIMEOUT_SECONDS` | `6` | Per-attempt Bot LLM timeout |
| `BOT_LLM_MAX_RETRIES` | `2` | Bot transient LLM attempts |
| `BOT_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Bot initial retry backoff |
| `BOT_HANDLER_SAFETY_MARGIN_SECONDS` | `4` | Bot non-provider safety margin |
| `PLANNER_FUNCTION_TIMEOUT_SECONDS` | `120` | Planner Lambda deadline |
| `PLANNER_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `10` | Planner Telegram HTTP timeout |
| `PLANNER_LLM_REQUEST_TIMEOUT_SECONDS` | `20` | Per-attempt Planner LLM timeout |
| `PLANNER_LLM_MAX_RETRIES` | `3` | Planner transient LLM attempts |
| `PLANNER_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Planner initial retry backoff |
| `PLANNER_HANDLER_SAFETY_MARGIN_SECONDS` | `20` | Planner non-provider safety margin |

The settings validator includes every LLM attempt, the maximum bounded retry
wait (5 seconds per retry), each sequential Telegram allowance, and a handler
safety margin. Each function's worst-case budget must fit its configured deadline.
The Bot budget remains below the 30-second HTTP API integration limit. Lambda
itself permits up to 900 seconds, but that larger service limit cannot extend
the synchronous Telegram webhook deadline ([Lambda timeout quota](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
[HTTP API integration quota](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html)).

The former global timeout and retry variables are ignored for compatibility;
use the function-specific settings above.

Never commit `.env` or secret values.

## AWS secrets and deployment

Create three Secrets Manager secrets whose `SecretString` is the raw value,
not a JSON object:

```bash
aws secretsmanager create-secret --name meal-planner/bot-token \
  --secret-string "$TELEGRAM_BOT_TOKEN"
aws secretsmanager create-secret --name meal-planner/webhook-secret \
  --secret-string "$TELEGRAM_WEBHOOK_SECRET"
aws secretsmanager create-secret --name meal-planner/llm-key \
  --secret-string "$LLM_API_KEY"
```

Validate, build, and deploy:

```bash
uvx --from aws-sam-cli sam validate --lint --region us-east-1
uvx --from aws-sam-cli sam build --beta-features
REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py
uvx --from aws-sam-cli sam deploy --guided \
  --parameter-overrides \
  TelegramBotTokenSecretName=meal-planner/bot-token \
  TelegramWebhookSecretName=meal-planner/webhook-secret \
  LlmApiKeySecretName=meal-planner/llm-key \
  SecretRefreshToken="$(date +%s)"
```

Use the stack's `WebhookUrl` output to register Telegram. The `secret_token`
must exactly match the webhook secret in Secrets Manager:

```bash
curl --fail-with-body \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

To rotate a secret, update one secret at a time, then deploy with a new unique
`SecretRefreshToken`. The marker changes both Lambda resources, forcing
CloudFormation to re-resolve the versionless Secrets Manager references. For
an LLM key or bot token:

```bash
aws secretsmanager put-secret-value --secret-id meal-planner/llm-key \
  --secret-string "$NEW_LLM_API_KEY"
uvx --from aws-sam-cli sam deploy --parameter-overrides \
  TelegramBotTokenSecretName=meal-planner/bot-token \
  TelegramWebhookSecretName=meal-planner/webhook-secret \
  LlmApiKeySecretName=meal-planner/llm-key \
  SecretRefreshToken="$(date +%s)"
```

For webhook-secret rotation, update the secret and deploy the Lambda before
registering the new Telegram webhook secret, then verify the webhook. A single
accepted secret necessarily has a brief coordinated transition window; dual
secret acceptance is outside this remediation. CloudFormation resolves
dynamic references on resource updates, while a secret-value-only change does
not refresh an existing Lambda environment ([dynamic reference rotation
behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html)).

For rollback, identify the last known-good commit, rebuild it, and redeploy the
same stack parameters. DynamoDB uses on-demand billing and is retained only as
configured by the deployed stack, so inspect CloudFormation changes before
confirming a rollback.

## User workflow

- `/start` begins onboarding. Supply the household size, every person's name
  and calorie target, allergies, preferences, restrictions, and goals.
- `/profile` shows the persisted profile.
- `/plan` asynchronously generates a complete seven-day draft.
- Ask conversationally to edit an existing meal; missing days or meal types
  are rejected rather than silently created.
- Tell the bot to confirm the draft. Confirmation starts grocery generation.
- Repeating confirmation on a confirmed plan retries groceries only when the
  previous grocery attempt is in `error`; `pending` and `ready` are not reset.
- `/grocery` reports `pending`, `ready`, or `error`, and shows ready sections.
- `/today` shows the active confirmed plan's meals for today.
- `/submit_meals` sends plan-specific buttons for `cooked`, `skipped`, and
  `swapped`. Old, draft, expired, or malformed callbacks are rejected.

Conversational metadata supports `log_meal`, `update_profile`, `edit_plan`,
`confirm_plan`, `suggestion`, and `chitchat`. Mutations are validated before
success is reported to the user.

## Prompt customization

Prompt builders live in `src/meal_planner/llm/prompts.py`. Keep their JSON
schemas synchronized with `src/meal_planner/models/schemas.py` and parser
tests. Plan output must include seven unique days, typed dates, allowed meal
types, and `unreported` outcomes. Grocery output must include at least one
non-empty section before it can become ready.

## Troubleshooting

- `403 forbidden`: verify Telegram's `secret_token` and the deployed webhook
  secret match exactly.
- No plan generation: complete every per-person calorie target and inspect the
  planner Lambda logs.
- Grocery state `error`: for the active plan week, confirm again and inspect
  LLM parsing or timeout logs. If that week has expired, generate and confirm
  the current week's plan instead.
- Grocery state `ready` is persisted before its Telegram notification. If the
  notification fails, retry `/grocery` after Telegram connectivity returns.
- A slow grocery worker whose plan revision is stale is discarded without
  changing state or sending a notification. Meal outcomes are targeted writes
  and do not invalidate grocery content; meal edits advance the revision and
  trigger a fresh grocery request.
- Incomplete profile updates are retained as a draft, including household
  size and partial member targets, so the next turn can finish onboarding.
- Telegram delivery failure: check the endpoint status in logs; logs omit bot
  tokens and message content.
- SAM smoke-test failure: rerun a clean SAM build. Tests reject stale source or
  generated templates.
- Timeout failures: keep configured request timeouts below Lambda deadlines;
  only transient LLM failures are retried. The bot must also fit API Gateway's
  non-increasable 30-second HTTP API integration timeout.
