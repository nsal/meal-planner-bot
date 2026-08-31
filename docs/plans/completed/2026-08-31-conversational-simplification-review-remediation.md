# Remediate Conversational Simplification Review Findings

Tracking issue: [#75](https://github.com/nsal/meal-planner-bot/issues/75)

## Overview

Remediate the independent review findings against the uncommitted
conversational-draft simplification. Restore safe rollout behavior for legacy
conversation records, make `/start` respond for complete profiles, rebuild the
bot-level regression coverage required by the original plan, and make the
completion records match repository state.

This work preserves the selected deletion-led architecture. It does not
restore the retired planner, rule engine, grocery workflow, batch coupling, or
removed commands. The retained product surface remains `/start`, `/help`,
`/profile`, `/plan`, and `/submit_meals`.

## Context (from discovery)

- `src/meal_planner/db/dynamo.py::get_conversation_state()` validates persisted
  records directly. Legacy `plan_request`, `plan_revision`, and old-shaped
  meal/profile states therefore raise instead of being treated as expired.
- `src/meal_planner/bot_handler.py::BotHandler._cmd_start()` sends its welcome
  message only in the incomplete-profile branch, leaving complete-profile
  users without a response.
- `tests/test_bot_handler.py` has six test functions and does not retain the
  plan-chat, profile-setup, scoped-control, and meal-workflow scenarios marked
  complete in the original simplification plan.
- `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`
  remains active while its final checklist and narrative say it was moved.
- The current gates pass with 414 tests and one optional SAM-artifact skip, so
  the remediation must add coverage that fails for the demonstrated defects
  before production changes are made.

## Development Approach

- **Testing approach:** TDD. Add each failing regression test before changing
  production code and record the expected failure in this plan.
- Complete each numbered task fully before beginning the next task.
- Keep production changes narrow and preserve the existing conversational
  draft architecture and public command surface.
- Every production change must have success, stale/concurrent, and error-path
  coverage where applicable.
- Run each task's focused tests and do not proceed until they pass.
- Use `uv` for execution, Ruff for formatting and linting at 80 columns, and
  strict Mypy for type verification.
- Update this plan immediately when scope or implementation details change.
- Preserve unrelated uncommitted work; do not commit, push, stash, reset,
  deploy, or perform cleanup while implementing this plan.

## Testing Strategy

- Repository tests will prove incompatible legacy conversation records are
  ignored and conditionally removed without deleting a concurrent replacement.
- Bot tests will exercise behavior through public command, conversational, and
  callback entry points rather than relying only on private helper calls.
- Profile setup coverage will include complete and incomplete profiles, every
  setup step, optional targets, malformed input, persistence failures,
  cancellation, stale state, restart, and final save behavior.
- Plan Chat coverage will include initial requests, follow-ups, busy sessions,
  duplicate updates, invocation recovery, and every scoped end-button outcome.
- Submitted-meal coverage will retain startup history, structured review,
  confirm, cancel, add-more, done, duplicate, stale, and persistence-failure
  behavior without restoring batch concepts.
- Final verification will run full Pytest, Ruff lint, Ruff format check, strict
  Mypy, and `git diff --check`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered in-scope tasks with a `➕` prefix.
- Document blockers or deviations with a `⚠️` prefix.
- Do not mark tests complete unless the named scenarios exist and pass.
- Keep both this remediation plan and the original simplification plan aligned
  with actual repository state.

## Solution Overview

At the persistence boundary, distinguish an incompatible legacy conversation
item from an operational DynamoDB failure. Treat the incompatible item as no
active workflow, and conditionally delete only the exact observed record so a
new concurrent workflow cannot be removed.

At the Bot boundary, send the complete-profile `/start` welcome response and
retain the existing guided setup branch. Rebuild behavioral tests around the
retained public workflows. These tests are part of the remediation, not a
reconstruction of deleted planner behavior.

After all implementation and verification gates pass, reconcile completion
tracking by moving both the original simplification plan and this remediation
plan to `docs/plans/completed/` in the final task.

## Technical Details

- Legacy conversation compatibility is read-only. Do not reintroduce removed
  workflow enum values, models, aliases, or handlers.
- Catch only Pydantic validation failures caused by persisted conversation
  shape. Do not convert DynamoDB transport or permission failures into an
  absent state.
- The invalid-item cleanup must use a conditional expression derived from the
  observed record's stable fields, such as workflow, step, revision,
  timestamps, and expiry. A conditional conflict means another writer won and
  must not be deleted.
- Invalid-item cleanup logs must contain only bounded categories and no user
  text or persisted payload content.
- Complete-profile `/start` must send exactly one welcome message and must not
  create, replace, or resume a setup draft.
- Incomplete-profile `/start` must retain its welcome plus the appropriate
  setup prompt and current restart/reconciliation behavior.
- Restored tests must assert observable state transitions, collaborator calls,
  messages, and callback acknowledgements. Avoid source-string tests when a
  behavioral test can protect the contract.

## What Goes Where

- **Implementation Steps:** repository code, focused regression tests, full
  verification, and plan completion records.
- **Post-Completion:** branch, commit, pull request, deployment, live Telegram
  verification, and issue comments requiring external systems.

## Implementation Steps

### Task 1: Expire incompatible legacy conversation records safely

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] add failing repository tests for representative retired `plan_request`,
  `plan_revision`, old meal, and old profile conversation items
