# Meal Planner Bot

A Telegram assistant for building temporary, conversational meal-plan drafts
from a household profile and recently submitted meals. It supports
variable-length meal plan drafts. A draft is plain text for the household to
review; it is not an official plan, a grocery list, or a nutrition or medical
guarantee.

## Architecture

```text
Telegram
   |
   v
API Gateway HTTP API -> Bot Lambda -> DynamoDB
                            |
                            +-> asynchronous Plan Chat Lambda
                                      |
                                      +-> DynamoDB (fresh context)
                                      +-> LiteLLM provider
                                      +-> Telegram
```

The Bot Lambda authenticates and authorizes Telegram updates, handles short
interactions, stores profiles, records submitted meals, and starts Plan Chat
work. The Plan Chat Lambda reloads the session, profile, and meal history
before making one text request to LiteLLM. Both functions use Python 3.14 on
ARM64 and are built from the locked `uv` environment.

DynamoDB stores household profiles, submitted meals, and short-lived
conversation state. Plan Chat does not create or maintain an official meal
plan record. Conversation state has a 24-hour expiry and is replaced when a
new workflow starts or the user ends the session.

## Telegram commands and workflows

The command menu and `/help` expose exactly these commands:

- `/start` — Set up a household profile or see the next setup question.
- `/help` — Show the available commands.
- `/profile` — View and amend the saved household profile.
- `/plan` — Start a conversational meal-plan draft.
- `/submit_meals` — Record meals eaten from UTC today through the previous
  seven dates, inclusive (eight calendar dates).

There is no top-level workflow cancellation command. Each workflow owns its
controls: Plan Chat has an `End planning` button, profile editing has `Back`,
`Done`, and `Close`, and meal review has its review controls. Starting
`/plan`, `/profile`, or `/submit_meals` replaces another unfinished workflow.

### Profile setup and editing

`/start` collects the household name, size, each member's name and calorie
target, dietary constraints, and dietary preferences one step at a time.
Optional protein and fibre targets can be supplied for each member. Answers
are retained in a temporary setup draft until the profile is complete.

`/profile` shows the saved profile and opens button-led editing for family
members, dietary constraints, and dietary preferences. Family changes use
messages such as `Alex 2000`, `Alex 2000 120 30`, or `Alex none` for an
optional target. Dietary entries remain bounded raw text. The application does
not interpret them into a rule language or promise that a generated draft
follows them.

Use `Done` to finish an edit, `Close` to leave without continuing, and `Back`
to navigate. Removal buttons are tied to the displayed profile revision, so a
stale button makes no change and asks the user to reopen `/profile`.

### Plan Chat

1. Use `/plan` after completing a profile.
2. Send a free-form request such as `three simple dinners for next week`.
3. Wait for the asynchronous draft response, then send a follow-up to refine
   it or use `End planning`.

The initial request and each follow-up are bounded plain text. The latest
generated response is retained as context; the application does not keep an
unlimited transcript. Every follow-up reloads the current profile and an
inclusive 21-day window of submitted meals ending on the UTC date associated
with that request. The previous response is included when one exists.

The model may ask one focused clarification question when essential
information is missing. A follow-up is sent through the same session with the
original request, latest response, and current instruction. A request made
while generation is running is not queued; the user can try again after the
current response arrives.

Drafts use plain headings and bullets suitable for Telegram. Meal details may
include approximate calorie estimates, but the application does not parse the
text or provide semantic validation, compliance, calorie, dietary, or medical
guarantees. Submitted history is preference evidence, not an obligation.

Provider failures yield bounded retry guidance only if state recovery and
Telegram delivery succeed. Persistence or Telegram delivery failures can be
silent to the user and are logged with bounded metadata. The first request
returns to an awaiting state; a failed follow-up preserves the last usable
draft. If a session is replaced, ended, expired, or otherwise fails its
ownership check, a late worker result is discarded without delivery.

### Submitted meals

`/submit_meals` first displays meals from UTC today and yesterday, then asks
for:

```text
when, meal type, what you ate
```

