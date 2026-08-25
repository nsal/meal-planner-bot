# Support Variable-Length Meal Plans

## Overview

- Change `/plan` so its single prompt asks for a duration and a
  request-specific preference in the form `N, preference`.
- Accept durations from 1 through 7, where day 1 is today and each additional
  day is the next consecutive calendar day.
- Split the initial response only once at the first comma so commas inside the
  preference remain intact.
- Generate, validate, persist, display, revise, and grocery-plan exactly the
  requested number of days.
- Preserve the existing dietary priority order: profile dietary constraints,
  current `/plan` preference, then stored profile preferences. A request may
  complete or override stored preferences but must never weaken or override a
  profile dietary constraint.

## Context (from discovery)

- **GitHub issue:** [#63 Support variable-length meal plans][issue-63].
- **Files/components involved:**
  `src/meal_planner/models/schemas.py`,
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`,
  `src/meal_planner/llm/prompts.py`,
  `src/meal_planner/preferences.py`,
  `src/meal_planner/dietary_rules.py`, planner-facing exports, Telegram command
  descriptions, test factories, focused tests, and `README.md`.
- **Current request pattern:** `/plan` creates an
  `AWAITING_PREFERENCE` conversation state. The next message is interpreted as
  a preference, resolved against stored preferences and permanent constraints,
  then carried through an asynchronous planner invocation and at most one
  repair attempt.
- **Current plan contract:** `WeeklyPlan` requires exactly days 1 through 7;
  prompt text, revision generation, and several user-facing messages also say
  seven-day or weekly plan. `week_end` always adds six days.
- **Related safety pattern:** `resolve_priority_rules()` already gives current
  request rules precedence over stored preference rules, while constraint
  validation and conflict checks prevent either preference tier from
  superseding dietary constraints.
- **Dependencies:** Pydantic validation, DynamoDB conversation-state
  compare-and-swap transitions, Lambda event serialization, LiteLLM prompt
  generation, bounded repair, and Telegram delivery. No new package is needed.

## Development Approach

- **Testing approach:** TDD. Add focused failing tests before each production
  change, implement the smallest change that makes them pass, and run the
  focused test command before proceeding.
- Complete each task fully before moving to the next and mark its checkboxes as
  soon as work completes.
- Keep changes small and typed, and use existing models and workflow paths
  rather than creating a parallel short-plan type.
- Every task that changes Python behavior includes new or updated success and
  failure tests as separate checklist items.
- All focused tests must pass before the next task starts.
- Update this plan whenever implementation discoveries change its scope.
- Maintain compatibility with historical serialized request states and planner
  events that do not contain `plan_days` by interpreting them as seven-day
  requests.
- Maintain compatibility with historical `AWAITING_PREFERENCE` request states
  that omit both `plan_days` and the explicit conversation phase: they are
  already waiting for preference-only text and remain seven-day requests.
- Follow `pyproject.toml`: format and lint with Ruff at 80 columns, type-check
  with mypy, and execute all tools through `uv run`.

## Testing Strategy

- **Schema tests:** duration bounds, legacy defaults, conversation-state shape,
  planner-context serialization, contiguous variable plan days, and dynamic
  end dates.
- **Bot workflow tests:** first-comma parsing, every duration boundary, invalid
  input, no-preference aliases, clarification continuation, conflict safety,
  retry retention, and duplicate update behavior.
- **Prompt tests:** exact duration/date horizon, dynamic output contract,
  bounded repair, grocery behavior, and duration-preserving revisions.
- **Planner tests:** event propagation, incorrect-length rejection, repair
  retention, persistence, delivery wording, and stale-request protection.
- **Preference tests:** exact-count capacity and weekday applicability within
  the requested consecutive date horizon, plus unchanged constraint priority.
- **Persistence and integration regressions:** active-plan coverage, target-day
  edits, confirmation, and groceries for plans of 1 through 7 days.
- **Full gates:** `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`.
- The project has no separate browser UI or end-to-end suite; the full pytest
  suite is the integration gate.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Prefix newly discovered work with ➕ and blockers with ⚠️.
- Keep this file synchronized with actual implementation and verification.
- Do not archive this plan until every required gate passes.

## Solution Overview

Use one native variable-duration plan path. Add a bounded `plan_days` value to
the durable plan-request state and `PlanGenerationContext`, with a legacy
default of 7. Parse it deterministically only on the first response to `/plan`;
after it is accepted, preference clarification messages remain preference-only
and reuse the retained duration.

Generalize `WeeklyPlan.days` from exactly seven days to a contiguous sequence
starting at day 1 with a length from 1 through 7. The requested duration remains
application-owned: prompts request an exact count, and planner validation
rejects output whose count differs even if it is otherwise a valid plan.
Repairs and retries carry the same value. Existing plans remain valid without a
storage migration because seven days is still accepted and the covered end date
can be derived from `len(days)`.

Add an explicit `duration_collected` conversation phase instead of inferring
the phase from whether `preference` is present. Give the field a legacy-safe
default of `True`, because historical states were already waiting for
preference-only text. Set it to `False` explicitly for new `/plan` states,
then set it to `True` atomically when the initial `N, preference` response is
accepted. A legacy `AWAITING_PREFERENCE` state that omits both new fields
therefore accepts preference-only text as a seven-day request. Require
generating and retry-ready plan-request states to have collected a duration,
and do not allow the false marker on other workflows.

Use this phase in the bot handler: parse `N, preference` only while
`duration_collected` is false; after it is accepted, treat clarification text
as preference-only, including text containing commas, and retain `plan_days`.

Before generation, evaluate structured preference counts and weekday scopes
against the actual dates from today through `today + plan_days - 1`. Keep the
existing resolution tiers intact and fail closed whenever a current or stored
preference conflicts with a permanent profile dietary constraint. For
weekday-scoped rules, evaluate each named weekday independently. An absent
weekday has zero capacity: a positive `EXACTLY` or `AT_LEAST` count that
exceeds that capacity is infeasible, while `AT_MOST` and `EXACTLY 0` remain
feasible when zero matches satisfy the rule.

## Technical Details

- Initial syntax is `N, preference`. Use a deterministic one-split operation
  such as `partition(",")`; never split the preference on later commas.
- Reject a missing comma, a non-integer first field, values outside 1 through
  7, and an empty second field. Remain in `AWAITING_PREFERENCE`, do not call the
  interpreter or planner, and return an example such as
  `3, fish for dinner`.
- Measure the existing 500-character limit against preference text only.
  `N, no preference` and existing no-preference synonyms produce no current
  preference.
- Add `duration_collected: bool = True` to `ConversationState`; the default is
  intentionally for backward compatibility, not for new workflow creation.
- Set `duration_collected=False` and the legacy-default `plan_days=7` in
  `_new_plan_state()`. The duration value is provisional until the initial
  response is accepted.
- Parse the initial duration only when `duration_collected` is false. On a
  valid initial response, persist `plan_days`, set `duration_collected=True`,
  and store the preference interpretation in the same compare-and-swap
  transition. On invalid input, leave the duration fields and revision
  unchanged and do not call the interpreter or planner.
- On clarification, do not parse a duration even when the text contains
  commas. Append the bounded text using the existing preference flow and
  preserve the retained duration and collected phase. A legacy state omitting
  `plan_days` and the marker is treated as a collected seven-day request.
- Persist `plan_days` on all plan-request states and include it in first
  generation, manual retry, automatic repair, and Lambda handler validation.
- Require a plan-request state in `GENERATING` or `RETRY_READY` to have
  `duration_collected=True`; non-plan workflows must not carry a false marker.
- Require `WeeklyPlan.days` to equal `[1, ..., N]` for some `N` from 1 through
  7. Derive `week_end` as `week_start + len(days) - 1` days.
- Add `plan_days` to generation prompts and describe the exact inclusive date
  range. Validate returned plan length against the requested context before
  publishing it.
- A whole-draft revision derives its required duration from the current plan
  and rejects/retries a replacement of a different length. Targeted edits may
  address only days that exist in the plan.
- Scope rule feasibility to the actual horizon. A positive `EXACTLY` or
  `AT_LEAST` count cannot proceed when its eligible capacity is insufficient,
  including a named weekday absent from the selected dates. An `AT_MOST` rule
  is not infeasible solely because its weekday has zero capacity, and
  `EXACTLY 0` remains feasible when no matching slots exist.
- For weekday-scoped rules, evaluate each named weekday independently with
  the generated-plan validator's semantics. A partly covered set may fail for
  one named weekday while remaining feasible for another.
- Keep DynamoDB sort keys based on the first date. Existing active-plan queries
  continue to use `week_start <= target <= week_end`, now with a dynamic end.
- Replace duration-inaccurate phrases such as `weekly meal plan` with `meal
  plan` or the concrete `N-day meal plan` where the duration is known.

## What Goes Where

- **Implementation Steps:** model contracts, parsing and durable state,
  horizon-aware preference validation, prompt and planner propagation,
  duration-preserving revisions and downstream lifecycle behavior, regression
  verification, and documentation.
- **Post-Completion:** create a dedicated feature branch if implementation is
  not already on one, create a Conventional Commit containing the associated
  issue number, open a pull request instead of pushing or merging to `master`,
  and comment on the issue with the commit or pull-request link.

## Implementation Steps

### Task 1: Define variable-duration request and plan contracts

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/factories.py`

- [x] Write failing schema tests for `plan_days` values 1 and 7, rejection of
  booleans and values outside 1 through 7, and seven-day defaults when legacy
  `ConversationState` and `PlanGenerationContext` payloads omit the field.
- [x] Write failing schema tests for the explicit `duration_collected` phase,
  its legacy-safe default, serialization round trips, and invalid workflow
  combinations for uncollected generating or retry-ready states.
- [x] Write failing plan tests accepting every contiguous duration 1 through 7
  and rejecting empty, gapped, duplicated, non-one-starting, and overlong day
  sequences.
- [x] Write failing tests proving `week_end` uses the actual plan length and
  extend `make_plan()` with a typed optional duration for later focused tests.
- [x] Add and export a bounded plan-duration type, carry it in plan-request
  conversation state and planner context, and enforce the required workflow
  shape without invalidating historical seven-day payloads.
- [x] Generalize `WeeklyPlan` validation and `week_end` to contiguous 1-through-
  7-day plans while retaining all meal and grocery invariants.
- [x] Run `uv run pytest tests/test_schemas.py`; all tests must pass before
  Task 2.

**Task 1 verification evidence (2026-08-25):**

- `tests/test_schemas.py` now accepts every contiguous horizon from 1 through
  7, rejects empty, gapped, duplicate, non-one-starting, and overlong day
  sequences, and checks dynamic end dates for 1, 3, and 7 days.
- `tests/factories.py::make_plan()` accepts typed `PlanDays = 7`, constructs
  exactly contiguous day numbers for every supported horizon, and retains the
  historical default meal, grocery, status, revision, and instruction values.
- TDD evidence: before the factory change, the new controls produced seven
  expected `TypeError` failures for the unsupported `plan_days` keyword; after
  the change they passed.
- `uv run pytest tests/test_schemas.py`: passed (248 tests).
- `uv run ruff format --check tests/factories.py tests/test_schemas.py`:
  passed; `uv run ruff check tests/factories.py tests/test_schemas.py`: passed;
  `uv run mypy`: passed.

### Task 2: Parse and retain duration in the `/plan` workflow

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write failing tests for initial responses covering durations 1 and 7,
  whitespace, first-comma-only parsing, embedded preference commas, and each
  existing no-preference synonym.
- [x] Write failing tests for missing commas, non-integers, booleans/textual
  numbers, 0, 8, and empty preferences; assert state is unchanged and neither
  preference interpretation nor planner invocation occurs.
- [x] Write failing tests proving clarification replies are appended in full
  without reparsing a duration and `/plan` reset replaces both retained duration
  and preference.
- [x] Write failing tests proving new `/plan` states require `N, preference`,
  while legacy in-flight `AWAITING_PREFERENCE` states accept preference-only
  text as seven-day requests.
- [x] Write failing tests for `N, no preference` followed by stored-rule,
  constraint, or horizon clarification, including comma-preserving replies,
  duplicate updates, and compare-and-swap loss without interpreter or planner
  side effects.
- [x] Update the `/plan` prompt with `N, preference` examples and implement one
  typed deterministic initial-response parser that splits only at the first
  comma and returns bounded, actionable validation messages.
- [x] Persist the accepted duration in every successful state transition and
  retain it across clarification, compare-and-swap retries, duplicate updates,
  and retry-ready recovery.
- [x] Update generation-start and retry messages to avoid claiming every plan
  is weekly.
- [x] Run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 3.

**Task 2 acceptance evidence:**

- Every allowed initial form preserves its selected horizon and preference
  text, and every prohibited form is side-effect free.
- Generated-plan progress, command/help, and README wording no longer
  promises seven days; fixed seven-day meal-history wording is unchanged.

**Task 2 verification evidence (2026-08-25):**

- Initial-input tests cover one- and seven-day requests, whitespace,
  first-comma-only splitting, embedded preference commas, and the aliases
  `anything`, `no preference`, `no preferences`, `none`, and `whatever`.
- Invalid-input tests cover missing comma, empty duration or preference,
  booleans, textual and fractional numbers, zero, and eight; they verify no
  state transition, interpreter, planner, persistence, or success delivery.
- Generation progress now says `Working on your {N}-day meal plan.` when the
  duration is known and `Working on your meal plan.` otherwise. The `/plan`
  command catalogue, `/help`, and README generated-plan descriptions are
  duration-neutral; fixed seven-day meal-history wording remains unchanged.
- `uv run pytest tests/test_bot_handler.py`: passed (339 tests, 2 existing
  Pydantic serializer warnings).
- Focused command/help and README verification:
  `uv run pytest tests/test_telegram_commands.py tests/test_readme.py` passed
  (13 tests).
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed. The full `uv run pytest` run passed 1350 tests
  with two stale-SAM-artifact failures for copied `bot_handler.py`; artifact
  regeneration remains completion-plan Task 4 work.

### Task 3: Validate preference rules against the requested date horizon

**Files:**
- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_dietary_rules.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write failing unit tests for exact, minimum, and maximum rule capacities
  over 1-day, intermediate, and 7-day horizons, including meal-type scopes.
- [x] Write failing tests for weekday-scoped rules whose named days are wholly
  outside, partly inside, and wholly inside the consecutive requested dates.
- [x] Write failing tests proving absent-weekday `AT_MOST` and `EXACTLY 0`
  rules are feasible, while positive `EXACTLY` and `AT_LEAST` counts fail only
  when their per-weekday capacity is insufficient.
- [x] Write failing tests for partly covered weekday sets, unscoped and
  meal-type-scoped capacities, bounded deterministic clarification text, and
  no planner invocation for genuinely impossible rules.
- [x] Write failing bot tests showing impossible current or stored rules request
  clarification before planner invocation and retain `plan_days` for the reply.
- [x] Write or retain explicit safety tests proving current preferences can
  supplement or replace conflicting stored preferences but cannot replace,
  weaken, or bypass dietary constraints.
- [x] Add a typed horizon-feasibility function that evaluates resolved rules
  against concrete start/end dates and produces bounded, user-safe
  clarification without changing general rule-priority semantics.
- [x] Call horizon validation after constraint-first priority resolution and
  before transitioning the request to `GENERATING`.
- [x] Run `uv run pytest tests/test_preferences.py tests/test_dietary_rules.py
  tests/test_bot_handler.py`; all tests must pass before Task 4.

### Task 4: Propagate exact duration through prompts and planner lifecycle

**Files:**
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_parser.py`

- [x] Write failing prompt tests for every duration boundary, exact inclusive
  date ranges, dynamic day-count contracts, and unchanged constraint priority
  language.
- [x] Write failing planner tests proving `plan_days` survives event parsing,
  initial generation, manual retry, automatic repair, and legacy events that
  omit it.
- [x] Write failing parser/planner tests accepting structurally valid shorter
  plans but rejecting a generated plan whose length differs from the requested
  context before persistence or display.
- [x] Extend `build_plan_prompt()`, `generate_plan()`, `_invoke_planner()`,
  repair serialization, and Lambda event handling with the bounded duration.
- [x] Make generation and repair prompts request exactly the selected number of
  consecutive days and enforce that count as an application-owned validation
  condition.
- [x] Preserve stale-request, idempotency, two-attempt repair, logging privacy,
  and failure-retention behavior while changing duration propagation.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_parser.py
  tests/test_planner_handler.py`; all tests must pass before Task 5.

### Task 5: Preserve duration through revisions and downstream plan use

**Files:**
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py` if focused tests expose a fixed-week
  assumption outside the generalized `week_end` property
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_telegram_api.py` if rendering assertions require updated
  duration-neutral text

- [x] Write failing revision prompt and planner tests proving whole-draft
  replacements request and return exactly the current plan's duration and
  reject a different-length replacement.
- [x] Write failing integration regressions for active-plan coverage at the
  first day, actual last day, and first date after a short plan.
- [x] Write failing workflow tests for target-day edits, confirmation, display,
  meal outcomes, and grocery generation using one-day and intermediate plans.
- [x] Generalize revision wording and validation to derive required duration
  from the current plan, while preserving permanent constraints and prior
  planning instructions.
- [x] Remove any downstream fixed-seven assumptions exposed by the tests; keep
  storage keys, atomic publication, and active-plan epoch semantics unchanged.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_planner_handler.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`;
  all tests must pass before Task 6.

**Task 5 verification evidence (2026-08-25):**

- Added short-plan active-boundary coverage in `tests/test_dynamo.py` for
  one- and three-day plans, including final-day lifecycle writes and rejection
  of the first day after the dynamic end date.
- Added downstream workflow coverage in `tests/test_bot_handler.py` and
  `tests/test_telegram_api.py` for final-day edits, out-of-range edits,
  confirmation and grocery dispatch, `/today`, check-in outcomes, and bounded
  short-plan rendering. Telegram plan output now uses duration-neutral `Meal
  Plan` wording; persisted date keys, epochs, CAS writes, atomic publication,
  and stale callbacks were unchanged.
- `uv run pytest tests/test_prompts.py tests/test_planner_handler.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`:
  passed (680 tests, 2 existing Pydantic serializer warnings).
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed.

### Task 6: Verify acceptance criteria and repository quality gates

**Files:**
- Modify: affected Python and test files only when a gate exposes a defect
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`

- [x] Verify initial input uses exactly one first-comma split and accepts only
  durations 1 through 7 with non-empty preference text.
- [x] Verify day 1 maps to today, all later plan dates are consecutive, and
  generation, repair, retry, revision, persistence, delivery, and groceries use
  exactly the selected duration.
- [x] Verify current preferences can complete or override stored preferences
  while dietary constraints remain inviolable.
- [x] Verify historical seven-day plan, conversation-state, and planner-event
  payloads still validate without migration.
- [x] Run `uv run ruff format .`, then `uv run ruff format --check .`.
- [x] Run `uv run ruff check .` and fix all findings.
- [x] Run `uv run mypy` and fix all findings.
- [x] Run `uv run pytest` and fix all failures.
- [x] Record verification results and mark completed plan items immediately.

**Task 6 verification results (2026-08-25):**

- Acceptance coverage: `tests/test_bot_handler.py`, `tests/test_schemas.py`,
  `tests/test_preferences.py`, `tests/test_dietary_rules.py`,
  `tests/test_planner_handler.py`, `tests/test_parser.py`,
  `tests/test_dynamo.py`, and `tests/test_telegram_api.py` cover first-comma
  parsing, consecutive duration propagation, legacy payload compatibility,
  priority resolution, and dietary-constraint safety.
- `uv run ruff format .`: passed; 94 files left unchanged.
- `uv run ruff format --check .`: passed; 94 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: passed; 1254 passed, 2 existing Pydantic serializer
  warnings.

**Final Task 4 gate evidence (2026-08-25):**

- Focused suites passed before artifact generation:
  `uv run pytest tests/test_schemas.py` (248 passed),
  `uv run pytest tests/test_bot_handler.py tests/test_telegram_commands.py
  tests/test_readme.py` (360 passed, 2 existing Pydantic serializer
  warnings), and
  `uv run pytest tests/test_prompts.py tests/test_planner_handler.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`
  (680 passed, 2 existing Pydantic serializer warnings).
- `uvx --from aws-sam-cli sam build --beta-features`: passed; generated
  `.aws-sam/build/template.yaml` and Lambda artifacts. The required artifact
  checks confirmed the generated template and copied `meal_planner` sources
  match the current source tree.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: passed (26
  tests), including artifact source, import, compatibility, and template
  checks.
- `uv run ruff format .`: passed; 97 files left unchanged.
- `uv run ruff format --check .`: passed; 97 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: passed; 1366 passed, 2 existing Pydantic serializer
  warnings.
- `git diff --check`: passed.

**Remediation integration note (2026-08-25):** The companion hardening plan
completed the migration-safe `duration_collected` phase, legacy
preference-only handling, and operator-aware horizon feasibility. The
primary variable-duration implementation tasks remain the source of truth
for end-to-end duration propagation through prompts, planner events,
revisions, persistence, delivery, and groceries.

The final documentation pass updated `README.md` for the new `N, preference`
input and retained-duration clarification behavior. The documentation-focused
suite and repository gates passed: `uv run pytest tests/test_template.py` (26
passed), `uv run ruff format --check .` (94 files already formatted),
`uv run ruff check .`, `uv run mypy` (20 source files), and `uv run pytest`
(1254 passed, 2 existing Pydantic serializer warnings).

### Task 7: [Final] Update user documentation and archive the plan

**Files:**
- Modify: `README.md`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: relevant command-description test if text is asserted
- Move: `docs/plans/2026-08-25-support-variable-length-meal-plans.md` to
  `docs/plans/completed/2026-08-25-support-variable-length-meal-plans.md`

- [x] Update `/plan` documentation and examples for `1, no preference`,
  `3, fish for dinner`, first-comma semantics, consecutive dates, clarification
  retention, and dietary priority.
- [x] Replace inaccurate weekly/seven-day command descriptions with
  duration-neutral language while leaving unrelated seven-day meal-history
  rules unchanged.
- [x] Run focused tests for any command-description behavior changed here.
- [x] Re-run `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest` after documentation-adjacent changes.
- [x] Confirm every implementation checkbox is complete and move this plan to
  `docs/plans/completed/`.

**Task 7 verification evidence (2026-08-25):**

- `README.md` documents `1, no preference`, `3, fish for dinner`, splitting
  only at the first comma, retained duration during clarification, and the
  dietary constraint/current/stored preference priority. The plan contract and
  focused workflow tests directly verify consecutive requested dates.
- `/plan` is described as creating or retrying a meal plan in
  `README.md` and `src/meal_planner/telegram/commands.py`; the fixed
  seven-day meal-history description remains unchanged.
- `uv run pytest tests/test_telegram_commands.py tests/test_readme.py`:
  passed (24 tests).
- The documented full quality gates and artifact checks passed in Task 4;
  no Post-Completion manual or external items are implementation checkboxes.

## Post-Completion

**Manual verification**

- In Telegram, start `/plan` and try `1, no preference`,
  `3, fish, pasta, and salad`, invalid lengths, and a preference that triggers a
  clarification reply containing a comma.
- Confirm the displayed date range, generated meals, revision behavior, and
  grocery list stop at the selected final day.

**GitHub and version-control follow-up**

- Work on a dedicated feature branch; never push or merge directly to
  `master`.
- Create a Conventional Commit such as
  `feat(plan): support variable plan lengths (#63)` using the issue created for
  this plan.
- Open a pull request and comment on the associated GitHub issue with a summary
  of the completed work and a link to the commit or pull request.

**External systems**

- Verify the deployed bot and planner Lambdas use the same artifact so the new
  planner-event field is supported on both sides. Legacy-event defaulting makes
  rolling deployment safe; no data migration or dependency update is expected.

[issue-63]: https://github.com/nsal/meal-planner-bot/issues/63
