# Meal Planner Bot Release Readiness Remediation

## Overview

- Remediate every actionable finding from the 2026-08-10 code review.
- Make the AWS SAM deployment secure, buildable, and operationally bounded.
- Complete onboarding, plan lifecycle, grocery, meal-history, and check-in
  behavior so the implemented product matches the original project plan.
- Finish acceptance verification and user/deployment documentation.
- Treat the project as pre-release: use a clean schema design without a legacy
  DynamoDB data migration.

## Context (from discovery)

- The project is a Python 3.12 Telegram bot deployed through AWS SAM as a bot
  Lambda and an asynchronous planner Lambda.
- Runtime code lives under `src/meal_planner/`; tests use pytest, moto, and
  mocks under `tests/`.
- The current SAM `CodeUri` excludes the root dependency manifests, and the
  public webhook does not authenticate Telegram requests.
- Domain and workflow gaps span Pydantic schemas, DynamoDB access, LLM output
  validation, profile onboarding, plan confirmation, grocery regeneration,
  and callback handling.
- The test suite currently passes, but deployment packaging and several
  end-to-end workflow invariants are not covered. The README and original
  acceptance tasks are incomplete.

## Development Approach

- **Testing approach**: Regular (code first, then tests).
- Use risk-first sequencing: deployment and security, data invariants,
  workflows, integration resilience, then acceptance and documentation.
- Complete each task fully before moving to the next.
- Make small, focused changes.
- Use a clean schema break; no compatibility layer or data migration is
  required because the project has no legacy production data.
- **CRITICAL: every task MUST include new or updated tests** for its code
  changes.
  - Write unit tests for new functions and methods.
  - Update tests for modified functions and methods.
  - Cover both success and error scenarios.
  - Add integration tests for persistence and handler boundaries where useful.
- **CRITICAL: all tests must pass before starting the next task.**
- **CRITICAL: update this plan when implementation scope changes.**
- Run all Python tools through `uv`; use `uvx` for the SAM CLI if it is not a
  project dependency.
- Run Ruff for linting and formatting with the configured 80-column limit.
- Run mypy in strict mode before completion.

## Testing Strategy

- **Unit tests**: required in every implementation task.
- **Repository integration tests**: use moto to verify DynamoDB key ranges,
  targeted updates, plan lookup, and state transitions.
- **Handler integration tests**: mock Telegram, Lambda invocation, and LLM
  responses while exercising complete command and conversational paths.
- **Infrastructure tests**: assert SAM configuration statically, run
  `sam validate`, build both functions, and smoke-import built handlers.
- **No browser e2e suite**: the project has no UI test framework.
- **Manual post-completion verification**: deploy to a non-production AWS
  stack and exercise the Telegram webhook and all bot commands.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document issues or blockers with a ⚠️ prefix.
- Keep this plan synchronized with implementation changes.
- Do not move to the next task while the current task's tests are failing.

## Solution Overview

- Build both Lambda functions from the repository root with SAM's uv build
  support so the application package and locked dependencies are included.
- Keep only SSM SecureString parameter names in deploy configuration and use
  secure dynamic references for the Lambda environment.
- Authenticate every webhook with Telegram's secret header before parsing or
  performing side effects.
- Replace permissive string/boolean fields with clean enums and typed dates.
  Require a complete seven-day plan with unique day numbers.
- Separate latest draft, specific plan, and active confirmed plan repository
  lookups so commands cannot serve expired or draft data as current.
- Treat grocery generation as asynchronous finalization: drafts contain meal
  choices, confirmation generates groceries, and later edits invalidate and
  refresh them.
- Encode the plan week in check-in callbacks and update only the selected
  nested meal outcome with a targeted DynamoDB expression.
- Send Telegram content as safe plain text unless a formatter explicitly
  escapes every dynamic value.
- Add explicit request timeouts and production retry behavior below Lambda
  deadlines.

## Technical Details

### Domain model

- Add `PlanStatus` values `draft` and `confirmed`.
- Add `MealOutcome` values `unreported`, `cooked`, `skipped`, and `swapped`.
- Add `GroceryStatus` values `not_requested`, `pending`, `ready`, and `error`.
- Store `week_start` and meal-log dates as validated ISO dates; serialize with
  `model_dump(mode="json")` before writing to DynamoDB.
