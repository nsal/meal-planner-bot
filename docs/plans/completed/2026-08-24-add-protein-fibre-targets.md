# Add Per-Member Protein and Fibre Targets

Associated issue: [#58](https://github.com/nsal/meal-planner-bot/issues/58)

## Overview

Extend each family member in a saved user profile with optional daily protein
and fibre targets, measured in grams. Surface those targets throughout profile
onboarding and amendment, and include every supplied target in initial meal-plan
generation and whole-plan revision prompts alongside calories.

This is intentionally prompt-guided nutrition adjustment. Generated meals keep
the existing output schema, and the application does not calculate or validate
daily calorie, protein, or fibre totals. Missing targets never block profile
completion or plan generation, preserving compatibility with all existing and
new profiles.

## Context (from discovery)

- `src/meal_planner/models/schemas.py` defines `FamilyMember`, profile update
  entities, profile completeness, and the profile editor operation contract.
- `src/meal_planner/llm/prompts.py` independently renders saved and pending
  member targets in conversational, generation, and revision prompts; the
  repeated formatting is a drift risk for the new fields.
- `src/meal_planner/bot_handler.py` owns conversational profile persistence and
  deterministic member add/remove/calorie amendments. Current member replacement
  reconstructs a calorie-only model and therefore must be changed to preserve
  optional nutrient targets.
- `src/meal_planner/telegram/api.py` renders profiles and the Family amendment
  buttons/prompts. `src/meal_planner/router.py` already validates operations via
  the enum, so new enum values fit the existing callback grammar.
- `src/meal_planner/db/dynamo.py` serializes and validates whole Pydantic profile
  documents. Optional fields require no table migration or dependency change.
- Existing coverage is concentrated in `tests/test_schemas.py`,
  `tests/test_prompts.py`, `tests/test_router.py`, `tests/test_telegram_api.py`,
  and `tests/test_bot_handler.py`. Project gates are Ruff at 80 columns, strict
  Mypy, and Pytest, all run through `uv`.

## Development Approach

- **Testing approach: TDD.** For every task, add or update the smallest focused
  tests first, run them to demonstrate the missing behavior, implement only the
  required production change, then rerun the focused suite successfully before
  proceeding.
- Complete each numbered task fully before starting the next task. Every code
  task includes explicit success and error/edge-case tests.
- Keep changes small and typed, follow the existing Pydantic and immutable
  profile-copy patterns, and format all Python at 80 columns.
- Maintain backward compatibility: no migration, no new required onboarding
  answers, and no change to the definition of a complete profile.
- Keep nutrient handling prompt-guided. Do not add meal macro fields, nutrient
  databases, daily-total validation, tolerances, or automatic repairs.
- Preserve the exact existing `name calories` member-add form while adding the
  unambiguous `name calories protein fibre` form.
- Keep this plan synchronized during implementation. Mark completed items
  immediately, prefix newly discovered tasks with `➕`, and record blockers
  with `⚠️`.
- If scope changes, update this plan before implementing the changed scope.

## Testing Strategy

- Schema tests prove valid values, broad bounds, optional defaults, legacy
  document loading, serialization, and unchanged completeness semantics.
- Prompt tests cover all saved/draft target combinations and both initial plan
  generation and revision, including the explicit best-effort priority wording.
- Router/schema tests prove the two new Family-only operations and reject them
  for unrelated categories without weakening Telegram callback validation.
- Telegram tests cover profile rendering, labels, callback payloads, input
  guidance, and missing-target presentation.
- Handler tests cover both member-add formats, multiword names, independent
  updates, clearing, bounds, unknown/ambiguous members, and preservation of
  unrelated target values.
- Run the focused test file after each red/green task. Before completion, run
  `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `uv run pytest`.
- The repository has no browser-based end-to-end test suite; handler and
  Telegram API tests provide the interaction-level coverage for this feature.

## Solution Overview

Add `protein_target` and `fibre_target` as nullable integer fields on
`FamilyMember`, each accepting 1 through 1,000 grams per day. Defaults remain
`None`, and `UserProfile.is_complete` continues to require only one member with
a calorie target per person.

Use one private member-target renderer inside the prompt module for saved
profiles and pending profile drafts. It will render calories and explicitly
render each optional nutrient as either a gram-per-day target or `not set`.
Generation and revision instructions will tell the LLM to adjust meal choices
and portions toward every supplied calorie, protein, and fibre target. Dietary
constraints and safety remain higher priority. Since the returned plan has no
protein/fibre estimates or per-member portions, failure to meet these targets
is not detected or repaired in this phase.

Extend the deterministic Family editor with separate `Change protein` and
`Change fibre` operations. Each accepts `<member name> <grams>` and accepts
`none` in place of grams to clear that optional target. Adding a member accepts
either `<name> <calories>` or `<name> <calories> <protein> <fibre>`; partial
positional nutrient input is rejected. All single-field changes preserve the
member's name and other targets.

## Technical Details

### Data contract

- `FamilyMember.protein_target: int | None = Field(default=None, ge=1,
  le=1_000)`
- `FamilyMember.fibre_target: int | None = Field(default=None, ge=1,
  le=1_000)`
- Field names use British `fibre` consistently with the requested product
  language; prompts and Telegram copy use `g/day` or `grams/day`.
- Missing fields in historical DynamoDB documents validate to `None`.
- `ProfileUpdateEntities.family_members` automatically carries the extended
  model; no new top-level fields are introduced.
- `UserProfile.is_complete` and member-count validation remain unchanged.

### Deterministic input grammar

- Add without optional targets: `<name> <calories>`.
- Add with both optional targets:
  `<name> <calories> <protein grams> <fibre grams>`.
- A three-number suffix is interpreted in exactly that order. A two-number
  suffix is rejected rather than guessing which nutrient was supplied.
- Change protein/fibre: `<name> <positive integer>`.
- Clear protein/fibre: `<name> none`, with supported no-value matching kept
  exact and case-insensitive.
- Names may contain spaces; parsing works from numeric/no-value suffixes.
- Calories remain required and cannot be cleared.

### Planning behavior and non-goals

- Saved and draft profile contexts expose all targets without inventing
  defaults for absent protein or fibre.
- Initial generation and whole-plan revision prompts apply all supplied targets
  to meal choice and portion guidance.
- The existing `PlannedMeal` contract remains unchanged, including its single
  `est_calories` field.
- Existing generated-plan validation continues to check meal completeness,
  positive calorie estimates, and exact food-frequency preferences only.
- There is no promise of measured nutrient compliance. Reported nutrient totals,
  per-member portions, tolerance checks, nutrition-database calculation, and
  automatic repair are deferred explicitly.

## Implementation Steps

### Task 1: Extend the family-member schema compatibly

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/factories.py`

- [x] first add schema tests for absent targets, independently supplied targets,
  both targets, serialization, and historical profile dictionaries that omit
  both new fields
- [x] first add boundary tests accepting 1 and 1,000 and rejecting zero,
  negatives, and values above 1,000 for each nutrient
- [x] add regression tests proving profile completeness still depends only on
  member count and calorie targets
- [x] run the new focused schema tests and record the expected pre-change
  failures caused by unknown/absent model fields
- [x] add optional typed `protein_target` and `fibre_target` fields to
  `FamilyMember` with inclusive 1–1,000 validation
- [x] update shared profile test factories with representative optional targets
  only where tests need them; keep a legacy-compatible fixture path
- [x] rerun `uv run pytest tests/test_schemas.py`; it must pass before Task 2

**Acceptance criteria:**

- Old profile documents load without modification.
- Either nutrient may be absent independently.
- Invalid supplied targets are rejected locally by Pydantic.
- Optional targets do not affect profile completeness.

### Task 2: Render nutrient targets consistently in LLM prompts

**Files:**
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_prompts.py`

- [x] first add parameterized prompt tests for members with neither, one, or
  both optional targets across saved and pending profile contexts
- [x] first add tests proving initial generation and whole-plan revision include
  the correct per-member targets and target-priority guidance
- [x] add regression tests proving absent targets are rendered as `not set` and
  are never replaced with inferred numerical defaults
- [x] run the new focused prompt tests and record the expected failures before
  production changes
- [x] introduce a small private member-target renderer and reuse it in saved
  conversational context, pending drafts, generation, and revision profile text
- [x] update generation and revision instructions to guide meal choices and
  portions toward supplied calorie, protein, and fibre targets while retaining
  dietary-constraint and safety precedence
- [x] preserve the existing plan JSON schema and add no nutrient-validation or
  repair claims
- [x] rerun `uv run pytest tests/test_prompts.py`; it must pass before Task 3

**Acceptance criteria:**

- All four prompt contexts render identical member-target semantics.
- Both generation paths receive every stored target.
- Prompt text accurately describes best-effort adjustment and priority.
- No plan-output field or validator is added.

### Task 3: Add Family-only protein and fibre edit operations

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/router.py` only if callback parsing needs an
  explicit compatibility change
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_router.py`

- [x] first add enum-validity tests for `CHANGE_PROTEIN` and `CHANGE_FIBRE` in
  the Family category and rejection in every non-Family category
- [x] first add callback parser tests accepting the two exact new operation
  payloads and rejecting malformed, wrong-category, and over-64-byte variants
- [x] run the new focused tests and record the expected pre-change failures
- [x] add stable serialized enum values `change_protein` and `change_fibre` and
  include them only in `ProfileEditOperation.is_valid_for(FAMILY)`
- [x] preserve the existing callback grammar and Telegram byte-limit guard
- [x] rerun `uv run pytest tests/test_schemas.py tests/test_router.py`; both must
  pass before Task 4

**Acceptance criteria:**

- Both new operations round-trip through the callback parser.
- Neither operation is valid for constraints, preferences, or goals.
- All documented profile callback payloads remain within Telegram's limit.

### Task 4: Expose targets and actions in Telegram profile UX

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_telegram_api.py`

- [x] first add profile-rendering tests for neither, one, and both optional
  targets, including explicit `not set` copy
- [x] first add keyboard tests for `Change protein` and `Change fibre`, their
  exact callback data, order, and Family-only visibility
- [x] first add operation-prompt tests covering numeric update syntax, `none`
  clearing, and both supported member-add forms
- [x] run the new Telegram API tests and record the expected pre-change failures
- [x] update saved-profile rendering with calories, protein, and fibre per
  member, clearly distinguishing absent optional values
- [x] add both buttons and focused operation guidance without changing unrelated
  category controls
- [x] update add-member guidance to document calories-only and full-target forms
- [x] rerun `uv run pytest tests/test_telegram_api.py`; it must pass before
  Task 5

**Acceptance criteria:**

- Users can see which nutrient targets are supplied or missing.
- Family controls expose three independent target-change actions.
- Instructions are sufficient to add, change, and clear supported targets.

### Task 5: Implement target-aware deterministic profile amendments

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] first add parser tests for calorie-only and full-target member creation,
  including multiword names and the 1/1,000 nutrient boundaries