`when` accepts `today`, `yesterday`, or a date from UTC today through the
previous seven dates, inclusive (eight calendar dates). The meal type is
`breakfast`, `lunch`, `dinner`, or `snack`. Only the first two commas separate
fields, so later commas stay in the description.
The entry is shown for review before it is saved. The user can cancel that
entry, add another, or finish logging. Repeated Telegram updates are handled
idempotently.

## Prerequisites

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/)
- AWS credentials for CloudFormation, Lambda, API Gateway, DynamoDB, IAM,
  S3, and Secrets Manager
- AWS SAM CLI (used through `uvx`)
- A Telegram bot token from BotFather
- An API key for a LiteLLM-supported provider

Install dependencies and run the local gates:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Configuration

Local development reads environment variables or an ignored `.env` file.
Deployment settings are loaded by `scripts/deploy.py`; Lambda settings are
scoped to the function that consumes them.

| Variable | Default | Used by | Purpose |
|---|---:|---|---|
| `TELEGRAM_BOT_TOKEN` | required | both | BotFather token |
| `DYNAMODB_TABLE_NAME` | `meal-planner` | both | DynamoDB table |
| `AWS_REGION` | `us-east-1` | both | AWS client region |
| `TELEGRAM_WEBHOOK_SECRET` | required | Bot | Telegram webhook secret |
| `TELEGRAM_ALLOWED_USER_IDS` | required | Bot | Comma-separated numeric IDs |
| `BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `5` | Bot | Telegram HTTP timeout |
| `PLAN_CHAT_FUNCTION_NAME` | deployment value | Bot | Async worker name |
| `LLM_API_KEY` | required | Plan Chat | LiteLLM provider credential |
| `PLAN_CHAT_LLM_MODEL` | `gpt-5.6-luna` | Plan Chat | Provider model |
| `PLAN_CHAT_LLM_REASONING_EFFORT` | `high` | Plan Chat | Provider reasoning effort |
| `PLAN_CHAT_LLM_REQUEST_TIMEOUT_SECONDS` | `240` | Plan Chat | Provider timeout |
| `PLAN_CHAT_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `10` | Plan Chat | Telegram timeout |

The Bot receives the webhook secret and allowlist. Plan Chat receives the LLM
key and Plan Chat settings. The template also shares the Telegram token and
table name as required to send the result and reload context. Do not add old
worker, grocery, or model settings to `.env`; unknown environment variables
are ignored by settings loading.

For the Bot allowlist, use immutable numeric Telegram user IDs, not usernames
or display names:

