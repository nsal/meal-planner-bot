# Remediate Protein and Fibre Target Review Findings

## Overview

Remediate the two actionable findings from the independent review of the
per-member protein and fibre target implementation in commits `ed8da93` and
`2108954`.

First, preserve a saved member's optional protein and fibre targets when a
conversational profile update supplies replacement member data without those
optional fields. This must also work across a multi-turn household-size change,
where a persisted draft may temporarily omit the replacement member list.

Second, allow calorie, protein, and fibre amendments for an existing member
whose name contains a numeric token, such as `Child 1`. Resolve the final input
suffix against stored member identities so the fix does not weaken the
deterministic add-member grammar or guess at an unknown identity.

## Context

- **Completed original plan:**
  `docs/plans/completed/2026-08-24-add-protein-fibre-targets.md`.
- **Original baseline:** commit `6ebc1bdf85d2ba24aca97c98767c9ec55c7d0ad0`.
- **Reviewed implementation:** commits `ed8da93` and `2108954`.
- `src/meal_planner/llm/prompts.py` correctly instructs conversational
  extraction to omit optional targets unless the user explicitly supplies
  them. The persistence boundary must therefore distinguish omitted nested
  fields from supplied values.
- `BotHandler._update_profile()` in `src/meal_planner/bot_handler.py` currently
  replaces `family_members` wholesale. An incoming calorie-only member object
  is validated with optional targets defaulted to `None`, which can erase
  stored targets when the replacement list is persisted.
- `BotHandler._parse_profile_name_calories()` and
  `BotHandler._parse_profile_target_change()` reject any decimal token in a
  parsed name. That ambiguity guard is appropriate for adding an unknown
  member but prevents deterministic edits to a stored identity such as
  `Child 1`.
- Existing focused coverage is in `tests/test_bot_handler.py`; the prompt
  omission contract is covered in `tests/test_prompts.py`.
- Project quality gates are Ruff formatting and linting at 80 columns, strict
  Mypy, Pytest, and `git diff --check`, all run through `uv` where applicable.
- The worktree contains unrelated modifications in `scripts/deploy.py` and
  `tests/test_deploy.py`, plus unrelated untracked documentation. Preserve
  those paths and do not attribute, reformat, stage, or remove them.

## Review Findings Covered

### P1: Preserve stored targets during conversational member updates

The extraction contract allows protein and fibre to be omitted from a current
update, but `_update_profile()` replaces `family_members` with models whose
omitted optional fields have defaulted to `None`. A calorie-only conversational
update can therefore silently clear saved nutrient targets. A household-size
change can expose the same defect across turns when the draft temporarily has
no member list and replacement members arrive later.

### P2: Allow numeric tokens in existing member names

The deterministic parsers reject all numeric name tokens. Existing members
such as `Child 1` therefore cannot use `Child 1 1800`, `Child 1 120`, or
`Child 1 none`, even though the stored member identity makes the intended name
and final value suffix unambiguous.

## Development Approach

- **Testing approach: TDD.** Add the smallest focused regression first, run it
  to demonstrate the reported defect, implement the minimal correction, and
  rerun the focused tests before proceeding.
- Complete the P1 preservation fix before the P2 parser fix. This follows
  severity order and ensures later amendment regressions run against correct
  target-preservation semantics.
- Preserve immutable Pydantic copy patterns and use nested
  `model_fields_set` only at the incoming-update boundary, before omitted
  values become indistinguishable from explicit `None` values.
- Treat the persisted profile draft as the newest source of member values when
  it contains members, with the saved profile as fallback for identities not
  present in the draft. This fallback is required for multi-turn household-size
  changes that intentionally clear the draft member list.
- Preserve an explicitly supplied target value, including explicit `None`, and
  inherit only a target field omitted from the incoming member object.
- Resolve edit input against actual stored identities. Keep add-member parsing
  and its rejection of ambiguous partial numeric suffixes unchanged.
- Keep profile writes atomic and preserve all controlled validation, unknown
  member, ambiguous legacy identity, stale workflow, and persistence-failure
  behavior.
- Use `uv run` for Python tools and Ruff as the only formatter. Do not proceed
  to the next task while its focused tests fail.
- Keep this plan synchronized during implementation. Mark checkboxes only
  after the stated evidence exists and record any scope deviation before
  implementing it.

