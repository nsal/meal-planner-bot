# Harden Variable-Length Plan Clarification and Weekday Feasibility

## Overview

- Add a migration-safe conversation phase that distinguishes a new `/plan`
  duration response from later preference clarification.
- Preserve in-flight legacy seven-day conversations, including states whose
  preference is still empty when the new code is deployed.
- Make horizon feasibility sensitive to rule operators so absent weekdays
  block positive obligations but do not block already-satisfied upper bounds.
- Integrate these corrections into the variable-length meal-plan work tracked
  by `2026-08-25-support-variable-length-meal-plans.md`.

## Context (from discovery)

- **Primary feature plan:**
  `docs/plans/2026-08-25-support-variable-length-meal-plans.md`.
- **Affected state model:** `ConversationState` currently represents both the
  initial preference prompt and clarification with `AWAITING_PREFERENCE`.
- **Ambiguous value:** `preference` may remain `None` after a valid
  `N, no preference` response when a stored rule, constraint, or horizon check
  asks for clarification, so preference presence cannot identify the phase.
- **Legacy behavior:** historical `AWAITING_PREFERENCE` states prompted for
  preference text only and must continue as seven-day requests after rollout.
- **Rule behavior:** `DietaryRule` supports `EXACTLY`, `AT_LEAST`, and
  `AT_MOST`; an absent weekday has zero achievable matches, which satisfies an
  upper-bound rule but may make a positive lower-bound obligation impossible.
- **Relevant files:** `src/meal_planner/models/schemas.py`, planner-facing model
  exports, `src/meal_planner/bot_handler.py`,
  `src/meal_planner/preferences.py`, focused schema/preference/bot tests, and
  the primary feature plan.
- **Dependencies:** Pydantic validation, durable DynamoDB conversation states,
  compare-and-swap transitions, existing dietary priority resolution, and the
  variable-length plan work. No new package is required.

## Development Approach

- **Testing approach:** TDD. Write focused failing tests before each production
  change and run them before continuing.
- Complete each task fully before moving to the next and mark its checkboxes as
  soon as work completes.
- Make small, typed changes and retain the existing plan-request workflow
  rather than adding a parallel conversation kind.
- Every task that changes Python behavior includes separate success and failure
  tests.
- All focused tests must pass before starting the next task.
- Update this plan and the primary feature plan whenever implementation
  discoveries alter scope or sequencing.
- Maintain compatibility with persisted states created before `plan_days` and
  the explicit phase marker existed.
- Follow `pyproject.toml`: use Ruff at 80 columns, run mypy in strict mode, and
  execute Python tools through `uv run`.

## Testing Strategy

- **Schema tests:** marker defaults, workflow-shape invariants, new-state
  construction, legacy payloads, and serialization round trips.
- **Bot tests:** initial parsing, `no preference` clarification, stored-rule
  clarification, legacy in-flight replies, `/plan` reset, duplicate updates,
  and compare-and-swap retention.
- **Preference tests:** operator/count combinations, meal-type capacity, absent
  and partial weekday coverage, and bounded deterministic feedback.
- **Integration tests:** priority resolution followed by horizon validation,
  retained duration across repeated clarification, and no planner invocation
  for genuinely impossible rules.
- **Full gates:** `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`.
- The project has no separate browser UI or end-to-end suite; pytest is the
  integration gate.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Prefix newly discovered work with ➕ and blockers with ⚠️.
- Keep this file and the primary feature plan synchronized with implementation.
- Do not archive this plan until every required gate passes.

## Solution Overview

Add an explicit boolean conversation phase such as `duration_collected`. Give
the field a legacy-safe default of `True`, because historical states were
already waiting for preference-only text. Set it to `False` explicitly only in
new `/plan` states created after deployment, then atomically set it to `True`
when `N, preference` is accepted. Require generating and retry-ready states to
have collected a duration. This avoids inferring workflow phase from
`preference`, which is legitimately nullable.

Use this phase in the bot handler: parse `N, preference` only when
`duration_collected` is false; otherwise treat the full message as
clarification text and retain `plan_days`. A legacy state that omits both new
fields validates as a collected seven-day request and therefore accepts the
preference-only response it originally requested.

Implement horizon feasibility as an application-owned calculation over the
actual dates and eligible meal slots. `EXACTLY` and `AT_LEAST` are impossible
when a positive requested count exceeds capacity. `AT_MOST` is never impossible
solely because the horizon or weekday scope has zero eligible slots. For a
weekday-scoped rule, preserve the existing per-named-weekday semantics and
evaluate each named weekday independently.

## Technical Details

- Add `duration_collected: bool = True` to `ConversationState`; the default is
  intentionally for backward compatibility, not for new workflow creation.
