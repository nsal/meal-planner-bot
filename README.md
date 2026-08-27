# Meal Planner Bot

A Telegram assistant that collects a household nutrition profile, generates a
variable-length meal plan through LiteLLM, confirms edits, builds groceries
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

- `/submit_meals` starts deterministic actual-meal logging. It first shows
  meals recorded for UTC today and yesterday, then asks for one entry in the
  form `when, meal type, what you ate`. Repeated meals of the same type and
  date are retained.
- `/checkin` shows buttons for cooked, skipped, or swapped outcomes on today's
  confirmed plan. `/submit_meals` does not require an active plan.
- `/cancel` clears an unfinished meal or plan workflow. Starting `/submit_meals`
  or `/plan` replaces an older unfinished workflow. `/profile` also replaces
  an older unfinished workflow when it opens the profile editor.
- `/profile` lets you view and amend the saved household profile. Tap `Amend profile`, choose
  `Family`, `Dietary constraints`, or `Dietary preferences`, then
  choose an operation. Each operation accepts one guided message: use
  `John 1500` to add a family member or change a member's calories, the exact
  member name to remove someone, or one item such as `dairy` or `eat more
  vegetables` to add or remove a constraint or preference. After a
  successful change the category menu returns so you can make another
  amendment. Use `Back` to navigate, `Done` to finish and save the session,
  `Close` to leave it, or `/cancel` to clear the active edit.
- The canonical profile has two dietary fields: `dietary_constraints` and
  `dietary_preferences`. A new dietary rule is interpreted and shown for
  confirmation; review the interpreted meaning before saving. Priority is
  dietary constraints > current plan preferences > stored dietary
  preferences. Constraints cannot be overridden, and a preference that
  conflicts with one is rejected.
- Only `/profile` interprets dietary text for saved rules, using strict
  structured output and confirmation; `/plan` never reinterprets saved
  dietary text. A preference supplied for the current plan is interpreted
  separately and is never written into the saved profile without a profile
  confirmation.
- Persistent food preferences are ISO-week quotas. For each requested
  horizon, the application projects only obligations due in each weekly
  segment and counts submitted meal history before each weekly segment as
  evidence. Draft or confirmed plan meals do not count as evidence. Explicit
  weekdays remain exact; rules without weekdays receive stable,
  Monday-anchored, evenly spaced target weekdays. Missed generated targets
  are carried forward within the week and short horizons are capped by their
  available meal slots.
- Batch cooking covers 2 or 3 total lunch/dinner portions. Draft publication
  creates provisional reservations tied to the request and plan revision;
  replacing a draft removes only its provisional reservations. After the
  preparation meal is submitted and confirmed, the remaining portions become
  available. A confirmed linked leftover consumes exactly one portion.
  Unsubmitted provisional reservations expire after the preparation date, and
  remaining portions expire at the ISO-week boundary.
- `/plan` asks for a duration and request-specific preference before starting
  asynchronous generation. Reply in the form `N, preference`, such as
  `1, no preference` or `3, fish for dinner`; only the first comma separates
  the duration, so later commas remain part of the preference. `no
  preference` and `anything` remove the extra constraint; the saved family
  profile is never changed. After the initial reply, clarification messages
  are treated as preference text and retain the selected duration. Historical
  in-progress seven-day requests continue to accept preference-only replies.
  A failed request retains the preference so `/plan` can retry it. The first
  invocation makes one whole-plan provider request. An invalid result gets
  one automatic repair in a fresh asynchronous invocation using the same
  immutable horizon, evidence, obligation, and batch snapshot. If the repair
  also fails, no draft is saved or displayed, the
  previous draft remains unchanged, and the saved preference is retained. A
  manual `/plan` retry reuses that snapshot's duration, request preference,
  and obligation projection; it does not reinterpret saved profile text.