## Testing Strategy

- **Conversational preservation:** exercise `_apply_intent_metadata()` and
  `_update_profile()` with saved nutrient targets and incoming member objects
  that omit one or both optional fields.
- **Field intent:** prove omitted fields inherit saved values, explicitly
  supplied numeric values replace saved values, and explicit `None` clears a
  saved optional target rather than being overwritten by inheritance.
- **Household-size workflow:** simulate the real two-turn draft flow: change
  `people_count` without members, then supply a replacement member list. Prove
  matching saved members retain omitted targets and a genuinely new member
  receives no invented targets.
- **Numeric identities:** run calorie, protein, fibre, and `none` amendment
  inputs against a profile containing `Child 1`; assert only the requested
  field changes and all unrelated member data remains intact.
- **Error paths:** retain controlled handling for unknown names, duplicate
  case-folded legacy identities, malformed suffixes, invalid bounds, and
  invalid profile categories, with no persistence on failure.
- **Regression gate:** run the focused handler tests after each task, then the
  prompt and profile-edit integration modules, Ruff format and lint, Mypy, the
  complete Pytest suite, and `git diff --check`.
- New tests must not use skips, xfails, network calls, live LLMs, or Telegram
  API access.

## Solution Overview

At the conversational persistence boundary, merge only omitted optional target
fields for incoming members that match a unique member identity from the
current draft or saved profile. Use stripped, case-folded identity semantics
already provided by `BotHandler._member_identity()`. The incoming name and
calorie target remain authoritative. Explicit incoming protein or fibre values
also remain authoritative, including explicit clearing with `None`. New or
unmatched members retain their supplied data and receive no inferred targets.

For deterministic amendments, separate add-member parsing from existing-member
target parsing. Addition must retain the documented two-field or four-field
grammar and its ambiguity checks. Existing-member changes may parse the final
calorie, nutrient, or `none` suffix and then resolve the remaining text against
the stored member list. A numeric token is valid only because the resulting
name uniquely matches a stored identity; unknown and duplicate legacy
identities continue to fail without writes.

## Technical Details

### Conversational member merge semantics

Implement a small typed helper near `_update_profile()` that receives incoming
members plus the available draft and saved-profile members. Its effective
contract is:

1. Build source candidates using `_member_identity()`.
2. Prefer the current persisted draft member for a unique identity when it is
   available; otherwise use the unique saved-profile member.
3. For each incoming member, inspect its nested `model_fields_set` before any
   full `model_dump()` replacement loses omission intent.
4. Copy `protein_target` and `fibre_target` from the source member only when
   that exact field was omitted from the incoming object.
5. Keep explicit numbers and explicit `None` unchanged.
6. Leave unmatched members unchanged so no optional target is invented.
7. Do not guess when a source identity is ambiguous. Preserve the existing
   duplicate-identity validation and return a controlled non-write result where
   ambiguity prevents safe inheritance.

The merge must happen before assigning the incoming `family_members` value to
the accumulated draft data. Existing behavior that clears draft members after
a changed `people_count` without replacement members remains intact; the saved
profile provides fallback values when the replacement list arrives later.

### Existing-member suffix resolution

Introduce or adapt a typed profile-aware parser/resolver used only by
`CHANGE_CALORIES`, `CHANGE_PROTEIN`, and `CHANGE_FIBRE`:

- calorie changes accept exactly one final decimal value in the existing
  1-10,000 range and never accept `none`;
- protein and fibre changes accept exactly one final decimal value in the
  existing 1-1,000 range or exact case-insensitive `none`;
- the remaining prefix is compared with stored member identities using the
  existing stripped, case-folded semantics;
- exactly one matching stored identity is required;
- malformed values retain the existing format response, no match retains the
  existing not-found response, and duplicate legacy identities retain the
  existing ambiguity response;
- successful edits continue to use `model_copy(update=...)` and
  `_profile_with_updates()` so unrelated targets and members are preserved.

Keep `_parse_profile_name_calories()` as the add-member grammar boundary. Do
not make `John 2000 1` silently become either a partial target form or a new
numeric-token member name.

## Implementation Steps