```dotenv
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

Never commit `.env` or secret values. Disable group joining in BotFather as
additional defense in depth. Unauthorized users and non-private chats receive
no Telegram reply while the webhook returns HTTP 200.

## AWS deployment

Deploy from the repository root with the typed orchestrator. It creates two
Python 3.14 ARM64 Lambda functions, an HTTP API, and an on-demand DynamoDB
table. The orchestrator is pinned to the `meal-planner` AWS profile and
`eu-west-1` region and requires AWS CLI 2.32.0 or newer.

Create an ignored `.env` containing the deployment inputs:

```dotenv
AWS_PROFILE=meal-planner
AWS_REGION=eu-west-1
STACK_NAME=meal-planner-dev
TELEGRAM_BOT_TOKEN=replace-with-token
TELEGRAM_WEBHOOK_SECRET=replace-with-webhook-secret
LLM_API_KEY=replace-with-llm-key
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
APP_SECRETS_SECRET_NAME=meal-planner/app-secrets
SYNC_SECRETS=false
PLAN_CHAT_LLM_MODEL=gpt-5.6-luna
PLAN_CHAT_LLM_REASONING_EFFORT=high
```

Run a routine deployment, first-time guided deployment, or post-deployment
Telegram recovery with:

```bash
uv run python scripts/deploy.py
uv run python scripts/deploy.py --guided
uv run python scripts/deploy.py --post-deploy-only
```

The routine stages authenticate and confirm the AWS identity, check the JSON
Secrets Manager secret, run SAM validation and build, deploy the stack,
resolve outputs, register the five Telegram commands, set the webhook, and
verify the webhook. The routine announcements are:

1. Check deployment prerequisites.
2. Authenticate with AWS and confirm identity.
3. Check configured Secrets Manager secrets.
4. Validate the SAM template.
5. Build SAM artifacts.
6. Deploy the SAM stack.
7. AWS deployment completed.
8. Resolve CloudFormation stack outputs.
9. Register Telegram commands.
10. Set the Telegram webhook.
11. Verify the Telegram webhook.

Recovery mode skips secret checks and SAM deployment but repeats output
resolution and Telegram configuration. The AWS deployment-completed boundary
is explicit: if deployment has already completed and a later stage fails, use
`--post-deploy-only` after correcting the problem.

Recovery uses the same webhook contract as routine deployment: Telegram's
reported URL must match the deployed output before recovery succeeds.

Webhook verification requires Telegram's reported URL to match the exact
deployed `WebhookUrl`. Retained historical delivery metadata is a warning,
reported as a warning and does not by itself fail the deployment. For a
current delivery problem, inspect the current webhook status and the Bot
Lambda logs.

Secret synchronization is an explicit external mutation. Set
`SYNC_SECRETS=true` and pass the flag only when intentionally replacing the
complete JSON secret containing `telegram_bot_token`,
`telegram_webhook_secret`, and `llm_api_key`:

```bash
uv run python scripts/deploy.py --sync-secrets
```

The stack outputs are `WebhookUrl`, `MealPlannerTableName`, `BotFunctionName`,
and `PlanChatFunctionName`. The Bot role has the retained DynamoDB transaction
grant and can invoke Plan Chat. Plan Chat has DynamoDB access and the LLM key,
but no Bot-only webhook or allowlist settings.

For a direct read-only check of the retained transaction permission:

```bash
uv run python scripts/verify_transaction_permission.py \
  --stack-name meal-planner-dev \
  --profile meal-planner \
  --region eu-west-1
```

The verifier is not part of routine, guided, or recovery deployment. It
requires the IAM simulation permissions described by its help output and does
not change AWS resources.
AWS deployment itself is an operator action; inspect the target stack and
confirmed identity before proceeding.

## Privacy and failure behavior

Profiles, submitted meals, and active Plan Chat context are private to the
allowlisted Telegram user. The Plan Chat state necessarily stores the bounded
initial request, current instruction, and latest response temporarily in the
user's DynamoDB item. It expires after 24 hours and is not an official record.

Events sent to the worker contain identifiers and an observed state revision,
not profile data, meal history, prompts, or generated text. Application logs
use bounded failure categories and do not log those private contents, tokens,
or raw Telegram events. Provider failures yield bounded retry guidance only if
state recovery and Telegram delivery succeed. Persistence or Telegram delivery
failures can be silent to the user and are logged with bounded metadata;
partial text is never presented as a saved official plan. A stale or
superseded worker result is ignored.

## Development guidance

The active implementation is intentionally small:

- `src/meal_planner/bot_handler.py` owns Telegram workflows and state claims.
- `src/meal_planner/plan_chat_handler.py` owns asynchronous text generation.
- `src/meal_planner/llm/prompts.py` builds the five-section plain-text prompt.
- `src/meal_planner/llm/client.py` owns the single bounded provider call.
- `src/meal_planner/models/schemas.py` defines profile, meal, and temporary
  conversation contracts.
- `template.yaml` and `scripts/deploy.py` define the Plan Chat deployment.

Keep user text bounded and delimiter-safe. Keep worker events identifier-only,
reload context with a strongly consistent state read, and preserve ownership
checks before delivery. Changes to prompt sections, state bounds, command
surfaces, or deployment settings need focused tests and the complete local
verification suite.

Useful checks are:

```bash
uv run pytest tests/test_readme.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git diff --check
```

Historical completed plan documents remain in `docs/plans/completed/` as
repository history. They are not active product or operator documentation and
are intentionally excluded from active-surface audits.
