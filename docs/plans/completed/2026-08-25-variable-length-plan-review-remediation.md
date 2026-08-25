# Remediate Variable-Length Plan Review Findings

## Overview

Remediate the three actionable findings from the independent review of the
variable-length meal-plan hardening work. The implementation must carry the
accepted duration through the complete planner lifecycle, keep best-effort
rules advisory when horizon capacity is insufficient, and revalidate a retry
against the dates it will actually generate.

The changes must preserve the migration-safe `duration_collected` phase,
legacy seven-day defaults, dietary-constraint priority, stale-request
protection, bounded repair, and the existing best-effort reporting path.

## Context

- **Primary feature plan:**
  `docs/plans/2026-08-25-support-variable-length-meal-plans.md`.
- **Completed hardening plan:** the 2026-08-25 clarification and weekday
  feasibility plan under `docs/plans/completed/`.
- **Review baseline:** Ruff, Mypy, and the full Pytest suite passed with 1,254
  tests and two existing Pydantic serializer warnings.
- **Request boundary:** `src/meal_planner/bot_handler.py` retains `plan_days`
  in `ConversationState`, but `_invoke_planner()` does not serialize it for
  initial dispatch or manual retry.
- **Planner boundary:** `src/meal_planner/planner_handler.py` builds and parses
  `PlanGenerationContext`, generates prompts, schedules one repair, validates
  output, persists drafts, and publishes user-visible results.
- **Prompt boundary:** `src/meal_planner/llm/prompts.py::build_plan_prompt()`
  still requests and describes exactly seven days.
- **Feasibility boundary:**
  `src/meal_planner/preferences.py::validate_horizon_feasibility()` computes
  capacity for every effective rule without distinguishing strict obligations
  from best-effort guidance.
- **Retry boundary:**
  `src/meal_planner/bot_handler.py::_retry_plan_request()` replaces the start
  date with the current date and dispatches retained rules without another
  horizon check.

## Review Findings Covered

1. **P1:** Propagate the selected duration into planner generation.
2. **P2:** Keep best-effort rules non-blocking during feasibility checks.
3. **P2:** Revalidate the date horizon before retry dispatch.

## Scope and Constraints

- **In scope:** focused production fixes and regression tests for all three
  findings, followed by the repository quality gates.
- **Out of scope:** new meal-planning features, schema migrations, unrelated
  refactors, README changes, edits to either contextual plan, GitHub issue
  creation, commits, pushes, pull requests, deployment, and external actions.
- Keep historical planner events that omit `plan_days` valid as seven-day
  requests.
- Do not add a parallel short-plan model. Continue using `WeeklyPlan` and
  enforce the request-specific count at the planner boundary.
- Preserve unrelated and pre-existing work in the dirty worktree.
- Record implementation discoveries only in this remediation plan; do not
  rewrite or archive either contextual plan.

## Development Approach

- **Testing approach: TDD.** Start every task with the named focused test,
  run it against the current implementation, and record the expected failure
  before changing production code.
- Complete tasks sequentially. Do not start a later task until the new tests
  and the listed regressions for the current task pass.
- Make `plan_days` an application-owned invariant across state, events,
  prompts, repair, output validation, persistence, and retry.
- Treat only strict rules as pre-generation blockers. Keep best-effort rules
  in the effective snapshot so post-generation advisory reporting remains
  available.
- Capture a retry start date once and use that same value for feasibility,
  state transition decisions, and planner dispatch.
- Keep Python changes typed, use Ruff at 80 columns, and run every Python tool
  through `uv run` as configured in `pyproject.toml`.
- Mark checkboxes complete only after the corresponding work and verification
  have actually completed.

## Testing Strategy

- **Bot workflow tests:** assert initial and manual-retry Lambda payloads carry
  1-day, intermediate, and seven-day durations; assert date-shifted retries
  revalidate before entering `GENERATING`.
- **Prompt tests:** assert the exact requested count, contiguous day numbers,
  and inclusive date range for 1, intermediate, and 7-day requests.