### Task 1: Preserve omitted targets in conversational member replacement

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._update_profile`,
`BotHandler._member_identity`, `ProfileUpdateEntities.family_members`

**TDD sequence:**

- [x] **Failing or missing test first:** Add a regression in
  `tests/test_bot_handler.py` that starts with a saved profile containing both
  nutrient targets, applies a conversational calorie-only replacement for the
  same members, and asserts calories update while omitted protein and fibre
  remain unchanged.
- [x] Add parameterized field-intent cases proving an omitted target inherits,
  an explicitly supplied target replaces the saved value, and explicit `None`
  clears only that target. Assert unrelated member fields and members remain
  unchanged.
- [x] Add a two-turn household-size regression: first submit a changed
  `people_count` without `family_members`, persist the resulting draft, then
  submit replacement members with omitted optional targets. Assert matching
  saved members retain their targets and a new member keeps both targets
  `None`.
- [x] Add controlled edge cases for unmatched identities and ambiguous legacy
  source identities. Assert the merge never copies targets to the wrong member
  and unsafe input performs no final profile write.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that the current wholesale assignment either changes preserved
  targets to `None` or fails the multi-turn preservation assertions.
- [x] **Implementation change:** Add a typed helper in
  `src/meal_planner/bot_handler.py` that merges only omitted
  `protein_target`/`fibre_target` fields from unique matching draft or saved
  members. Call it from `_update_profile()` before replacing accumulated
  `family_members` data.
- [x] Ensure persisted-draft members take precedence over saved-profile
  members, while the saved profile remains a fallback after a household-size
  turn has cleared the draft member list.
- [x] Preserve explicit incoming values and `None`, duplicate validation,
  completeness checks, draft persistence, final profile persistence, and all
  existing failure messages and write boundaries.
- [x] **Verify the new test passes:** Run the focused new preservation tests in
  `tests/test_bot_handler.py` and require every test to pass without skip or
  xfail.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_prompts.py` and resolve
  all failures before Task 2.

**Acceptance criteria:**

- [x] A calorie-only conversational update cannot erase a saved protein or
  fibre target for the same member.
- [x] Omitted fields inherit values; explicitly supplied values, including
  explicit `None`, remain authoritative.
- [x] A multi-turn household-size update preserves targets for matching saved
  members even when the intermediate draft has no member list.
- [x] New or unmatched members receive no inferred nutrient target.
- [x] Ambiguous identities never cause target data to be copied between
  members or persisted unsafely.
- [x] Existing profile completion, duplicate-name, draft, and persistence
  behavior remains green.

**Task 1 verification and blocker notes:**

- Pre-implementation `uv run pytest tests/test_bot_handler.py -k
  profile_member_replacement_preserves_omitted_nutrient_targets` failed with
  saved protein and fibre targets replaced by `None`. The expanded pre-
  implementation preservation run had 8 failures and 1 already-passing
  unmatched-member test.
- Post-implementation focused preservation tests passed (`9 passed`), the
  handler and prompt regression modules passed (`263 passed`), and the full
  suite passed (`1026 passed, 2 skipped`; the skips were unchanged).
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check` all passed. No blockers or scope deviations remain for
  Task 1. Tasks 2 and 3 were not attempted.

### Task 2: Resolve numeric-name amendments against stored identities

**Severity:** P2

**Depends on:** Task 1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._parse_profile_name_calories`,
`BotHandler._parse_profile_target_change`,
`BotHandler._apply_family_amendment`, `BotHandler._member_identity`

**TDD sequence:**

- [x] **Failing or missing test first:** Add handler regressions using a stored
  member named `Child 1` for `CHANGE_CALORIES`, `CHANGE_PROTEIN`, and
  `CHANGE_FIBRE`, including both a numeric fibre update and case-insensitive
  `none` clearing.
- [x] Assert each successful input resolves the complete stored name, changes
  only the requested field, preserves the member's other targets, and uses the
  existing atomic profile/state write path.
- [x] Add error regressions for an unknown numeric-token name, malformed or
  extra suffix tokens, out-of-range values, and duplicate case-folded legacy
  identities. Assert controlled messages and no write.
- [x] Add an add-member regression proving the existing two-field/four-field
  grammar and rejection of ambiguous partial numeric suffixes remain
  unchanged; this task permits numeric tokens only when resolving an existing
  stored identity.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that the current decimal-name-token checks return format errors for
  valid `Child 1` calorie, protein, and fibre amendments.
