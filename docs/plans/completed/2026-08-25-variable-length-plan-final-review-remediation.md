# Variable-Length Plan Final Review Remediation

## Overview

Close the two P2 test-coverage gaps identified by the final independent review
of variable-length meal plans. The remediation must prove that the grocery
worker builds and completes grocery lists from the exact persisted one- or
three-day plan, and that forged check-in callbacks targeting the day after a
short plan are rejected at both the handler and repository boundaries.

The executor-attributable production behavior otherwise appears correct. This
plan is therefore test-led: implementation changes may be unnecessary when
the new tests demonstrate that the existing behavior already satisfies the
contract. Change production code only when a focused failing test exposes a
real defect.

## Context

- The original implementation plan is archived at
  `docs/plans/completed/2026-08-25-finalize-variable-length-meal-plan-work.md`.
- `tests/test_bot_handler.py::test_short_plan_confirmation_starts_grocery_generation`
  proves only that confirmation dispatches the `finalize_grocery` action.
- Existing `tests/test_planner_handler.py` grocery-worker tests use the default
  seven-day `make_plan()` fixture and do not inspect a short plan's generated
  prompt.
- `tests/test_dynamo.py::test_short_plan_lifecycle_bounds_edits_outcomes_and_groceries`
  checks an out-of-range meal edit, but calls `update_meal_outcome()` only for
  the valid final day.
- `tests/test_bot_handler.py::test_short_plan_checkin_and_outcome_use_actual_last_day`
  covers only a valid final-day callback. It does not exercise a forged
  callback for `len(plan.days) + 1`.
- The prior implementation reported 1,366 passing tests. The read-only review
  did not rerun that full suite; it verified current source/SAM artifact parity
  but could not reconstruct attribution for ignored generated artifacts.

## Review Findings Covered

1. **P2 — Exercise the short-plan grocery worker:** test the worker itself for
   persisted one- and three-day plans, inspect the exact prompt input, and
   verify successful grocery completion.
2. **P2 — Verify out-of-range short-plan callbacks are rejected:** test a
   forged `plan_days + 1` check-in through the handler and directly through the
   repository, with no outcome mutation or success response.

## Scope and Constraints

- **In scope:** focused regression tests for both findings; the smallest
  production correction demonstrated as necessary by those tests; relevant
  focused regressions; and final repository quality gates.
- **Out of scope:** new meal-plan behavior, new duration values, unrelated
  refactors, schema changes, deployment, GitHub issues or comments, commits,
  pushes, pull requests, and edits to archived plans.
- Keep `WeeklyPlan.days` as the source of truth for the persisted horizon.
- Preserve confirmation, grocery compare-and-swap, active-epoch, stale-worker,
  stale-callback, and legacy seven-day behavior.
- Do not use live Telegram, AWS, DynamoDB, or LLM services in tests.
- Preserve all pre-existing dirty-worktree changes. Modify only the files
  listed for the active task and this remediation plan for progress evidence.
- Run Python tools with `uv run`; use the configured Ruff formatting and lint
  rules and strict Mypy checks.

## Development Approach

- **Testing approach: TDD.** Add each missing regression first and run it
  before changing production code.
- Both findings are P2 and have no production dependency on each other. Address
  them in review order, completing each focused suite before proceeding.
- A newly added test is permitted to pass immediately. That result demonstrates
  that the defect was missing regression coverage rather than faulty production
  behavior; record it and do not make a speculative implementation change.
- If a new test fails, record the failure scenario, make the narrowest change
  at the named boundary, and rerun the exact test before broader regressions.
- Do not weaken assertions to accommodate existing behavior. Tests must prove
  exact short-plan bounds and observable side effects.
- Mark checkboxes only after the stated evidence exists and passes without
  skips or xfails.

## Testing Strategy

- **Grocery worker:** parameterize one- and three-day confirmed plans with
  `GroceryStatus.PENDING`; capture the prompt passed to the grocery LLM; use
  unique meal markers to prove every persisted day is included and no
  non-persisted day is included; then verify parsed sections are completed at
  the plan's current revision and the ready notification is sent.
- **Handler callback:** parameterize one- and three-day active confirmed plans;
  send a syntactically valid callback for `plan_days + 1`; assert no repository
  outcome update, no success message, and one controlled non-success callback
  acknowledgement.
- **Repository callback:** persist and confirm one- and three-day plans, call
  `update_meal_outcome()` with `plan_days + 1`, assert `False`, and reload the
  plan to prove every meal outcome remains unchanged.