- [x] assert incompatible items return `None` instead of propagating a
  Pydantic `ValidationError`
- [x] add a failing concurrency test proving cleanup cannot delete a newer
  replacement conversation item
- [x] add error tests proving non-conditional DynamoDB failures still
  propagate and cleanup logs contain no persisted content
- [x] implement narrow incompatible-item handling in
  `get_conversation_state()` without restoring retired schemas or enums
- [x] conditionally delete only the exact incompatible item observed by the
  read, treating a conditional conflict as a harmless concurrent replacement
- [x] run `uv run pytest tests/test_dynamo.py`; all tests must pass before
  Task 2

### Task 2: Restore `/start` and deterministic profile-setup coverage

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a failing complete-profile `/start` test that expects exactly one
  welcome response and no setup-state or profile-draft mutation
- [x] add failing end-to-end Bot tests for family name, household size,
  multiple member lines, constraints, preferences, final profile save, and
  cleanup
- [x] add profile-setup tests for optional protein/fibre targets, explicit
  `none`, duplicate names, invalid counts, malformed targets, and bounds
- [x] add profile-setup tests for scoped Close, duplicate updates, stale state,
  restart/reconciliation, profile-save conflicts, and persistence failures
- [x] move the common `/start` welcome delivery to the correct control-flow
  boundary while preserving the incomplete-profile prompt sequence
- [x] fix only additional profile-setup defects demonstrated by the restored
  tests; do not redesign profile persistence or reintroduce LLM onboarding
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_schemas.py \
  tests/test_dynamo.py tests/test_telegram_api.py`; all tests must pass before
  Task 3

### Task 3: Restore Plan Chat orchestration and scoped-control coverage

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify only if a restored test demonstrates a defect:
  `src/meal_planner/bot_handler.py`

- [x] add tests for `/plan` replacing each prior workflow with a fresh session
  and retaining the complete-profile requirement
- [x] add tests for initial request and ready-state follow-up transitions,
  refreshed UTC context dates, retained initial/latest context, and
  identifier-only invocation payloads
- [x] add tests for duplicate updates, concurrent transition loss, generating
  sessions, expired or malformed state reads, and invocation failure recovery
- [x] add callback tests for ending awaiting, generating, and ready sessions;
  already-ended, stale-session, conditional-conflict, persistence-error, and
  acknowledgement paths must be covered
- [x] prove a stale end button cannot delete or suppress a replacement session
  and every retained Plan Chat message carries the matching end control
- [x] fix only Plan Chat orchestration defects demonstrated by these tests;
  do not add plan persistence, semantic validation, or worker self-invocation
- [x] run `uv run pytest tests/test_bot_handler.py \
  tests/test_plan_chat_handler.py tests/test_router.py \
  tests/test_telegram_api.py`; all tests must pass before Task 4

### Task 4: Restore submitted-meal and scoped profile-control coverage

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify only if a restored test demonstrates a defect:
  `src/meal_planner/bot_handler.py`

- [x] retain active-route tests for `/submit_meals` startup history, structured
  input, review, and both inclusive date boundaries
- [x] add tests for meal confirm, duplicate confirm, Telegram delivery retry,
  cancel, add-more, done, stale callbacks, and callback acknowledgements
- [x] add tests for conditional persistence loss and DynamoDB or Telegram
  failures without restoring planned-batch reads or batch callback fields
- [x] add retained profile-edit tests proving Back, Done, and Close remain
  state-scoped and stale numbered-removal controls cannot mutate current data
- [x] assert removed `/cancel`, `/today`, `/checkin`, and `/grocery` commands
  and retired callback payloads remain unavailable
- [x] fix only retained-workflow defects demonstrated by these tests; preserve
  the simplified persistence and command boundaries
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py \
  tests/test_router.py tests/test_telegram_api.py \
  tests/test_telegram_commands.py`; all tests must pass before Task 5

### Task 5: Verify acceptance criteria and reconcile completion records