- **Planner tests:** cover Lambda parsing, legacy defaults, initial generation,
  repair payload retention, exact-length acceptance, wrong-length rejection,
  persistence, and user delivery.
- **Preference tests:** distinguish strict blocking shortfalls from
  best-effort advisory shortfalls for exact and minimum operators, including
  weekday and meal-type scopes.
- **Integration regressions:** preserve rule priority, constraint safety,
  stale-request ownership, bounded repair, compare-and-swap behavior, and
  best-effort summaries.
- No test may use a skip, xfail, live AWS, live Telegram, network request, or
  live LLM request.

## Solution Overview

Thread the existing bounded `PlanDays` value through every generation boundary.
The bot will include it in initial and retry events. The planner will parse it
with the legacy default, retain it in `PlanGenerationContext`, pass it to the
prompt, copy it into repair events, and reject a parsed plan whose length does
not match the request before persistence or display.

Make horizon feasibility strength-aware. Capacity calculations should still
produce typed results for all rules, but only an infeasible strict rule should
make the aggregate result blocking or produce clarification. Best-effort rules
must remain in `effective_rules` and continue to flow into generation and the
existing post-generation advisory summary.

Before manual retry changes a request back to `GENERATING`, capture the current
UTC date and recompute feasibility over that date through
`plan_days - 1`. If a retained strict rule no longer fits, use the existing
bounded clarification workflow and do not invoke the planner. If the request
still fits, dispatch with that same captured date and retained duration.

## Technical Details

- Add a required typed `plan_days` argument at internal generation call sites;
  preserve a default of 7 only at serialized legacy input boundaries.
- Include `plan_days` in bot Lambda payloads, planner Lambda event parsing,
  generation context construction, bounded-repair payloads, and prompt calls.
- Derive the inclusive end date as
  `week_start + timedelta(days=int(plan_days) - 1)`.
- Describe exactly days `1..plan_days` in generation and repair prompts.
- After parsing provider output, require `len(plan.days) == plan_days` before
  domain validation, persistence, Telegram delivery, or repair completion.
  A mismatch should use bounded structural feedback and the existing one-repair
  lifecycle.
- Represent rule strength in horizon results or otherwise retain enough typed
  information to distinguish blocking strict shortfalls from advisory
  best-effort shortfalls. Do not drop best-effort results or rules.
- Generate clarification only from blocking strict results. Never echo raw
  source preference text.
- In manual retry, compute one `retry_start` before any state mutation. Validate
  retained effective rules over the retry horizon before transitioning to
  `GENERATING`.
- On a newly infeasible retry, preserve `plan_days`,
  `duration_collected=True`, request ownership, and bounded preference state;
  return to the existing preference-clarification path without planner side
  effects.

## What Goes Where

- **Task 1:** bot dispatch, planner context/event/repair propagation, prompt
  contract, exact-length validation, persistence, and delivery.
- **Task 2:** strength-aware horizon results and non-blocking bot integration
  for best-effort rules.
- **Task 3:** retry-date horizon revalidation and final repository gates.

## Implementation Steps

### Task 1: Propagate exact duration through planner generation

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_parser.py` only if parser-facing feedback assertions are
  required for wrong-length output

**Symbols:** `_handle_plan_preference()`, `_retry_plan_request()`,
`_invoke_planner()`, `PlannerHandler.generate_plan()`,
`PlannerHandler._schedule_repair()`, `PlannerHandler.handle_event()`,
`PlanGenerationContext`, `build_plan_prompt()`, parsed-plan validation and
draft publication

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_plan_request_dispatches_selected_duration` in
  `tests/test_bot_handler.py`, parameterized for `1, no preference` and
  `3, no preference`. Assert the asynchronous `GENERATE_PLAN` payload includes
  `plan_days` with the exact accepted value and retains the matching
  `week_start`.
- [x] Add bot retry payload coverage proving a retained intermediate duration
  is serialized again, plus a seven-day compatibility case.
- [x] Add prompt tests for 1, 3, and 7 days. Assert each prompt names the exact
  count, inclusive start/end dates, and output day sequence, and does not
  retain contradictory seven-day wording for shorter requests.