- Validate that every `WeeklyPlan` contains exactly one `PlanDay` for each day
  number from 1 through 7.

### Plan lifecycle

```text
generate meals -> draft / not_requested
       edit -> draft / not_requested
    confirm -> confirmed / pending
grocery success -> confirmed / ready
grocery failure -> confirmed / error
confirmed edit -> confirmed / pending -> refresh grocery
```

- `get_latest_plan` supplies planning context and draft editing.
- `get_plan(user_id, week_start)` resolves an exact callback or async job.
- `get_active_plan(user_id, on_date)` returns only a confirmed, non-expired
  plan containing `on_date`.

### Callback format

- Use `checkin:<week_start>:<day>:<meal_type>:<outcome>`.
- Validate the exact part count, ISO date, day range, meal type, outcome, and
  Telegram's callback-data length limit.
- Answer the callback query on every handled path, including validation and
  persistence errors.

### Configuration

- Add `TELEGRAM_WEBHOOK_SECRET`.
- Add bounded `TELEGRAM_REQUEST_TIMEOUT_SECONDS` and
  `LLM_REQUEST_TIMEOUT_SECONDS` values.
- Add configurable LLM retries and initial backoff with production-safe
  defaults.
- Keep Lambda timeouts larger than individual request timeouts so handlers can
  send a controlled failure response.

## What Goes Where

- **Implementation Steps**: Python code, tests, SAM configuration, acceptance
  checks, README updates, and plan tracking in this repository.
- **Post-Completion**: create actual SecureString values, deploy a test stack,
  register the webhook secret with Telegram, manually exercise the bot, and
  link the implementation commit or PR from the GitHub issue.

## Implementation Steps

### Task 1: Produce complete Lambda build artifacts

**Files:**
- Modify: `template.yaml`
- Modify: `pyproject.toml` if SAM uv build metadata requires it
- Create: `tests/test_template.py`

- [ ] configure both functions to use a build context containing
  `pyproject.toml`, `uv.lock`, and `src/meal_planner`
- [ ] configure SAM's Python uv build method without duplicating dependency
  manifests
- [ ] preserve importable `meal_planner.bot_handler` and
  `meal_planner.planner_handler` handler paths in built artifacts
- [ ] write static template tests for both functions' build configuration
- [ ] write build-artifact smoke tests that import both Lambda handlers
- [ ] run `uv run pytest tests/test_template.py` and make it pass
- [ ] run `uvx aws-sam-cli validate --template-file template.yaml`
- [ ] run `uvx aws-sam-cli build` and verify both artifacts include runtime
  dependencies before Task 2

### Task 2: Authenticate webhooks and secure deployment parameters

**Files:**
- Modify: `src/meal_planner/config.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `template.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_template.py`

- [ ] add required webhook-secret configuration with non-empty validation
- [ ] compare the case-insensitive API Gateway header value with
  `hmac.compare_digest` before decoding the request body
- [ ] return a controlled forbidden response without initializing AWS or LLM
  clients when the secret is missing or incorrect
- [ ] replace plain token/key parameters with SSM SecureString parameter-name
  inputs and secure dynamic references
- [ ] add an SSM-backed parameter for the Telegram webhook secret
- [ ] write tests for valid, missing, malformed, differently cased, and
  incorrect webhook-secret headers
- [ ] write template tests proving no secret value is accepted as a plain
  CloudFormation parameter
- [ ] run the focused config, handler, and template tests before Task 3

### Task 3: Enforce clean domain and LLM-output invariants

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_prompts.py`

- [ ] add `PlanStatus`, `MealOutcome`, and `GroceryStatus` enums
- [ ] replace `was_cooked` with the clean `outcome` field throughout schemas
- [ ] use typed date and datetime fields for plans and meal logs
- [ ] validate exactly seven unique plan days numbered 1 through 7
- [ ] constrain meal types and callback-bound text to safe non-empty lengths
- [ ] update plan and conversational prompt schemas to emit the new fields and
  ISO dates
- [ ] reject partial, duplicate-day, invalid-date, invalid-status, and invalid
  outcome LLM responses in parser tests
- [ ] update success tests for complete seven-day responses
- [ ] run schema, parser, and prompt tests before Task 4