- Request-specific preferences support structured natural-language rules.
  An unqualified positive food preference such as `eggs for breakfast` means
  a strict `at_least 1` rule for both request-specific preferences and saved
  profile preferences. This includes `I'd like eggs for breakfast`. Explicit
  counts and operators, exclusion wording, and best-effort qualifiers override
  this default; for example, `beans for breakfast if convenient` is best effort
  and may be omitted. Malformed saved rules fail closed and block `/plan`
  rather than being silently ignored.
  A current plan preference overrides only conflicting stored preferences:
  three stored egg breakfasts plus a current maximum of two resolves to
  exactly two preferred days rather than permitting zero. The conversational
  LLM asks a focused clarification question when a clause is ambiguous,
  unsupported, conflicting, or incomplete. The original wording stays
  attached to the same `/plan` workflow, and your next reply is combined with
  it. Positive exact-count or minimum obligations are also clarified when the
  selected date horizon cannot provide enough eligible weekday or meal slots;
  maximum rules and an exact count of zero remain feasible when no matching
  slots exist.
- The application validates constraints first, then strict rules and plan
  completeness. It matches whole words or phrases in declared meal names and
  ingredient items, ignoring case, punctuation, whitespace, and conservative
  singular/plural differences. Alternative foods share one count, and each
  distinct day and meal type counts at most once; culinary knowledge alone is
  not evidence. Best-effort misses do not invalidate a plan. The candidate is
  checked before anything is persisted or displayed. The Planner never saves
  or displays a failing candidate: it makes one automatic repair in a fresh
  asynchronous invocation. A second invalid result is terminal, leaves the
  previous draft unchanged, retains the preference, and can be retried
  manually with `/plan`.
- Profile changes apply only to newly generated plans. Existing meal plans
  are not revalidated or altered after a constraint changes, and users are
  responsible for regenerating them. Declared-ingredient validation does not
  detect undeclared product cross-contamination and is not medical
  cross-contamination certification.
  For an in-progress request, publication and release of that request happen
  together, so a cancelled or replaced request cannot save or display a stale
  result.
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
| `AWS_REGION` | `eu-west-1` for deployment | AWS client region |
| `BOT_FUNCTION_TIMEOUT_SECONDS` | `30` | Bot Lambda deadline |
| `BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `5` | Bot Telegram HTTP timeout |
| `BOT_LLM_REQUEST_TIMEOUT_SECONDS` | `6` | Per-attempt Bot LLM timeout |
| `BOT_LLM_MAX_RETRIES` | `2` | Bot transient LLM attempts |
| `BOT_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Bot initial retry backoff |
| `BOT_HANDLER_SAFETY_MARGIN_SECONDS` | `4` | Bot non-provider safety margin |
| `PLANNER_FUNCTION_TIMEOUT_SECONDS` | `300` | Planner application deadline |
| `PLANNER_TELEGRAM_REQUEST_TIMEOUT_SECONDS` | `10` | Planner Telegram HTTP timeout |
| `PLANNER_LLM_REQUEST_TIMEOUT_SECONDS` | `240` | Whole-plan Planner provider request timeout |
| `PLANNER_LLM_MAX_RETRIES` | `1` | Total whole-plan Planner provider attempts; manual `/plan` retries remain available |
| `PLANNER_LLM_INITIAL_BACKOFF_SECONDS` | `1` | Planner initial retry backoff |
| `PLANNER_GROCERY_LLM_REQUEST_TIMEOUT_SECONDS` | `120` | Per-attempt grocery provider timeout |
| `PLANNER_GROCERY_LLM_MAX_RETRIES` | `2` | Total grocery provider attempts |
| `PLANNER_HANDLER_SAFETY_MARGIN_SECONDS` | `20` | Planner non-provider safety margin |