- Set `duration_collected=False` and the legacy-default `plan_days=7` in
  `_new_plan_state()`. The duration value is provisional until the initial
  response is accepted.
- On a valid initial response, persist the parsed `plan_days`, set
  `duration_collected=True`, and store the preference interpretation in the same
  compare-and-swap transition.
- On invalid initial input, leave both duration fields and the revision
  unchanged, and do not call preference interpretation or the planner.
- On clarification, do not parse a duration even when the text contains commas;
  append the bounded text using the existing preference flow and preserve the
  retained duration and collected phase.
- A plan-request state in `GENERATING` or `RETRY_READY` must have
  `duration_collected=True`. Non-plan workflows must not carry a false marker.
- Compute concrete dates from `week_start` through
  `week_start + plan_days - 1`; map `date.isoweekday()` to `Weekday`.
- Capacity is the sum of eligible meal slots in the horizon. A meal-type-scoped
  rule has one eligible slot per matching date; an unscoped rule uses the
  application-owned daily meal capacity.
- For weekday-scoped rules, evaluate each named weekday separately, matching
  the generated-plan validator's semantics. An absent weekday has capacity
  zero.
- Reject an `EXACTLY` or `AT_LEAST` rule only when its positive count exceeds
  capacity. Do not reject `AT_MOST`, or `EXACTLY 0`, because zero matches are
  feasible.
- Return bounded, stable clarification text without echoing raw preference or
  profile content.

## What Goes Where

- **Implementation Steps:** correct the primary plan, define the durable phase,
  use it in the bot workflow, implement operator-aware feasibility, integrate
  validation, and run repository gates.
- **Post-Completion:** create a Conventional Commit and pull request on a
  feature branch, then update the associated GitHub issues. Never push or merge
  directly to `master`.

## Implementation Steps

### Task 1: Synchronize the primary feature plan

**Files:**
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`
- Modify: this remediation plan if implementation discoveries require it

- [x] Replace preference-presence phase detection with an explicit,
  migration-safe duration-collected phase in the primary plan.
- [x] Specify preference-only handling for legacy in-flight
  `AWAITING_PREFERENCE` states that omit `plan_days` and the phase marker.
- [x] Qualify absent-weekday rejection by operator and positive count.
- [x] Add the focused regression cases from this remediation plan to the
  relevant primary-plan task checklists.
- [x] Review both plans for consistent terminology and sequencing before Task 2.

### Task 2: Define the durable duration phase contract

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/factories.py` if state factories need phase control

- [x] Write failing success tests for explicit collected/uncollected states and
  serialization round trips.
- [x] Write failing compatibility tests proving legacy plan-request payloads
  default to collected seven-day requests.
- [x] Write failing error tests rejecting uncollected `GENERATING` and
  `RETRY_READY` states and invalid phase use by other workflows.
- [x] Add and export the typed phase field with its legacy-safe default and
  workflow-shape validation.
- [x] Update typed factories to build initial, clarification, generating, and
  legacy states without ambiguous defaults.
- [x] Run `uv run pytest tests/test_schemas.py`; all tests must pass before
  Task 3.

### Task 3: Use the explicit phase in `/plan` input handling

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write failing tests proving new `/plan` states require `N, preference`
  while legacy in-flight states accept preference-only text as seven days.
- [x] Write failing tests for `N, no preference` followed by stored-rule,
  constraint, and horizon clarification; replies must remain preference-only.
- [x] Write failing tests proving clarification commas are preserved and
  `/plan` reset creates a fresh uncollected phase.
- [x] Write failing error tests for invalid initial syntax, duplicate updates,
  and compare-and-swap loss without interpreter or planner side effects.
- [x] Set the explicit phase in new states and branch initial parsing solely on
  that phase rather than on preference presence.
- [x] Atomically retain `plan_days` and the collected phase through
  clarification, generation, and retry-ready transitions.
- [x] Run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 4.

### Task 4: Implement operator-aware horizon feasibility

**Files:**
- Modify: `src/meal_planner/preferences.py`
- Modify: planner-facing exports if the helper is public
- Modify: `tests/test_preferences.py`

- [x] Write failing success tests for feasible `AT_MOST` and `EXACTLY 0` rules
  when all named weekdays are outside the horizon.
- [x] Write failing error tests for positive `EXACTLY` and `AT_LEAST` counts
  exceeding whole-horizon or named-weekday capacity.
- [x] Write failing tests for partly covered weekday sets and preserve
  per-weekday evaluation for each named day.
- [x] Write failing capacity tests for meal-type-scoped and unscoped rules over
  one-day, intermediate, and seven-day horizons.
- [x] Add a typed feasibility result and deterministic helper based on concrete
  dates, operators, counts, and eligible slot capacity.
