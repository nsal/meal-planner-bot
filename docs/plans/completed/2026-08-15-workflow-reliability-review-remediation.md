# Workflow Reliability Review Remediation

## Overview

- Resolve all five review findings affecting guided meal logging, workflow
  replacement, plan-generation lifecycle cleanup, atomic meal persistence,
  and configured planner attempt limits.
- Restore the normal date → meal type → description workflow and prevent stale
  or concurrent invocations from duplicating meals or overwriting newer
  conversation state.
- Preserve existing commands, user-facing behavior, data models, and public
  interfaces except for the narrow repository and handler APIs needed to make
  the completed meal write atomic.
- GitHub issue: [#31](https://github.com/nsal/meal-planner-bot/issues/31).

## Context (from discovery)

- **Files/components involved:** `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`, `src/meal_planner/config.py`,
  `src/meal_planner/db/dynamo.py`, and the corresponding handler,
  configuration, and DynamoDB tests.
- **Related patterns found:** conversation state uses optimistic revision checks;
  meal idempotency uses DynamoDB transactions for Telegram update markers; plan
  generation already carries a request ID and state revision into the worker.
- **Dependencies identified:** Pydantic JSON-mode serialization for enum values,
  boto3 DynamoDB transactional writes and conditional expressions, and the
  configured `PLANNER_LLM_MAX_RETRIES` budget.
- **Project constraints:** Python 3.14, Ruff formatting and linting at 80 columns,
  strict mypy, and all commands executed through `uv`.
- **Recent context:** commit `887319a` introduced guided meal logging and reliable
  preference-aware planning, including the paths covered by these findings.

## Development Approach

- **Testing approach:** TDD. Add a focused failing regression test before each
  production-code change, then make the smallest change that passes it.
- Complete each task fully before moving to the next and keep changes focused.
- Every task that changes code includes new or updated tests covering successful
  behavior and relevant stale-state, failure, or boundary behavior.
- All tests for a task must pass before starting the next task.
- Update this plan immediately if implementation scope or design changes.
- Preserve backward compatibility for existing commands, stored items, and
  repository callers.

## Testing Strategy

- **Unit tests:** exercise enum round-tripping, replacement revision assignment,
  confirmed-plan cleanup, and one-versus-two configured planner attempts with
  deterministic mocks.
- **DynamoDB integration tests:** use the existing moto-backed repository fixture
  to verify that state advancement and meal creation commit or fail together,
  including competing writes from the same completed draft.
- **Handler integration tests:** verify the complete guided meal flow and ensure
  a losing concurrent handler does not call a separate non-atomic meal write.
- **Configuration coverage:** retain validation that one or two planner attempts
  are valid and verify runtime behavior matches the value included in timeout
  budgeting.
- **Full verification:** run Ruff formatting/checks, strict mypy, and the complete
  pytest suite through `uv`.
- There is no browser UI or UI-based end-to-end test suite in this repository.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document issues or blockers with a ⚠️ prefix.
- Update the plan if implementation deviates from the original scope.
- Keep this document synchronized with the actual work.

## Solution Overview

- Serialize the accumulated `MealLogDraft` in JSON mode before revalidation so
  stored `MealType` members become their string values.
- Give every replacement workflow `previous.revision + 1`; retain revision zero
  only when creating state where no prior state exists. The existing conditional
  write then rejects concurrent replacement and delayed-transition ABA races.
- Add one DynamoDB repository operation that transactionally writes the meal and
  advances conversation state only when the expected revision still exists.
  Guided completion will use this operation instead of two independent writes.
- Clear a matching stateful planner request before returning when the target week
  is already confirmed.
- Pass the configured total planner attempt count into `PlannerHandler` and use
  it as the bound for generation/repair attempts, keeping runtime behavior equal
  to configuration budget validation.

## Technical Details

- `MealLogDraft.model_dump(mode="json")` yields enum values and ISO-compatible
  scalar data that the existing normalization and validation flow can consume.
- Replacement writes continue to condition on the previously observed revision,
  but the persisted candidate's revision is strictly greater than that snapshot.
- The atomic meal operation accepts the completed `MealLogEntry`, next
  `ConversationState`, expected state revision, and optional Telegram update ID.
  Its transaction includes the conditional conversation-state put, meal put,
  and the existing update-id marker when present. A conditional cancellation
  returns `False`; other DynamoDB errors propagate. No meal remains when the
  state claim loses or any transaction action fails.
- The handler constructs the `AWAITING_ANOTHER_MEAL` state first and reports a
  stale-workflow result when the transaction returns `False`. It retains the
  current retry message when persistence raises, because the transaction leaves
  the original draft intact.
- Confirmed-week cleanup uses `clear_conversation_state_if_matches` only when
  both `request_id` and `state_revision` are supplied, so an unrelated or newer
  workflow cannot be deleted.
- `PlannerHandler` receives a positive `max_attempts` value (defaulting to the
  existing two-attempt behavior for direct callers). The Lambda factory passes
  `settings.planner_llm_max_retries`, and terminal-attempt checks derive from
  that bound rather than the literal attempt index `1`.

## What Goes Where

- **Implementation Steps:** repository, handler, and regression-test changes in
  this codebase are tracked with checkboxes below.
- **Post-Completion:** manual concurrency smoke testing and GitHub workflow steps
  are informational because they require external action.

## Implementation Steps

### Task 1: Preserve stored meal-type values during draft revalidation

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Add a failing regression test that completes the date → type → description
  sequence across separate updates and asserts one valid meal is submitted.
- [x] Add an edge-case test showing an invalid newly supplied meal type still
  produces the existing validation prompt without corrupting saved draft fields.
- [x] Serialize the accumulated meal draft in JSON mode before merging and
  revalidating fields so `MealType` values round-trip correctly.
- [x] Run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 2.

### Task 2: Make replacement workflow revisions strictly monotonic

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`

- [x] Add failing tests for replacing a workflow at revisions zero and greater
  than zero, asserting the saved candidate is exactly one revision newer.
- [x] Add a regression test simulating two replacements from the same snapshot
  and a delayed old transition; assert only the first replacement succeeds and
  the delayed transition cannot overwrite it.
- [x] Update replacement construction/persistence to store revision zero only on
  first creation and `previous.revision + 1` on replacement.
- [x] Confirm failed conditional replacement retains the existing user-facing
  retry behavior and does not mutate the caller's stale snapshot unexpectedly.
- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py`; it must
  pass before Task 3.

### Task 3: Persist completed guided meals and state transitions atomically

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] Add failing DynamoDB tests proving a successful transaction writes exactly
  one meal, advances the expected conversation revision, and preserves the
  existing Telegram update-id marker behavior.
