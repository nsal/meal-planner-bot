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
| `CONVERSATIONAL_LLM_MODEL` | `gpt-5.6-luna` | Conversational LiteLLM model |
| `CONVERSATIONAL_LLM_REASONING_EFFORT` | `medium` | Conversational reasoning effort |
| `PLANNER_LLM_MODEL` | `gpt-5.6-terra` | Planner and grocery LiteLLM model |
| `PLANNER_LLM_REASONING_EFFORT` | `medium` | Planner reasoning effort |
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
The application retry loop is the sole LLM retry layer; provider adapter retries
are disabled so the configured attempts and backoff remain within that deadline.
The Bot budget remains below the 30-second HTTP API integration limit. Lambda
itself permits up to 900 seconds, but that larger service limit cannot extend
the synchronous Telegram webhook deadline ([Lambda timeout quota](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
[HTTP API integration quota](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html)).

The former global timeout and retry variables are ignored for compatibility;
use the function-specific settings above.

Never commit `.env` or secret values.

## AWS deployment

Deploy from the repository root. The deployment creates two ARM64 Lambda
functions, an HTTP API, and an on-demand DynamoDB table. The deploying
principal needs permission to use CloudFormation, Lambda, API Gateway,
DynamoDB, IAM, S3, and Secrets Manager.

Select the account and region explicitly before creating resources. The
commands below use `us-east-1`; replace it with your target region when
needed:

```bash
export AWS_REGION=us-east-1
export STACK_NAME=meal-planner-dev
aws sts get-caller-identity
```

Create three Secrets Manager secrets whose `SecretString` is the raw value,
not a JSON object. Secret names are deployment parameters, so you may use
different names if required by your account:

```bash
aws secretsmanager create-secret --name meal-planner/bot-token \
  --secret-string "$TELEGRAM_BOT_TOKEN"
aws secretsmanager create-secret --name meal-planner/webhook-secret \
  --secret-string "$TELEGRAM_WEBHOOK_SECRET"
aws secretsmanager create-secret --name meal-planner/llm-key \
  --secret-string "$LLM_API_KEY"
```

Validate the template, build the locked `uv` dependencies, and run the
template tests against the fresh SAM artifact:

```bash
uvx --from aws-sam-cli sam validate --lint --region "$AWS_REGION"
uvx --from aws-sam-cli sam build --beta-features
REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py
```

For the first deployment, use guided mode. Accept the generated S3 bucket,
save the answers to `samconfig.toml`, and confirm the IAM capability prompt:

```bash
uvx --from aws-sam-cli sam deploy --guided \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
  TelegramBotTokenSecretName=meal-planner/bot-token \
  TelegramWebhookSecretName=meal-planner/webhook-secret \
  LlmApiKeySecretName=meal-planner/llm-key \
  SecretRefreshToken="$(date +%s)"
```

For later deployments, rerun validation and build, then deploy the same stack
without prompts. Keep the secret names unchanged unless you are deliberately
switching credentials:

```bash
uvx --from aws-sam-cli sam validate --lint --region "$AWS_REGION"
uvx --from aws-sam-cli sam build --beta-features
uvx --from aws-sam-cli sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
  TelegramBotTokenSecretName=meal-planner/bot-token \
  TelegramWebhookSecretName=meal-planner/webhook-secret \
  LlmApiKeySecretName=meal-planner/llm-key \
  SecretRefreshToken="$(date +%s)"
```

After deployment, read the generated URL and register it with Telegram. The
`secret_token` must exactly match the webhook secret in Secrets Manager:

```bash
export WEBHOOK_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookUrl`].OutputValue' \
  --output text)"
curl --fail-with-body \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
curl --fail-with-body \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

The stack also outputs the DynamoDB table and both Lambda function names:

```bash
aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" --query 'Stacks[0].Outputs' --output table
```