- [x] Add planner event tests proving explicit durations survive Lambda
  parsing and `generate_plan()` construction, while an event omitting
  `plan_days` still becomes a seven-day request.
- [x] Add planner repair tests proving attempt 2 carries the original
  `plan_days`, request ownership fields, rules, and start date unchanged.
- [x] Add generated-output tests for requested lengths 1 and 3. Assert a plan
  with exactly the requested contiguous days is persisted and delivered, while
  a structurally valid plan with a different allowed length is rejected before
  persistence or display and enters the existing bounded repair/failure path.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that bot and repair payloads omit `plan_days`, planner context falls
  back to 7, prompt text requests seven days, or a wrong-length plan passes
  request-specific validation.
  Observed before implementation: both parameterized bot cases failed with a
  missing `plan_days` payload key.
- [x] **Implementation change:** Add typed `plan_days` propagation through the
  initial bot dispatch, manual retry dispatch, `_invoke_planner()` payload,
  Lambda event parsing, `PlannerHandler.generate_plan()`, generation context,
  prompt construction, and repair serialization. Keep 7 as the legacy default
  only where old serialized events enter the application.
- [x] Update `build_plan_prompt()` to render the exact duration, inclusive date
  range, and dynamic `1..plan_days` contract while preserving constraint and
  effective-rule priority wording.
- [x] Enforce `len(plan.days) == int(context.plan_days)` after provider parsing
  and before validation, persistence, delivery, or repair success. Convert a
  mismatch into bounded structural feedback and retain the same duration for
  the single allowed repair.
- [x] Preserve stale-request suppression, idempotent publication, privacy-safe
  logging, current/stored/constraint snapshots, legacy seven-day events, and
  the one-repair limit.
- [x] **Verify the new test passes:** Run the exact new bot, prompt, event,
  repair, and output-length tests and require all 1, 3, and 7-day cases to pass
  without skip or xfail.
- [x] **Relevant regression commands:** Run `uv run pytest
  tests/test_bot_handler.py tests/test_prompts.py tests/test_parser.py
  tests/test_planner_handler.py` and resolve every failure before Task 2.

**Acceptance criteria:**

- [x] Initial generation and manual retry events contain the accepted
  `plan_days` value.
- [x] Lambda parsing, planner context, prompt generation, and automatic repair
  preserve that value exactly.
- [x] Legacy events without `plan_days` continue as seven-day requests.
- [x] Prompts request exactly the selected consecutive dates and contain no
  contradictory fixed-seven contract for shorter requests.
- [x] Exactly sized 1-day and intermediate plans can be persisted and
  delivered.
- [x] Any generated length different from the requested duration is rejected
  before persistence or display and cannot be accepted by repair.
- [x] Rule priority, stale ownership, bounded repair, and failure retention
  remain green.

### Task 2: Keep best-effort horizon shortfalls advisory

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/bot_handler.py` only if aggregate-result handling
  needs an explicit blocking/advisory branch
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py` only if advisory-summary integration
  needs additional coverage

