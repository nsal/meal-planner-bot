# Planner Consistency and Delivery Reliability

> Planned for GitHub issue
> [#22](https://github.com/nsal/meal-planner-bot/issues/22).

## Overview

Address two planner reliability findings without changing persisted schemas or
public event formats. Grocery finalization must read the exact plan strongly
enough to observe the confirmation, retry, or edit that launched its one-shot
worker. Draft delivery failures must be handled after persistence without
misreporting successful generation as a generation failure.

The changes remain focused on the planner workflow, its tests, operator
documentation, and the generated SAM artifact. Deployment configuration may be
updated only if implementation proves a runtime setting is necessary; the
expected solution requires no new dependency, environment variable, or schema.

## Context (from discovery)

- Files/components involved: `src/meal_planner/planner_handler.py`,
  `tests/test_planner_handler.py`, `README.md`, and the generated SAM planner
  artifact verified by `tests/test_template.py`.
- `DynamoRepository.get_plan` already exposes a keyword-only
  `consistent_read` option; generation snapshots use it, while grocery
  finalization currently relies on the eventually consistent default.
- Confirmation, grocery retry, and confirmed-plan editing persist a `pending`
  plan before asynchronously invoking `finalize_grocery` for the exact week.
- `generate_plan` persists its conditional draft before sending the plan and
  follow-up instructions, but those delivery calls remain inside the broad
  generation failure boundary.
- The repository uses Python 3.14, `uv`, Ruff at 80 columns, strict mypy,
  pytest, and SAM artifact freshness/import checks.

## Development Approach

- **Testing approach**: TDD; add failing regression tests before each
  implementation change.
- Complete each task fully before moving to the next.
- Make small, focused changes and retain current repository, event, plan-state,
  revision, and notification contracts.
- Every code task must add or update tests for its success and failure paths.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation scope changes.
- Avoid new dependencies, schema changes, or deployment settings unless a
  failing test demonstrates they are required.

## Testing Strategy

- **Unit tests**: extend `tests/test_planner_handler.py` for the exact repository
  call shape, successful delivery, first-message delivery failure, and
  follow-up-message delivery failure.
- **Regression behavior**: retain controlled notification for failures before
  draft persistence, stale generated-draft rejection, revision-aware grocery
  completion/failure, and silent duplicate non-pending grocery events.
- **Deployment tests**: rebuild the SAM artifact after source changes and run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`.
- **Project gates**: run the full pytest suite, Ruff check and format check,
  strict mypy, and `git diff --check`.
- There is no UI test suite; Telegram delivery behavior is covered through unit
  tests around the API boundary.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered work with a ➕ prefix.
- Record blockers or failed assumptions with a ⚠️ prefix.
- Keep this plan synchronized with implementation and verification results.
- Move the plan to `docs/plans/completed/` only after every required gate passes.

## Solution Overview

Use the repository's existing `consistent_read=True` option for the initial
exact-week read in `finalize_grocery`. This ensures the worker observes the
write that triggered it before deciding whether the plan exists, is confirmed,
and is pending. Revision-conditional completion and failure writes remain the
authority for races occurring after that snapshot.

Move draft delivery outside the generation-and-persistence exception boundary,
or place it behind a small delivery helper with equivalent isolation. Once
`save_generated_draft` succeeds, Telegram failures should be logged as delivery
failures and must not invoke the generation-failure notification path or alter
the persisted draft. Preserve the existing controlled failure message for
profile, repository, prompt, LLM, parsing, and persistence failures.

## Technical Details

- Call `get_plan(user_id, week_start, consistent_read=True)` at the beginning of
  `finalize_grocery`.
- Keep the exact-week, confirmed-status, pending-status, and captured-revision
  checks unchanged after the stronger read.
- Preserve conditional `complete_grocery` and `fail_grocery` behavior so stale
  workers still cannot publish or overwrite newer revisions.
- Treat both `send_plan` and the follow-up `send_message` as post-persistence
  delivery operations.
- A delivery exception must be logged without calling `_notify_failure` with a
  generation error and without retrying or rewriting the draft.
- Do not add a new environment variable or SAM parameter unless the chosen
  delivery boundary requires runtime configuration; none is expected.
- Document that a generated draft remains persisted when its Telegram delivery
  fails and that requesting another plan is the recovery path.

## What Goes Where

- **Implementation Steps**: regression tests, planner handler changes, README
  clarification, SAM artifact rebuild, and local verification.
- **Post-Completion**: manual DynamoDB/AWS timing verification, deployment, PR
  publication, and issue follow-up.

## Implementation Steps

### Task 1: Make grocery finalization observe its triggering write

**Files:**

- Modify: `tests/test_planner_handler.py`
- Modify: `src/meal_planner/planner_handler.py`

- [x] add a failing regression test asserting `finalize_grocery` requests the
  exact plan with `consistent_read=True`
- [x] verify the regression covers a normal pending-plan success path and keeps
  the captured revision passed to `complete_grocery`
- [x] update `finalize_grocery` to opt into the existing strongly consistent
  exact-plan read without changing later lifecycle conditions
- [x] run `uv run pytest tests/test_planner_handler.py`; all tests must pass
  before Task 2

### Task 2: Isolate persisted-draft delivery from generation failure handling

**Files:**

- Modify: `tests/test_planner_handler.py`
- Modify: `src/meal_planner/planner_handler.py`

- [x] add a failing test where `send_plan` raises after
  `save_generated_draft` succeeds and prove no generation-failure notification
  or persistence rollback occurs
- [x] add a failing test where the plan is delivered but the follow-up
  instruction message raises, with the same no-misreporting guarantee
- [x] retain or update success coverage proving both delivery calls still occur
  in order after successful persistence
- [x] separate post-persistence Telegram delivery from the broad generation
  failure boundary and log delivery failures with plan/user context
- [x] verify pre-persistence repository, LLM, parsing, and conditional-write
  failures retain their current controlled behavior
- [x] run `uv run pytest tests/test_planner_handler.py`; all tests must pass
  before Task 3

### Task 3: Reconcile operations documentation and deployment artifacts

**Files:**

- Modify: `README.md`
- Verify: `template.yaml`
- Verify: `tests/test_template.py`
- Rebuild: `.aws-sam/build/`

- [x] document that draft persistence precedes Telegram delivery and that a
  delivery failure does not roll back or invalidate the draft
- [x] document the user recovery path without exposing internal error details
- [x] verify no new environment variable, SAM parameter, IAM permission, or
  dependency is required; update `template.yaml` and its tests only if this
  assumption is disproved during implementation
- [x] rebuild the artifact with
  `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`; all tests must
  pass before Task 4

### Task 4: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/planner_handler.py`
- Verify: `tests/test_planner_handler.py`
- Verify: `README.md`
- Verify: `template.yaml`
- Verify: `tests/`

- [x] verify a grocery worker cannot make lifecycle decisions from an
  eventually consistent exact-plan read
- [x] verify post-persistence delivery errors never produce a generation-failed
  message and never mutate the saved draft
- [x] verify genuine generation and persistence errors still use the controlled
  failure notification path
- [x] run `uv run pytest` and fix failures until the full suite passes
- [x] run `uv run ruff check .` and fix every finding
- [x] run `uv run ruff format --check .` and fix every formatting difference
- [x] run `uv run mypy` and fix every type error
- [x] run `git diff --check` and fix every whitespace error

### Task 5: Finalize plan tracking and documentation

**Files:**

- Modify: `README.md` only if verification reveals missing operator guidance
- Modify: `AGENTS.md` only if implementation discovers a reusable project rule
- Move: `docs/plans/2026-08-14-planner-consistency-delivery-reliability.md` to
  `docs/plans/completed/`

- [x] record implementation deviations and final verification results in this
  plan
- [x] confirm every implementation and verification checkbox is complete
- [x] rerun `uv run pytest` after final documentation changes
- [x] move this plan to `docs/plans/completed/` only after all checks pass

### Implementation Notes and Verification

- No implementation deviation was required: the existing
  `consistent_read=True` repository option and current deployment contract
  were sufficient, so no dependency, schema, environment variable, IAM, or
  SAM template change was needed.
- `finalize_grocery` now takes a strongly consistent exact-week snapshot and
  retains the captured revision for conditional completion or failure writes.
- Draft delivery now runs after the generation-and-persistence boundary;
  delivery failures are logged with user and week context while the saved
  draft remains unchanged.
- Verification passed: `uv run pytest` (168 tests),
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` (20 tests),
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
  `git diff --check`.

## Post-Completion

**Manual verification:**

- Confirm or retry a plan in an AWS/DynamoDB environment and verify the
  immediately invoked grocery worker observes the confirmed pending revision.
- Edit an active confirmed plan and verify the replacement grocery worker does
  not ignore the new pending revision because of a stale read.
- Simulate Telegram unavailability after draft persistence and verify logs
  describe a delivery failure while the saved draft remains usable.

**External system updates:**

- Implement on the existing dedicated branch; do not push or merge directly to
  `master`.
- Open a pull request after local verification.
- Comment on the associated GitHub issue with the Conventional Commit or PR link
  and concise verification results after implementation.