- [x] first add malformed-input tests for partial positional targets,
  non-integers, extra values, zero, negative, and above-bound values
- [x] first add handler tests for independent protein and fibre changes and
  case-insensitive `none` clearing
- [x] first add error-path tests for unknown members, ambiguous legacy member
  identities, malformed clear phrases, and attempts to use the operations in an
  invalid category
- [x] first add preservation tests proving calorie changes retain protein and
  fibre, protein changes retain calories and fibre, fibre changes retain
  calories and protein, and removal/addition leaves other members unchanged
- [x] run the new focused handler tests and record the expected failures before
  production changes
- [x] extend member-add parsing to accept exactly the existing two-field form or
  the four-field form while parsing multiword names safely from the suffix
- [x] implement a typed target-change parser supporting one integer or exact
  `none`, and route the new enum operations through the Family amendment path
- [x] update family-member copies immutably without reconstructing away
  unrelated optional fields
- [x] retain atomic profile-and-state persistence and all existing stale/error
  behavior
- [x] rerun `uv run pytest tests/test_bot_handler.py`; it must pass before
  Task 6

**Acceptance criteria:**

- Existing `John 1500` input behaves exactly as before.
- `John Smith 2000 120 30` creates all three targets correctly.
- Nutrient targets can be changed or cleared independently.
- No single-field edit drops another saved target.
- Invalid input never changes the stored profile or workflow state.