- **Regressions:** retain valid final-day callbacks, all supported outcomes,
  active-epoch conflict behavior, grocery failures, stale grocery events,
  notification failures, and seven-day defaults.
- **Final gates:** rerun the full suite rather than relying only on the prior
  review report, then verify formatting, lint, typing, artifact-required tests,
  and whitespace.

## Solution Overview

Extend the existing grocery success coverage in
`tests/test_planner_handler.py` so the test invokes the actual
`PlannerHandler.finalize_grocery()` worker for one- and three-day persisted
plans. Inspect `grocery_llm_client.chat_json_sync()` to prove
`build_grocery_prompt()` receives only the meals represented by the persisted
days, and verify the worker completes and announces the grocery list.

Add a forged next-day check-in regression beside the short-plan callback tests
in `tests/test_bot_handler.py`, plus a direct repository regression beside the
short-plan lifecycle tests in `tests/test_dynamo.py`. The handler should reject
an absent day or meal before requesting a write and acknowledge the callback
without emitting the `Marked ...` success message. The repository remains the
defense-in-depth boundary and must return `False` without mutating the plan.

## Technical Details

- Use `make_plan(plan_days=plan_days, status=PlanStatus.CONFIRMED,
  grocery_status=GroceryStatus.PENDING)` for grocery-worker cases.
- Give each persisted meal a unique marker such as
  `short-plan-day-{day}-lunch`; inspect the first argument passed to
  `chat_json_sync()` and assert exactly markers `1..plan_days` are present.
  Also assert a sentinel marker for `plan_days + 1` is absent.
- Return a deterministic grocery JSON payload and assert
  `complete_grocery(user_id, week_start, plan.revision, sections)` receives the
  parsed sections before the ready notification is sent.
- Build callback data as
  `checkin:{week_start}:{plan_days + 1}:lunch:cooked` against an
  `ActivePlanSnapshot` for the same active plan.
- Handler validation should resolve `callback.day` and
  `callback.meal_type.value` against `snapshot.plan.days` before calling
  `update_meal_outcome()`. If either target is absent, use a controlled
  invalid/outdated acknowledgement and message, and never enter the success
  `else` branch.
- `DynamoRepository.update_meal_outcome()` must continue returning `False` for
  a missing day or meal and must preserve every stored outcome. Keep active
  epoch and transactional condition behavior unchanged.

## What Goes Where

- **Task 1:** grocery worker prompt, completion, and notification coverage;
  prompt or worker production changes only if a focused failure requires them.
- **Task 2:** forged callback rejection at handler and repository boundaries,
  followed by all final quality gates.

## Implementation Steps

### Task 1: Exercise grocery generation for persisted short plans

**Severity:** P2

**Files:**

- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_prompts.py` only if a prompt-helper defect requires a
  direct regression
- Modify: `src/meal_planner/planner_handler.py` only if the worker mishandles a
  persisted short plan
- Modify: `src/meal_planner/llm/prompts.py` only if the prompt contains days
  outside the persisted plan
- Modify: this remediation plan with verification evidence

**Symbols:** `PlannerHandler.finalize_grocery()`, `build_grocery_prompt()`,
`test_finalize_grocery_success`, and a new parameterized short-plan worker
test.

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_finalize_grocery_uses_exact_persisted_short_plan_days` in
  `tests/test_planner_handler.py`, parameterized with `plan_days` values 1 and
  3. Use a confirmed, pending plan and unique meal markers for every persisted
  day.
- [x] Capture the grocery client's `chat_json_sync()` call and assert its prompt
  contains each unique persisted marker exactly once, contains no marker for
  `plan_days + 1`, and retains the profile's people count.
- [x] Return one deterministic grocery section and assert
  `repo.complete_grocery()` receives the same user, week start, current plan
  revision, and parsed section. Assert `repo.fail_grocery()` is not called and
  the ready message is sent once.
- [x] **Expected failure demonstrating the gap:** Run
  `uv run pytest tests/test_planner_handler.py -k
  "finalize_grocery and short_plan"`. Record whether the new test exposes a
  seven-day prompt/completion assumption. If it passes immediately, record
  that production behavior was already correct and the remediation is the new
  regression coverage; do not alter production code.
- [x] **Implementation change only if required:** If the focused test fails,
  make `build_grocery_prompt()` and/or `PlannerHandler.finalize_grocery()`
  consume only `plan.days`. Do not derive seven days from `week_start`, pad a
  short plan, or change grocery status and compare-and-swap semantics. The
  focused test passed after the test fixture was corrected, so no production
  change was required.