### Task 4: Complete and truthfully persist profile onboarding

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_bot_handler.py`

- [ ] define explicit profile-update entities including `family_members` and
  per-person calorie targets
- [ ] make onboarding collect name, people count, member targets, allergies,
  preferences, restrictions, and goals across conversational turns
- [ ] validate the complete candidate profile before saving any update
- [ ] persist `family_members` instead of silently dropping them
- [ ] return a mutation result so validation and DynamoDB failures replace the
  LLM success reply with a truthful error message
- [ ] write onboarding tests for new, partial, and completed profiles
- [ ] write update tests for family members, invalid targets, inconsistent
  people counts, and repository failures
- [ ] run focused profile and handler tests before Task 5

### Task 5: Implement confirmed and non-expired plan lifecycle

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [ ] replace ambiguous current-plan lookup with `get_latest_plan`, exact
  `get_plan`, and date-aware `get_active_plan` methods
- [ ] make `/today`, `/grocery`, and `/submit_meals` reject drafts and expired
  plans with accurate messages
- [ ] add a `confirm_plan` conversational intent and transition the selected
  draft to `confirmed`
- [ ] ensure edits address an explicit latest draft or active plan and never
  silently save when the requested day or meal does not exist
- [ ] remove the expired-plan fallback to Day 1
- [ ] write repository tests for draft, confirmed, future, active, and expired
  plan selection
- [ ] write command and conversational tests for confirmation and failed edits
- [ ] run lifecycle tests before Task 6

### Task 6: Preserve correct meal history and outcome feedback

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [ ] query all meals in the requested inclusive ISO-date window instead of
  truncating to an item count
- [ ] handle DynamoDB pagination while preserving reverse chronological order
- [ ] normalize conversational meal-log dates before constructing DynamoDB
  sort keys
- [ ] persist cooked, skipped, and swapped as distinct outcomes
- [ ] omit `unreported` meals from positive or negative previous-plan feedback
- [ ] write history tests with multiple meals per day, old records, boundaries,
  pagination, and malformed dates
- [ ] write feedback tests covering every meal outcome
- [ ] run history, prompt, and handler tests before Task 7

### Task 7: Finalize and refresh groceries asynchronously

**Files:**
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_parser.py`

- [ ] add explicit planner events for meal-plan generation and grocery
  finalization for a specific week
- [ ] save new meal plans as drafts without claiming groceries are ready
- [ ] invoke grocery finalization asynchronously after confirmation
- [ ] clear or mark groceries pending after edits to a confirmed plan, then
  invoke refresh for that exact week
- [ ] require at least one valid non-empty grocery section before marking the
  list ready
- [ ] persist `error` state and notify the user when grocery generation or
  parsing fails
- [ ] write full success tests for generate, confirm, finalize, edit, and
  refresh flows
- [ ] write tests for empty, malformed, stale-week, and LLM failure responses
- [ ] run planner and bot workflow tests before Task 8

### Task 8: Make check-in callbacks plan-specific and atomic

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_telegram_api.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`

- [ ] encode the source `week_start` and validated outcome in callback data
- [ ] parse and validate the exact callback format and Telegram length limit
- [ ] reject callbacks for stale, missing, draft, or expired plans
- [ ] update only the selected nested meal outcome with a targeted DynamoDB
  update expression rather than rewriting the whole plan
- [ ] report failure when the day or meal type does not exist
- [ ] implement `answer_callback_query` and call it on every callback path
- [ ] write tests for every action, malformed data, old keyboards, missing
  meals, and persistence errors
- [ ] write repository tests proving independent meal updates are not lost
- [ ] run callback, Telegram, router, and DynamoDB tests before Task 9

### Task 9: Make Telegram output safe and observable

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_telegram_api.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

- [ ] make plain text the default for all Telegram messages
- [ ] remove unsafe legacy Markdown interpolation from profiles, plans,
  groceries, check-ins, and raw LLM replies
- [ ] preserve readable chunking without splitting formatting entities
- [ ] define a Telegram API error result or exception that handlers cannot
  silently ignore
- [ ] log endpoint, status, and safe error details without logging tokens or
  personal content
- [ ] write tests with Markdown control characters, long content, HTTP errors,
  and partial multi-chunk failures
- [ ] update handler tests for controlled Telegram delivery failures
- [ ] run Telegram and handler tests before Task 10

### Task 10: Bound external calls and production retries

**Files:**
- Modify: `src/meal_planner/config.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `template.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_telegram_api.py`

