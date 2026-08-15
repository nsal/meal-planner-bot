# Family Name Semantics and Empty Profile Value Normalization

> Tracked by GitHub issue
> [#26](https://github.com/nsal/meal-planner-bot/issues/26).

## Overview

Fix two related conversational onboarding failures. The top-level profile
`name` remains the persisted compatibility field, but every prompt and user
message will describe it as the household's family name so it cannot be
confused with `family_members[*].name`. Profile list fields will also accept
clear natural-language no-value responses such as `"none"` and
`"no restrictions"`, normalize them to `[]`, and continue rejecting ambiguous
non-empty scalar values.

This change preserves the DynamoDB schema, existing profile records, Telegram
commands, and LLM metadata key names. It makes multi-turn onboarding complete
successfully when a family first supplies its members and later supplies its
family name and an explicit absence of restrictions.

## Context (from discovery)

- Files/components involved: `src/meal_planner/models/schemas.py`,
  `src/meal_planner/llm/prompts.py`, `src/meal_planner/bot_handler.py`,
  `tests/test_schemas.py`, `tests/test_prompts.py`,
  `tests/test_bot_handler.py`, and `README.md`.
- `UserProfile.name` and `ProfileUpdateEntities.name` currently represent the
  top-level profile label, while every `FamilyMember` has a separate `name`.
  The prompt calls both concepts `name`, so the LLM can extract a member named
  Nick without satisfying the top-level required field.
- `_update_profile` correctly merges persisted drafts across turns, but it
  validates incoming entities before merging. A scalar value such as
  `{"restrictions": "none"}` therefore fails because list fields accept only
  lists or `None`.
- `None` currently means an onboarding field is still missing, while `[]`
  means the user explicitly has no values. That distinction must remain.
- The project uses Pydantic models as the LLM mutation boundary, focused
  handler and prompt tests, Ruff at 80 columns, strict mypy, `uv`, and SAM
  artifact freshness checks.

## Development Approach

- **Testing approach**: TDD; add failing regression tests before each code
  change.
- Complete each task fully before moving to the next.
- Make small, focused changes and preserve backward compatibility.
- Every code task must add or update tests for both successful and rejected
  inputs.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation scope changes.
- Keep `name` as the persisted and LLM metadata key; do not add a migration or
  duplicate `family_name` field.
- Normalize only unambiguous no-value phrases; do not silently coerce an
  arbitrary scalar such as `"vegetarian"` into a list.

## Testing Strategy

- **Schema unit tests**: cover each list-based profile field, case and
  whitespace normalization, field-specific no-value phrases, preserved
  `None`, preserved lists, and rejected non-empty scalar strings.
- **Prompt unit tests**: prove the conversational contract distinguishes the
  family name from individual household-member names and still emits the
  existing `name` metadata key.
- **Handler unit tests**: reproduce the reported two-turn conversation and
  prove it persists one complete profile with all three members unchanged and
  restrictions normalized to an empty list.
- **Failure tests**: retain invalid-calorie and persistence failure behavior,
  and prove ambiguous scalar list values produce a safe rejection without
  corrupting the saved draft.
- **Deployment artifact tests**: rebuild SAM because Lambda source changes,
  then require current source in the generated artifacts.
- **Project gates**: full pytest, Ruff lint and format checks, strict mypy,
  SAM validation/build, required artifact tests, and `git diff --check`.
- There is no UI test suite; Telegram behavior is tested at the handler
  boundary and manually in a non-production chat after deployment.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document blockers or failed assumptions with a ⚠️ prefix.
- Keep this plan synchronized with implementation and verification results.
- Move this plan to `docs/plans/completed/` only after every local gate passes.

## Solution Overview

Retain `UserProfile.name` and `ProfileUpdateEntities.name` to avoid a data
migration, but define the field as the family name at every human-facing and
LLM-facing boundary. Update `/start`, `/profile`, missing-field feedback, and
the conversational prompt so `name` means a household label while
`family_members[*].name` means an individual's name.

Add a Pydantic pre-validation rule to `ProfileUpdateEntities` for
`allergies`, `dietary_preferences`, `restrictions`, and `goals`. It will map a
small explicit vocabulary of no-value strings to `[]`, preserve `None` and
proper lists, and leave all other values for normal validation to reject. By
placing normalization at the typed mutation boundary, every conversational
profile update receives the same behavior without weakening persisted
`UserProfile` validation.

## Technical Details

- The JSON field remains `name`; only its documented and displayed meaning
  changes to `family name`.
- The family name is not inferred from the first household member because the
  household label and a member's given name are distinct concepts.
- `family_members` remains a list of objects containing `name` and
  `calorie_target`, with length equal to `people_count` before completion.
- `None` remains the sentinel for a missing onboarding field. An empty list
  remains an explicit answer that the household has no entries for that
  category.
- Normalize case-insensitive, whitespace-trimmed generic phrases such as
  `none`, `no`, `nothing`, `n/a`, and `not applicable`, plus exact
  field-specific forms such as `no allergies`, `no dietary preferences`,
  `no preferences`, `no restrictions`, and `no goals`.
- Trivial trailing sentence punctuation may be ignored when matching these
  exact phrases. Do not use substring matching, because input such as
  `no peanuts` is meaningful data rather than an empty answer.
- Proper lists remain unchanged and continue through existing `ShortText`
  validation. Empty strings and arbitrary scalar strings remain invalid unless
  explicitly added to the no-value vocabulary during implementation and
  recorded in this plan.
- Missing-field feedback should use `family name` rather than exposing the
  internal field name `name`.

## What Goes Where

- **Implementation Steps**: schema normalization, family-name prompt and
  message semantics, multi-turn regression coverage, documentation, SAM
  rebuild, and project verification.
- **Post-Completion**: deploy to a non-production stack, repeat the reported
  Telegram conversation, inspect the saved profile and logs, update the GitHub
  issue, and publish through the normal pull-request workflow.

## Implementation Steps

### Task 1: Normalize explicit no-value profile entities

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`

- [x] add failing parameterized tests for accepted generic and field-specific
  no-value phrases across allergies, dietary preferences, restrictions, and
  goals
- [x] add failing tests proving `None`, `[]`, and valid lists retain their
  existing meanings
- [x] add failing tests proving ambiguous non-empty scalar strings remain
  validation errors
- [x] implement a typed Pydantic pre-validator that maps only the agreed exact
  no-value vocabulary to `[]`
- [x] verify persisted `UserProfile` validation and family-member validation
  are unchanged
- [x] run `uv run pytest tests/test_schemas.py`; all tests must pass before
  Task 2

### Task 2: Make family-name semantics explicit

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_bot_handler.py`

- [x] add failing prompt tests requiring `name` to be described as the family
  name and `family_members[*].name` as individual member names
- [x] add failing handler tests requiring `/start`, `/profile`, and missing
  onboarding feedback to say `family name`
- [x] update the conversational prompt while retaining the JSON metadata key
  `name`
- [x] update user-facing onboarding and profile messages without changing
  stored field names or command behavior
- [x] add failure-oriented assertions preventing internal labels such as bare
  `name` or `family_members` from leaking into onboarding guidance
- [x] run
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py`; all tests
  must pass before Task 3

### Task 3: Lock in the reported multi-turn onboarding scenario

**Files:**

- Modify: `tests/test_bot_handler.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `src/meal_planner/models/schemas.py`

- [x] add a failing regression test for a first turn containing household size,
  three named members with calorie targets, no allergies, no preferences, and
  goals, while family name and restrictions remain missing
- [x] add the second turn with `name` set to `Nick` and restrictions supplied
  as a no-value scalar
- [x] assert the completed profile uses `Nick` as its family name, preserves
  Nick, Val, and Mike as members, preserves all calorie targets, and stores
  restrictions as `[]`
- [x] assert the draft is deleted only after the complete profile is saved
- [x] add a rejection test proving an ambiguous scalar does not overwrite or
  delete the accumulated draft
- [x] run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 4

### Task 4: Document the corrected onboarding contract

**Files:**

- Modify: `README.md`
- Verify: `tests/test_prompts.py`
- Verify: `tests/test_bot_handler.py`

- [x] update the user workflow to request a family name separately from every
  household member's name and calorie target
- [x] document that natural-language no-value answers are stored as empty
  categories rather than missing fields
- [x] add or update prompt and handler assertions for any documented behavior
  changed during this task
- [x] run
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py`; all tests
  must pass before Task 5

### Task 5: Rebuild and verify deployment artifacts

**Files:**

- Verify: `template.yaml`
- Rebuild: `.aws-sam/build/`
- Verify: `tests/test_template.py`

- [x] verify no DynamoDB schema, Lambda environment variable, IAM policy, or
  dependency change was introduced
- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] rebuild with `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`; all artifact
  tests must pass before Task 6

### Task 6: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/models/schemas.py`
- Verify: `src/meal_planner/llm/prompts.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `tests/`
- Verify: `README.md`

- [x] verify the stored `name` field is unchanged and consistently presented
  as the family name
- [x] verify member names and calorie targets remain distinct and unchanged
- [x] verify only explicit no-value phrases normalize to empty lists and
  ambiguous scalars fail closed
- [x] run `uv run pytest` and fix failures until the full suite passes
- [x] run `uv run ruff check .` and fix every finding
- [x] run `uv run ruff format --check .` and fix every formatting difference
- [x] run `uv run mypy` and fix every type error
- [x] run `git diff --check` and fix every whitespace error

### Task 7: Finalize plan tracking and documentation

**Files:**

- Modify: this plan with final deviations and verification results
- Modify: `AGENTS.md` only if implementation discovers a reusable project rule
- Move: this plan to `docs/plans/completed/`

- [x] record implementation deviations and final verification results in this
  plan
- [x] update `AGENTS.md` only if a reusable rule not already covered by project
  standards is discovered
- [x] confirm every implementation and verification checkbox is complete
- [x] rerun `uv run pytest` after final documentation changes
- [x] move this plan to `docs/plans/completed/` only after all local checks pass

## Implementation Notes and Verification Results

- No deviations from the planned scope were needed. The persisted `name` key,
  DynamoDB records, commands, Lambda configuration, IAM policy, and dependency
  set remain unchanged.
- Added exact no-value normalization at the `ProfileUpdateEntities` typed
  mutation boundary. Generic phrases are `none`, `no`, `nothing`, `n/a`, and
  `not applicable`; field-specific phrases remain exact and do not use
  substring matching.
- Updated conversational and plan prompts, `/start`, `/profile`, missing-field
  feedback, README workflow documentation, and regression coverage to call the
  top-level value the family name and member values individual names.
- Follow-up review restored `people_count` to the advertised conversational
  profile-update entities and added a prompt regression assertion for it.
- Verification passed: `uv run pytest` (246 tests),
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` (21 tests),
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  `uvx --from aws-sam-cli sam validate --lint --region us-east-1`,
  `uvx --from aws-sam-cli sam build --beta-features`, and `git diff --check`.

## Post-Completion

**Manual verification:**

- Deploy the updated Bot Lambda to a dedicated non-production stack.
- In a Telegram test chat, submit the reported household details in one turn,
  then answer `Family name: Nick. Restrictions: none.` in the second turn.
- Confirm the bot reports that the profile was saved instead of returning a
  generic mutation failure.
- Run `/profile` and verify the family name, three individual names, calorie
  targets, goals, and empty restrictions are displayed correctly.
- Inspect CloudWatch logs for validation warnings and DynamoDB for one complete
  profile with no stale profile draft.

**External system updates:**

- Commit with a Conventional Commit message containing the associated issue
  number, push a dedicated branch, and open a pull request.
- Comment on the GitHub issue with the commit or pull-request link and local
  and deployed verification results, then close it when the work is complete.
