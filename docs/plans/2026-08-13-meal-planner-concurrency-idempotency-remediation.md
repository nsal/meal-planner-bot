# Meal Planner Concurrency and Idempotency Remediation

## Overview

- Fix the three actionable findings from the release-readiness branch review.
- Prevent late asynchronous plan workers from overwriting newer draft content.
- Ensure callback outcome writes fail when plan activity changes concurrently.
- Deduplicate meal-log persistence when Telegram repeats the same update.
- Preserve current commands, callback payloads, and persisted plan records.

## Context (from discovery)

- **Associated GitHub issue:**
  [#21](https://github.com/nsal/meal-planner-bot/issues/21).
- **Associated branch:**
  `plan/2026-08-10-meal-planner-bot-release-readiness-remediation`.
- **Files/components involved:**
  `src/meal_planner/db/dynamo.py`,
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`,
  `tests/test_dynamo.py`,
  `tests/test_bot_handler.py`, and
  `tests/test_planner_handler.py`.
- **Related patterns:** plans already use optimistic `revision` checks;
  repository integration tests use Moto; handler tests use mocks; Telegram's
  `update_id` remains available in `RouteResult.raw_update`.
- **Dependencies:** DynamoDB conditional and transactional writes, Pydantic
  models, synchronous Lambda handlers, and asynchronous planner invocation.
- The repository has no UI-based end-to-end test framework. Handler and Moto
  integration tests provide the relevant end-to-end coverage.

## Development Approach

- **Testing approach:** TDD. Add a focused failing regression test before each
  production change, then make the smallest implementation pass.
- Complete each task fully before moving to the next.
- Make small, focused changes and preserve unrelated worktree changes.
- Every task that changes code must add or update tests for every changed path.
- Cover success, conflict, duplicate, and persistence-error behavior.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation discoveries change the scope.
- Maintain compatibility with existing plans, meal-history keys, commands, and
  Telegram callback payloads.
- Run all tools through `uv`; use Ruff at the configured 80-column width.

## Testing Strategy

- **Repository integration tests:** use Moto to reproduce stale draft workers,
  concurrent active-plan changes, transaction conflicts, legacy missing state,
  and stable meal-log keys.
- **Handler tests:** prove callbacks pass an activity snapshot and repeated raw
  updates pass the same stable source identifier to persistence.
- **Planner tests:** prove generation captures an exact-week revision before the
  LLM call and rejects results when the draft changes before persistence.
- **Regression tests:** retain confirmed-plan protection, grocery revisions,
  independent meal outcomes, missing `update_id` fallback, and existing command
  behavior.
- **Verification:** after each task, run its focused pytest files. At
  completion, run pytest, Ruff check/format, mypy, and `git diff --check`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document blockers with a `⚠️` prefix.
- Keep this plan synchronized with implementation and test behavior.
- Do not proceed while a task's focused tests are failing.

## Solution Overview

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
  between selection and mutation, so future confirmed plans retain the current
  date-based behavior.

## Technical Details

### Generated-draft snapshot

- Read `get_plan(user_id, target_week)` before calling the LLM.
- Treat no exact plan as an absent snapshot and a draft as a revision snapshot.
- Reject an exact confirmed plan before spending an LLM request.
- For an absent snapshot, save revision `0` only if the item remains absent.
- For a draft snapshot at revision `N`, save revision `N + 1` only when the
  stored item is still a draft at revision `N`.
- A concurrent edit, confirmation, or completed generation makes the worker's
  conditional write return `False`; the worker must not send the stale plan.

### Active-plan epoch

- Store plan activity metadata at `PK=USER#<user_id>, SK=PLAN_STATE` with an
  integer `active_epoch`.
- Change confirmation to a transaction that conditionally confirms the draft
  and atomically increments `active_epoch`.
- Read the epoch before a strongly consistent active-plan query and return both
  as one repository snapshot.
- Change outcome persistence to a transaction containing an epoch condition and
  the nested outcome update. A changed epoch returns `False` without mutation.
- Support existing users without a `PLAN_STATE` item by representing the epoch
  as absent and conditioning on the metadata item remaining absent.
- Keep ordinary read-only commands on `get_active_plan`; only callback mutation
  needs the stronger snapshot API.

### Meal-log idempotency

- Extract `update_id` from `RouteResult.raw_update` at the conversational
  boundary and normalize it to a string.
- Add an optional keyword-only source identifier to mutation and repository
  methods so direct/internal callers remain compatible.
- Use a stable `UPDATE#<update_id>` component in the meal sort key when present.
- Retain a timestamp-based `TIME#<created_at>` fallback when no update ID
  exists.
- Preserve the existing date prefix so meal-history range queries continue to
  include both old and new key formats.

## What Goes Where

- **Implementation Steps:** tests, Python code, repository transactions,
  documentation, and local verification in this repository.
- **Post-Completion:** manual concurrency checks and linking the implementation
  commit or pull request from the GitHub issue.

## Implementation Steps

### Task 1: Reject stale generated drafts with revision snapshots

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`

- [ ] add failing repository tests proving a worker cannot replace a draft after
  an edit, confirmation, or another generation advances the snapshot
- [ ] add failing planner tests proving the exact-week revision is captured
  before the LLM call and a rejected result is never sent as the active draft
- [ ] update `save_generated_draft` to distinguish absent and revisioned draft
  snapshots with conditional expressions
- [ ] update plan generation to reject confirmed exact weeks, assign revision
  `0` for new drafts, and increment revisions for intentional replacements
- [ ] update existing success and conflict tests for the new method contract
- [ ] run `uv run pytest tests/test_dynamo.py tests/test_planner_handler.py` and
  make it pass before Task 2

### Task 2: Make active callback outcome writes transactional

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [ ] add failing Moto tests that snapshot an older active plan, confirm a newer
  overlapping plan, and prove the older outcome write is rejected
- [ ] add failing tests for a successful unchanged snapshot, missing legacy
  `PLAN_STATE`, transaction cancellation, and non-conditional DynamoDB errors
- [ ] add failing handler tests proving callbacks use the snapshot API and pass
  the expected epoch into outcome persistence
- [ ] implement the plan-state epoch and transactional confirmation update
- [ ] implement strongly consistent active snapshots and transactional outcome
  writes for present and absent epoch states
- [ ] preserve callback acknowledgement and user-facing conflict responses
- [ ] run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and
  make it pass before Task 3

### Task 3: Deduplicate repeated Telegram meal-log updates

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [ ] add failing repository tests proving the same source update produces one
  meal item while different update IDs produce independent items
- [ ] add failing handler tests that process the same raw Telegram update twice
  and verify the stable update ID reaches persistence both times
- [ ] add a regression test proving callers without `update_id` retain unique
  timestamp-based logging
- [ ] thread the optional source update ID through conversational mutations
- [ ] update `log_meal` to select stable update or timestamp key components
  without changing the date prefix used by history queries
- [ ] run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and
  make it pass before Task 4

### Task 4: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/db/dynamo.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `src/meal_planner/planner_handler.py`
- Verify: `tests/test_dynamo.py`
- Verify: `tests/test_bot_handler.py`
- Verify: `tests/test_planner_handler.py`

- [ ] verify all three review findings have a red-then-green regression test
- [ ] verify stale workers and stale callbacks fail without partial mutations
- [ ] verify existing records without plan-state metadata remain usable
- [ ] verify distinct meal logs and current callback payloads remain compatible
- [ ] run `uv run pytest` and fix failures until the full suite passes
- [ ] run `uv run ruff check .` and fix every finding
- [ ] run `uv run ruff format --check .` and fix every formatting difference
- [ ] run `uv run mypy` and fix every type error
- [ ] run `git diff --check` and confirm no whitespace errors

### Task 5: Finalize documentation and plan tracking

**Files:**

- Modify: `README.md` if operational behavior needs clarification
- Modify: `AGENTS.md` only if a reusable project rule is discovered
- Modify:
  `docs/plans/2026-08-13-meal-planner-concurrency-idempotency-remediation.md`
- Move to:
  `docs/plans/completed/`
  `2026-08-13-meal-planner-concurrency-idempotency-remediation.md`

- [ ] document revision, activity-epoch, or idempotency behavior only where it
  affects operators or future maintainers
- [ ] update this plan with any implementation deviations and final test results
- [ ] rerun `uv run pytest` after documentation changes and fix all failures
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

**External system updates:**

- Implement on a dedicated branch and open a pull request; do not push or merge
  directly to `master`.
- Comment on the associated GitHub issue with the Conventional Commit or pull
  request link and a concise verification summary.
