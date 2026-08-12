# Meal Planner Lifecycle Follow-Up Remediation

Associated issue: [#17](https://github.com/nsal/meal-planner-bot/issues/17)

## Overview

- Address the three actionable findings from the read-only review of the
  uncommitted post-review remediation.
- Prevent a plan edit racing with confirmation from advancing the confirmed
  plan revision without starting grocery generation for that revision.
- Restrict grocery retries to the active confirmed week and silently discard
  duplicate asynchronous grocery events after their request is no longer
  pending.
- Preserve the existing Telegram, Lambda, DynamoDB, and optimistic-revision
  architecture without introducing a queue, new persistence records, or a
  broader workflow redesign.

## Context (from discovery)

- **Primary stack:** Python 3.14, Pydantic, boto3/DynamoDB, AWS Lambda, Telegram
  Bot API, pytest, Ruff, and strict mypy.
- **Edit and confirmation paths:** `src/meal_planner/bot_handler.py` decides
  whether an edit needs grocery refresh, while
  `src/meal_planner/db/dynamo.py` performs the conditional nested-meal update.
- **Retry selection:** `src/meal_planner/bot_handler.py` currently allows the
  latest `confirmed/error` plan to bypass the active-plan lookup.
- **Asynchronous finalization:** `src/meal_planner/planner_handler.py` currently
  sends a failure-style Telegram message when a duplicate event observes a
  non-pending grocery state.
- **Existing patterns:** plan revisions protect meal-content writes, grocery
  finalization uses targeted conditional updates, and handler tests use mocks
  while repository concurrency tests use moto DynamoDB.
- **Dependencies:** no new runtime or development dependencies are required.

## Development Approach

- **Testing approach:** TDD; write a failing regression test for each finding
  before changing production code.
- Complete each task fully before moving to the next.
- Make small, focused changes and retain the existing repository boundaries.
- Every task that changes code must add or update success and failure tests.
- All tests for a task must pass before the next task begins.
- Update this plan immediately if scope or implementation details change.
- Maintain backward compatibility for existing valid plan and grocery states.
- Use `uv run` for project tools, Ruff for formatting and linting at 80 columns,
  and strict mypy for Python typing.

## Testing Strategy

- **Repository integration tests:** use moto DynamoDB to interleave
  confirmation and a stale draft edit, proving the edit conflicts instead of
  advancing the confirmed revision.
- **Bot handler tests:** cover normal draft confirmation, active grocery retry,
  inactive errored weeks, and controlled edit-conflict responses.
- **Planner handler tests:** cover duplicate events for `ready` and `error`
  states and prove they cause no LLM, repository, or Telegram side effects.
- **Regression checks:** retain successful confirmed-plan edits, grocery
  refresh invocation, invocation-failure recovery, and active error retries.
- **Release verification:** run pytest, Ruff lint, Ruff format check, strict
  mypy, SAM validation/build, and the required built-artifact template tests.
- This project has no browser UI or UI-based end-to-end test suite.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document issues or blockers with a ⚠️ prefix.
- Update the plan if implementation deviates from the design below.
- Keep this plan synchronized with the code and test state.

## Solution Overview

- Make the plan status observed by `BotHandler._edit_plan` part of the
  repository's optimistic-lock contract. A stale draft edit must fail if the
  plan became confirmed before the write; the user can retry against the
  confirmed state, which then schedules the required grocery refresh.
- Keep draft confirmation focused on the latest draft, but resolve every
  grocery retry through `get_active_plan`. An inactive past or future
  `confirmed/error` plan must never be transitioned or invoked.
- Treat a non-pending grocery event as stale asynchronous delivery. Log its
  current state and return without LLM work, persistence changes, or Telegram
  notification.

## Technical Details

### Atomic edit status contract

- Extend `DynamoRepository.update_meal` with a typed `expected_status` argument.
- Use `expected_status` both in the DynamoDB condition expression and when
  deciding whether to clear groceries and set them to `pending`.
- Pass the status loaded by `BotHandler._edit_plan` to the repository.
- Keep `refresh` derived from that same status so a successful write and the
  handler's invocation decision cannot disagree.
- If confirmation wins the race, the stale edit returns a controlled conflict;
  it must not increment the revision or alter the confirmed grocery request.

### Active retry selection

- Continue to confirm the latest draft when one exists.
- For all retry attempts, load the active confirmed plan and require its
  grocery state to be `error` before calling `retry_grocery`.
- Reject inactive errored weeks without invoking the Planner Lambda or changing
  DynamoDB state.
- Retain distinct confirmation, retry, conflict, and unsupported-state
  responses.

### Duplicate grocery events

- After validating the exact plan and confirmed status, treat any grocery state
  other than `pending` as a stale or duplicate event.
- Log the ignored week and observed grocery state at informational level.
- Do not call the LLM, grocery completion/failure operations, or Telegram API.

## What Goes Where

- **Implementation Steps:** repository contract, bot handler selection logic,
  planner duplicate-event handling, automated tests, and plan documentation in
  this repository.
- **Post-Completion:** deployment to a test stack and observation of real
  concurrent Telegram/Lambda deliveries.

## Implementation Steps

### Task 1: Make meal edits conflict with concurrent confirmation

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] write a failing moto test that loads a draft, confirms it, then attempts
  the stale draft edit and proves the edit is rejected without changing the
  confirmed revision or grocery state