- [x] If production prompt code changes, add a direct one-/three-/seven-day
  `build_grocery_prompt()` regression in `tests/test_prompts.py` before making
  the change, and record its pre-fix failure. Not applicable because production
  prompt code was unchanged.
- [x] **Verify the new test passes:** Rerun `uv run pytest
  tests/test_planner_handler.py -k "finalize_grocery and short_plan"` and
  require both parameter cases to pass without skip or xfail.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_prompts.py
  tests/test_planner_handler.py` and resolve every newly introduced failure
  before Task 2.

**Acceptance criteria:**

- [x] The real grocery worker is exercised for persisted one- and three-day
  plans, not merely dispatched or bypassed with `complete_grocery()`.
- [x] The LLM prompt contains every and only persisted plan-day meal marker.
- [x] Grocery sections complete successfully against the exact plan revision,
  and the ready notification is sent.
- [x] Existing seven-day, dedicated-client, stale-event, error, compare-and-
  swap, and notification-failure behavior remains green.
- [x] Production files remain unchanged if the focused tests demonstrate the
  existing implementation is already duration-safe.

Task 1 evidence: The new parameterized worker test passed for both one- and
three-day plans with `uv run pytest tests/test_planner_handler.py -k
"finalize_grocery and short_plan"` (`2 passed`). The first run exposed only a
test-fixture marker assignment error in the new three-day case; correcting that
fixture produced the passing result without any production change. The prompt
contained each persisted marker once, excluded the next-day sentinel, and
retained the two-person profile count. The worker completed the parsed grocery
section at revision 4 before sending the ready notification. The focused
regressions passed with `uv run pytest tests/test_prompts.py
tests/test_planner_handler.py` (`214 passed`), and the full suite passed with
`uv run pytest` (`1368 passed, 2 warnings`). `uv run ruff format --check
tests/test_planner_handler.py`, `uv run ruff check tests/test_planner_handler.py`,
and `uv run mypy` also passed.

### Task 2: Reject forged callbacks beyond a short plan's final day

**Severity:** P2

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `src/meal_planner/bot_handler.py` only if handler-side validation is
  absent or incorrect
- Modify: `src/meal_planner/db/dynamo.py` only if repository-side rejection or
  non-mutation is incorrect
- Modify: this remediation plan with final verification evidence

**Symbols:** `BotHandler.handle_callback()`,
`DynamoRepository.update_meal_outcome()`,
`test_short_plan_checkin_and_outcome_use_actual_last_day`, and
`test_short_plan_lifecycle_bounds_edits_outcomes_and_groceries`.

**TDD sequence:**

- [x] **Failing or missing handler test first:** Add
  `test_short_plan_callback_rejects_day_after_persisted_end` in
  `tests/test_bot_handler.py`, parameterized for one- and three-day confirmed
  active plans. Send a validly encoded `cooked` lunch callback for
  `plan_days + 1`.
- [x] Assert the handler does not call `repo.update_meal_outcome()`, does not
  send the `Marked lunch as cooked.` success message, sends at most one
  controlled invalid/outdated user message, and answers the callback exactly
  once with a non-success acknowledgement.
- [x] **Failing or missing repository test first:** Extend the focused
  short-plan lifecycle coverage or add
  `test_short_plan_outcome_rejects_day_after_persisted_end` in
  `tests/test_dynamo.py`, parameterized for one and three days. Call
  `update_meal_outcome()` with `plan_days + 1` and the current active epoch.
- [x] Assert the repository returns `False`; reload the plan and prove all meal
  outcomes remain `MealOutcome.UNREPORTED`, with revision, status, grocery
  state, and active epoch unchanged.
- [x] **Expected failure demonstrating the gap:** Run `uv run pytest
  tests/test_bot_handler.py tests/test_dynamo.py -k
  "short_plan and (callback or outcome)"`. Record whether the handler delegates
  a forged day to the repository, emits a success response, or the repository
  mutates/raises instead of returning `False`. If both tests pass immediately,
  record that no production change is required.
- [x] **Implementation change only if required:** If the handler test fails,
  validate that the callback day and meal exist in the active snapshot before
  calling `update_meal_outcome()`. Set a controlled non-success
  acknowledgement/message and return without reaching the success-delivery
  branch.
- [x] If the repository test fails, make the smallest typed change in
  `DynamoRepository.update_meal_outcome()` so a missing day or meal returns
  `False` before any transaction. Preserve active-epoch checks and existing
  conditional-write error handling.
- [x] **Verify the new tests pass:** Rerun `uv run pytest
  tests/test_bot_handler.py tests/test_dynamo.py -k
  "short_plan and (callback or outcome)"`; require all one- and three-day
  cases to pass without skip or xfail.
- [x] **Relevant regression tests:** Run `uv run pytest
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_planner_handler.py
  tests/test_prompts.py` and resolve every newly introduced failure.
- [x] Run `uv run ruff format .`, followed by
  `uv run ruff format --check .`; confirm formatting is stable.
- [x] Run `uv run ruff check .` and resolve every new lint finding.
- [x] Run `uv run mypy` and resolve every new type error.
- [x] Run `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`. If a
  production source changed and the required artifact check is stale, rebuild
  with `uvx --from aws-sam-cli sam build --beta-features`, rerun the artifact
  test, and attribute only that task's generated changes.
- [x] Run `uv run pytest` to close the read-only review limitation; require the
  entire suite to pass and record the exact count and warnings.
- [x] Run `git diff --check`; verify the archived plans and unrelated
  pre-existing work remain unchanged, and record final evidence in this plan.

**Acceptance criteria:**

- [x] A forged check-in callback for `len(plan.days) + 1` is rejected for both
  one- and three-day plans before any handler-requested outcome write.
- [x] Rejection produces no success message and exactly one controlled
  non-success callback acknowledgement.
- [x] Direct repository calls for the out-of-range day return `False` and do
  not mutate any persisted plan field.
- [x] Valid final-day callbacks and every supported outcome continue to pass.
- [x] Active-plan, active-epoch, stale-plan, transaction-conflict, and
  seven-day regression behavior remains intact.
- [x] Production files remain unchanged wherever the tests prove existing
  behavior already meets the contract.
- [x] Ruff formatting and lint, strict Mypy, required SAM artifact checks, the
  full Pytest suite, and `git diff --check` pass.
- [x] Both P2 review findings have direct regression coverage and no unrelated
  remediation work was introduced.

Task 2 evidence: The new handler regression initially failed for both plan
horizons because `handle_callback()` delegated forged day 2/4 callbacks to
`update_meal_outcome()` and reached the success path. The narrow handler fix
now resolves the callback day and meal against `snapshot.plan.days`, sends
one `That check-in button is invalid or outdated.` message, acknowledges once
with `Invalid check-in`, and performs no repository write. The direct
repository regression passed after correcting its fixture expectation that
confirmation sets grocery state to `PENDING`; the repository returned `False`
and preserved the complete confirmed plan, all `UNREPORTED` outcomes,
revision, status, grocery state, and active epoch. No repository production
change was required.

The focused boundary command
`uv run pytest tests/test_bot_handler.py tests/test_dynamo.py -k "short_plan
and (callback or outcome)"` passed with `8 passed`. The relevant regression
command `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py
tests/test_planner_handler.py tests/test_prompts.py` passed with `655 passed,
2 warnings`. `uv run ruff format .` reformatted the changed handler; the
subsequent `uv run ruff format --check .` reported 98 files already formatted.
`uv run ruff check .` and `uv run mypy` passed.