Before a smoke test, verify the deployed Bot execution role has the
transaction permission required by meal logging, plan confirmation, and meal
outcome callbacks. The verifier is read-only and requires the calling
principal to have `cloudformation:DescribeStacks`,
`lambda:GetFunctionConfiguration`, `dynamodb:DescribeTable`, and
`iam:SimulatePrincipalPolicy` for the selected region and stack. It does not
change the role or substitute for an end-to-end Telegram test:

```bash
uv run python scripts/verify_transaction_permission.py \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

Success prints an explicit allow for `dynamodb:TransactWriteItems` on the
stack's exact table ARN. Missing outputs, malformed responses, denied or
implicit-deny decisions, and AWS API errors are failures; stop and correct the
deployment before continuing. The command does not print credentials.

This project is greenfield: source-backed meal writes have always created the
date-indexed meal and its `MEAL_UPDATE#<telegram_update_id>` marker together.
No backfill, scan, migration, or markerless-record compatibility lookup is
included. If a markerless `MEAL#...#UPDATE#...` item is discovered before a
deployment, stop the release and investigate the data and deployment history
before using this workflow.

For a non-production Telegram smoke test, send one conversational meal log,
then inspect the table using the update ID shown in the webhook or Lambda
logs. Confirm that the user partition contains one date-indexed meal and one
`MEAL_UPDATE#<update_id>` marker. Replay the same Telegram update (even with
different extracted date, meal type, description, or timestamp) and confirm
that the original meal remains the only meal for that update. Also confirm a
plan confirmation and a meal check-in callback to exercise the other Bot
transaction paths. Remove the webhook and delete the test stack only after
preserving any data needed for investigation.

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

To remove a non-production deployment, first remove the Telegram webhook and
then delete the CloudFormation stack. This deletes the stack-managed table;
export or back up any data you need first:

```bash
curl --fail-with-body \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
aws cloudformation delete-stack --stack-name "$STACK_NAME" \
  --region "$AWS_REGION"
```

## User workflow

- `/start` begins onboarding. Supply the household size, every person's name
  and calorie target, allergies, preferences, restrictions, and goals. Known
  onboarding fields carry across conversational turns, so provide only the
  fields the bot still requests.
- `/profile` shows the persisted profile.
- `/plan` asynchronously generates a complete seven-day draft. The draft is
  persisted before Telegram delivery; if delivery fails, the draft remains
  valid and requesting `/plan` again is the recovery path.
- Ask conversationally to edit an existing meal; missing days or meal types
  are rejected rather than silently created.
- Tell the bot to confirm the draft. Confirmation starts grocery generation.
- Expired drafts cannot be confirmed or edited. Confirmed plans can be edited
  only while they are the active plan covering today.
- Each plan day contains at most one breakfast, lunch, dinner, and snack, so a
  day-and-meal-type selection always addresses one meal.
- Repeating confirmation on a confirmed plan retries groceries only when the
  previous grocery attempt is in `error`; `pending` and `ready` are not reset.
- `/grocery` reports `pending`, `ready`, or `error`, and shows ready sections.
- `/today` shows the active confirmed plan's meals for today.
- `/submit_meals` sends plan-specific buttons for `cooked`, `skipped`, and
  `swapped`. Old, draft, expired, malformed, and superseded overlapping-plan
  callbacks are rejected, even if the older plan still covers today.

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
- Generated drafts are persisted before their Telegram messages. A delivery
  failure does not roll back or invalidate the draft; request `/plan` again to
  recover if the draft was not delivered.
- A slow grocery worker whose plan revision is stale is discarded without
  changing state or sending a notification. Meal outcomes are targeted writes
  and do not invalidate grocery content; meal edits advance the revision and
  trigger a fresh grocery request.
- Incomplete profile updates are retained as a draft, including household
  size and partial member targets, so the next turn can finish onboarding
  without resubmitting known fields.
- Telegram delivery failure: check the endpoint status in logs; logs omit bot
  tokens and message content.
- SAM smoke-test failure: rerun a clean SAM build. Tests reject stale source or
  generated templates.
- Timeout failures: keep configured request timeouts below Lambda deadlines;
  only transient LLM failures are retried. The bot must also fit API Gateway's
  non-increasable 30-second HTTP API integration timeout.