- [x] **Implementation change:** Introduce or adapt a typed profile-aware
  suffix resolver that parses one operation-specific final value and resolves
  the remaining name against `profile.family_members`. Route all three change
  operations through it while leaving add-member parsing separate.
- [x] Keep calorie bounds at 1-10,000, nutrient bounds at 1-1,000, and `none`
  support limited to protein and fibre. Preserve case-insensitive identity and
  clear-token behavior.
- [x] Preserve unknown-member, ambiguous-legacy-name, malformed-input,
  invalid-category, stale-state, and persistence-failure behavior without
  invoking the LLM.
- [x] **Verify the new test passes:** Run the focused numeric-name amendment
  tests in `tests/test_bot_handler.py` and require every test to pass without
  skip or xfail.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_schemas.py \
  tests/test_router.py tests/test_telegram_api.py` and resolve all failures
  before final verification.

**Acceptance criteria:**

- [x] `Child 1 1800` changes calories for the stored member `Child 1`.
- [x] `Child 1 120` and `Child 1 none` change or clear the selected nutrient
  target for that stored member.
- [x] Every single-field edit preserves all unrelated member targets and other
  household members.
- [x] Numeric tokens are accepted only after unique resolution against a
  stored identity; unknown and duplicate identities remain controlled errors.
- [x] Add-member grammar does not reinterpret partial target forms or broaden
  beyond the completed original plan.
- [x] Existing nonnumeric names and all profile-edit operations remain
  backward compatible.

**Task 2 verification and blocker notes:**

- Pre-implementation focused run `uv run pytest tests/test_bot_handler.py -k
  'numeric_member_name or add_member_numeric_name'` failed 7 tests: valid
  numeric-name amendments returned format errors, and unknown numeric-token
  names were rejected as malformed. Six tests already passed, including the
  two add-member rejection regressions.
- Post-implementation focused numeric-name tests passed (`15 passed`), and
  `uv run pytest tests/test_bot_handler.py tests/test_schemas.py
  tests/test_router.py tests/test_telegram_api.py` passed (`561 passed`).
- The complete `uv run pytest` suite passed (`1042 passed, 2 skipped`); the
  two skips are unchanged documented template skips. `uv run ruff format
  --check .`, `uv run ruff check .`, `uv run mypy`, and `git diff --check`
  all passed.
- No blockers or scope deviations remain for Task 2. Task 3 was not
  attempted. Unrelated deployment changes and documentation remain
  preserved.

### Task 3: Verify acceptance criteria and archive the remediation record

**Severity:** Release gate

**Depends on:** Tasks 1 and 2

**Files:**

- Modify, then move on completion:
  `docs/plans/2026-08-24-add-protein-fibre-targets-review-remediation.md`
  to `docs/plans/completed/`

**Verification sequence:**

- [x] **Failing or missing test first:** No additional production behavior is
  introduced in this verification-only task. Confirm Tasks 1 and 2 added
  test-first coverage for every review scenario before running the aggregate
  gates; add a missing regression first if any acceptance criterion lacks one.
  Direct inspection confirmed nine P1 preservation cases and fifteen P2
  numeric-name cases covering the listed review scenarios. They are ordinary
  tests using controlled repository/LLM mocks, with no skip, xfail, network,
  live LLM, or Telegram API dependency.
- [x] **Expected failure:** No new red test is expected solely for this task.
  Any failing acceptance or regression test demonstrates incomplete work in
  Task 1 or Task 2 and must be corrected there before continuing.
- [x] **Implementation change:** No production change is planned. If a gate
  exposes a new defect within the two findings, add a focused failing test,
  update this plan, and make the smallest correction in the owning task's
  symbols. Do not broaden scope silently.
- [x] Re-run the P1 and P2 regressions together and confirm they are not
  skipped, xfailed, order-dependent, or dependent on live services.
- [x] **Verify the new tests pass:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_prompts.py \
  tests/test_schemas.py tests/test_router.py tests/test_telegram_api.py`.
- [x] Run `uv run ruff format --check .` and require no formatting changes.
- [x] Run `uv run ruff check .` and resolve all diagnostics.
- [x] Run `uv run mypy` and resolve all static typing errors.
- [x] **Relevant regression tests:** Run `uv run pytest` and require the full
  suite to pass, allowing only unchanged, documented pre-existing skips.
