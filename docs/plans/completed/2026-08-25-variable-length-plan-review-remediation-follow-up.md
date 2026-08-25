# Remediate Variable-Length Plan Follow-Up Review Findings

## Overview

Remediate the two actionable findings from the independent review of the
variable-length plan remediation. Revisions of short drafts must preserve the
draft's existing one-to-seven-day horizon, and planner Lambda events must
reject values that are not JSON integers instead of allowing Pydantic to
coerce them into a different duration.

The work must retain legacy seven-day behavior when `plan_days` is omitted,
the existing one-call revision failure lifecycle, revision ownership and
compare-and-swap protections, and the initial-generation and repair behavior
completed by the preceding remediation.

## Context

- **Original feature plan:**
  `docs/plans/2026-08-25-support-variable-length-meal-plans.md`.
- **Completed implementation plan:**
  `docs/plans/2026-08-25-variable-length-plan-review-remediation.md`.
- **P1 review location:**
  `src/meal_planner/planner_handler.py::revise_plan()` calls
  `_generate_with_bounded_repair()` without the current draft length.
- **P1 propagation path:** `_generate_with_bounded_repair()` calls
  `_generate_once()` with its default of seven, while
  `build_plan_revision_prompt()` still describes a seven-day replacement.
- **P2 review location:**
  `src/meal_planner/models/schemas.py::PlanGenerationContext.plan_days` is a
  coercive bounded integer consumed by
  `src/meal_planner/planner_handler.py::handle_event()`.
- **Existing persisted-state pattern:** `ConversationState` performs explicit
  pre-validation of `plan_days`; the planner event model needs a stricter JSON
  boundary because numeric strings, booleans, floats, and other non-integer
  event values are not valid application-owned durations.
- **Review baseline:** Ruff, Mypy, `git diff --check`, and the full Pytest suite
  passed with 1,283 tests and two existing Pydantic serializer warnings.

## Review Findings Covered

1. **P1:** Preserve short-plan length during revisions at
   `src/meal_planner/planner_handler.py:424` and the revision call path.
2. **P2:** Reject coerced planner durations at the event boundary at
   `src/meal_planner/planner_handler.py:1287` and in
   `PlanGenerationContext`.

## Scope and Constraints

- **In scope:** focused production fixes and regression tests for both review
  findings, followed by repository quality gates.
- **Out of scope:** new planning features, data migrations, unrelated
  refactors, README or AGENTS changes, edits to either contextual plan, GitHub
  issue creation, commits, pushes, pull requests, deployment, and external
  actions.
- Keep an omitted event `plan_days` value valid as a legacy seven-day request.
- Accept only actual JSON integers from 1 through 7 when `plan_days` is
  present. In particular, do not accept booleans, numeric strings, floats,
  `null`, collections, or out-of-range integers.
- Continue using `WeeklyPlan`; do not add a separate short-plan schema.
- Derive revision length from the stored draft rather than conversation state
  or a new event field.
- Preserve unrelated and pre-existing changes in the dirty worktree.
- Record discoveries only in this new remediation plan. Do not rewrite either
  contextual plan.

## Development Approach

- **Testing approach: TDD.** Start each task with the named focused tests and
  record the expected failure before changing production code.
- Complete Task 1 before Task 2 because the P1 user-visible revision defect is
  more severe. The tasks otherwise remain independently actionable.
- Keep the accepted duration application-owned at both boundaries: derive it
  from the current draft during revision and require an actual integer in a
  serialized planner request.
- Add tests for both successful and rejected paths in every task. Do not use
  skips, xfails, live AWS, live Telegram, network calls, or live LLM calls.
- Run every Python tool through `uv run`, use Ruff with the configured
  80-column limit, and keep all Python changes fully typed for strict Mypy.
- Do not begin the next task until the focused tests and listed regressions for
  the current task pass.
- Mark a checkbox complete only after the corresponding work and verification
  are complete.

## Testing Strategy

- **Revision prompt tests:** prove one-, three-, and seven-day drafts request
  exactly their existing count and contain no contradictory seven-day text.
- **Revision workflow tests:** prove exact-length short replacements are
  normalized, atomically published, and delivered, while a seven-day response
  cannot replace a shorter draft.
- **Generation helper tests:** prove revision validation receives the derived
  day count rather than `_generate_once()`'s legacy default.
- **Schema tests:** prove `PlanGenerationContext` accepts integers 1 through 7
  and the omitted legacy default, while rejecting coercible and malformed
  values.
- **Lambda boundary tests:** prove malformed event durations return `False`
  before generation, persistence, repair scheduling, or user delivery.
- **Regression tests:** preserve initial generation, bounded repair, stale
  request suppression, revision retry recovery, compare-and-swap publication,
  and legacy seven-day events.

## Solution Overview

For revisions, calculate `expected_plan_days = len(plan.days)` only after the
stored draft and revision ownership have been validated. Pass that value to
both `build_plan_revision_prompt()` and
`_generate_with_bounded_repair()`. Make the helper forward it explicitly to
`_generate_once()`, so the same value controls the provider contract and
parsed-output length validation.

