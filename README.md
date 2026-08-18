# Meal Planner Bot

A Telegram assistant that collects a household nutrition profile, generates a
seven-day meal plan through LiteLLM, confirms edits, builds groceries
asynchronously, records actual meals, and records cooked, skipped, or swapped
meal outcomes.

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

## Telegram workflows

- `/submit_meals` starts guided actual-meal logging. It collects a date from
  today through the previous seven days, a meal type, and a description one
  at a time. Repeated meals of the same type and date are retained.
- `/checkin` shows buttons for cooked, skipped, or swapped outcomes on today's
  confirmed plan. `/submit_meals` does not require an active plan.
- `/cancel` clears an unfinished meal or plan workflow. Starting `/submit_meals`
  or `/plan` replaces an older unfinished workflow.
- `/plan` asks for a request-specific preference before starting asynchronous
  generation. `no preference` and `anything` remove the extra constraint; the
  saved family profile is never changed. A failed request retains the
  preference so `/plan` can retry it.
- After a draft is displayed, describe whole-plan changes in natural language
  to start an asynchronous revision. The bot replaces the complete draft for
  the same week, preserves earlier plan-specific instructions, and leaves the
  household profile unchanged. While a revision is running, confirmation and
  additional amendments are blocked. A failed revision leaves the original
  draft intact and accepts `retry` or `/cancel`.
- A successful revision is shown with another review/edit/confirm prompt.
  Confirming the final draft starts grocery generation for that exact plan;
  targeted `edit_plan` changes remain available for active confirmed plans.

Conversation state is stored in the user's `CONVERSATION_STATE` DynamoDB item
with a 24-hour expiry, revision checks, and Telegram update idempotency. The
state is treated as expired immediately even if DynamoDB's TTL cleanup has not
run yet.

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
| `TELEGRAM_ALLOWED_USER_IDS` | required for bot | Comma-separated numeric Telegram user IDs allowed in private chats |
| `LLM_API_KEY` | required | Provider credential used by LiteLLM |
| `CONVERSATIONAL_LLM_MODEL` | `gpt-5.6-luna` | Conversational LiteLLM model |
| `CONVERSATIONAL_LLM_REASONING_EFFORT` | `medium` | Conversational reasoning effort |
| `PLANNER_LLM_MODEL` | `gpt-5.6-luna` | Planner and grocery LiteLLM model |
| `PLANNER_LLM_REASONING_EFFORT` | `high` | Planner reasoning effort |
| `DYNAMODB_TABLE_NAME` | `meal-planner` | DynamoDB table |
| `AWS_REGION` | `us-east-1` | AWS client region |
| `BOT_FUNCTION_TIMEOUT_SECONDS` | `30` | Bot Lambda deadline |
| `BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `5` | Bot Telegram HTTP timeout |
| `BOT_LLM_REQUEST_TIMEOUT_SECONDS` | `6` | Per-attempt Bot LLM timeout |
| `BOT_LLM_MAX_RETRIES` | `2` | Bot transient LLM attempts |
| `BOT_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Bot initial retry backoff |
| `BOT_HANDLER_SAFETY_MARGIN_SECONDS` | `4` | Bot non-provider safety margin |
| `PLANNER_FUNCTION_TIMEOUT_SECONDS` | `300` | Planner application deadline |
| `PLANNER_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `10` | Planner Telegram HTTP timeout |
| `PLANNER_LLM_REQUEST_TIMEOUT_SECONDS` | `45` | Per-attempt Planner LLM timeout |
| `PLANNER_LLM_MAX_RETRIES` | `2` | Planner total provider attempts |
| `PLANNER_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Planner initial retry backoff |
| `PLANNER_HANDLER_SAFETY_MARGIN_SECONDS` | `20` | Planner non-provider safety margin |