- [x] Add failing stale-revision and transaction-error tests proving neither the
  meal nor next state is partially persisted when the transaction loses or
  fails.
- [x] Add a handler regression test for two distinct replies from the same
  completed draft, asserting only the revision winner creates a meal.
- [x] Implement a typed repository method that transactionally puts the meal,
  conditionally advances conversation state, and optionally writes the
  idempotency marker.
- [x] Replace the guided workflow's separate `log_meal` and state transition
  calls with the atomic repository method while retaining retry-safe messaging.
- [x] Run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py`; it must
  pass before Task 4.

### Task 4: Clear a matching planner request on confirmed-week early return

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Add a failing stateful-generation test for an already confirmed target
  week that expects matching conversation-state cleanup before return.
- [x] Add edge-case tests proving stateless calls do not request cleanup and a
  failed conditional cleanup cannot delete a newer workflow.
- [x] Clear the request with its request ID and expected revision on the terminal
  confirmed-plan path while preserving the existing notification.
- [x] Run `uv run pytest tests/test_planner_handler.py`; it must pass before
  Task 5.

### Task 5: Honor the configured total planner attempt count

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Add failing tests showing an attempt limit of one makes exactly one
  provider call for transient failure and invalid output, with no repair call.
- [x] Add tests showing an attempt limit of two retains the existing single
  repair/retry opportunity and terminal notifications.
- [x] Add constructor validation or an equivalent typed invariant that prevents
  non-positive attempt counts.
- [x] Replace the hard-coded two-iteration loop and `attempt == 1` terminal checks
  with the configured bound, and pass the setting from `lambda_handler`.
- [x] Verify mocked Lambda construction forwards
  `settings.planner_llm_max_retries` to both the runtime handler behavior and the
  existing client configuration without adding provider-level retries.
- [x] Run `uv run pytest tests/test_planner_handler.py tests/test_config.py`; it
  must pass before Task 6.

### Task 6: Verify all acceptance criteria and quality gates

**Files:**

- Verify: `src/meal_planner/`
- Verify: `tests/`
- Modify if required: files changed in Tasks 1–5

- [x] Verify the normal guided meal flow saves a meal and reaches
  `AWAITING_ANOTHER_MEAL`.
- [x] Verify replacement revisions are monotonic and stale writers cannot win.
- [x] Verify concurrent completed-draft replies cannot produce duplicate or
  conflicting meal entries.
- [x] Verify confirmed-week state cleanup and one/two-attempt planner behavior.
- [x] Run `uv run ruff format --check .` and fix any formatting failures.
- [x] Run `uv run ruff check .` and fix all lint failures.
- [x] Run `uv run mypy` and fix all type failures.
- [x] Run `uv run pytest` and confirm the complete suite passes.

### Task 7: [Final] Update documentation and close plan tracking

**Files:**

- Modify if needed: `README.md`
- Modify if needed: `AGENTS.md`
- Move: `docs/plans/2026-08-15-workflow-reliability-review-remediation.md`
  to `docs/plans/completed/`

- [x] Update `README.md` only if runtime configuration or documented behavior
  changes; otherwise record that no README change is required.
- [x] Update `AGENTS.md` only if implementation discovers a reusable project
  convention; otherwise leave repository instructions unchanged.
- [x] Confirm all implementation and verification checkboxes are complete.
- [x] Move this plan to `docs/plans/completed/`.

README.md and AGENTS.md required no changes: the runtime configuration and
repository conventions were already documented.

## Post-Completion

**Manual verification:**

- Exercise `/submit_meals` in Telegram through date, meal type, description, and
  the follow-up prompt.
- Send two distinct description replies close together and confirm only one meal
  is retained and the conversation remains usable.
- Start a stateful `/plan` for a confirmed week and confirm the bot does not keep
  intercepting ordinary messages as generation work.

**External system updates:**

- Create implementation commits on the existing feature/bug branch using
  Conventional Commits and include the associated issue number.
- Open or update a pull request; do not push or merge directly to `master`.
- After completion, comment on the associated GitHub issue with the commit or PR
  link and a concise summary of the fixes and verification results.