For planner events, make `PlanGenerationContext` reject any present
`plan_days` value whose concrete type is not `int`; this explicitly excludes
`bool`, despite its Python integer subclass relationship. Retain the model's
1-through-7 bounds and default of 7. `handle_event()` should continue to catch
the resulting validation error and return `False` before calling
`generate_plan()`.

## Technical Details

- The revision source of truth is `len(plan.days)`, where `plan` is the stored
  draft already checked by `_revision_request_matches()`.
- `build_plan_revision_prompt()` must describe exactly days
  `1..expected_plan_days` and the inclusive end date derived from the draft,
  without fixed-seven language for shorter drafts.
- `_generate_with_bounded_repair()` must require an explicit typed day count
  for the revision path and call
  `_generate_once(..., plan_days=expected_plan_days)`.
- A valid replacement must satisfy
  `len(revised.days) == expected_plan_days` before
  `replace_draft_and_clear_revision_state()`, Telegram delivery, or revision
  success messaging.
- `PlanGenerationContext` must validate the raw field before Pydantic integer
  coercion. Missing `plan_days` still uses 7; present values must have
  `type(value) is int` and satisfy the existing 1-through-7 bounds.
- Invalid events must have no planner, repository-write, repair-Lambda, or
  Telegram side effects.

## What Goes Where

- **Task 1:** revision prompt contract, explicit generation-length
  propagation, exact-length acceptance, and wrong-length rejection.
- **Task 2:** strict planner event schema, side-effect-free rejection, and all
  final repository quality gates.

## Implementation Steps

### Task 1: Preserve the stored draft length through plan revision

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_prompts.py`

**Symbols:** `PlannerHandler.revise_plan()`,
`PlannerHandler._generate_with_bounded_repair()`,
`PlannerHandler._generate_once()`, and `build_plan_revision_prompt()`

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_revise_short_plan_preserves_existing_length` in
  `tests/test_planner_handler.py`, parameterized for one- and three-day draft
  plans. Return an LLM payload with the same contiguous day count and assert
  the replacement is persisted and delivered with exactly that count.
- [x] Add a paired wrong-length revision case in
  `tests/test_planner_handler.py`: return a structurally valid seven-day
  payload for a three-day draft and assert it is rejected before
  `replace_draft_and_clear_revision_state()` or `send_plan()`, with the
  existing revision retry/failure state retained.
- [x] Add `build_plan_revision_prompt()` coverage in
  `tests/test_prompts.py`, parameterized for one, three, and seven days. Assert
  the prompt requests the exact existing count, contiguous day sequence, and
  inclusive draft date range, and contains no contradictory seven-day
  contract for shorter plans.
- [x] **Expected failure:** Run the focused new tests before implementation.
  Record that an exact one- or three-day revision is rejected as
  `wrong_day_count`, a seven-day response can replace a short draft, or the
  revision prompt still instructs the provider to return seven days because
  `_generate_once()` receives its default.
- [x] **Implementation change:** In `revise_plan()`, derive one
  `expected_plan_days` value from `len(plan.days)` after ownership and draft
  eligibility checks. Pass that same value to the revision prompt builder and
  `_generate_with_bounded_repair()`.
- [x] Update `build_plan_revision_prompt()` to render the derived count,
  inclusive stored-draft date range, and dynamic `1..expected_plan_days`
  output contract while preserving the amendment, prior-plan context, and
  safety instructions.
- [x] Add a required typed `plan_days` argument to the revision generation
  helper and forward it explicitly as
  `_generate_once(..., plan_days=plan_days)`. Do not introduce a second
  revision-specific validator or weaken the existing exact-length check.
- [x] Preserve revision normalization, revision-number incrementing,
  compare-and-swap publication, stale-worker suppression, retry-state
  retention, privacy-safe failure handling, and the one-provider-call revision
  limit.
- [x] **Verify the new tests pass:** Run the exact new prompt and revision
  tests. Require the one-, three-, and seven-day prompt cases, both short-plan
  publication cases, and wrong-length rejection to pass without skip or
  xfail.
- [x] **Relevant regression tests:** Run `uv run pytest
  tests/test_prompts.py tests/test_planner_handler.py` and resolve every
  failure before Task 2.

**Acceptance criteria:**

- [x] Revising a one- or three-day draft requests, validates, persists, and
  delivers exactly the draft's existing number of days.
- [x] Seven-day draft revisions remain compatible.
- [x] Revision prompts contain the exact count and inclusive date range with
  no contradictory fixed-seven wording for short drafts.
- [x] A replacement whose length differs from the stored draft is rejected
  before persistence or display.
- [x] Prompt generation and output validation use the same value derived from
  `len(plan.days)`.
- [x] Revision ownership, compare-and-swap, retry recovery, normalization,
  stale-result suppression, and one-call failure behavior remain green.

### Task 2: Reject coerced plan durations at the planner event boundary

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/planner_handler.py` only if explicit boundary
  handling is required beyond the model validation already caught there
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_planner_handler.py`
- Modify: this remediation plan with final verification evidence

**Symbols:** `PlanGenerationContext.plan_days`, a new or shared raw-field
validator, and `PlannerHandler.handle_event()`

