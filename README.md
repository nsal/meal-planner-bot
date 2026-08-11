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
| `TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `10` | Telegram HTTP timeout, maximum 20 |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `20` | LLM timeout, maximum 25 |
| `LLM_MAX_RETRIES` | `3` | Total transient LLM attempts, maximum 5 |
| `LLM_INITIAL_BACKOFF_SECONDS` | `1` | Initial exponential backoff, maximum 5 |

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
  LlmApiKeySecretName=meal-planner/llm-key
```

Use the stack's `WebhookUrl` output to register Telegram. The `secret_token`
must exactly match the webhook secret in Secrets Manager:

```bash
curl --fail-with-body \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

To rotate a secret, write a new version, update Telegram first when rotating
the webhook secret, then redeploy so CloudFormation resolves the new value:

```bash
aws secretsmanager put-secret-value --secret-id meal-planner/llm-key \
  --secret-string "$NEW_LLM_API_KEY"
uvx --from aws-sam-cli sam deploy
```

CloudFormation dynamic references are resolved on resource updates; a secret
rotation alone does not refresh an existing Lambda environment.

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
- Grocery state `error`: confirm the exact plan week again and inspect LLM
  parsing or timeout logs.
- Telegram delivery failure: check the endpoint status in logs; logs omit bot
  tokens and message content.
- SAM smoke-test failure: rerun a clean SAM build. Tests reject stale source or
  generated templates.
- Timeout failures: keep configured request timeouts below Lambda deadlines;
  only transient LLM failures are retried.