- [x] Bound clarification output and avoid returning raw rule source text.
- [x] Run `uv run pytest tests/test_preferences.py`; all tests must pass before
  Task 5.

### Task 5: Integrate feasibility with priority resolution

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dietary_rules.py` only if integration exposes a priority
  contract gap

- [x] Write failing integration tests proving feasibility runs after current
  rules override applicable stored rules and after constraint conflict checks.
- [x] Write failing tests proving infeasible effective rules retain duration and
  phase for clarification without invoking the planner.
- [x] Write success tests proving absent-weekday `AT_MOST` rules proceed to
  generation and retain the resolved priority tiers.
- [x] Call the feasibility helper after constraint-first priority resolution and
  before the transition to `GENERATING`.
- [x] Preserve current/stored priority, permanent-constraint safety,
  compare-and-swap ownership, and bounded preference accumulation.
- [x] Run `uv run pytest tests/test_preferences.py tests/test_dietary_rules.py
  tests/test_bot_handler.py`; all tests must pass before Task 6.

### Task 6: Verify acceptance criteria

**Files:**
- Modify: affected Python and test files only when a gate exposes a defect
- Modify: both active plan documents with final verification results

- [x] Verify new states parse duration exactly once and all clarification paths
  preserve the full accepted duration.
- [x] Verify legacy preference-only conversations continue as seven-day
  requests without requiring users to restart `/plan`.
- [x] Verify positive lower-bound obligations fail only when their eligible
  capacity is insufficient and upper bounds do not fail on absent weekdays.
- [x] Verify dietary constraints remain inviolable and current rules retain
  precedence over applicable stored rules.
- [x] Run `uv run ruff format .`, then `uv run ruff format --check .`.
- [x] Run `uv run ruff check .` and fix all findings.
- [x] Run `uv run mypy` and fix all findings.
- [x] Run `uv run pytest` and fix all failures.
- [x] Record gate results and mark completed plan items immediately.

**Task 6 verification results (2026-08-25):**

- Acceptance coverage: `tests/test_schemas.py`, `tests/test_bot_handler.py`,
  and `tests/test_preferences.py` verify one-time duration parsing, retained
  clarification duration, legacy seven-day behavior, operator-aware horizon
  capacity, constraint safety, and current-over-stored precedence.
- `uv run ruff format .`: passed; 94 files left unchanged.
- `uv run ruff format --check .`: passed; 94 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: passed; 1254 passed, 2 existing Pydantic serializer
  warnings.

### Task 7: [Final] Update documentation and archive the remediation plan

**Files:**
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`
- Move: this file to
  `docs/plans/completed/2026-08-25-harden-variable-length-plan-clarification-and-weekday-feasibility.md`

- [x] Update the primary feature plan with any final implementation discoveries
  and verification evidence.
- [x] Confirm user documentation remains accurate; update `README.md` only if
  phase or feasibility behavior changes its documented user contract.
- [x] Re-run focused tests for any documentation-adjacent behavior changed.
- [x] Re-run `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest` after final updates.
- [x] Confirm every remediation checkbox is complete and move this plan to
  `docs/plans/completed/`.

**Task 7 verification results (2026-08-25):**

- Updated the primary feature plan with the hardening implementation note and
  retained its incomplete primary implementation tasks as the source of truth.
- Updated `README.md` to document `N, preference` parsing, first-comma
  semantics, retained duration during clarification, legacy seven-day
  compatibility, and operator-aware horizon clarification behavior.
- `uv run pytest tests/test_template.py`: passed.
- Full repository gates were rerun after the documentation updates:
  `uv run ruff format --check .` passed with 94 files already formatted,
  `uv run ruff check .` passed, `uv run mypy` passed with no issues in 20
  source files, and `uv run pytest` passed with 1254 tests and 2 existing
  Pydantic serializer warnings.

## Post-Completion

**Manual verification**

- Start `/plan`, submit `3, no preference`, trigger a saved-rule clarification,
  and reply with text containing a comma; confirm the duration remains three.
- Resume a persisted pre-deployment `AWAITING_PREFERENCE` state and confirm a
  preference-only reply starts a seven-day request.
- Try an upper-bound rule scoped only to a weekday outside a short plan and
  confirm generation proceeds.
- Try a positive lower-bound rule scoped only to an absent weekday and confirm
  generation pauses for clarification.

**GitHub and version-control follow-up**

- Work on a dedicated feature branch and use a Conventional Commit containing
  the relevant issue number.
- Open a pull request instead of pushing or merging directly to `master`.
- Comment on the primary feature issue and the remediation issue with the
  commit or pull-request link.

**External systems**

- Verify deployed bot instances deserialize pre-deployment conversation states
  with the legacy-safe phase default during rolling deployment.
- No storage migration, dependency update, or planner-Lambda field change is
  required solely for this remediation.