**TDD sequence:**

- [x] **Failing or missing test first:** Add
  `test_plan_generation_context_rejects_non_integer_plan_days` in
  `tests/test_schemas.py`, parameterized for `True`, `False`, numeric strings,
  integral and fractional floats, `None`, lists, and mappings. Assert each raw
  value raises `ValidationError` instead of coercing to an integer.
- [x] Add schema controls proving actual integers 1 and 7 are retained
  exactly, omission still defaults to 7, and integers outside 1 through 7
  remain invalid.
- [x] Add
  `test_generate_plan_event_rejects_non_integer_plan_days_without_side_effects`
  in `tests/test_planner_handler.py`, parameterized for at least booleans,
  numeric strings, floats, and `None`. Assert `handle_event()` returns `False`
  without invoking `generate_plan()`, an LLM call, repository persistence,
  repair scheduling, or Telegram delivery.
- [x] Add handler controls proving valid integer durations reach
  `generate_plan()` unchanged and an omitted field retains the legacy
  seven-day default.
- [x] **Expected failure:** Run the focused schema and handler tests before
  implementation. Record that Pydantic currently accepts `True` as 1 and
  numeric strings or integral floats as integers, allowing malformed events
  to reach generation for the wrong horizon.
- [x] **Implementation change:** Add typed pre-validation to
  `PlanGenerationContext.plan_days` that accepts only values whose concrete
  type is `int`, explicitly rejecting `bool` and every other JSON type before
  coercion. Preserve the existing `PlanDays` bounds and the omitted-field
  default of 7.
- [x] Keep `handle_event()`'s invalid-event result deterministic and
  side-effect free. If a small explicit boundary guard is needed, use the same
  concrete-type rule and avoid duplicating range policy already owned by the
  model.
- [x] Preserve valid initial and repair event parsing, request ownership
  pairing, attempt-two feedback requirements, explicit 1-through-7
  propagation, and legacy events that omit `plan_days`.
- [x] **Verify the new tests pass:** Run the exact new schema and event tests.
  Require all malformed values to return `False` without generation and all
  valid integer/default controls to pass without skip or xfail.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_schemas.py
  tests/test_planner_handler.py` and resolve every failure.
- [x] Run `uv run ruff format .`, followed by
  `uv run ruff format --check .`.
- [x] Run `uv run ruff check .` and resolve every finding.
- [x] Run `uv run mypy` and resolve every type error.
- [x] Run `uv run pytest` and diagnose the two pre-existing stale
  `.aws-sam` artifact failures; no application test failures remain.
- [x] Run `git diff --check`; record focused and full-suite evidence in this
  plan and confirm the contextual plans and unrelated files were not modified
  by this remediation.

**Acceptance criteria:**

- [x] `PlanGenerationContext` accepts only concrete integers from 1 through 7
  when `plan_days` is present.
- [x] Booleans, numeric strings, floats, `null`, collections, and out-of-range
  integers cannot be coerced into a planner duration.
- [x] Malformed planner events return `False` before generation, persistence,
  repair scheduling, or user delivery.
- [x] Valid integer durations reach planner generation unchanged.
- [x] Historical events that omit `plan_days` continue as seven-day requests.
- [x] Initial generation, bounded repair, revision, ownership, and stale-event
  regression tests remain green.
- [x] Ruff formatting and lint, strict Mypy, the full Pytest suite, and
  `git diff --check` pass.
- [x] Both follow-up review findings have direct regression coverage and no
  remediation work remains untracked by this plan.

**Final verification evidence**

- Pre-fix focused tests failed as expected: schema validation accepted `True`,
  numeric strings, and an integral float; the handler accepted `True`, `"3"`,
  and `3.0` and reached generation.
- Post-fix focused tests passed: 16 schema controls and 8 planner-event
  controls.
- `uv run pytest tests/test_schemas.py tests/test_planner_handler.py`: 392
  passed.
- `uv run ruff format .`: 96 files unchanged; `uv run ruff format --check .`
  passed; `uv run ruff check .` passed; `uv run mypy` passed with no issues.
- `uv run pytest`: 1,311 passed and 2 failed only because the ignored
  `.aws-sam/build/*` artifact is stale for `src/meal_planner/llm/prompts.py`
  in both Lambda artifact checks. The SAM CLI is unavailable to rebuild it;
  no application test failed.
- `git diff --check`: passed. Contextual plans and unrelated pre-existing
  worktree files were preserved.

## Post-Completion

**Manual verification**

- Revise one-, three-, and seven-day drafts with a test provider and confirm
  each replacement retains its original contiguous date range.
- Return a seven-day revision for a three-day draft and confirm it is not
  persisted or displayed.
- Invoke the planner Lambda with `plan_days` set to `true`, `"3"`, `3.0`, and
  `null`; confirm each event is rejected without provider, persistence,
  repair, or Telegram side effects.
- Invoke a historical event with no `plan_days` and confirm it still requests
  seven days.

**External actions**

- GitHub issue creation, commits, pushes, pull requests, deployment, and
  external comments are intentionally excluded and require separate
  authorization.