**Files:**

- Modify: `tests/test_readme.py`
- Modify, then move:
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`
  to `docs/plans/completed/`
- Modify, then move:
  `docs/plans/2026-08-31-conversational-simplification-review-remediation.md`
  to `docs/plans/completed/`

Task 5 scope amendment (2026-08-31): `tests/test_readme.py` is included to
replace its superseded active-path and fixed-hash assertion with the completed
plan-location contract required by this task. The test continues to enforce
that deployment, pull-request, and issue-comment work remains external under
`Post-Completion`.

- [x] review the attributable diff and confirm every P1-P3 review finding is
  covered by implementation and executable regression tests
- [x] run an `rg` audit confirming no restored test or source path imports the
  deleted planner, parser, dietary-rule, preference, or normalization modules
- [x] run `uv run pytest` and confirm the complete suite passes
- [x] run `uv run ruff check .` and resolve every finding
- [x] run `uv run ruff format --check .` and confirm 80-column formatting
- [x] run `uv run mypy` and confirm strict static typing passes
- [x] run `git diff --check` and confirm no whitespace errors
- [x] if SAM artifacts are present, run the built-handler import test; otherwise
  record the existing optional skip without representing it as executed
- [x] update both plan records with exact final test results and any resolved
  deviations; do not retain false claims about tests or file moves
- [x] move the original simplification plan and this remediation plan to
  `docs/plans/completed/` only after every implementation item and gate passes

⚠️ Task 5 retry verification (2026-08-31): The attributable diff review
confirmed that the six P1-P3 findings recorded in the prior Plan Chat review
remediation have implementation or executable regression coverage in the
current source and test changes. The deletion-import `rg` audit exited 1 with
zero matches, which is the expected no-findings result. `uv run pytest`
collected 482 tests and reported 480 passed, 1 failed, and 1 skipped (exit 1).
The sole failure was
`tests/test_readme.py::test_original_plan_remains_active_until_external_gates_complete`:
the final gate rerun observed active plan hash
`98994c86d33f05bb31e8bf24fed05f42032de3709843fdbb4c1eb4d622f2c257`, while
the test requires
`b2c03866c50eb295c77ffcde5fd6bb9165c55f769a344f5e33666ea34f2e7e6e`.
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
`git diff --check` exited 0. `.aws-sam/build` contained no built Python
handler artifacts, so the conditional built-handler import check was skipped
and not executed. The original plan remains at its active path and neither
plan was moved: moving it would violate the test's active-path and
completed-path assertions, while changing it would violate the fixed-byte
hash assertion. No symlink or other workaround was used. No separate
executor review report was present, so no such outcome is inferred.

Additional final gate rerun evidence: the same `uv run pytest` invocation also
observed the pre-existing concurrency test
`tests/test_dynamo.py::test_competing_ordinary_saves_allow_only_one_revision_owner`
fail with `[True, True]` instead of the expected `[False, True]`. That run
therefore reported 479 passed, 2 failed, and 1 skipped. This unrelated
concurrency failure was not changed because Task 5 authorizes documentation
and final verification only.

Task 5 unblock and completion (2026-08-31): The stale active-path and
fixed-hash test was replaced with a completed-plan contract after explicit
scope authorization. `uv run pytest tests/test_readme.py` passed all 16 tests,
and `uv run pytest -q` passed 481 tests with the one existing optional SAM
artifact test skipped. The previously observed DynamoDB concurrency failure
did not recur, including across five isolated runs before the final suite.
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, the
deleted-module import audit, and `git diff --check` all passed. No built Python
handler artifacts were present under `.aws-sam/build`, so the conditional
import check was not executed. Both plan records were moved to
`docs/plans/completed/`; the earlier retry records above are retained as
historical failure evidence and are superseded by this final result.

## Post-Completion

### Manual verification

- Deploy through a dedicated branch and pull request; never push or merge
  directly to `master`.
- Verify `/start` responds for both a complete profile and a partially saved
  setup in Telegram.
- Seed or retain one old conversation item in a non-production environment and
  verify the first retained command replaces it without a user-visible error.
- Exercise Plan Chat initial, follow-up, busy, stale-button, and end-session
  paths against the deployed Bot and worker.
- Exercise submitted-meal review, confirm, cancel, add-more, done, and duplicate
  delivery behavior in Telegram.

### External system updates

- Commit with a Conventional Commit message referencing the tracking issue,
  open a pull request, and link the commit or PR from the issue.
- After deployment, inspect bounded CloudWatch diagnostics and confirm no
  profile, meal, request, prompt, or generated text is logged.
- Comment on the associated GitHub issue with the completed verification
  results and commit or PR link.
