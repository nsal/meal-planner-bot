# Profile Amendment Workflow

## Overview

Add a deterministic, button-led way for a user to amend their saved household
profile from `/profile` (GitHub issue #49). The command will display the
current profile with **Amend profile** and **Close** buttons. Amendments use
inline buttons, while each concrete change is one guided text message, such as
`John 1500`.

Replace the separate persisted `allergies` and `restrictions` fields with one
`dietary_constraints` field. Existing DynamoDB profiles must retain their data
through a lazy, backwards-compatible migration. The result avoids LLM parsing
for edits and keeps the profile safe for meal planning.

## Context

- The project is a typed Python 3.14 AWS Lambda Telegram bot, using Pydantic
  models and DynamoDB persistence.
- `src/meal_planner/bot_handler.py` owns command, callback, conversational,
  and durable-workflow orchestration; `/profile` currently only renders text.
- `src/meal_planner/telegram/api.py` already sends inline keyboards and
  `src/meal_planner/router.py` validates namespaced check-in callbacks.
- `src/meal_planner/models/schemas.py` has strict workflow/profile models;
  `src/meal_planner/db/dynamo.py` persists profiles and CAS-protected
  conversation states.
- Profile changes currently enter via LLM metadata. This feature adds a
  dedicated deterministic workflow instead of repurposing that path.

## Development Approach

- **Testing approach:** TDD. For each task, add or update its focused tests
  before implementation, then run the relevant test file(s) to green before
  starting the next task.
- Keep edits small and preserve compatibility with profiles persisted before
  this change.
- Use a dedicated profile-edit state rather than an LLM-derived edit intent.
- Run `uv run ruff format .`, `uv run ruff check .`, `uv run mypy`, and
  `uv run pytest` before considering the feature complete.
- Update this plan if scope or its decisions change during implementation.

## Testing Strategy

- Unit-test schema migration, normalization, profile invariants, callback
  parsing, keyboard payloads, and each deterministic text grammar.
- Add handler tests for the full `/profile` → callback → text save → menu
  sequence, Back/Done/Close/cancel paths, stale state, and controlled errors.
- Add Dynamo repository tests for legacy records and state-guarded atomic
  profile/workflow writes.
- The project has no browser/e2e test harness; Telegram interaction behavior
  is verified by handler, router, and API payload tests.

## Solution Overview

The profile is the authoritative document. A profile-edit conversation state
records only its menu or selected operation and expires like existing durable
workflows. Inline callback payloads select navigation actions; the following
text is interpreted solely by the selected operation, never by the LLM.

`dietary_constraints` includes allergies, intolerances, religious
restrictions, and diet exclusions. Dietary preferences are positive likes or
preferred eating styles, and goals remain desired outcomes. Existing allergy
and restriction values merge into constraints, retain their original order,
and are de-duplicated case-insensitively. A normal subsequent profile write
stores only the canonical field.

## Technical Details

- Add `dietary_constraints` to `UserProfile` and `ProfileUpdateEntities`; use
  a pre-validation compatibility adapter for legacy DynamoDB records that
  merges `allergies` followed by `restrictions` when the new field is absent.
  Remove legacy fields from canonical model dumps and prompts.
- The remediation design intentionally removes profile revisions. The bot is
  the only supported profile writer, and the active conversation state is the
  concurrency authority. Each deterministic amendment writes the profile and
  its next workflow state in one state-guarded DynamoDB transaction.
- Add a `PROFILE_EDIT` workflow kind with menu/awaiting-input state and typed
  category/operation values. Its model validation must exclude unrelated meal
  and plan fields and require an operation only while awaiting input.
- Use compact `profile:` callback actions for root, category, operation,
  back, done, and close. Validate payload length and exact values alongside
  existing check-in callback parsing.
- Add Telegram API helpers to render the profile view/root actions, category
  menu, and operation menu. After every successful deterministic mutation,
  render the category menu again.
- Family-member add and calorie change accept `name calories`; removal accepts
  an exact name. Names are unique case-insensitively, changing/removing an
  unknown member is rejected, and the last member cannot be removed.
  Successful family mutations keep `people_count` equal to member count.
- Constraint, preference, and goal add/remove accept one non-empty item.
  Duplicate additions and missing removals report a controlled result without
  writing. Matching is case-insensitive while retained display spelling stays
  stable.

## Implementation Steps

### Task 1: Canonicalize dietary constraints and legacy profile reads

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/factories.py`

- [x] Write schema tests for canonical `dietary_constraints`, legacy
  allergy/restriction merging, ordering, de-duplication, and no-data cases.
- [x] Replace legacy profile/update fields with the canonical constraint field
  and implement validated compatibility normalization for existing records.
- [x] Update conversational and planner prompt rendering to use only
  `Dietary constraints`.
- [x] Update fixtures and prompt tests to construct the canonical model and
  assert no legacy labels are rendered.
- [x] Run the focused schema and prompt tests; they must pass before Task 2.
- ⚠️ Repository-wide verification previously exposed five existing handler
  tests that still reference
  legacy profile fields; those integration call sites are outside Task 1 and
  remain for the later workflow tasks.

### Task 2: Define profile-edit and callback contracts

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_router.py`

- [x] Write tests for valid and invalid profile-edit workflow state shapes,
  including expired/incorrect operation states.
- [x] Add typed profile-edit category/operation enums, workflow kind, and
  workflow steps with strict state validation.
- [x] Write router tests for every accepted `profile:` callback, malformed
  payloads, and the 64-byte Telegram limit.
- [x] Implement a dedicated callback parser that cannot accept check-in data
  or arbitrary profile actions.
- [x] Run the focused schema and router tests; they must pass before Task 3.

### Task 3: Initial revision-aware profile saves (superseded)

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_schemas.py`

- [x] Write repository tests for creating canonical profiles, atomically
  updating a matching revision, rejecting stale revisions, and first writes
  of legacy revision-less records.
- [x] Add a validated profile revision with a zero default for legacy reads.
- [x] Extend repository profile persistence with an expected-revision write
  path that conditionally increments the revision without breaking onboarding
  saves.
- [x] Ensure successful canonical writes omit legacy allergy/restriction
  attributes and failed conditional writes do not alter data.
- [x] Run the focused DynamoDB and schema tests; they must pass before Task 4.

> Superseded by the remediation plan: profile revisions and guarded standalone
> profile saves will be removed in favor of a single-writer profile model and
> an atomic transaction guarded by the exact observed conversation state.

### Task 4: Render profile amendment controls and navigate callbacks

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_telegram_api.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write API tests for the profile summary, root controls, category menu,
  and operation menus, including compact callback data.
- [x] Add Telegram rendering helpers for root, category, and operation menus.
- [x] Change `/profile` to render canonical profile text and Amend/Close
  controls, replacing any unfinished workflow only through the existing CAS
  pattern.
- [x] Dispatch validated profile callbacks, acknowledge them promptly, and
  implement Back, Done, and Close without unintended profile writes.
- [x] Run the focused Telegram API and handler tests. The later remediation
  regression run passed the handler, router, and Telegram API coverage after
  canonical onboarding was restored.
- The five legacy-field onboarding failures recorded here were resolved by
  remediation Tasks 1 and 2; the historical blocker is closed.

### Task 5: Apply deterministic profile amendments

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write handler tests before implementation for every successful add,
  remove, and calorie change, including return to the category menu.
- [x] Implement parsers for `name calories`, member name, and one-item text
  inputs; route active profile-edit text before the conversational LLM path.
- [x] Implement immutable profile transformations with case-insensitive
  duplicate/match checks and family-count invariants.
- [x] Apply updates through revision-aware persistence and provide controlled
  messages for malformed input, duplicate/missing values, stale writes, and
  protected last-member removal. This two-write implementation is superseded
  by the remediation plan's state-guarded profile/workflow transaction.
- [x] Run focused handler tests; the 17 Task 5 amendment tests pass. The full
  handler file still has five pre-existing onboarding failures caused by
  legacy `allergies`/`restrictions` integration outside Task 5.

### Task 6: Verify cross-workflow behavior and update user documentation

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `README.md`

- [x] Write regression tests proving `/cancel` clears an edit state and
  `/plan`, `/submit_meals`, and `/profile` safely replace an active edit.
- [x] No integration fix was needed: existing revision-checked replacement and
  cancellation semantics pass the new regression tests without changing
  unrelated workflow behavior.
- [x] Document `/profile` amendment navigation and one-message input examples
  in the README.
- [x] Run the relevant handler and README tests; the four Task 6 regression
  tests and four README tests pass. The full handler file retains five known
  onboarding failures from legacy `allergies`/`restrictions` references.

### Task 7: Verify acceptance criteria

**Files:**
- Modify: `docs/plans/2026-08-20-profile-amendment-workflow.md`

- [x] Verify every agreed category and operation is accessible by buttons and
  every edit takes one guided text action. The focused acceptance run passed
  171 tests, covering all four categories, all nine category-operation
  combinations, callback validation, deterministic edits, and guided prompts.
- [x] Verify legacy allergies and restrictions retain their information after
  read and after canonical re-save. Schema and DynamoDB migration tests passed;
  legacy values merge in order on read, and canonical writes contain only
  `dietary_constraints`. The focused DynamoDB migration test intentionally
  changes the canonical list before re-save, so an unchanged re-save retaining
  every legacy value remains a coverage gap for a later test improvement.
- [x] Re-run `uv run ruff format .` and `uv run ruff check .` after
  remediation. Ruff format left 79 files unchanged and linting passed.
- [x] Run `uv run mypy` and the complete suite with `uv run pytest` after
  remediation. Mypy passed for 19 source files and pytest passed with 800
  tests, with 2 unrelated template checks skipped.
- [x] Record any scope changes or blockers in this plan before Task 8.
- Remediation Tasks 1–8 closed the five onboarding failures, removed profile
  revision machinery, added state-guarded atomic amendments, hardened member
  identity and callback acknowledgement, and aligned `/profile` documentation.

### Task 8: Complete delivery documentation

**Files:**
- Modify: `README.md` (if verification identifies a gap)
- Move: `docs/plans/2026-08-20-profile-amendment-workflow.md` to
  `docs/plans/completed/2026-08-20-profile-amendment-workflow.md`

- [x] Confirm the README accurately documents the delivered profile experience;
  the command reference now describes `/profile` as view-and-amend and uses
  the canonical dietary-constraint terminology.
- [x] Mark the implementation tasks complete as each is actually finished;
  the superseding remediation tasks completed the historical gaps and all
  original-plan checklist items are now satisfied.
- [x] Move this plan to `docs/plans/completed/` only after every verification
  command passes. The plan was moved after the authorized final gate passed.
- The final automated gate is green. Manual Telegram timing and live workflow
  checks remain outstanding because no live Telegram verification environment
  was used for this execution.

## Post-Completion

Manual Telegram verification: open `/profile`, inspect the combined constraints
line, use every navigation button, submit `John 1500`, validate an error case,
then make a second amendment before tapping Done. Confirm an existing account
with legacy data retains all former allergies and restrictions.

Align the Telegram command catalogue and `/help` description for `/profile`
with the README. No deployment configuration change is required. Create a
conventional-commit feature branch and pull request; do not push or merge
directly to `master`.
