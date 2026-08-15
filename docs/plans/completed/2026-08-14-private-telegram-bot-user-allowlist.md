# Private Telegram Bot User Allowlist

> Tracked by GitHub issue
> [#27](https://github.com/nsal/meal-planner-bot/issues/27).

## Overview

Restrict the Telegram meal-planner bot to an explicitly configured set of
Telegram users. The HTTP webhook must remain publicly reachable so Telegram
can deliver updates, but the application will authorize the immutable numeric
Telegram sender ID and require a private chat before any command, callback,
database, planner, Telegram reply, or LLM action is allowed.

The allowlist will be a required comma-separated SAM deployment parameter
passed only to the Bot Lambda. Missing or malformed configuration fails closed.
Valid Telegram updates from unauthorized users and all group-chat updates are
silently ignored with a concise log entry and HTTP 200 response, preventing
Telegram retries without disclosing the authorization policy.

## Context (from discovery)

- Files/components involved: `template.yaml`, `src/meal_planner/config.py`,
  `src/meal_planner/router.py`, `src/meal_planner/bot_handler.py`, a focused
  Telegram access-policy module, corresponding tests, and `README.md`.
- The webhook secret currently authenticates Telegram as the HTTP caller, but
  there is no user authorization after the request is accepted.
- `route_update` already extracts the stable numeric `from.id` into
  `RouteResult.user_id` for messages and callback queries. It does not retain
  the Telegram chat type.
- `BotHandler.handle_update` is the common route boundary before commands,
  callbacks, conversations, repository calls, planner invocation, and LLM
  use, making it the appropriate enforcement point.
- Profiles and plans are partitioned by Telegram user ID, but any Telegram user
  who finds the bot can currently create data and incur AWS and provider costs.
- A separate uncommitted family-name onboarding plan exists in the worktree;
  implementation must preserve it and coordinate overlapping edits to
  `bot_handler.py`, tests, README, and SAM artifacts.

## Development Approach

- **Testing approach**: TDD; add failing configuration, routing, policy,
  handler, and template tests before implementation.
- Complete each task fully before moving to the next.
- Make small, focused changes and preserve existing webhook authentication and
  authorized-user behavior.
- Every code task must add or update tests for successful and denied paths.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation scope changes.
- Use numeric Telegram user IDs, never usernames, display names, or phone
  numbers, because those are mutable or unavailable.
- Keep the API Gateway webhook public and retain Telegram's secret-token check;
  the allowlist is a second, user-level authorization layer.
- Do not add a database table, admin command, invite workflow, API Gateway
  authorizer, or runtime dependency.

## Testing Strategy

- **Configuration tests**: require one or more positive numeric IDs, normalize
  harmless surrounding whitespace, deduplicate IDs, and reject empty,
  negative, zero, and non-numeric entries.
- **Router tests**: preserve `chat.type` for private messages and callbacks,
  group/supergroup messages, and malformed updates.
- **Policy tests**: cover allowed private users, unauthorized private users,
  allowed users in groups, chat/user mismatch, missing sender ID, missing chat
  type, commands, conversations, and callback queries.
- **Handler tests**: prove denied updates return HTTP 200 without repository,
  Telegram, Lambda, or LLM calls, while authorized private updates retain
  current behavior.
- **Template tests**: require a no-default, validated SAM parameter wired only
  to Bot Lambda, and reject public-access defaults or Planner Lambda exposure.
- **Deployment artifact tests**: validate and rebuild SAM, then require fresh
  source and template artifacts.
- **Project gates**: full pytest, Ruff lint and format checks, strict mypy, SAM
  validation/build, required artifact tests, and `git diff --check`.
- There is no UI suite; Telegram access is tested at the update boundary and
  manually with authorized and unauthorized accounts after deployment.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document blockers or failed assumptions with a ⚠️ prefix.
- Keep this plan synchronized with implementation and verification results.
- Move this plan to `docs/plans/completed/` only after every local gate passes.

## Solution Overview

Add a required `TelegramAllowedUserIds` SAM parameter containing a
comma-separated list of positive numeric Telegram user IDs. Pass it to
Bot Lambda as `TELEGRAM_ALLOWED_USER_IDS`; do not expose it to Planner Lambda.
Although user IDs are identifiers rather than credentials, document that the
parameter is visible to operators through CloudFormation and Lambda
configuration.

Parse and validate the setting into an immutable set. Extend `RouteResult`
with `chat_type`, then evaluate each routed update with a small typed access
policy. Authorization requires all of the following:

- `user_id` is present and belongs to the configured allowlist;
- `chat_type` is exactly `private`; and
- the private `chat_id`, normalized as text, equals `user_id`.

Enforce this policy immediately after routing in `BotHandler.handle_update`.
Denied updates log only the sender ID, chat type, and denial reason; they do
not log message text, callback data, profile information, or credentials. The
handler returns its existing `{"statusCode": 200, "body": "ok"}` response so
Telegram does not retry. No denial message is sent to the unauthorized user.

## Technical Details

- Add a required CloudFormation string parameter with no default and an exact
  comma-separated positive-integer pattern. Deployment must explicitly choose
  the authorized IDs.
- Permit optional whitespace in local environment parsing, but use a canonical
  comma-separated form in CloudFormation examples.
- Reject empty entries, signs, decimals, booleans, zero, and non-numeric text.
  Deduplicate valid IDs without changing authorization behavior.
- Keep IDs as canonical decimal strings to match the router's existing
  `str(from.id)` representation and avoid integer-size assumptions.
- Add `chat_type: str | None` to `RouteResult` and populate it from
  `message.chat.type` and `callback_query.message.chat.type` only when the
  source value is a string.
- Treat missing or malformed routing identity as unauthorized. Unknown update
  types continue to return HTTP 200 without action.
- A whitelisted user in a group, supergroup, or channel remains unauthorized,
  even if the bot was previously added there.
- Apply authorization to commands, conversations, and callback queries through
  the single `handle_update` boundary; do not duplicate checks in each handler.
- Preserve the webhook-secret check before JSON parsing. It authenticates
  Telegram transport, while the new policy authorizes the sender.
- BotFather's group-join setting is defense in depth only and remains a manual
  post-deployment step, not the primary authorization control.

## What Goes Where

- **Implementation Steps**: validated allowlist configuration, routed chat
  context, access policy, handler enforcement, SAM wiring, operator
  documentation, artifact rebuild, and project verification.
- **Post-Completion**: deploy with real Telegram IDs, disable group joining in
  BotFather, run authorized and denied Telegram smoke tests, monitor logs, and
  publish through the normal pull-request workflow.

## Implementation Steps

### Task 1: Add fail-closed allowlist configuration

**Files:**

- Modify: `src/meal_planner/config.py`
- Modify: `tests/test_config.py`

- [x] add failing tests for one ID, multiple IDs, surrounding whitespace, and
  duplicate IDs
- [x] add failing tests for missing, empty, zero, negative, decimal, and
  non-numeric entries
- [x] add a typed setting for `TELEGRAM_ALLOWED_USER_IDS` and expose an
  immutable canonical set to authorization code
- [x] ensure malformed configuration raises a concise validation error without
  echoing unrelated secrets
- [x] verify existing timeout budgets and required bot/LLM settings are
  unchanged
- [x] run `uv run pytest tests/test_config.py`; all tests must pass before
  Task 2

### Task 2: Preserve Telegram chat type in routed updates

**Files:**

- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_router.py`

- [x] add failing tests for private message and private callback chat types
- [x] add failing tests for group and supergroup chat types
- [x] add failing tests for missing or non-string chat types and malformed
  callback messages
- [x] add optional `chat_type` to `RouteResult` and populate it for message and
  callback routes without changing route selection
- [x] verify sender and chat identifiers preserve their existing types and
  fallback behavior
- [x] run `uv run pytest tests/test_router.py`; all tests must pass before
  Task 3

### Task 3: Implement the typed Telegram access policy

**Files:**

- Create: `src/meal_planner/telegram/access.py`
- Create: `tests/test_telegram_access.py`

- [x] add failing tests for an allowlisted sender in a matching private chat
- [x] add failing tests for an unknown sender, group/supergroup/channel chat,
  mismatched private chat ID, and missing identity fields
- [x] implement an immutable access policy over the configured user-ID set
- [x] return a typed or stable denial reason suitable for concise operational
  logging without exposing message contents
- [x] verify policy evaluation performs no network, database, or environment
  mutation
- [x] run `uv run pytest tests/test_telegram_access.py`; all tests must pass
  before Task 4

### Task 4: Enforce authorization before bot actions

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add failing handler tests proving unauthorized private updates and
  whitelisted group updates return HTTP 200 without replies or side effects
- [x] add failing callback tests proving denied callbacks do not mutate plans,
  answer callback queries, or invoke Planner Lambda
- [x] inject the access policy into `BotHandler` and enforce it immediately
  after routing, before route-specific handlers
- [x] wire the validated setting into the production Lambda construction path
- [x] add success tests proving authorized private commands, conversations, and
  callbacks preserve current behavior
- [x] assert denial logs omit message text, callback payloads, tokens, and
  profile data
- [x] run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 5

### Task 5: Add the required SAM deployment contract

**Files:**

- Modify: `template.yaml`
- Modify: `tests/test_template.py`

- [x] add a failing template test requiring `TelegramAllowedUserIds` with no
  default and an exact positive-integer-list constraint
- [x] add failure-oriented assertions rejecting wildcard/public defaults and
  accidental Planner Lambda exposure
- [x] add `TELEGRAM_ALLOWED_USER_IDS` only to Bot Lambda using the parameter
  reference
- [x] verify the HTTP API and webhook-secret configuration remain unchanged
- [x] verify no IAM, DynamoDB schema, dependency, or additional API Gateway
  resource change is introduced
- [x] run
  `uv run pytest tests/test_template.py -k "not built_artifact"`; all selected
  source-template tests must pass before Task 6

### Task 6: Document private-bot deployment and operation

**Files:**

- Modify: `README.md`
- Verify: `template.yaml`
- Verify: `tests/test_template.py`

- [x] document how each approved person retrieves their numeric Telegram user
  ID without using mutable usernames as authorization
- [x] add `TelegramAllowedUserIds` to guided and repeat deployment examples
- [x] document local `.env` configuration and the fact that user IDs are
  visible in deployment configuration
- [x] document silent-denial behavior, private-chat-only enforcement, and the
  distinction between webhook authentication and user authorization
- [x] document BotFather group-join disabling as defense in depth
- [x] add or update tests for any deployment contract changed during this task
- [x] run
  `uv run pytest tests/test_template.py -k "not built_artifact"`; all selected
  source-template tests must pass before Task 7

### Task 7: Rebuild and verify deployment artifacts

**Files:**

- Verify: `template.yaml`
- Rebuild: `.aws-sam/build/`
- Verify: `tests/test_template.py`

- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] rebuild with `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`; all artifact
  tests must pass before Task 8

### Task 8: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/config.py`
- Verify: `src/meal_planner/router.py`
- Verify: `src/meal_planner/telegram/access.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `template.yaml`
- Verify: `tests/`
- Verify: `README.md`

- [x] verify only configured users in matching private chats can trigger bot
  actions
- [x] verify unauthorized, group, malformed, and missing-configuration paths
  fail closed without Telegram retries or downstream side effects
- [x] verify existing webhook-secret rejection still returns HTTP 403 before
  update processing
- [x] run `uv run pytest` and fix failures until the full suite passes
- [x] run `uv run ruff check .` and fix every finding
- [x] run `uv run ruff format --check .` and fix every formatting difference
- [x] run `uv run mypy` and fix every type error
- [x] run `git diff --check` and fix every whitespace error

### Task 9: Finalize plan tracking and documentation

**Files:**

- Modify: this plan with final deviations and verification results
- Modify: `AGENTS.md` only if implementation discovers a reusable project rule
- Move: this plan to `docs/plans/completed/`

- [x] record implementation deviations and final verification results in this
  plan
- [x] update `AGENTS.md` only if a reusable rule not already covered by project
  standards is discovered
- [x] confirm every implementation and verification checkbox is complete
- [x] rerun `uv run pytest` after final documentation changes
- [x] move this plan to `docs/plans/completed/` only after all local checks pass

### Final implementation notes

- The Bot and Planner Lambdas use separate settings loaders. `Settings` keeps
  the required allowlist for the Bot, while `PlannerSettings` intentionally
  excludes it so the parameter is not exposed to the Planner Lambda.
- Existing uncommitted family-name onboarding changes were preserved and
  verified alongside this work.
- Review follow-up: invalid Bot settings now return a sanitized HTTP 200
  response before downstream clients are initialized, and missing chat IDs
  remain missing so the policy denies malformed private updates.
- Verification completed: `uv run pytest` passed with 290 tests,
  `uv run ruff check .` passed, `uv run ruff format --check .` passed,
  `uv run mypy` passed, `git diff --check` passed, SAM validation passed, and
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` passed with
  22 tests.

## Post-Completion

**Manual verification:**

- Obtain the numeric Telegram user IDs for every approved family member and
  deploy a non-production stack with the exact comma-separated allowlist.
- Disable the bot's ability to join groups through BotFather as defense in
  depth, while retaining application enforcement.
- From an approved account in a private chat, run `/start`, complete a profile,
  generate a plan, and exercise one callback.
- From an unapproved account, send a command and conversational message; verify
  no reply appears, no DynamoDB item is created, no Planner or LLM request is
  made, and the webhook remains healthy.
- Add the bot to a test group or use an existing group with an approved sender;
  verify the update is silently denied and no household data is displayed.
- Inspect CloudWatch logs for concise denial records that contain no message
  contents or credentials.

**External system updates:**

- Commit with a Conventional Commit message containing the associated issue
  number, push a dedicated branch, and open a pull request.
- Comment on the GitHub issue with the commit or pull-request link and local
  and deployed verification results, then close it when the work is complete.
