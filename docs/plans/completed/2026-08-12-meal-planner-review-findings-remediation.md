# Meal Planner Review Findings Remediation

## Overview

- Associated GitHub issue:
  [#18](https://github.com/nsal/meal-planner-bot/issues/18).
- Address all four actionable findings from the release-readiness review:
  preserve onboarding context across conversational turns, reject expired
  drafts during confirmation, restrict confirmed-plan edits to the active
  plan, and make every planned meal uniquely addressable by the existing
  day-and-meal-type key.
- Keep the remediation focused on the existing handler, prompt, and Pydantic
  model boundaries so the DynamoDB item shape and Telegram callback format do
  not change.
- Preserve current valid workflows for profile completion, current and future
  draft editing, active confirmed-plan editing, grocery refreshes, and meal
  check-ins while rejecting stale or ambiguous state.

## Context (from discovery)

- **Files/components involved:**
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/llm/prompts.py`,
  `src/meal_planner/models/schemas.py`,
  `tests/test_bot_handler.py`,
  `tests/test_prompts.py`,
  `tests/test_schemas.py`,
  `tests/test_parser.py`, and
  `tests/test_planner_handler.py`.
- **Related patterns found:** incomplete profiles already persist as
  `ProfileUpdateEntities`; `get_latest_plan` provides planning context and
  draft selection; `get_active_plan` provides the confirmed plan covering
  today; optimistic revisions protect edits and grocery refreshes; check-in
  callbacks address a meal by week, day, and meal type.
- **Dependencies identified:** Pydantic model validation, DynamoDB repository
  lookups, conversational prompt construction, plan parsing, and asynchronous
  grocery finalization. No dependency, SAM template, DynamoDB migration, or
  callback payload change is required.
- **Review evidence:** the existing 140-test suite, Ruff lint/format checks,
  strict mypy, and locked dependency checks pass, but do not cover persisted
  draft prompt context, expired draft confirmation, expired confirmed-plan
  editing, or duplicate meal types within one day.

## Development Approach

- **Testing approach:** TDD. Add a failing regression test for each reviewed
  defect before implementing its fix.
- Complete each task fully before moving to the next and keep changes small and
  focused.
- Every task that changes code must add or update tests for the modified paths,
  including success and error cases.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation discoveries change its scope
  or chosen design.
- Preserve the existing DynamoDB schema and Telegram callback format. Existing
  valid plan data remains compatible; plans containing duplicate meal types
  are treated as invalid because they cannot be addressed unambiguously.
- Use only `uv run` for project tools, Ruff for formatting/linting, and the
  strict mypy configuration in `pyproject.toml`.

## Testing Strategy

- **Handler tests:** prove persisted onboarding drafts reach the next LLM
  prompt; current/future drafts remain editable; expired drafts cannot be
  confirmed or edited; active confirmed plans remain editable and trigger the
  exact grocery refresh; expired or future confirmed plans are rejected.
- **Prompt tests:** prove active profile data and incomplete draft data are
  represented distinctly and that omitted fields remain visibly missing
  rather than receiving fabricated defaults.
- **Schema/parser tests:** prove unique breakfast, lunch, dinner, and snack
  entries are accepted while duplicate meal types for a day invalidate both
  direct model construction and LLM plan parsing.
- **Planner tests:** prove a generated plan with ambiguous duplicate meal types
  is not persisted or sent to Telegram.
- **Regression checks:** run the focused files after each task, followed by
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and
  `uv run mypy` during final verification.
- **End-to-end tests:** this repository has no UI/browser end-to-end suite.
  Handler and DynamoDB integration tests provide the relevant workflow
  coverage.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document issues or blockers with a `⚠️` prefix.
- Keep this plan synchronized with the implementation and test state.

## Solution Overview

- Load the persisted `ProfileUpdateEntities` draft before invoking the
  conversational LLM and pass it to the prompt builder alongside the active
  profile. Render pending onboarding data as partial state instead of coercing
  it into a complete `UserProfile`; `_update_profile` remains responsible for
  merging and validating the resulting mutation.
- Add small handler-level selection helpers for confirmable and editable plans.
  A draft is eligible while its week has not expired, including a future draft.
  A confirmed plan is editable only when `get_active_plan` resolves it for
  today. Grocery-error retries continue to resolve only through the active-plan
  lookup.
- Add a `PlanDay` invariant requiring each `MealType` to appear at most once.
  The existing callback and edit tuple `(week_start, day, meal_type)` then
  identifies exactly one meal without adding IDs or changing persisted field
  names.

## Technical Details

### Conversational onboarding context

- `BotHandler.handle_conversational` loads the active profile and persisted
  profile draft before building the prompt.
- `build_conversational_prompt` accepts an optional
  `ProfileUpdateEntities` value and renders only fields actually known in the
  draft. It distinguishes saved profile values from pending onboarding/update
  values so the LLM can continue the current collection flow.
- Missing lists and family members stay explicitly missing; they must not be
  rendered as empty completed values because that would allow premature
  profile completion.
- Persistence continues to prefer the stored draft during `_update_profile`,
  maintaining the existing merge and validation behavior.

### Plan eligibility

- Define draft eligibility from the typed plan dates: a draft is stale when
  `plan.week_end < date.today()`.
- Confirmation selects the latest non-expired draft; if no eligible draft is
  available, it may retry only the active confirmed plan in grocery `error`.
- Editing selects the latest eligible draft when present. Otherwise it selects
  `get_active_plan`; an expired or future confirmed plan is never edited or
  sent for grocery refresh.
- Existing optimistic revision and expected-status conditions remain the final
  guard against races after plan selection.
- Rejection paths do not call `confirm_plan`, `update_meal`, `retry_grocery`,
  `fail_grocery`, or the Planner Lambda.

### Meal identity invariant

- `PlanDay` validates that its `meals` contain no repeated `MealType` values.
- The maximum of four meals per day remains unchanged and aligns with the four
  supported enum values.
- Invalid LLM plans fail through the existing `WeeklyPlan` parser path and are
  handled by the current controlled plan-generation failure response.
- No meal ID, DynamoDB migration, or callback payload version is introduced.

## What Goes Where

- **Implementation Steps:** test and implement onboarding prompt context, plan
  eligibility, meal-type uniqueness, full acceptance verification, and local
  documentation updates.
- **Post-Completion:** manually exercise the conversational workflows and link
  the implementing commit or pull request from the associated GitHub issue.

## Implementation Steps

### Task 1: Preserve persisted onboarding context in each LLM turn

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [x] add failing handler tests proving a stored partial profile draft is
  loaded before the LLM call and appears in the generated prompt
- [x] add failing prompt tests for partial draft fields, missing fields, family
  members, and the coexistence of a saved profile with pending updates
- [x] extend `build_conversational_prompt` with typed optional draft context
  that renders only known pending values
- [x] update `handle_conversational` to load and pass the draft while retaining
  the active profile for mutation merging
- [x] add success tests proving a later turn can complete a stored draft and
  regression tests proving conversations without a draft are unchanged
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_prompts.py` and
  fix all failures before Task 2

### Task 2: Restrict confirmation and editing to eligible plans

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add failing tests proving an expired draft cannot be confirmed, cannot
  invoke grocery generation, and returns a controlled stale-plan response
- [x] add failing tests proving expired and future confirmed plans cannot be
  edited or trigger grocery refreshes
- [x] add failing success tests proving current/future drafts remain editable
  and the active confirmed plan remains editable with revision checking and an
  exact-week grocery refresh
- [x] add focused handler helpers that select a non-expired latest draft or the
  active confirmed plan according to the operation
- [x] preserve active grocery-error retry behavior and add regression tests for
  the interaction between stale drafts and active retry selection
- [x] assert every rejection path leaves repository mutation methods and the
  Planner Lambda uncalled
- [x] run `uv run pytest tests/test_bot_handler.py` and fix all failures before
  Task 3

### Task 3: Enforce unique meal types within every plan day

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_planner_handler.py`

- [x] add failing schema tests proving duplicate meal types in one day are
  rejected and four distinct supported meal types remain valid
- [x] add failing parser tests proving an otherwise complete LLM plan with a
  duplicated daily meal type returns no `WeeklyPlan`
- [x] add a `PlanDay` model invariant that compares normalized `MealType`
  values and rejects duplicates with a specific validation error
- [x] add planner-handler regression tests proving an ambiguous generated plan
  is neither persisted nor sent as a usable draft
- [x] retain existing callback/edit behavior and document through tests that
  `(week_start, day, meal_type)` now identifies at most one meal
- [x] run `uv run pytest tests/test_schemas.py tests/test_parser.py`
  `tests/test_planner_handler.py` and fix all failures before Task 4

### Task 4: Verify remediation acceptance criteria

**Files:**
- Modify: `docs/plans/2026-08-12-meal-planner-review-findings-remediation.md`
- Modify: relevant test files only if an acceptance gap is discovered

- [x] verify a persisted partial onboarding draft informs the next LLM turn and
  can be completed without resubmitting previously supplied fields
- [x] verify expired drafts cannot be confirmed or edited and inactive
  confirmed plans cannot be edited
- [x] verify valid draft and active confirmed-plan edits retain optimistic
  revision and grocery-refresh behavior
- [x] verify duplicate meal types cannot enter storage through direct model
  construction or plan generation
- [x] run the full suite with `uv run pytest` and fix every failure
- [x] run `uv run ruff check .` and `uv run ruff format --check .`
- [x] run strict type checking with `uv run mypy`
- [x] run `uv lock --check` and confirm no dependency changes were introduced

### Task 5: [Final] Update documentation and close plan tracking

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-08-12-meal-planner-review-findings-remediation.md`
- Move to:
  `docs/plans/completed/2026-08-12-meal-planner-review-findings-remediation.md`

- [x] update README onboarding guidance to state that known draft fields carry
  across turns and only missing fields need to be supplied
- [x] document that expired drafts and inactive confirmed plans cannot be
  mutated
- [x] document the one-meal-per-type-per-day plan invariant without describing
  a schema or callback migration
- [x] record final verification evidence and implementation deviations in this
  plan
- [x] confirm no new project-wide conventions require an `AGENTS.md` update
- [x] mark every completed item, move this plan to `docs/plans/completed/`, and
  rerun `uv run pytest` after the documentation move

### Verification evidence

- Focused handler, prompt, schema, parser, and planner tests: 78 passed.
- Full suite: 151 passed with `uv run pytest`.
- `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`
  passed.
- `uv lock --check` passed with no dependency changes.
- SAM artifacts were rebuilt with `uvx --from aws-sam-cli sam build
  --beta-features` so the artifact freshness tests covered the implementation.
- No implementation deviations or new project-wide conventions were found;
  no `AGENTS.md` update is required.

## Post-Completion

### Manual verification

- Start onboarding, provide only a name and household size, then provide member
  targets and constraints over later Telegram messages without repeating the
  earlier values.
- Attempt to confirm and edit a draft after its seven-day window, verifying no
  Planner Lambda invocation or DynamoDB mutation occurs.
- Keep an expired/future confirmed plan alongside an active confirmed plan and
  verify conversational edits affect only the active plan.
- Generate a provider response with duplicate daily meal types and verify the
  bot sends the controlled invalid-plan response rather than a keyboard with
  ambiguous callbacks.

### External system updates

- Add a completion comment to the associated GitHub issue after implementation,
  linking the Conventional Commit and pull request.
- Deploy through the existing protected-branch pull-request workflow; do not
  push or merge directly to `master`.