### Task 6: Align conversational onboarding and user-facing guidance

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [x] first add conversational contract tests requiring optional
  `protein_target` and `fibre_target` inside each `family_members` entity and
  forbidding invented values
- [x] first add onboarding-message tests showing calories are required while
  protein and fibre are optional grams-per-day targets
- [x] add regression tests for multi-turn profile drafts that mix legacy members
  and members with one or both optional targets
- [x] run the new focused bot and prompt tests and record the expected failures
- [x] update conversational extraction instructions and onboarding copy to
  describe the optional per-member fields without making them completion gates
- [x] ensure profile draft merge and final persistence retain extended
  `FamilyMember` data without adding custom top-level merge behavior
- [x] rerun `uv run pytest tests/test_bot_handler.py tests/test_prompts.py`; both
  must pass before Task 7

**Acceptance criteria:**

- The LLM contract places targets on the correct family member.
- The bot never asks users to supply optional targets to unlock planning.
- Partial and completed profile drafts preserve explicitly supplied targets.

### Task 7: Verify acceptance criteria and project quality gates

**Files:**
- Modify: any files above only when required to correct a verification failure
- Modify: corresponding test file for every correction

- [x] verify every Overview requirement and explicit non-goal against the final
  diff
- [x] verify legacy profile loading, optional completeness, all edit grammars,
  field preservation, prompt propagation, and lack of nutrient-total validation