The settings validator includes every LLM attempt, the maximum bounded retry
wait (5 seconds per retry), each sequential Telegram allowance, and a handler
safety margin. Each function's worst-case budget must fit its configured
deadline; the Planner now has a five-minute (300-second) application deadline
while retaining its 45-second request timeout and two provider attempts. Its
Lambda timeout is 310 seconds, reserving ten seconds for the application to
return after the planner deadline. The application retry loop is the sole LLM
retry layer; provider adapter retries are disabled so the configured attempts
and backoff remain within that deadline. The Bot budget remains below the
30-second HTTP API integration limit. Lambda itself permits up to 900 seconds,
but that larger service limit cannot extend
the synchronous Telegram webhook deadline ([Lambda timeout quota](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
[HTTP API integration quota](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html)).

The former global timeout and retry variables are ignored for compatibility;
use the function-specific settings above.

Never commit `.env` or secret values.

Planner generation makes at most two whole-week provider requests. A timeout
failure is reported separately from invalid structured output; an invalid first
response receives bounded Pydantic validation feedback on the second request.
Neither failure persists a partial plan.

The allowlist must contain Telegram's immutable numeric user IDs, not
usernames or display names. Each approved person can retrieve their numeric
ID with a trusted Telegram ID lookup bot such as `@userinfobot`; verify the
result before adding it to the deployment. For local development, use a
comma-separated value in `.env`, with optional surrounding whitespace:

```dotenv
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

The IDs are identifiers rather than secrets, but they are visible to
operators in CloudFormation parameters and the Bot Lambda configuration.
Only an allowlisted user in a matching private chat can trigger commands,
conversations, or callbacks. Unauthorized users and all group, supergroup,
or channel updates receive no Telegram reply; the webhook still returns HTTP
200 so Telegram does not retry. The webhook secret authenticates Telegram's
HTTP request, while the allowlist authorizes the individual sender.

As defense in depth, disable the bot's ability to join groups through
BotFather after deployment. Application enforcement remains authoritative even
if the bot is later added to a group.

## AWS deployment

Deploy from the repository root with the single typed orchestrator. It creates
two Python 3.14 ARM64 Lambda functions, an HTTP API, and an on-demand
DynamoDB table. The deploying principal needs CloudFormation, Lambda, API
Gateway, DynamoDB, IAM, S3, and Secrets Manager access.

The orchestrator is intentionally pinned to the `meal-planner` AWS profile and
`eu-west-1` region. AWS CLI 2.32.0 or newer is required because authentication
uses `aws login --remote`; the CLI owns the interactive authorization URL and
code exchange. After login, the resolved STS identity is printed and must be
confirmed before any deployment or secret mutation.

Create an ignored `.env` with the required non-example fields below. Never
commit this file or its values:

```dotenv
AWS_PROFILE=meal-planner
AWS_REGION=eu-west-1
STACK_NAME=meal-planner-dev
TELEGRAM_BOT_TOKEN=replace-with-token
TELEGRAM_WEBHOOK_SECRET=replace-with-webhook-secret
LLM_API_KEY=replace-with-llm-key
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
TELEGRAM_BOT_TOKEN_SECRET_NAME=meal-planner/bot-token
TELEGRAM_WEBHOOK_SECRET_NAME=meal-planner/webhook-secret
LLM_API_KEY_SECRET_NAME=meal-planner/llm-key
SYNC_SECRETS=false
```

The routine workflow checks that all three named Secrets Manager secrets exist,
runs Ruff, mypy, pytest, SAM validation, SAM build, and fresh artifact tests,
deploys with a generated refresh token, registers the canonical Telegram
command menu, sets and verifies the webhook, and verifies the deployed
DynamoDB transaction permission:

```bash
uv run python scripts/deploy.py
```

Use `--guided` for the first SAM deployment. SAM retains its interactive
terminal behavior in this mode; routine deployments are non-interactive and
accept an empty changeset:

```bash
uv run python scripts/deploy.py --guided
```

Secret synchronization is a deliberate external mutation and requires both
`SYNC_SECRETS=true` in `.env` and the command-line `--sync-secrets` flag. The
orchestrator creates missing secrets or updates existing ones, but never puts
secret values in command arguments, logs, errors, or summaries:

```bash
uv run python scripts/deploy.py --sync-secrets
```

If deployment succeeds but Telegram configuration or read-only AWS
verification fails, rerun only the idempotent post-deployment stages after
correcting the issue:

```bash
uv run python scripts/deploy.py --post-deploy-only
```

Recovery mode still performs remote login, identity confirmation, stack output
resolution, command registration, webhook verification, and IAM verification;
it never checks or changes secrets, runs quality gates, builds, or deploys.
The stack outputs `WebhookUrl`, `MealPlannerTableName`, `BotFunctionName`, and
`PlannerFunctionName`; malformed or missing outputs are failures. The direct
read-only verifier remains available and accepts an explicit profile:

```bash
uv run python scripts/verify_transaction_permission.py \
  --stack-name meal-planner-dev \
  --profile meal-planner \
  --region eu-west-1
```

The verifier requires `cloudformation:DescribeStacks`,
`lambda:GetFunctionConfiguration`, `dynamodb:DescribeTable`, and
`iam:SimulatePrincipalPolicy`. It does not change the role or substitute for
an end-to-end Telegram test. Secret synchronization and a live deployment are
external mutations; inspect target names and the confirmed AWS identity first.

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

The Telegram command menu and `/help` show the same command reference:

- `/start` — Start onboarding or view what to do next.
- `/help` — Show the available commands.
- `/profile` — View the household profile.
- `/plan` — Create or retry a weekly meal plan.
- `/grocery` — View the active grocery list.
- `/today` — View today's planned meals.
- `/submit_meals` — Log meals eaten in the past week.
- `/checkin` — Record today's planned meal outcomes.
- `/cancel` — Cancel an unfinished workflow.

- `/start` begins onboarding. Supply the family name separately from the
  household size, every household member's name and calorie target, allergies,
  preferences, restrictions, and goals. Known onboarding fields carry across
  conversational turns, so provide only the fields the bot still requests.
- `/profile` shows the persisted family name and individual member details.
- Natural-language no-value answers such as `none`, `nothing`, `no allergies`,
  and `no restrictions` are stored as empty categories. They count as answers,
  while omitted fields remain missing until supplied.
- `/plan` asks for a one-time request preference before asynchronously
  generating a complete seven-day draft. The draft is persisted before
  Telegram delivery; failed generation retains the preference for `/plan` to
  retry.
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
- `/submit_meals` guides actual meal logging, including multiple meals of the
  same type on one date. `/checkin` sends plan-specific buttons for `cooked`,
  `skipped`, and `swapped`. Old, draft, expired, malformed, and superseded
  overlapping-plan callbacks are rejected, even if the older plan still
  covers today.

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