- [x] write a failing bot-handler test proving an edit conflict does not invoke
  grocery refresh and returns the controlled retry response
- [x] add a typed expected-status argument to `update_meal` and use it in both
  the condition expression and confirmed-plan grocery invalidation decision
- [x] pass the handler's loaded plan status into `update_meal` and keep the
  refresh decision aligned with that status
- [x] retain tests for successful draft edits, successful confirmed edits,
  revision increments, grocery invalidation, and invocation-failure recovery
- [x] run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and fix
  every failure before Task 2

### Task 2: Restrict grocery retries to the active confirmed week

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing tests proving expired and future `confirmed/error` plans
  are not retried or invoked
- [x] write or update a success test proving an active `confirmed/error` plan
  transitions to `pending` and invokes its exact week
- [x] separate latest-draft confirmation selection from active-plan retry
  selection in `_confirm_plan`
- [x] return truthful controlled responses for inactive, missing, `pending`,
  and `ready` retry attempts without mutating state
- [x] retain conditional-conflict and asynchronous-invocation-failure coverage
- [x] run `uv run pytest tests/test_bot_handler.py` and fix every failure before
  Task 3

### Task 3: Suppress stale non-pending grocery events

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] write failing parameterized tests for duplicate events observing `ready`
  and `error` grocery states
- [x] assert duplicate events do not call the LLM, `complete_grocery`,
  `fail_grocery`, or any Telegram notification method
- [x] replace the non-pending failure notification with an informational log and
  an immediate return
- [x] retain controlled notifications for genuinely missing or unconfirmed
  plans and successful notification for newly completed groceries
- [x] run `uv run pytest tests/test_planner_handler.py` and fix every failure
  before Task 4

### Task 4: Verify acceptance criteria

**Files:**

- Modify: `tests/` only if final integration coverage is missing
- Modify: `docs/plans/2026-08-11-meal-planner-lifecycle-follow-up.md`

- [x] verify a confirmation/edit race cannot strand a new revision in
  `pending`
- [x] verify only an active `confirmed/error` plan can enter grocery retry
- [x] verify duplicate non-pending grocery events are silent and side-effect
  free
- [x] run `uv run pytest` and confirm all tests pass
- [x] run `uv run ruff check .` and confirm it passes
- [x] run `uv run ruff format --check .` and confirm it passes
- [x] run `uv run mypy` and confirm strict typing passes
- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] run `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` against the
  fresh build
- [x] record exact final verification evidence and fix every failure before
  Task 5

Final verification evidence: `uv run pytest` passed with 136 tests;
`uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`
passed; SAM validation passed; SAM build succeeded; and the fresh-build
artifact suite passed with 20 tests.

### Task 5: [Final] Update documentation and close plan tracking

**Files:**

- Modify: `README.md` only if user-visible lifecycle wording changes
- Modify: `AGENTS.md` only if a reusable engineering convention is introduced
- Modify: `docs/plans/2026-08-11-meal-planner-lifecycle-follow-up.md`
- Move: `docs/plans/2026-08-11-meal-planner-lifecycle-follow-up.md` to
  `docs/plans/completed/`

- [x] update README lifecycle or troubleshooting text only where implementation
  behavior differs from the existing documentation
- [x] update AGENTS.md only if implementation establishes a reusable project
  rule
- [x] verify every plan checkbox and test result matches the completed work
- [x] move the completed plan to `docs/plans/completed/`
- [x] comment on the associated GitHub issue with implementation status; add
  the implementation commit or draft PR link when the uncommitted changes are
  published

## Post-Completion

*Items requiring manual intervention or external systems; these are not
implementation checkboxes.*

### Manual verification

- In a test stack, submit confirmation and edit updates concurrently and verify
  the final plan either keeps the confirmed revision or requires the edit to be
  retried; it must not remain indefinitely `pending` without a worker.
- Attempt to confirm after an errored plan's week expires and verify no Planner
  Lambda invocation occurs.
- Replay a completed grocery event and verify logs record the ignored duplicate
  without sending a Telegram message.

### External system updates

- Monitor DynamoDB conditional-check conflicts and Planner Lambda duplicate
  deliveries after deployment.
- Add the implementation commit or draft PR link to the associated GitHub issue
  once code changes are published.
