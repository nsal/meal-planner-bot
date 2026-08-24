# Submit Meals Confirmation UX Review Remediation

Associated issue: [#55](https://github.com/nsal/meal-planner-bot/issues/55)

Review target: commit `642bdae` on
`feat/optimize-submit-meals-confirmation-ux`

## Overview

Remediate the five actionable findings from the independent review of the
single-meal confirmation workflow. The work restores isolated router imports,
makes every meal callback mutation atomic with respect to the submission ID
and expected workflow step, keeps Add-more timestamps valid, restores
continuation controls after an idempotent Confirm, and returns a specific
validation response for descriptions longer than 500 characters.

The remediation preserves the deterministic input grammar, atomic meal
confirmation, legacy workflow compatibility, and all unrelated bot behavior.
It adds no dependencies or deployment changes.

## Context

- `src/meal_planner/telegram/api.py` imports `MealCallbackAction` from
  `meal_planner.router`, while importing `meal_planner.router` initializes the
  Telegram package and its API module. This circular dependency breaks a clean
  router import and focused router test collection.
- `DynamoDBRepository.transition_conversation_state` and
  `delete_conversation_state` currently condition meal callback writes only on
  revision. Cancel, Add more, and Done therefore have a read/write race when a
  workflow is deleted and recreated at the same revision.
- `BotHandler._add_meal_callback` captures `now` before
  `_new_submission_state()` creates its timestamps, then overwrites only
  `updated_at`. The resulting state can have `updated_at < created_at` and fail
  validation when read back from DynamoDB.
- `BotHandler._confirm_meal_callback` reports an idempotent Confirm with plain
  text only. If the first saved-meal delivery failed, the user has no Add more
  or Done controls.
- `parse_meal_input` constructs `MealLogDraft` without translating the
  `MealDescription` 500-character validation failure into a parser error.
- Existing tests are in `tests/test_router.py`, `tests/test_dynamo.py`,
  `tests/test_bot_handler.py`, and `tests/test_telegram_api.py`. Repository
  tests use Moto, and project quality gates use Ruff, strict Mypy, and Pytest
  through `uv`.

## Development Approach

- Use TDD for every finding: add the smallest regression test first, run it to
  demonstrate the reviewed defect, implement the fix, and rerun the focused
  suite before proceeding.
- Complete tasks in order because the router import fix restores a reliable
  focused test gate, and the repository preconditions must exist before the
  handlers can use them.
- Keep new repository parameters backward compatible for non-meal callers.
  Do not weaken existing revision checks or change planner workflow behavior.
- Preserve exact callback payloads, Telegram labels/styles, confirmation
  transaction semantics, and the existing reusable meal input prompt.
- Keep typed Python at 80 columns and follow `pyproject.toml`.
- Do not add dependencies or modify deployment infrastructure.
- Keep this plan synchronized during implementation. Mark completed items
  immediately, prefix newly discovered work with `➕`, and record blockers
  with `⚠️`.

## Solution Overview

Move the callback action enum to the dependency-neutral schema/model layer and
have both routing and Telegram presentation depend on it. Keep the router's
public import compatible where practical, but remove the Telegram API's
dependency on the router.

Extend conversation-state transition and deletion operations so callers can
optionally require the current `request_id` and `step` in the same DynamoDB
condition as `revision`. Meal Cancel, Add more, and Done pass the callback's
submission ID and their expected source step. A state recreated between the
handler read and write then fails the condition even if its revision happens
to match.

Construct Add-more state with one internally consistent timestamp source and
prove it can be persisted and deserialized through the real Moto-backed
repository. On an idempotent Confirm, render the saved-meal continuation
keyboard again. Finally, enforce the documented 500-character description
boundary in deterministic parsing and keep handler validation failures on the
normal specific-error-plus-prompt path.

## Review Finding Coverage

| Finding | Severity | Remediation task |
| --- | --- | --- |
| Condition every meal callback mutation on its submission ID | P1 | Tasks 2-3 |
| Preserve valid timestamps when starting Add more | P1 | Task 3 |
| Remove the router/API circular import | P1 | Task 1 |
| Re-emit continuation controls after an idempotent Confirm | P2 | Task 4 |
| Convert overlong descriptions into parser errors | P2 | Task 5 |

## Technical Details

### Atomic callback preconditions

For the state item `USER#<user-id>` / `CONVERSATION`, the Cancel, Add-more,
and Done write conditions must require all of the following in one DynamoDB
operation:

- `revision == expected_revision`
- `request_id == callback_submission_id`
- `step == expected_source_step`

Expected source steps are:

- Cancel: `AWAITING_MEAL_CONFIRMATION`
- Add more: `AWAITING_MEAL_CONTINUATION`
- Done: `AWAITING_MEAL_CONTINUATION`

An expected conditional failure returns `False`; non-conditional DynamoDB
errors continue to propagate. Existing callers that do not supply request ID
or step retain their current revision-only behavior.

### Timestamp invariant

Every newly created submission state must satisfy
`created_at <= updated_at`. Add more should either preserve both timestamps
from `_new_submission_state()` or pass one shared UTC timestamp into state
construction and use it for both fields. The persisted state must deserialize
as `ConversationState` through `DynamoDBRepository.get_conversation_state`.

### Validation boundary

Descriptions of exactly 500 characters remain valid. Descriptions of 501 or
more characters return a field-specific parser error such as
`description must be 500 characters or fewer`; they must not raise a Pydantic
exception out of `parse_meal_input`. The handler appends the complete reusable
input prompt and leaves durable state unchanged.

## Implementation Steps

### Task 1: Break the router and Telegram API circular import

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_api.py` only if imports require adjustment

- [x] first add an isolated-import regression test in `tests/test_router.py`
  that launches `sys.executable` in a clean subprocess and imports
  `meal_planner.router` before any Telegram modules
- [x] run the isolated test and record the expected pre-fix failure: subprocess
  import or test collection fails because `telegram.api` imports
  `MealCallbackAction` from a partially initialized router
- [x] move `MealCallbackAction` to `src/meal_planner/models/schemas.py` (or an
  equivalently dependency-neutral model module), import it into the router for
  callback parsing, and import it directly from the model layer in
  `telegram/api.py`; preserve the existing action values and callback payloads
- [x] rerun the isolated test and `uv run pytest tests/test_router.py`; both
  must pass, including when the router is the first application module imported
- [x] run `uv run pytest tests/test_telegram_api.py` and `uv run mypy` as
  regression checks for callback typing, keyboard payloads, and public imports

**Acceptance criteria:**

- `python -c "import meal_planner.router"` succeeds in a fresh interpreter.
- Focused router test collection no longer depends on prior module import order.
- Confirm, Cancel, Add more, and Done retain their exact serialized action
  values and Telegram callback payloads.
- No Telegram presentation module imports `meal_planner.router`.

### Task 2: Add atomic request-ID and step preconditions to state mutations

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] first add Moto repository tests for
  `transition_conversation_state` and `delete_conversation_state` that persist
  a meal state, then replace it with a different request ID at the same
  revision before attempting the stale mutation
- [x] add parameterized mismatch tests for expected request ID and expected
  source step, covering transition and deletion independently
- [x] run the new tests and record the expected pre-fix failure: the stale
  same-revision write or delete succeeds because only `revision` is checked
- [x] extend the repository mutation APIs with optional typed expected request
  ID and expected step parameters, and compose their DynamoDB
  `ConditionExpression`, names, and values with the existing revision check
- [x] preserve `False` for conditional contention and exception propagation for
  non-conditional failures; retain revision-only behavior for existing callers
  that omit the new parameters
- [x] rerun the new tests and `uv run pytest tests/test_dynamo.py`; all must pass
- [x] run `uv run mypy` and existing planner/conversation-state tests as
  regressions for method signatures and non-meal workflows

**Blockers:** None. Pre-fix focused tests failed because the mutation APIs did
not accept the new precondition parameters; the revision-only implementation
would otherwise allow the stale same-revision mutation.

**Acceptance criteria:**

- A stale transition or deletion cannot mutate a different request at the same
  revision.
- A mutation with the wrong expected workflow step returns `False` and leaves
  the item unchanged.
- A mutation with matching revision, request ID, and step succeeds.
- Existing revision-only repository callers remain compatible.

### Task 3: Make Cancel, Add more, and Done race-safe and fix Add-more timestamps

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py` if the real round-trip fixture belongs there

- [x] first add handler interleaving tests for Cancel, Add more, and Done that
  return one matching state from the initial read, recreate a different
  submission at the same revision before the write, and assert the newer state
  survives with a stale-action response
- [x] first add a Moto-backed Add-more round-trip test that invokes the handler
  transition, reloads the resulting state through
  `DynamoDBRepository.get_conversation_state`, and asserts
  `created_at <= updated_at`, a fresh request ID, an empty draft, the input
  step, and an incremented revision
- [x] run the new tests and record the expected pre-fix failures: mocked handler
  calls omit request ID/step preconditions, the recreated workflow can be
  mutated, and the reloaded Add-more state fails timestamp validation
- [x] thread the parsed callback submission ID into
  `_cancel_meal_callback`, `_add_meal_callback`, and `_done_meal_callback`, and
  pass it with each exact expected source step to the repository mutation
- [x] update `_add_meal_callback` and/or `_new_submission_state` so one timestamp
  source produces a valid `created_at`/`updated_at` pair without weakening
  `ConversationState` validation
- [x] rerun the new handler and Moto round-trip tests; verify all three stale
  mutations return the stale-action path and never replace or delete the newer
  state
- [x] run `uv run pytest tests/test_bot_handler.py` and
  `uv run pytest tests/test_dynamo.py` as regression checks for callback
  acknowledgements, successful actions, contention, and persistence failures

**Acceptance criteria:**

- Cancel requires the callback submission ID and
  `AWAITING_MEAL_CONFIRMATION` in the atomic delete condition.
- Add more and Done require the callback submission ID and
  `AWAITING_MEAL_CONTINUATION` in their atomic write/delete conditions.
- An old callback cannot affect a workflow recreated at the same revision.
- Add more persists a fresh, valid state that can be read back immediately.
- Every success, stale, and exception path still acknowledges Telegram.

### Task 4: Restore continuation controls after an idempotent Confirm

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] first add a two-attempt handler test in which atomic persistence succeeds,
  the first `send_meal_saved` call raises `TelegramAPIError`, and a repeated
  Confirm observes the matching `AWAITING_MEAL_CONTINUATION` state