- [ ] add validated request-timeout, retry-count, and initial-backoff settings
  with production-safe defaults
- [ ] pass a bounded timeout to LiteLLM and Telegram requests
- [ ] retry only transient LLM failures and honor provider retry guidance when
  available
- [ ] use bounded exponential backoff that fits within each Lambda deadline
- [ ] ensure exhausted requests return one controlled user-facing failure and
  do not persist partial state
- [ ] update SAM environment variables for timeout and retry configuration
- [ ] write deterministic tests for transient recovery, permanent failures,
  timeouts, retry exhaustion, and configured defaults
- [ ] run config, LLM, Telegram, and handler tests before Task 11

### Task 11: Verify release acceptance criteria

**Files:**
- Modify: `tests/` where final integration coverage is missing
- Modify: `docs/plans/2026-08-05-meal-planner-bot.md`
- Modify:
  `docs/plans/2026-08-10-meal-planner-bot-release-readiness-remediation.md`

- [ ] verify `/start`, `/profile`, `/plan`, `/grocery`, `/today`, and
  `/submit_meals` through handler integration tests
- [ ] verify meal logging, profile updates, plan edits, confirmation, grocery
  finalization, and all check-in outcomes
- [ ] verify missing profiles/plans, expired plans, malformed LLM output,
  DynamoDB failures, timeouts, and Telegram failures
- [ ] run `uv run pytest` and confirm all tests pass
- [ ] run `uv run ruff check .` and confirm it passes
- [ ] run `uv run ruff format --check .` and confirm it passes
- [ ] run `uv run mypy` and confirm strict typing passes
- [ ] run `uvx aws-sam-cli validate --lint` and `uvx aws-sam-cli build`
- [ ] smoke-import both built Lambda handlers with the Python 3.12 runtime
- [ ] mark the corresponding original-plan acceptance items complete only
  after their evidence is verified

### Task 12: [Final] Complete documentation and plan tracking

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-08-05-meal-planner-bot.md`
- Move: `docs/plans/2026-08-05-meal-planner-bot.md` to
  `docs/plans/completed/2026-08-05-meal-planner-bot.md`
- Move:
  `docs/plans/2026-08-10-meal-planner-bot-release-readiness-remediation.md`
  to `docs/plans/completed/`

- [ ] document architecture, prerequisites, uv setup, and local test commands
- [ ] document all environment variables, SSM SecureString parameters, and
  safe secret-rotation steps
- [ ] document SAM validation, build, deployment, and rollback commands
- [ ] document Telegram webhook registration with `secret_token`
- [ ] document all commands, onboarding, confirmation, grocery states, and
  check-in outcomes
- [ ] document prompt customization and operational troubleshooting
- [ ] confirm no new reusable engineering convention requires an AGENTS.md
  update; update it only if one was introduced
- [ ] update both plan files so all completed work and deviations are accurate
- [ ] run the full pytest, Ruff lint, Ruff format, mypy, and SAM verification
  suite one final time
- [ ] move both completed plans to `docs/plans/completed/`
- [ ] comment on the associated GitHub issue with the implementation commit or
  PR link

## Post-Completion

### Manual verification

- Create the bot token, LLM key, and webhook secret as SSM SecureStrings in a
  non-production AWS account.
- Deploy a dedicated feature branch through a pull request; never push or
  merge directly to `master`.
- Register the deployed webhook URL and matching `secret_token` with Telegram.
- Exercise onboarding through per-person calorie targets.
- Generate, edit, confirm, and retrieve a complete seven-day plan and grocery
  list.
- Exercise cooked, skipped, and swapped callbacks, including an old keyboard.
- Verify timeout and provider-failure messages without exposing secrets or
  personal data in logs.

### External system updates

- Store SSM parameters using the names supplied to the SAM stack.
- Update the Telegram webhook after deployment.
- Link the final conventional commit or draft PR from the associated GitHub
  issue and summarize completed remediation there.