**Symbols:** `RuleHorizonFeasibility`, `HorizonFeasibilityResult`,
`_capacity_is_insufficient()`, `validate_horizon_feasibility()`,
`format_horizon_clarification()`, plan-request feasibility integration, and
`format_best_effort_summary()`

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_best_effort_exact_rule_does_not_block_short_horizon` in
  `tests/test_preferences.py`. Use a best-effort exact-two breakfast rule over
  a one-day horizon; assert the aggregate result permits generation while the
  typed per-rule result still records the capacity shortfall as advisory.
- [x] Add the equivalent best-effort `AT_LEAST` case and parameterize whole
  horizon, absent-weekday, partly covered weekday, and meal-type-scoped
  capacities.
- [x] Add paired strict controls for the same operators and scopes. Assert
  strict shortfalls remain blocking and still produce bounded deterministic
  clarification.
- [x] Add bot tests proving infeasible best-effort exact/minimum rules invoke
  the planner with the rule still present in `effective_rules`, preserve
  `plan_days`, and do not send horizon clarification.
- [x] Add or retain a planner integration test proving an omitted best-effort
  rule is reported through the existing advisory summary and does not trigger
  strict repair or terminal failure.
- [x] **Expected failure:** Run the new focused cases before implementation and
  record that `_capacity_is_insufficient()` marks the best-effort rule
  blocking, `HorizonFeasibilityResult.is_feasible` becomes false, and the bot
  prevents planner invocation.
  Observed before implementation: all six best-effort capacity cases were
  blocking, and the result lacked `advisory_shortfalls`.
- [x] **Implementation change:** Make horizon feasibility strength-aware.
  Retain a typed result for every rule, but calculate aggregate blocking status,
  `infeasible_rules`, and clarification from strict shortfalls only. Expose a
  clearly named advisory-shortfall collection if needed by tests or callers.
- [x] Keep best-effort rules in `effective_rules`; do not weaken, silently
  delete, or reinterpret them. Preserve their prompt guidance and
  post-generation `format_best_effort_summary()` behavior.
- [x] Keep strict `EXACTLY` and `AT_LEAST` capacity failures unchanged, and
  preserve feasible `AT_MOST` and `EXACTLY 0` semantics for every strength.
- [x] **Verify the new test passes:** Run the new preference and bot cases and
  require best-effort exact/minimum shortfalls to proceed while paired strict
  cases still block.
- [x] **Relevant regression commands:** Run `uv run pytest
  tests/test_preferences.py tests/test_bot_handler.py
  tests/test_planner_handler.py` and resolve every failure before Task 3.

**Acceptance criteria:**

- [x] Insufficient capacity for a best-effort exact or minimum rule never
  blocks generation by itself.
- [x] The typed horizon result retains the best-effort shortfall as advisory
  information rather than discarding it.
- [x] Best-effort rules reach the prompt and remain eligible for the existing
  post-generation advisory summary.
- [x] Equivalent strict shortfalls still request bounded clarification before
  planner invocation.
- [x] Weekday, meal-type, upper-bound, zero-count, and rule-priority behavior
  remains green.

### Task 3: Revalidate the current date horizon before manual retry

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_preferences.py` only if retry integration exposes a
  missing helper contract case
- Modify: this remediation plan with final verification evidence