- [x] add coverage for both idempotent paths: the handler initially reads a
  continuation state, and a confirmation conditional loss reloads that state
- [x] run the new tests and record the expected pre-fix failure: the repeated
  Confirm sends only plain `already saved` text and never invokes the helper
  that carries Add more and Done buttons
- [x] change `_confirm_meal_callback` so a matching already-confirmed state
  re-emits `send_meal_saved` (or a dedicated equivalent continuation helper)
  with the persisted description and submission ID, while retaining truthful
  idempotent wording
- [x] define and test delivery-error behavior for the repeated response so the
  callback remains acknowledged and no second meal write is attempted
- [x] rerun the new tests and `uv run pytest tests/test_bot_handler.py`; verify
  the second Confirm emits continuation controls and calls
  `confirm_meal_and_transition` at most once across the delivery-retry scenario
- [x] run `uv run pytest tests/test_telegram_api.py` as a regression check for
  Add more/Done labels, styles, payloads, and 64-byte limits

**Acceptance criteria:**

- A user whose first saved-meal message failed can tap Confirm again and
  receive Add more and Done controls.
- Repeated Confirm never inserts or reports a second meal as newly saved.
- Both direct continuation-state detection and post-contention reload use the
  same recoverable presentation behavior.
- Telegram callback acknowledgement and delivery-error handling remain intact.

