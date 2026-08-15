# Guided Meal Logging and Reliable Preference-Aware Planning

Associated issue: [#29](https://github.com/nsal/meal-planner-bot/issues/29)

## Overview

Replace the current `/submit_meals` planned-meal check-in with a durable,
guided workflow for recording one actual meal at a time. The workflow must
collect an explicit date, meal type, and description, accept dates from today
through the previous seven days, and preserve multiple meals of the same type
on one day. Move the existing planned-meal outcome buttons to `/checkin`.

Change `/plan` so it asks for a free-text preference before starting the
asynchronous planner. Carry that request-specific preference into generation
without changing the permanent family profile. Make whole-week generation
reliable enough for the observed provider latency by using two bounded
45-second attempts, distinguishing transport timeouts from invalid responses,
and using validation feedback on the second attempt.

This work fixes the misleading `No active plan for today.` meal-submission
failure and the plan-generation failure caused by repeated 20-second provider
timeouts.

## Context (from discovery)

- **Primary stack:** Python 3.14, Aiogram-compatible Telegram updates, AWS
  Lambda, DynamoDB, Pydantic, LiteLLM, and AWS SAM.
- **Bot routing and mutations:** `src/meal_planner/bot_handler.py` and
  `src/meal_planner/router.py` currently map `/submit_meals` to active-plan
  check-in buttons. Conversational meal logging exists but silently defaults
  missing values to today, snack, and `Logged meal`.
- **Persistence:** `src/meal_planner/db/dynamo.py` already stores distinct meal
  history records, including multiple entries of the same type and date, and
  provides Telegram-update idempotency. It does not persist general
  conversation workflow state.
- **Contracts and prompts:** `src/meal_planner/models/schemas.py` defines a
  singular `MealLogEntry`; `src/meal_planner/llm/prompts.py` does not describe
  pending meal or plan-preference workflows and the plan prompt has no
  request-specific preference.
- **Planner reliability:** `src/meal_planner/planner_handler.py` treats an empty
  response after timeout like any other invalid plan.
  `src/meal_planner/llm/client.py` retries transport failures but loses their
  category and cannot retry invalid plan content with validation feedback.
- **Deployment budget:** `src/meal_planner/config.py` and `template.yaml`
  configure two 45-second planner attempts in a 180-second Lambda. The
  observed CloudWatch request reached the 20-second per-attempt limit.
- **Project standards:** follow `pyproject.toml`; use `uv`, Ruff at 80 columns,
  strict Mypy, typed Python, and `uv run pytest`. All changes must be delivered
  on a non-`master` branch through a pull request with a Conventional Commit
  referencing the associated issue.

## Development Approach

- **Testing approach:** TDD. Write failing tests for each behavior before its
  implementation.
- Complete each task fully before moving to the next and make small, focused
  changes.
- Every task that changes code must add or update tests for both successful and
  unsuccessful paths.
- Run the full test suite after each implementation task. Do not proceed while
  tests fail.
- Keep the plan file synchronized with implementation. Mark completed items
  immediately, add newly discovered tasks with a `➕` prefix, and record
  blockers with a `⚠️` prefix.
- Preserve existing profile, plan editing, confirmation, grocery, callback,
  authorization, and webhook-idempotency behavior unless this plan explicitly
  changes it.

## Testing Strategy

- **Model tests:** validate workflow kinds, steps, partial meal drafts,
  revisions, timestamps, expiry, and impossible field combinations.
- **Repository tests:** use the existing Moto-backed DynamoDB fixture to cover
  state round trips, expiry, conditional transitions, deletion, and contention.
- **Bot tests:** exercise command routing and multi-turn workflows through
  separate handler calls, including duplicate Telegram updates and recovery
  paths.
- **Prompt and parser tests:** assert current-date and pending-state guidance,
  explicit-field extraction, preference rendering, and absence of invented
  defaults.
- **Planner and LLM tests:** mock provider timeouts, permanent failures,
  malformed JSON, schema-invalid plans, successful repair, and exhausted
  attempts without real network calls.
- **Configuration tests:** prove the configured worst-case provider, retry,
  Telegram, and safety budget is below the deployed Lambda timeout.
- **End-to-end scope:** this repository has no browser UI or E2E framework.
  Lambda boundary tests and a post-deployment Telegram smoke test cover the
  integrated workflow.
- **Final gates:** `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest` must all pass.

## Progress Tracking

- Mark completed checklist items with `[x]` immediately.
- Add newly discovered work with a `➕` prefix in the appropriate task.
- Add blockers with a `⚠️` prefix and describe their impact.
- Update architectural or scope sections before implementing an approved
  deviation.
- Move this document to `docs/plans/completed/` only after all acceptance gates
  pass.

## Solution Overview

Use one typed, persisted conversation-state record per Telegram user. It holds
either a meal-log workflow or a plan-request workflow, the current step,
collected values, a monotonic revision, timestamps, and a 24-hour expiry. The
bot merges explicitly extracted meal fields into this state and asks only for
the next missing value. Conditional state transitions ensure a replayed update
cannot save a meal twice or invoke the planner twice.

`/submit_meals` starts the actual-meal workflow. `/checkin` retains the existing
active-plan outcome buttons. `/cancel` clears unfinished state. Starting either
`/submit_meals` or `/plan` replaces an older unfinished workflow and tells the
user that it did so.

`/plan` stores an awaiting-preference workflow and asks, “Do you have any
preferences for the next plan?” Its next ordinary text reply is a one-time
preference; `anything` and `no preference` normalize to no constraint. The
asynchronous event carries the preference and a request identifier. Failed
generation retains the preference in retry-ready state for 24 hours; a later
`/plan` retries it without requiring re-entry. Successful generation clears
the workflow.

Keep a single whole-week generation request per attempt. Give the provider two
45-second attempts inside a 180-second Planner Lambda. Preserve failure type,
validate each response with Pydantic, and add concise validation feedback to
the second request when the first response is structurally invalid.

## Technical Details

### Conversation state

- Add enums for workflow kind and step plus typed models for a partial meal-log
  draft and persisted conversation state.
- Use a fixed DynamoDB sort key such as `CONVERSATION_STATE` under the existing
  `USER#{user_id}` partition. Store a revision and numeric TTL attribute.
- Treat an expired item as absent even before DynamoDB's asynchronous TTL
  cleanup removes it.
- Save and consume state with conditional revision checks. A transition that
  loses a race must reload or return a safe result indicating that the state
  was already handled or changed. It must not repeat an external mutation.
- Keep profile drafts in their existing independent record and contract.

### Guided meal submission

- `/submit_meals` does not query an active plan. It creates an empty draft and
  asks for the date.
- Valid meal dates form the inclusive interval `[today - 7 days, today]`.
- Valid types remain `breakfast`, `lunch`, `dinner`, and `snack`.
- Remove handler defaults for date, type, and description. The LLM contract may
  omit unknown entities but may not invent them.
- Supply today's ISO date and pending meal fields to the conversational prompt.
  If one message explicitly contains every field, validate and save it without
  redundant questions; otherwise request date, then type, then description.
- After saving, transition to `awaiting_another_meal`. A recognized affirmative
  response starts a new empty draft; a recognized negative response clears
  state; unclear input asks the yes/no question again.
- Retain the existing `MealLogEntry` and `log_meal` key design so repeated meal
  types on one date remain separate and source-update retries remain
  idempotent.

### Preference-aware planning

- `/plan` still requires a complete profile, but it no longer invokes the
  Planner Lambda immediately.
- The awaiting-preference state consumes the next non-command text exactly
  once. Normalize explicit no-preference phrases to `None`; otherwise preserve
  trimmed free text within a bounded typed field.
- Extend the Planner Lambda event with `preference` and a stable request ID.
  Validate both at the event boundary.
- Render preference as a separate high-priority request section in the plan
  prompt. Permanent allergies, restrictions, calorie targets, and profile
  requirements continue to take precedence over it.
- Mark a request retry-ready on terminal generation failure and retain its
  preference. Clear matching state only after the draft has been persisted.
- Use revision/request-ID checks so stale workers cannot clear or overwrite a
  newer interaction.

### Planner attempts and failure reporting

- Refactor the LLM boundary to expose typed timeout, transient/provider,
  permanent, and response-format failures to callers that need strict handling,
  while preserving the conversational fallback behavior.
- Enforce exactly two total whole-plan provider attempts; avoid multiplying
  client-level transport retries by handler-level schema retries.
- On a schema-invalid first response, construct a concise, bounded repair
  instruction from Pydantic validation errors and retry once with the original
  prompt and preference.
- On timeout exhaustion, tell the user generation timed out and that `/plan`
  will retry the saved preference. Use a different message for repeated invalid
  output. Never persist a partial or invalid plan.
- Keep existing compare-and-swap draft persistence and stale-result rejection.
- Configure a 45-second per-attempt timeout, two attempts, and a 180-second
  Planner Lambda deadline; update budget validation and documentation together.

## Implementation Steps

### Task 1: Define durable conversation workflow contracts

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/test_schemas.py`

- [x] write failing tests for valid meal-log and plan-request workflow states
- [x] write failing tests for invalid steps, revisions, expiry values, field
  limits, and incompatible workflow data
- [x] add typed workflow-kind, workflow-step, partial-meal, and
  conversation-state models with bounded values and timestamps
- [x] export the new public contracts through `models/__init__.py`
- [x] run `uv run pytest tests/test_schemas.py`, then `uv run pytest`; both must
  pass before Task 2

### Task 2: Persist and conditionally transition workflow state

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] write failing round-trip and deletion tests for both workflow kinds
- [x] write failing tests for logical expiry before DynamoDB TTL cleanup
- [x] write failing contention tests proving stale revisions cannot overwrite,
  consume, or delete newer state
- [x] implement typed get, create/replace, conditional-transition, and
  conditional-delete repository methods using a fixed per-user key and TTL
- [x] ensure serialized values follow existing DynamoDB/Pydantic conventions and
  do not interfere with profile drafts or meal-history queries
- [x] run `uv run pytest tests/test_dynamo.py`, then `uv run pytest`; both must
  pass before Task 3

### Task 3: Separate actual-meal submission from planned-meal check-in

**Files:**
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing routing tests for `/checkin` and `/cancel`
- [x] write failing handler tests proving `/submit_meals` starts a meal draft
  without loading an active plan
- [x] write failing regression tests proving `/checkin` retains current active
  plan checks, buttons, and callback behavior
- [x] implement `/submit_meals`, `/checkin`, and `/cancel`, including explicit
  messaging when a new command replaces an unfinished workflow
- [x] update command help/start messaging where command choices are presented
- [x] run `uv run pytest tests/test_router.py tests/test_bot_handler.py`, then
  `uv run pytest`; both must pass before Task 4

### Task 4: Implement one-at-a-time guided meal logging

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [x] write failing multi-turn tests for date, meal type, description, save, and
  “log another?” transitions across independent handler calls
- [x] write failing tests for today, the seven-day boundary, future/older dates,
  unknown meal types, empty descriptions, and ambiguous yes/no answers
- [x] write failing tests proving two same-type meals on one date are distinct
  and duplicate Telegram updates cause only one save
- [x] write failing prompt tests requiring explicit meal fields, today's date,
  pending workflow context, and no invented defaults
- [x] merge only explicit entities into state, validate at the handler boundary,
  ask for the next missing field, and atomically save completed entries
- [x] remove the current today/snack/`Logged meal` fallback values and return
  specific validation/recovery messages
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_prompts.py`, then
  `uv run pytest`; both must pass before Task 5

### Task 5: Capture plan preferences before asynchronous invocation

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing tests proving `/plan` asks the preference question and does
  not immediately invoke the planner
- [x] write failing tests for normal preferences, normalized no-preference
  phrases, length validation, duplicate updates, and invocation failure
- [x] write failing tests proving retry-ready `/plan` requests reuse the saved
  preference and newer workflows cannot be consumed by stale replies
- [x] implement awaiting-preference, generating, and retry-ready transitions
  with stable request IDs and conditional revisions
- [x] include validated preference and request ID in the asynchronous payload
  exactly once, retaining recoverable state when invocation cannot start
- [x] run `uv run pytest tests/test_bot_handler.py`, then `uv run pytest`; both
  must pass before Task 6

### Task 6: Carry request preferences through planner events and prompts

**Files:**
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_prompts.py`

- [x] write failing event-boundary tests for valid, absent, oversized, and
  incorrectly typed preference/request-ID values
- [x] write failing prompt tests showing the request preference separately from
  permanent profile constraints and giving safety constraints precedence
- [x] write failing state-lifecycle tests for clearing a matching request after
  persistence and retaining/marking it retry-ready after terminal failure
- [x] extend planner dispatch and `generate_plan` with typed request context
  without changing the stored `UserProfile`
- [x] extend `build_plan_prompt` with the normalized one-plan preference and
  maintain the existing complete-week JSON contract
- [x] guard state updates with request ID and revision so stale planner events
  cannot alter a newer workflow
- [x] run `uv run pytest tests/test_planner_handler.py tests/test_prompts.py`,
  then `uv run pytest`; both must pass before Task 7

### Task 7: Add bounded plan validation repair and typed LLM failures

**Files:**
- Modify: `src/meal_planner/llm/client.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_planner_handler.py`

- [x] write failing LLM tests that distinguish timeout, transient, permanent,
  malformed-JSON, and successful structured responses
- [x] write failing planner tests for first-attempt success, timeout recovery,
  schema repair success, timeout exhaustion, invalid-output exhaustion, and the
  exact two-attempt ceiling
- [x] add a strict typed LLM result/failure boundary while preserving existing
  safe fallback semantics for ordinary bot conversation
- [x] expose bounded Pydantic validation details needed for repair without
  weakening `WeeklyPlan` validation
- [x] implement one repair attempt that includes concise validation feedback
  and keeps the original profile, history, week, and preference context
- [x] send accurate timeout versus invalid-plan failure messages, persist no bad
  draft, and retain the preference for `/plan` retry
- [x] run `uv run pytest tests/test_llm_client.py tests/test_parser.py
  tests/test_planner_handler.py`, then `uv run pytest`; both must pass before
  Task 8

### Task 8: Expand and verify the asynchronous planner time budget

**Files:**
- Modify: `src/meal_planner/config.py`
- Modify: `template.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_template.py`

- [x] write failing settings tests for two 45-second attempts within a
  180-second planner deadline and for rejected over-budget configurations
- [x] write failing template tests for the matching Planner Lambda timeout and
  environment values
- [x] update planner timeout bounds/defaults, attempt count, Lambda timeout, and
  safety-budget validation without relaxing the 30-second Bot/API Gateway
  boundary
- [x] verify SAM template values and application defaults cannot silently drift
- [x] run `uv run pytest tests/test_config.py tests/test_template.py`, then
  `uv run pytest`; both must pass before Task 9

### Task 9: Verify all acceptance criteria and quality gates

**Files:**
- Modify if needed: tests associated with changed modules
- Modify if needed: implementation files identified above

- [x] verify `/submit_meals` logs actual meals without an active plan and
  `/checkin` preserves planned outcomes
- [x] verify the full inclusive date window, explicit required fields, repeated
  meal types, multi-turn recovery, expiry, cancellation, and idempotency
- [x] verify `/plan` always obtains or reuses an explicit preference decision
  before generation and never persists it into the family profile
- [x] verify slow-provider retry, invalid-output repair, truthful failure
  messages, compare-and-swap plan persistence, and recoverable preferences
- [x] run `uv run ruff format .` and inspect the formatting changes
- [x] run `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`,
  and `uv run pytest`; all must pass before Task 10

### Task 10: [Final] Update documentation and close plan tracking

**Files:**
- Modify: `README.md`
- Modify: this plan document in `docs/plans`
- Move when complete: this plan document to `docs/plans/completed`

- [x] document `/submit_meals`, `/checkin`, `/cancel`, the guided date window,
  repeated meal types, and preference-aware `/plan` behavior
- [x] document the new conversation-state persistence/recovery behavior and
  planner timeout/failure troubleshooting
- [x] update configuration tables to the deployed 45-second/two-attempt/
  180-second values
- [x] update `AGENTS.md` only if implementation establishes a genuinely reusable
  project convention not already captured there
- [x] run `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`,
  and `uv run pytest` one final time
- [x] mark every completed item, record any approved deviations, and move this
  plan to `docs/plans/completed/`

## Post-Completion

### Manual verification

- Deploy from a dedicated feature branch through a pull request; never push or
  merge directly to `master`.
- In a non-production Telegram chat, run `/submit_meals` through a complete
  entry, add a second meal of the same type/date, decline another entry, and
  confirm both records influence subsequent plan context.
- Verify `/checkin` still displays and applies cooked, skipped, and swapped
  outcomes for today's active confirmed plan.
- Run `/plan`, reply with `Indian and pasta`, confirm the delivered draft
  reflects that preference, and verify the saved profile is unchanged.
- Repeat with `no preference` and verify generation proceeds without a cuisine
  constraint.
- Review CloudWatch duration and timeout metrics after deployment to confirm
  normal plan generation completes within the new budget.

### External system updates

- Deploy the SAM/CloudFormation timeout and environment changes with the code.
- Verify the DynamoDB table has TTL enabled for the conversation-state expiry
  attribute; include the table setting in the same CloudFormation change if it
  is not already enabled.
- After implementation, create a Conventional Commit referencing the associated
  GitHub issue, push the feature branch, open a pull request, and comment on the
  issue with the commit or PR link as required by `AGENTS.md`.