The first required artifact run failed because the ignored SAM artifact was
stale for the changed `src/meal_planner/bot_handler.py`. After
`uvx --from aws-sam-cli sam build --beta-features`,
`REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` passed with
`26 passed`. The full suite `uv run pytest` passed with `1372 passed, 2
warnings`. `git diff --check` passed. Archived plans and unrelated
pre-existing work were preserved; the only executor-attributable source/test
changes are the handler validation and the two direct regressions, plus this
Task 2 evidence. The rebuilt `.aws-sam/build` output is ignored.

## Risks and Limitations

- The prior full-suite pass was reported by the implementation workflow but
  was not rerun during the independent read-only review. Task 2 explicitly
  reruns it before this remediation can be complete.
- `.aws-sam/build` is ignored and had no pre-build snapshot during the review.
  Current parity was verified, but generated-file attribution cannot be
  reconstructed retrospectively. Rebuild artifacts only if a production
  source change makes the required parity test fail, and record that fact.
- Mocked worker tests verify prompt composition, persistence calls, and
  notifications without a live LLM or DynamoDB service. Existing parser and
  repository regressions remain the deterministic integration boundary.

## Post-Completion

No manual or external action is required for these two test-coverage findings.
Any commit, pull request, issue update, deployment, or live Telegram check
requires separate authorization.