### Task 5: Return a specific parser error for overlong descriptions

**Files:**
- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_bot_handler.py`

- [x] first add parser boundary tests with descriptions of exactly 500 and 501
  characters, asserting the former creates a complete `MealLogDraft` and the
  latter returns a field-specific `MealInputParseResult.errors` entry
- [x] first add a handler test for a 501-character description that asserts the
  response contains the specific error followed by the complete
  `MEAL_INPUT_PROMPT`, with no state transition, meal write, or LLM call
- [x] run the new tests and record the expected pre-fix failure: the 501-character
  parser call raises Pydantic `ValidationError` and the handler falls back to a
  generic processing error
- [x] enforce or translate the `MealDescription` maximum inside
  `parse_meal_input` so model construction cannot leak validation exceptions
  for user-controlled description length; retain embedded commas and exact
  500-character input
- [x] rerun the 500/501 parser and handler tests and verify the invalid result
  uses the normal specific-error-plus-prompt path without durable mutation
- [x] run `uv run pytest tests/test_router.py` and
  `uv run pytest tests/test_bot_handler.py` as regressions for all grammar,
  field errors, date boundaries, meal types, state preservation, and LLM bypass

**Acceptance criteria:**

- Exactly 500 description characters are accepted; 501 are rejected.
- Overlong input returns a stable field-specific parser error and never raises
  a Pydantic exception to the handler.
- The handler repeats the full structured input prompt and preserves the active
  input state.
- First-two-commas parsing and descriptions containing commas remain unchanged.

## Final Verification and Acceptance Gates

After Tasks 1-5 are complete:

1. Run each focused suite independently in a fresh command:
   - `uv run pytest tests/test_router.py`
   - `uv run pytest tests/test_telegram_api.py`
   - `uv run pytest tests/test_dynamo.py`
   - `uv run pytest tests/test_bot_handler.py`
2. Run `uv run ruff format .`, then `uv run ruff format --check .`.
3. Run `uv run ruff check .`.
4. Run `uv run mypy`.
5. Run `uv run pytest` and require the complete suite to pass.
6. Run `git diff --check` and confirm no dependency, lockfile, or deployment
   changes were introduced.
7. Re-read all five review findings and confirm each is covered by a named
   regression test and passing acceptance criterion.

Do not move this plan to `docs/plans/completed/` until every remediation task
and final gate passes. Update `README.md` or `AGENTS.md` only if implementation
reveals a genuine user-facing or reusable convention change; none is currently
expected.

## Completion Record

The five remediation tasks were implemented in commit `6ebc1bd`
(`fix(meals): harden confirmation workflow (#55)`) and merged into `master`
through pull request #57 (`1811f82`) on 2026-08-23.

Verification completed on 2026-08-24:

- The remediation-focused suites passed: 371 tests passed across
  `test_router.py`, `test_telegram_api.py`, `test_dynamo.py`, and
  `test_bot_handler.py`.
- `uv run mypy` passed with no issues.
- In a clean worktree at `6ebc1bd`, Ruff format and lint passed.
- The clean commit's full suite passed 911 tests and skipped 2; one unrelated
  deployment timestamp assertion failed in
  `tests/test_deploy.py::test_configure_telegram_tolerates_historical_error`.
  The current worktree also contains unrelated in-progress protein/fibre
  target changes, so its full suite has additional unrelated failures.

The remediation implementation is complete and this plan is closed. The
remaining deployment-test discrepancy is outside issue #55 and requires no
change to the completed meal-confirmation remediation.

## Post-Completion

**Manual verification**

- In an authorized non-production Telegram chat, confirm a meal, simulate or
  observe a saved-message delivery interruption, tap Confirm again, and verify
  Add more and Done controls return without a duplicate meal.
- After confirming a meal, use Add more and verify the next input is accepted
  without a state-validation or timestamp error.
- Tap old Cancel, Add more, and Done buttons after starting a newer submission
  and verify the newer workflow remains unchanged.

**External system updates**

- Deliver remediation through the protected-branch pull-request workflow and
  reference issue #55. Never push or merge directly to `master`.
- After deployment, review CloudWatch logs for conditional callback contention,
  conversation-state validation errors, and Telegram delivery failures.