**Symbols:** `_retry_plan_request()`, `validate_horizon_feasibility()`,
`ConversationWorkflowStep.RETRY_READY`,
`ConversationWorkflowStep.AWAITING_PREFERENCE`,
`ConversationWorkflowStep.GENERATING`, and `_invoke_planner()`

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_retry_revalidates_rules_after_utc_date_change` in
  `tests/test_bot_handler.py`. Build a one-day `RETRY_READY` request accepted on
  Monday with a strict Monday obligation, advance the controlled UTC date to
  Tuesday, and assert retry does not transition to `GENERATING` or invoke the
  planner.
- [x] In the same scenario, assert the bot enters the existing bounded
  preference-clarification path and retains `plan_days=1`,
  `duration_collected=True`, request ownership, and the state needed for a
  safe clarification reply.
- [x] Add a feasible date-shift case proving retry captures the current date
  once, validates through `retry_start + plan_days - 1`, transitions with the
  expected compare-and-swap revision, and dispatches that same date and
  duration.
- [x] Add retry controls proving an infeasible best-effort rule from Task 2
  remains non-blocking, an infeasible strict intermediate-duration rule blocks,
  and a legacy seven-day retry remains compatible.
- [x] Add duplicate-update and compare-and-swap-loss cases proving date
  revalidation creates no planner side effect when the caller no longer owns
  the request.
- [x] **Expected failure:** Run the new focused retry cases before
  implementation and record that the current code transitions directly to
  `GENERATING`, substitutes Tuesday as `week_start`, and invokes the planner
  with a strict Monday rule that was never revalidated for Tuesday's horizon.
- [x] **Implementation change:** Capture `retry_start = date.today()` once at
  the beginning of the owned retry attempt. Validate retained effective rules
  over the concrete retry horizon before constructing or persisting a
  `GENERATING` candidate.
- [x] If a strict rule is newly infeasible, use the existing bounded horizon
  clarification semantics, preserve duration and phase, transition safely to
  clarification with compare-and-swap ownership, and do not invoke the
  planner. Keep best-effort-only shortfalls non-blocking.
- [x] If feasible, use the same captured `retry_start` and retained
  `plan_days` for the state transition and `_invoke_planner()` call. Preserve
  request ID, revision, rules, retry-ready recovery, and duplicate-update
  behavior.
- [x] **Verify the new test passes:** Run every new retry-date test and require
  strict shifted-horizon failures to clarify without dispatch while feasible
  and best-effort retries dispatch exactly once.
- [x] **Relevant regression commands:** Run `uv run pytest
  tests/test_preferences.py tests/test_bot_handler.py tests/test_prompts.py
  tests/test_parser.py tests/test_planner_handler.py` and resolve all failures.
- [x] Run `uv run ruff format .`, then `uv run ruff format --check .`.
- [x] Run `uv run ruff check .` and fix all findings.
- [x] Run `uv run mypy` and fix all findings.
- [x] Run `uv run pytest` and fix all failures.
- [x] Run `git diff --check`, record focused and full-suite results in this
  plan, and confirm no unrelated or contextual-plan files changed.

**Acceptance criteria:**

- [x] Manual retry validates strict rules against the exact dates it will send
  to the planner before entering `GENERATING`.
- [x] A date-shifted strict rule that no longer fits cannot reach planner
  dispatch.
- [x] A newly infeasible retry retains its accepted duration and explicit
  phase and returns bounded clarification without losing request ownership.
- [x] Feasible and best-effort-only retries dispatch once with one captured
  start date and the retained `plan_days`.
- [x] Legacy seven-day retry, compare-and-swap, duplicate-update,
  retry-ready recovery, and dietary-constraint behavior remains green.
- [x] Ruff formatting and lint, Mypy, the full Pytest suite, and
  `git diff --check` pass.
- [x] All three independent-review findings have direct regression coverage
  and no remediation work remains untracked by this plan.

**Task 3 verification evidence:**

- The pre-implementation focused retry run failed in
  `test_retry_revalidates_rules_after_utc_date_change` because the old code
  invoked the planner for the shifted strict weekday rule; the three control
  tests passed against the baseline.
- Focused retry tests: `uv run pytest tests/test_bot_handler.py -k
  'retry_revalidates_rules_after_utc_date_change or
  retry_feasible_date_shift_uses_one_start_date or
  retry_best_effort_shortfall_remains_non_blocking or
  retry_date_revalidation_does_not_dispatch_after_state_loss'` — 4 passed.
- Relevant regressions: `uv run pytest tests/test_preferences.py
  tests/test_bot_handler.py tests/test_prompts.py tests/test_parser.py
  tests/test_planner_handler.py` — 662 passed.
- Quality gates: `uv run ruff format .`, `uv run ruff format --check .`,
  `uv run ruff check .`, and `uv run mypy` passed.
- Full suite: `uv run pytest` — 1,283 passed with two existing Pydantic
  serializer warnings. The ignored SAM artifact was refreshed with
  `uvx --from aws-sam-cli sam build --beta-features` after the initial stale
  artifact failure.
- `git diff --check` passed; no contextual plan files were modified.

## Post-Completion

**Manual verification**

- Start `/plan`, submit `1, no preference` and `3, no preference`, and confirm
  generated, displayed, and persisted plans contain exactly the selected
  consecutive dates.
- Force one invalid-length provider response and confirm the single repair
  keeps the original duration; a second invalid response must not publish.
- Submit an impossible one-day best-effort request such as two egg breakfasts
  "if convenient" and confirm generation proceeds with advisory reporting.
- Create a one-day retry-ready request with a weekday-specific strict rule,
  retry after the UTC date changes, and confirm the shifted horizon is checked
  before dispatch.

**External actions**

- GitHub issue creation, commits, pushes, pull requests, deployment, and
  comments on external issues are intentionally outside this remediation-plan
  creation and require separate authorization.