The settings validator includes every LLM attempt, the maximum bounded retry
wait (5 seconds per retry), each sequential Telegram allowance, and a handler
safety margin. Each function's worst-case budget must fit its configured
deadline; the Planner now has a five-minute (300-second) application deadline
while using one 240-second whole-plan provider attempt. The configured Planner
budget is 290 seconds: 240 seconds for LiteLLM, three sequential 10-second
Telegram allowances for the plan, bounded summary, and review follow-up, and a
20-second safety margin. Its Lambda timeout is 310
seconds, reserving ten seconds for the application to return after the
300-second Planner application deadline. Grocery generation retains two
120-second provider attempts; including its maximum five-second retry wait,
one 10-second Telegram allowance, and the 20-second safety margin, its budget
is 275 seconds. The application retry loop is the sole LLM retry layer;
provider adapter retries are disabled. If generation fails, the saved
preference remains available for a manual `/plan` retry. The Bot budget remains
below the 30-second HTTP API integration limit. Lambda itself permits up to
900 seconds, but that larger service limit cannot extend
the synchronous Telegram webhook deadline ([Lambda timeout quota](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
[HTTP API integration quota](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html)).

The former global timeout and retry variables are ignored for compatibility;
use the function-specific settings above.

Never commit `.env` or secret values.

Planner generation makes one whole-plan provider request per invocation. An
invalid first result can trigger one bounded repair in a fresh asynchronous
Planner invocation; each invocation still makes only one provider request. A
timeout failure is reported separately from invalid structured output, and
neither failure persists a partial plan. A failed or terminally invalid
generation retains the saved preference so the user can retry with `/plan`.

Preference interpretation is LLM-assisted: the conversational model maps the
user's natural-language clauses to supported food alternatives, counts, and
meal scopes, or requests clarification. Application code is authoritative for
the measurable parts. It validates the generated plan's structure, exact
counts, and evidence from generated meal names and ingredient items before
anything is persisted or displayed. The Planner model receives the interpreted
rules as generation guidance, but its response is not trusted as proof of
compliance.

Planner LLM failures produce one sanitized CloudWatch warning per failed typed
provider attempt. The record includes `attempt`, `elapsed_ms`, `model`, and a
normalized `category` such as `timeout`, `transient`, `permanent`, or
`response_format`. A LiteLLM `timeout` category means the 240-second provider
request ended without a response; a Planner application deadline is the
separate 300-second in-process guard and returns a planner-deadline response.
These diagnostics do not include prompts, preferences, generated plans,
credentials, raw events, chat IDs, or user IDs.

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
APP_SECRETS_SECRET_NAME=meal-planner/app-secrets
SYNC_SECRETS=false
```

### One-time development dietary reset

After deploying the compatible code, an operator may clear the dietary
preferences and constraints for one exact development user. The command
requires the table name, AWS profile, region, and user ID explicitly:

```bash
uv run python scripts/reset_profile_dietary_fields.py \
  --table meal-planner-dev-table \
  --profile meal-planner \
  --region eu-west-1 \
  --user-id 123456789
```

It conditionally updates only `dietary_preferences`,
`dietary_constraints`, and `profile_revision` on that user's `PROFILE` item.
It does not scan the table or alter family details, nutrition targets, plans,
conversation state, or meal history. Repeating a successful reset is a safe
no-op. This is a one-time development operation: do not add it to routine
deployment orchestration or rerun it after the profile has been recreated.

The routine workflow prints numbered stage headings as it checks prerequisites,
authenticates and confirms the AWS identity, checks that the configured JSON
Secrets Manager secret exists, runs `sam validate --lint` and
`sam build --beta-features`, deploys with a generated refresh token, resolves
the required stack outputs, registers the canonical Telegram command menu,
sets the webhook, and verifies the webhook:

Webhook verification confirms that Telegram reports the exact deployed
`WebhookUrl`. Telegram can retain delivery error metadata from an earlier
attempt; that metadata is reported as a warning and does not by itself fail
the deployment. For an actual delivery problem, inspect the current webhook
status and the Bot Lambda logs.

The routine announcements are, in order:

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
orchestrator creates or replaces one JSON secret containing the stable fields
`telegram_bot_token`, `telegram_webhook_secret`, and `llm_api_key`. Secret
updates replace the complete object; individual field updates are not merged.
Secret values never appear in command arguments, logs, errors, or summaries:

```bash
uv run python scripts/deploy.py --sync-secrets
```

The runner stops at the first failed stage. Once `sam deploy` succeeds it
prints an explicit AWS deployment-completed boundary. If output resolution or
Telegram configuration then fails, the error says that AWS deployment
completed and points to the idempotent recovery command below:

```bash
uv run python scripts/deploy.py --post-deploy-only
```

Recovery mode still performs prerequisite checks, remote login, identity
confirmation, stack output resolution, command registration, webhook setup,
and webhook verification. It announces and skips secret checks, SAM
validation, SAM building, deployment, and IAM simulation; it never checks or
changes secrets. The same recovery mode can be used after a Telegram API
failure without repeating an AWS deployment.
Recovery uses the same webhook contract: verification requires Telegram's
reported URL to match the deployed `WebhookUrl`; retained delivery errors are
warnings, while current delivery problems require inspecting the current
webhook status and Bot Lambda logs.
The stack outputs `WebhookUrl`, `MealPlannerTableName`, `BotFunctionName`, and
`PlannerFunctionName`; malformed or missing outputs are failures. The direct
read-only IAM verifier remains available as a troubleshooting command and
accepts an explicit profile:

```bash
uv run python scripts/verify_transaction_permission.py \
  --stack-name meal-planner-dev \
  --profile meal-planner \
  --region eu-west-1
```

The verifier is not part of routine, guided, or recovery deployment. Use it
for an initial deployment, an IAM/template change, or suspected authorization
problem. It requires the caller to have
`cloudformation:DescribeStacks`,
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

To rotate a credential, write the complete JSON object to a restricted local
file, publish it, and deploy with a new unique `SecretRefreshToken`. The
deployment script generates that refresh token and changes both Lambda
resources, forcing CloudFormation to re-resolve the versionless field
references:

```bash
umask 077
cat > /tmp/meal-planner-app-secrets.json <<'JSON'
{"telegram_bot_token":"replace-with-token","telegram_webhook_secret":"replace-with-webhook-secret","llm_api_key":"replace-with-llm-key"}
JSON
aws secretsmanager put-secret-value \
  --secret-id "$APP_SECRETS_SECRET_NAME" \
  --secret-string file:///tmp/meal-planner-app-secrets.json \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"
uv run python scripts/deploy.py
rm -f /tmp/meal-planner-app-secrets.json
```

For webhook-secret rotation, publish the complete JSON object first, deploy the
Lambda with the new refresh token second, then register and verify the Telegram
webhook with the new webhook-secret field. A single accepted secret necessarily
has a brief coordinated transition window; dual-secret acceptance is outside
this remediation. CloudFormation resolves dynamic references on resource
updates, while a secret-value-only change does not refresh an existing Lambda
environment ([dynamic reference rotation behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html)).

Keep the three legacy Secrets Manager secrets until the new deployment and
webhook have passed live verification. Remove or schedule deletion of those
legacy secrets manually through the approved AWS process; this repository does
not automate that destructive cleanup.

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
- `/profile` — View and amend the household profile.
- `/plan` — Create or retry a meal plan.
- `/grocery` — View the active grocery list.
- `/today` — View today's planned meals.
- `/submit_meals` — Submit one meal eaten in the past seven UTC calendar days.
- `/checkin` — Record today's planned meal outcomes.
- `/cancel` — Cancel an unfinished workflow.

- `/start` begins onboarding. Supply the family name separately from the
  household size, every household member's name and calorie target, dietary
  constraints, and dietary preferences. Known onboarding fields carry across
  conversational turns, so provide only the fields the bot still requests.
- `/profile` shows the persisted family name and individual member details,
  with button-led amendment navigation for family members, dietary
  constraints, and dietary preferences. Family add and calorie changes
  use one message such as `John 1500`; list changes use one item such as
  `dairy` or `eat more vegetables`.
- Natural-language no-value answers such as `none`, `nothing`, `no allergies`,
  and `no restrictions` are stored as empty categories. They count as answers,
  while omitted fields remain missing until supplied.
- `/plan` asks for a duration and request-specific preference in the form
  `N, preference`, for example `1, no preference` or `3, fish for dinner`.
  Split only at the first comma when the preference contains more commas.
  After the initial response, clarification replies are preference-only and
  retain the selected duration. If the request cannot be interpreted
  completely, the bot asks one focused clarification and keeps the raw
  preference in the same workflow; your next reply is combined with it.
  Historical in-progress seven-day requests continue to accept
  preference-only replies. The draft is persisted before Telegram delivery
  only after application validation. An invalid first result receives one
  automatic repair in a separate Planner invocation. If that repair also
  fails, no draft is saved, the preference is retained, and `/plan` is the
  manual retry. Cancelling or replacing an in-progress request prevents its
  older asynchronous result from being published.
- An unqualified positive food preference such as `eggs for breakfast` means
  a strict `at_least 1` rule for both request-specific preferences and saved
  profile preferences. Explicit counts and operators, exclusion wording, and
  best-effort qualifiers override this default. Malformed saved rules fail
  closed and block `/plan` rather than being silently ignored.
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
- `/submit_meals` sends the recent history first, grouped under UTC `Today`
  and `Yesterday`, followed by a separate input prompt. Enter exactly three
  comma-separated fields:

  ```text
  when, meal type, what you ate
  ```

  Only the first two commas are separators, so later commas remain part of
  the description. For `when`, use `today`, `yesterday`, or a strict
  `YYYY-MM-DD` date. The bot interprets aliases and dates in UTC and accepts
  only the inclusive seven-calendar-day range from six days ago through
  today. The four valid meal types are `breakfast`, `lunch`, `snack`, and
  `dinner`, case-insensitively. For example:

  ```text
  today, lunch, vegetable soup, with bread
  ```

  Invalid entries are rejected with an explanation and the full input
  instructions again; they do not save anything. A valid entry is echoed in
  a review message with `✅ Confirm` and `❌ Cancel`. Confirm saves exactly one
  meal and shows `➕ Add more` and `✅ Done`; `Add more` starts another empty
  submission, while `Done` ends meal logging. `Cancel` discards the
  unconfirmed meal. Old or repeated buttons cannot change a newer submission
  or save a duplicate. `/checkin` sends plan-specific buttons for `cooked`,
  `skipped`, and `swapped`. Old, draft, expired, malformed, and superseded
  overlapping-plan callbacks are rejected, even if the older plan still
  covers today.

### Per-member nutrition targets

Protein and fibre targets are optional grams/day values per member. Calories
remain required for profile completion; the absence of optional targets does
not block profile completion or plan generation. Add a family member with
either `name calories` or `name calories protein fibre`, for example
`John Smith 2000 120 30`.

The Family profile menu changes protein and fibre independently. For either
action, send `name grams`, such as `John Smith 120`. To clear an optional
target, send `name none`; clearing is case-insensitive. Calorie, protein, and
fibre changes preserve the other saved targets, and omitted targets remain
`not set` rather than being inferred.

Target adjustment is prompt-guided and best-effort. The generated plan keeps
the existing calorie estimate schema; this phase does not calculate daily
calorie, protein, or fibre totals or automatically repair them. Missed
calorie, protein, or fibre targets are not automatically detected in this
phase.

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
- Telegram delivery failure: inspect the current webhook status and the Bot
  Lambda logs; logs omit bot tokens and message content. Retained historical
  delivery metadata is a warning during webhook verification, not a substitute
  for checking current delivery behavior.
- SAM smoke-test failure: rerun a clean SAM build. Tests reject stale source or
  generated templates.
- Timeout failures: keep configured request timeouts below Lambda deadlines;
  only transient LLM failures are retried. The bot must also fit API Gateway's
  non-increasable 30-second HTTP API integration timeout.