- [x] run `uv run ruff format .`, then `uv run ruff format --check .`
- [x] run `uv run ruff check .` and resolve all findings
- [x] run `uv run mypy` and resolve all strict typing findings
- [x] run the complete suite with `uv run pytest` and fix/retest any failures
- [x] inspect `git diff --check` and confirm no dependency or lockfile changes
- [x] add tests first for any defect discovered during verification, then apply
  the correction and rerun all gates

**Acceptance criteria:**

- Ruff formatting/checks, strict Mypy, and the entire Pytest suite pass.
- All changed behavior is covered by tests-first regression cases.
- No DynamoDB migration, dependency, meal schema, or nutrient validator exists.

### Task 8: Finalize documentation and implementation record

**Files:**
- Modify: `README.md`
- Modify: `docs/prompt.md`
- Modify: `docs/plans/2026-08-24-add-protein-fibre-targets.md`
- Move on completion:
  `docs/plans/2026-08-24-add-protein-fibre-targets.md` to
  `docs/plans/completed/2026-08-24-add-protein-fibre-targets.md`
- Modify: relevant README/documentation tests if documented text is asserted

- [x] first update or add documentation tests for any README text with an
  executable consistency contract
- [x] document optional per-member gram targets, deterministic edit forms,
  clearing behavior, and backward-compatible completeness in `README.md`
- [x] update `docs/prompt.md` examples and requirements to reflect prompt-guided
  protein/fibre adjustment without claiming validation
- [x] state plainly that missed calorie, protein, or fibre targets are not
  automatically detected in this phase
- [x] update this plan with actual implementation decisions, scope deviations,
  test commands, and final results
- [x] rerun documentation tests followed by `uv run pytest`
- [x] move the fully checked plan into `docs/plans/completed/`
- [x] use a Conventional Commit containing the associated issue number; never
  push or merge directly to `master`
- [x] comment on the associated GitHub issue with the completed work and a link
  to the commit or pull request

**Task 8 implementation record:**

- Implementation decisions: added a README section documenting optional
  per-member protein/fibre grams/day targets, both deterministic member-add
  forms, independent nutrient edits, case-insensitive `name none` clearing,
  preservation of other targets, and calorie-only completeness. Added a
  rendered member-target example and explicit best-effort/no-validation
  requirements to `docs/prompt.md`.
- Documentation contract: extended `tests/test_readme.py` with assertions for
  the new user-facing grammar and non-goals. The test was run first and failed
  because the README contract was absent, then passed after the documentation
  changes.
- Scope deviations: none. No implementation behavior, dependencies, lockfile,
  generated artifacts, or unrelated paths were changed by Task 8.
- Tests and quality gates:
  - `uv run pytest tests/test_readme.py` — 6 passed.
  - `uv run pytest` — 1,017 passed, 2 skipped.
  - `uv run ruff format --check .` — passed.
  - `uv run ruff check .` — passed.
  - `uv run mypy` — passed.
  - `git diff --check` — passed.
- Parent finalization completed: commit `ed8da93` uses the Conventional Commit
  format with issue `#58`, and the completed work was posted to GitHub issue
  `#58` with a link to that commit.

## Post-Completion

**Manual verification:**

- In a non-production Telegram environment, open a legacy profile and confirm
  both optional targets display as not set without blocking `/plan`.
- Add one calories-only member and one full-target member with a multiword name;
  change and clear both optional targets through the Family menu.
- Generate and revise a draft, then inspect application logs or captured LLM
  requests to confirm all supplied targets appear with the intended priority.
- Review a generated menu knowing that target compliance is best-effort and is
  not automatically measured.

**External system updates:**

- No DynamoDB table migration, dependency update, deployment configuration, or
  nutrition-provider integration is required.
- Deployment remains a separate, explicitly authorized action through a feature
  branch and pull request; never push or merge directly to `master`.