- [x] Run `git diff --check` and inspect the accumulated diff. Confirm the
  unrelated `scripts/deploy.py`, `tests/test_deploy.py`, and untracked
  documentation remain untouched and unattributed.
- [x] Verify both findings and every acceptance criterion are covered, then
  update this plan with actual commands, results, deviations, blockers, and
  residual risks.
- [x] Move only this completed plan into `docs/plans/completed/`. Do not modify
  the archived original plan.

**Task 3 verification and blocker notes:**

- The exact aggregate focused command passed: `598 passed` with no skips or
  xfails. The P1 and P2 regressions ran together in this suite and use only
  controlled test doubles; no order, network, live LLM, or Telegram service
  dependency was observed.
- `uv run ruff format --check .` passed (`85 files already formatted`),
  `uv run ruff check .` passed (`All checks passed!`), and `uv run mypy`
  passed (`Success: no issues found in 19 source files`).
- `uv run pytest` passed (`1042 passed, 2 skipped`). The two skips are the
  unchanged, documented template skips in `tests/test_template.py`.
- `git diff --check` passed. Path inspection found executor-attributable
  changes only in `src/meal_planner/bot_handler.py`,
  `tests/test_bot_handler.py`, and this remediation plan. The pre-existing
  changes in `scripts/deploy.py`, `tests/test_deploy.py`,
  `docs/plans/completed/2026-08-22-submit-meals-confirmation-ux-review-remediation.md`,
  and `docs/plans/ideas/` remained untouched and unattributed.
- No production change, dependency change, lockfile change, deviation, or
  blocker was introduced by Task 3. Residual risks are limited to the
  manual live Telegram/LLM verification retained in Post-Completion and the
  documented dependence on stripped, case-folded member identity semantics.

**Acceptance criteria:**

- [x] Focused and full test suites pass with no new skips or xfails.
- [x] Ruff format, Ruff lint, Mypy, and `git diff --check` pass.
- [x] The final diff is limited to the two reviewed implementation paths,
  their tests, and this remediation plan.
- [x] Both actionable review findings have direct regression coverage and
  verified corrections.
- [x] The completed original plan and all unrelated pre-existing work remain
  unchanged.

## Explicit Non-Goals

- Do not change `FamilyMember`, `ProfileUpdateEntities`, DynamoDB schemas,
  migrations, dependencies, lockfiles, meal-plan schemas, or nutrient
  validators.
- Do not change the conversational extraction wording unless a focused test
  proves it no longer preserves the established omission contract.
- Do not infer, default, calculate, validate, or repair calorie, protein, or
  fibre compliance.
- Do not add fuzzy member-name matching, rename behavior, internal-whitespace
  normalization, or numeric-token support to deterministic member creation.
- Do not alter Telegram labels, keyboards, callback payloads, or profile
  rendering for these fixes.
- Do not redesign draft persistence, profile completeness, atomic profile/state
  writes, or stale-workflow concurrency handling.
- Do not modify the completed original plan, `README.md`, `docs/prompt.md`,
  deployment files, or unrelated documentation.
- Do not create a GitHub issue, comment on an issue, commit, push, merge,
  deploy, or perform any other external action as part of this remediation
  planning or implementation record.

## Post-Completion

### Manual verification retained from the original plan

- In a non-production Telegram environment, update calories for a saved member
  with protein and fibre targets and confirm both optional targets remain
  visible afterward.
- Change household size conversationally, resupply a mixed old/new member list,
  and confirm matching old members retain targets while the new member has no
  invented targets.
- For a stored member named `Child 1`, change calories and both nutrient
  targets, then clear one target through the Family menu.
- Generate and revise a draft, then inspect captured LLM requests to confirm
  supplied targets remain present with the intended priority wording.
- Treat target compliance as best effort; this remediation adds no measured
  nutrient-compliance guarantee.

### External actions and residual risks

- Live-model and Telegram verification remain manual because automated tests
  use controlled boundaries and do not call external services.
- Matching relies on the existing stripped, case-folded member identity. Any
  future change to identity normalization must rerun both preservation and
  numeric-name regressions.
- Delivery through a feature branch and pull request, issue updates, commits,
  pushes, merges, and deployment remain separate, explicitly authorized
  actions.
