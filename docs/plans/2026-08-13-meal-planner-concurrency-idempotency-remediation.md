# Meal Planner Baseline, Concurrency, and Idempotency Remediation

## Overview

- Restore a coherent release-readiness baseline before implementing the three
  actionable findings from the branch review.
- Reconcile handlers, models, repository APIs, callback payloads, deployment
  configuration, and tests that drifted in commit `0379395`.
- Prevent late asynchronous plan workers from overwriting newer draft content.
- Ensure callback outcome writes fail when plan activity changes concurrently.
- Deduplicate meal-log persistence when Telegram repeats the same update.
- Preserve the intended release-ready commands, five-part callback payloads,
  persisted plan records, deployment target, and supported legacy data.

## Context (from discovery)

- **Associated GitHub issue:**
  [#21](https://github.com/nsal/meal-planner-bot/issues/21). Reuse this issue;
  do not create a duplicate for the updated plan.
- **Associated branch:**
  `plan/2026-08-10-meal-planner-bot-release-readiness-remediation`.
- **Current baseline is not green:** `uv run pytest` reports 20 failures and
  107 passes; `uv run mypy` reports 17 errors; Ruff check passes, while Ruff
  format reports two unformatted files.
- **Handler/repository drift:** `bot_handler.py` and `planner_handler.py` call
  removed `get_current_plan` and `update_meal_status` methods, use stale model
  fields, and bypass the current draft/grocery lifecycle.
- **Callback drift:** `router.py` and `TelegramAPI` use a plan-specific
  five-part callback, while `bot_handler.py` parses a legacy four-part callback.
- **Deployment drift:** `pyproject.toml` and deployment tests require Python
  3.14, ARM64, and the `python-uv` SAM build workflow, while `template.yaml`
  currently declares Python 3.12 and an invalid singular `Architecture` key.
- **Reference point:** commit `b8f0327` contains the last internally coherent
  release-readiness contracts. Use it as a selective reference, not as a
  wholesale reset; retain intentional later changes only when they satisfy the
  current schemas, APIs, configuration tests, and acceptance criteria.
- **Primary files/components:**
  `src/meal_planner/db/dynamo.py`,
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`,
  `src/meal_planner/config.py`,
  `src/meal_planner/llm/client.py`,
  `src/meal_planner/router.py`,
  `src/meal_planner/telegram/api.py`,
  `template.yaml`, and their tests.
- **Related patterns:** plans use optimistic `revision` checks; repository
  integration tests use Moto; handler tests use mocks; Telegram's `update_id`
  is available in `RouteResult.raw_update`.
- **Dependencies:** DynamoDB conditional and transactional writes, Pydantic
  models, synchronous Lambda handlers, and asynchronous planner invocation.
- The repository has no UI-based end-to-end test framework. Handler, Lambda
  boundary, deployment-contract, and Moto tests provide the relevant coverage.

## Development Approach

- **Testing approach:** TDD. First restore the existing release-readiness tests
  and make the complete baseline green. For each subsequent finding, add a
  focused failing regression test before the production change.
- Complete each task fully and make the full test suite pass before moving to
  the next task.
- Make small, focused changes and preserve unrelated worktree changes.
- Do not use a blanket revert of `0379395`; reconcile each affected contract so
  intentional later model-selection changes are not discarded accidentally.
- Every task that changes code must add or update tests for every changed path,
  including success, conflict, validation, and persistence-error behavior.
- Update this plan immediately if implementation discoveries change the scope.
- Maintain compatibility with existing plan and meal-history records.
- Preserve the release-ready five-part callback format:
  `checkin:<week_start>:<day>:<meal_type>:<outcome>`.
- Run all tools through `uv`; use Ruff at the configured 80-column width.

## Testing Strategy

- **Baseline regression tests:** restore schema-valid fixtures and tests for
  command routing, onboarding, plan lifecycle, callback acknowledgement,
  Lambda boundaries, configuration, LLM behavior, and SAM deployment.
- **Repository integration tests:** use Moto to reproduce stale draft workers,
  concurrent active-plan changes, transaction conflicts, legacy missing state,
  and stable meal-log keys.
- **Handler tests:** prove callbacks pass an activity snapshot and repeated raw
  updates pass the same stable source identifier to persistence.
- **Planner tests:** prove generation captures an exact-week revision before the
  LLM call and rejects results when the draft changes before persistence.
- **Regression tests:** retain confirmed-plan protection, grocery revisions,
  independent meal outcomes, missing `update_id` fallback, webhook security,
  timeout/retry behavior, and existing command behavior.
- **Deployment tests:** verify Python 3.14 ARM64, root `python-uv` builds,
  Secrets Manager references, and importable Lambda artifacts.
- **Verification:** after every task, run the full pytest suite. At completion,
  run pytest, Ruff check/format, mypy, and `git diff --check`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document blockers with a `⚠️` prefix.
- Keep this plan synchronized with implementation and test behavior.
- Do not proceed while the full test suite is failing.

## Solution Overview

- First realign the branch with the current repository, schema, router,
  Telegram API, and deployment contracts. Use `b8f0327` as a behavioral
  reference and reapply only compatible intentional changes from `0379395`.
- Capture the exact week's draft revision before plan generation. Persist the
  result only when that snapshot is still current; increment the content
  revision when intentionally replacing an existing draft.
- Add a per-user plan-state epoch. Confirmation updates the plan and increments
  the epoch in one DynamoDB transaction. Callback handling reads a strongly
  consistent active-plan snapshot, and outcome persistence transactionally
  checks that epoch before writing.
- Pass Telegram's stable `update_id` through conversational mutation handling
  into `log_meal`. Use it in the meal sort key so a repeated update overwrites
  its prior result while distinct Telegram updates remain distinct meals.
- Keep query-derived active-plan selection. The epoch protects only the gap
  between selection and mutation, so future confirmed plans retain current
  date-based behavior.

## Technical Details

### Baseline contract

- Repository selection methods remain `get_latest_plan`, `get_plan`, and
  date-aware `get_active_plan`; do not reintroduce `get_current_plan`.
- Plan generation persists a draft through `save_generated_draft`; grocery
  generation starts only after a conditional confirmation and remains a
  revision-aware asynchronous workflow.
- Commands operate on the active confirmed plan where appropriate. Draft
  confirmation and editing retain their explicit eligibility rules.
- Callbacks are parsed with `parse_checkin_callback`, are tied to an exact plan
  week, update `MealOutcome`, and acknowledge Telegram on every path.
- Runtime configuration retains validated settings, webhook-secret checking,
  bounded LLM/Telegram calls, and the Python 3.14 ARM64 build contract.

### Generated-draft snapshot

- Read `get_plan(user_id, target_week)` before calling the LLM.
- Treat no exact plan as an absent snapshot and a draft as a revision snapshot.
- Reject an exact confirmed plan before spending an LLM request.
- Change `save_generated_draft` to accept the expected prior revision as an
  explicit keyword-only value: `None` for absence or an integer for a draft.
- For an absent snapshot, save revision `0` only if the item remains absent.
- For a draft snapshot at revision `N`, save revision `N + 1` only when the
  stored item is still a draft at revision `N`.
- A concurrent edit, confirmation, or completed generation makes the worker's
  conditional write return `False`; the worker must not send the stale plan.

### Active-plan epoch

- Add a typed immutable repository snapshot containing `plan` and
  `active_epoch: int | None`.
- Store plan activity metadata at `PK=USER#<user_id>, SK=PLAN_STATE` with an
  integer `active_epoch`.
- Change confirmation to a DynamoDB transaction that conditionally confirms the
  draft and atomically increments `active_epoch`, creating the state item for a
  legacy user when necessary.
- Read the epoch with a strongly consistent item read, then query active plans
  with `ConsistentRead=True`, and return both values as one snapshot.
- Change outcome persistence to accept the expected epoch and use a transaction
  containing an epoch condition plus the nested outcome update.
- If the epoch changes, return `False` without mutating the plan. Re-raise
  cancellation and service errors that are not expected condition conflicts.
- Support users without `PLAN_STATE` by representing the epoch as `None` and
  conditioning on the metadata item remaining absent.
- Keep read-only commands on `get_active_plan`; only callback mutation uses the
  stronger snapshot API.

### Meal-log idempotency

- Extract `update_id` from `RouteResult.raw_update` at the conversational
  boundary. Accept Telegram's integer identifier, excluding booleans, and
  normalize it to a string; invalid or missing identifiers use the fallback.
- Add an optional keyword-only `source_update_id` to conversational mutation
  and repository methods so direct and internal callers remain compatible.
- Use `MEAL#<date>#UPDATE#<update_id>#<meal_type>` when a source ID is present.
- Use `MEAL#<date>#TIME#<created_at>#<meal_type>` when no source ID exists.
- Preserve the `MEAL#<date>` prefix so range queries include legacy and new key
  formats. Continue sorting parsed results by `created_at`.

## What Goes Where

- **Implementation Steps:** baseline repair, regression tests, Python code,
  repository transactions, deployment configuration, documentation, and local
  verification in this repository.
- **Post-Completion:** manual concurrency checks, pull-request publication, and
  linking the implementation from the existing GitHub issue.

## Implementation Steps

### Task 1: Restore the release-readiness baseline

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/config.py` if configuration compatibility requires
  it
- Modify: `src/meal_planner/llm/client.py` if timeout/retry compatibility
  requires it
- Modify: `template.yaml`
- Modify: `README.md`
- Modify: `tests/conftest.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_template.py`

- [ ] restore schema-valid handler and planner fixtures instead of weakening
  current Pydantic invariants
- [ ] realign commands and conversational mutations with `get_latest_plan`,
  `get_active_plan`, `get_plan`, conditional lifecycle writes, and typed models
- [ ] restore exact-week asynchronous plan and grocery event handling, including
  revision-aware completion/failure paths
- [ ] restore five-part callback parsing, exact-week validation, outcome writes,
  callback acknowledgement, and controlled conflict messages
- [ ] restore validated webhook, timeout, retry, and Lambda boundary behavior;
  reconcile split model settings only if they preserve these guarantees
- [ ] restore the Python 3.14 ARM64 root `python-uv` SAM build and Secrets
  Manager contract without exposing secrets as plain template parameters
- [ ] update success tests for all restored command, mutation, planner, and
  deployment paths
- [ ] update error tests for invalid schemas, inactive plans, persistence
  failures, delivery failures, invalid events, and deployment mismatches
- [ ] run `uv run pytest` and fix all baseline failures before Task 2
- [ ] run `uv run ruff check .`, `uv run ruff format --check .`, and
  `uv run mypy`; fix every finding before Task 2

### Task 2: Reject stale generated drafts with revision snapshots

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`

- [ ] add failing repository success tests for absent and matching-revision
  draft snapshots
- [ ] add failing repository conflict tests for edit, confirmation, another
  completed generation, and non-conditional DynamoDB errors
- [ ] add failing planner tests proving the exact-week snapshot is captured
  before the LLM call and a confirmed exact week avoids the LLM call
- [ ] add failing planner tests proving a rejected result is neither sent nor
  reported as the active draft
- [ ] update `save_generated_draft` with explicit absent/revision conditional
  expressions and the new expected-snapshot contract
- [ ] update generation to assign revision `0` for a new draft and `N + 1` for
  an intentional replacement while retaining canonical generated state
- [ ] update existing success and conflict tests for the new method contract
- [ ] run `uv run pytest`; fix all failures before Task 3

### Task 3: Make active callback outcome writes transactional

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [ ] add failing Moto tests that snapshot an older active plan, confirm a newer
  overlapping plan, and prove the older outcome write is rejected
- [ ] add failing repository success tests for unchanged present and legacy
  absent epoch snapshots
- [ ] add failing repository error tests for transaction cancellation reasons,
  conditional conflicts, and non-conditional DynamoDB errors
- [ ] add failing handler tests proving callbacks use the snapshot API and pass
  the expected epoch into outcome persistence
- [ ] add handler tests for success, inactive callback, epoch conflict,
  persistence error, user notification, and callback acknowledgement paths
- [ ] implement the typed active-plan snapshot and strongly consistent reads
- [ ] implement transactional confirmation with atomic epoch increment
- [ ] implement transactional outcome writes for present and absent epoch states
- [ ] run `uv run pytest`; fix all failures before Task 4

### Task 4: Deduplicate repeated Telegram meal-log updates

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [ ] add failing repository tests proving the same source update produces one
  meal item while different update IDs produce independent items
- [ ] add failing repository tests proving callers without a source ID retain
  unique timestamp-based logging and legacy records remain queryable
- [ ] add failing handler tests that process the same raw Telegram update twice
  and pass the same normalized update ID to persistence both times
- [ ] add handler tests proving missing, boolean, and malformed `update_id`
  values use the timestamp fallback
- [ ] thread optional `source_update_id` through conversational mutations
- [ ] update `log_meal` to select stable update or timestamp key components
  without changing the date prefix used by history queries
- [ ] run `uv run pytest`; fix all failures before Task 5

### Task 5: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/db/dynamo.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `src/meal_planner/planner_handler.py`
- Verify: `src/meal_planner/config.py`
- Verify: `src/meal_planner/llm/client.py`
- Verify: `template.yaml`
- Verify: `tests/`

- [ ] verify the release-readiness baseline contracts and all three review
  findings have regression coverage
- [ ] verify stale workers and stale callbacks fail without partial mutations
- [ ] verify records without plan-state metadata and legacy meal keys remain
  usable
- [ ] verify distinct meal logs and five-part callback payloads remain
  compatible
- [ ] run `uv run pytest` and fix failures until the full suite passes
- [ ] run `uv run ruff check .` and fix every finding
- [ ] run `uv run ruff format --check .` and fix every formatting difference
- [ ] run `uv run mypy` and fix every type error
- [ ] run `git diff --check` and confirm no whitespace errors

### Task 6: Finalize documentation and plan tracking

**Files:**

- Modify: `README.md` if operational behavior needs clarification
- Modify: `AGENTS.md` only if a reusable project rule is discovered
- Modify:
  `docs/plans/2026-08-13-meal-planner-concurrency-idempotency-remediation.md`
- Move to:
  `docs/plans/completed/`
  `2026-08-13-meal-planner-concurrency-idempotency-remediation.md`

- [ ] document revision, activity-epoch, idempotency, deployment, or operational
  behavior only where it affects operators or future maintainers
- [ ] update this plan with implementation deviations and final test results
- [ ] rerun `uv run pytest` after documentation changes and fix all failures
- [ ] rerun Ruff check/format, mypy, and `git diff --check`
- [ ] confirm all implementation checkboxes are complete
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

**Manual verification:**

- Start plan generation, edit the existing draft before the worker finishes,
  and verify the late generated result is discarded.
- Confirm overlapping plans while submitting an older callback and verify no
  outcome is written to the superseded plan.
- Deliver the same meal-log update twice and verify history contains one entry;
  then submit a distinct update for the same meal type and verify both remain.
- Build both Lambda artifacts in the documented Linux ARM64 environment and
  verify their handlers import before deployment.

**External system updates:**

- Implement on a dedicated branch and open a pull request; do not push or merge
  directly to `master`.
- Comment on GitHub issue #21 with the Conventional Commit or pull-request link
  and a concise verification summary.
