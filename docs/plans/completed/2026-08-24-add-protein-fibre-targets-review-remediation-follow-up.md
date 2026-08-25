# Follow Up Protein and Fibre Target Ambiguity Remediation

## Overview

Remediate the remaining P2 review finding in conversational family-member
replacement. A saved profile or persisted draft with duplicate legacy member
identities currently blocks a replacement even when the incoming member
explicitly supplies both optional nutrient targets and therefore requires no
source lookup.

Make source-identity ambiguity relevant only when at least one omitted
`protein_target` or `fibre_target` must be inherited. Fully explicit incoming
values, including explicit `None`, remain authoritative and may safely repair
a duplicate legacy profile through the existing atomic update path. Omitted
targets must continue to fail without writes when inheritance would require
choosing between ambiguous source members.

This document is a remediation plan only. Do not implement it during the
remediation-planning phase.

## Context

- **Completed prior remediation:**
  `2026-08-24-add-protein-fibre-targets-review-remediation.md` in
  `docs/plans/completed/`.
- **Implementation:** `BotHandler._merge_omitted_member_targets()` in
  `src/meal_planner/bot_handler.py` currently builds draft and saved member
  identity indexes, then rejects duplicate matches before checking which
  incoming target fields were omitted.
- **Integration boundary:** `BotHandler._update_profile()` calls the helper
  before assigning incoming `family_members`, maps a `None` helper result to a
  controlled non-write response, and separately rejects duplicate identities
  in the incoming replacement.
- **Tests:** preservation, field-intent, persisted-draft precedence,
  saved-profile fallback, and ambiguous-source coverage live in
  `tests/test_bot_handler.py`.
- **Project gates:** Ruff formatting and linting at 80 columns, strict Mypy,
  Pytest, and `git diff --check`; Python commands run through `uv`.
- The worktree contains unrelated deployment and documentation changes. Do
  not modify, format, stage, remove, or attribute those paths.

## Review Finding Covered

### P2: Bypass ambiguous-source lookup when all targets are explicit

`BotHandler._merge_omitted_member_targets()` rejects duplicate matching draft
or saved members before determining whether inheritance is necessary. This
violates the established field-intent contract: explicit protein and fibre
values, including explicit clears with `None`, need no source and must not be
blocked by source ambiguity. Ambiguity must remain a controlled error whenever
an omitted target requires a source lookup.

## Development Approach

- **Testing approach: TDD.** Add separate saved-profile and persisted-draft
  regressions first, run them to demonstrate the defect, then make the
  smallest helper change and rerun the focused tests.
- Keep the change within `BotHandler._merge_omitted_member_targets()` and its
  `_update_profile()` integration unless a focused failing test proves an
  additional location is necessary.
- Determine each incoming member's omitted target fields from nested
  `model_fields_set` before resolving a draft or saved source.
- Skip source lookup entirely when both optional targets were explicitly
  supplied. Preserve the incoming values unchanged, including explicit
  `None`.
- When either target is omitted, preserve draft-first lookup, saved-profile
  fallback, exact stripped and case-folded identities, and controlled failure
  for an ambiguous source.
- Preserve incoming-replacement duplicate validation, profile completeness,
  draft behavior, and atomic profile/state persistence.
- Complete Task 1 and its focused tests before starting Task 2. Keep this plan
  synchronized with implementation evidence and record any scope deviation.

## Testing Strategy

- Exercise the public conversational path through
  `BotHandler._apply_intent_metadata()` and `_update_profile()` rather than
  testing only the helper.
- Cover duplicate source identities independently in the saved profile and in
  a populated persisted draft.
- For each source type, prove fully explicit numeric targets and explicit
  `None` values succeed without consulting the ambiguous source.
- For each source type, omit a target while explicitly supplying the other and
  prove ambiguity still returns a controlled result with no profile or draft
  write.
- Assert successful replacements use the existing atomic persistence path and
  retain the exact incoming calorie, protein, and fibre values.
- Re-run existing draft-precedence, saved-fallback, unmatched-member,
  duplicate-replacement, profile-completion, and persistence-failure tests.
- New tests must not use skips, xfails, network calls, live LLMs, or Telegram
  API access.

## Solution Overview

For each incoming member, first compute whether `protein_target`,
`fibre_target`, or both are absent from `incoming.model_fields_set`. If neither
field is absent, append the incoming member unchanged and continue without
looking in the draft or saved identity indexes.

If at least one field is absent, retain the current source resolution order:
use a unique matching persisted-draft member first; if the draft has no match,
fall back to a unique matching saved-profile member. Return the existing
controlled ambiguity signal when the selected source tier has duplicate
matches. Copy only the omitted fields from a unique source. This preserves
explicit-value authority without weakening safety for inherited data.

## Technical Details

### Field-intent decision

Within `BotHandler._merge_omitted_member_targets()`:

1. Inspect `incoming.model_fields_set` for `protein_target` and
   `fibre_target` before reading matching source candidates.
2. If both fields are present, keep the incoming model unchanged. Explicit
   numeric values and explicit `None` are equally authoritative.
3. If one or both fields are absent, resolve the source using the existing
   `_member_identity()` indexes and draft-first/saved-fallback rules.
4. Reject only ambiguity that prevents a required inheritance decision.
5. Copy only absent target fields with the existing immutable
   `model_copy(update=...)` pattern.

Do not remove the source indexes or change identity normalization. The minimal
change is to gate source resolution by omission intent, not to relax duplicate
validation globally.

### Persistence and validation boundaries

`BotHandler._update_profile()` must continue to:

- translate unsafe inheritance ambiguity into the existing controlled message;
- reject duplicate identities in the incoming replacement itself;
- preserve persisted-draft precedence and saved-profile fallback;
- avoid profile, draft, or state writes on controlled failure; and
- use the existing atomic final persistence path on success.

## Implementation Steps

### Task 1: Resolve source ambiguity only for omitted nutrient targets

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._merge_omitted_member_targets`,
`BotHandler._update_profile`, `FamilyMember.model_fields_set`

**TDD sequence:**

- [x] **Failing or missing tests first:** Add a saved-profile regression with
  duplicate stripped, case-folded source identities and a unique incoming
  replacement that explicitly supplies both `protein_target` and
  `fibre_target`. Parameterize or otherwise cover explicit numeric values and
  explicit `None`; assert the replacement succeeds with the exact incoming
  targets.
- [x] Add the equivalent persisted-draft regression. Keep the saved profile
  unambiguous so the test proves duplicate draft identities are bypassed when
  no inheritance is needed, and assert the existing atomic success path is
  used.
- [x] Add saved-profile and persisted-draft error regressions where one target
  is omitted and the other is explicit. Cover omission of protein and fibre,
  assert the existing controlled ambiguity message, and assert no profile,
  profile-draft, or state write occurs.
- [x] Retain or extend the incoming-replacement duplicate test to prove that
  bypassing source lookup does not permit duplicate identities in the new
  `family_members` list.
- [x] **Expected failure:** Run the new explicit-target regressions before the
  implementation. Record that the current helper returns `None` for duplicate
  saved and draft sources, causing safe fully explicit replacements to fail.
  Confirm the omitted-target ambiguity cases already remain controlled.
- [x] **Implementation change:** In
  `BotHandler._merge_omitted_member_targets()`, determine omitted target fields
  before source resolution and skip draft/saved lookup when neither field
  requires inheritance. Keep explicit numbers and explicit `None` unchanged.
- [x] Preserve draft-first lookup and saved-profile fallback when inheritance
  is required. Keep duplicate-source ambiguity, `_member_identity()`
  semantics, immutable copies, unmatched-member behavior, and the helper's
  typed return contract unchanged.
- [x] Preserve `_update_profile()` integration, duplicate incoming identity
  validation, completeness checks, controlled messages, draft persistence,
  and atomic profile/state writes.
- [x] **Verify the new tests pass:** Run the focused saved- and draft-source
  ambiguity tests in `tests/test_bot_handler.py`; require all to pass without
  skips or xfails.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_prompts.py` and resolve
  every failure before Task 2.

**Acceptance criteria:**

- [x] Duplicate identities in a saved profile do not block a unique incoming
  replacement when both optional targets are explicit.
- [x] Duplicate identities in a persisted draft do not block a unique incoming
  replacement when both optional targets are explicit.
- [x] Explicit numeric values and explicit `None` remain authoritative and are
  persisted exactly as supplied.
- [x] An omitted protein or fibre target still produces a controlled non-write
  when the required saved or draft source identity is ambiguous.
- [x] Draft precedence, saved fallback, unmatched-member behavior, and
  stripped, case-folded identity semantics remain unchanged.
- [x] Duplicate identities in the incoming replacement remain invalid.
- [x] Successful updates retain the existing atomic persistence path; failed
  updates perform no profile, draft, or state write.

### Task 2: Verify acceptance criteria and archive the follow-up plan

**Severity:** Release gate

**Depends on:** Task 1

**Files:**

- Modify, then move on completion:
  `2026-08-24-add-protein-fibre-targets-review-remediation-follow-up.md` in
  `docs/plans/`, then to `docs/plans/completed/`

**Verification sequence:**

- [x] Confirm Task 1 has direct test-first coverage for saved and persisted
  draft sources, explicit numeric values, explicit `None`, omitted protein,
  omitted fibre, controlled no-write behavior, and incoming duplicate
  validation. Add a missing regression in Task 1 before continuing if any
  acceptance criterion lacks coverage.
- [x] **Expected failure:** No new red test is expected solely for this
  verification task. Any failing acceptance test means Task 1 is incomplete
  and must be corrected before continuing.
- [x] Re-run the new follow-up regressions together and confirm they are not
  skipped, xfailed, order-dependent, or dependent on live services.
- [x] **Verify the new tests pass:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_prompts.py`.
- [x] Run `uv run ruff format --check .` and require no formatting changes.
- [x] Run `uv run ruff check .` and resolve all diagnostics.
- [x] Run `uv run mypy` and resolve all static typing errors.
- [x] **Relevant regression tests:** Run `uv run pytest` and require the full
  suite to pass, allowing only unchanged, documented pre-existing skips.
- [x] Run `git diff --check` and inspect the accumulated diff. Confirm all
  unrelated pre-existing deployment and documentation changes remain
  untouched and unattributed.
- [x] Update this plan with exact commands, results, deviations, blockers, and
  residual risks. Verify the P2 finding and every acceptance criterion are
  covered.
- [x] Move only this completed follow-up plan into `docs/plans/completed/`.
  Do not modify either previously archived protein/fibre plan.

**Acceptance criteria:**

- [x] Focused and full test suites pass with no new skips or xfails.
- [x] Ruff format, Ruff lint, Mypy, and `git diff --check` pass.
- [x] The final follow-up diff is limited to the helper, its focused tests, and
  this plan.
- [x] The actionable review finding has direct saved- and draft-source
  regression coverage and a verified correction.
- [x] Previously completed plans and unrelated worktree changes remain
  unchanged.

### Task 2 verification evidence

Verified on 2026-08-24 in `/workspace/meal-planner-bot`:

- Direct coverage is present in `tests/test_bot_handler.py` for duplicate
  saved and persisted-draft sources, explicit numeric targets, explicit
  `None`, omitted protein and fibre targets, controlled no-write behavior,
  incoming duplicate validation, draft precedence, saved fallback, and the
  atomic success path. The focused tests use repository mocks and the public
  conversational path; they do not use skips, xfails, live services, network
  calls, or Telegram API access.
- `uv run pytest tests/test_bot_handler.py -k
  'profile_replacement_explicit_targets_bypass_ambiguous or
  profile_replacement_ambiguous_required_source_does_not_write or
  profile_replacement_rejects_duplicate_member_identities'` — 13 passed,
  241 deselected in 8.36s; no skips or xfails.
- `uv run pytest tests/test_bot_handler.py tests/test_prompts.py` — 291 passed
  in 12.39s.
- `uv run ruff format --check .` — 86 files already formatted; passed.
- `uv run ruff check .` — all checks passed.
- `uv run mypy` — success, no issues found in 19 source files.
- `uv run pytest` — 1054 passed, 2 skipped in 40.39s. The two unchanged
  documented skips are the optional missing-SAM-artifact checks in
  `tests/test_template.py`; no xfails or new skips appeared.
- `git diff --check` — passed.
- Accumulated diff inspection confirmed the Task 1 change is limited to the
  helper and focused handler tests, with this plan as the only executor
  documentation change. Pre-existing `scripts/deploy.py`,
  `tests/test_deploy.py`, overlapping source/test work, and unrelated or
  untracked documentation were preserved and not attributed to this follow-up.
  The two previously completed protein/fibre plans were not modified.

The P2 finding is verified as corrected: fully explicit numeric or `None`
targets bypass duplicate saved/draft source lookup, while omitted targets
retain controlled ambiguity failure and no-write behavior. All Task 1
acceptance criteria are covered by the direct regressions and the full suite.

Deviations: none. Blockers: none. Residual risk: because source and test files
contained pre-existing overlapping changes at the executor baseline, exact
line-by-line attribution of those files remains limited; no unrelated change
was introduced by this verification task.

## Explicit Non-Goals

- Do not redesign target inheritance, profile drafts, profile completeness, or
  atomic profile/state persistence.
- Do not permit ambiguous lookup when any omitted target needs inheritance.
- Do not weaken duplicate validation for the incoming replacement list.
- Do not change identity normalization, numeric-name amendment parsing,
  add-member grammar, schemas, validators, dependencies, or `uv.lock`.
- Do not change prompts, Telegram UI, callback payloads, meal-plan generation,
  deployment files, README, or unrelated documentation.
- Do not modify either completed protein/fibre plan except for moving this new
  follow-up plan after its implementation is verified.
- Do not create a GitHub issue, comment on an issue, commit, push, merge,
  deploy, or perform any external action as part of this remediation plan.

## Post-Completion

### Manual verification

- In a non-production Telegram environment, replace a member in a legacy
  profile containing duplicate source identities while explicitly setting
  both nutrient targets; confirm the safe replacement succeeds.
- Repeat with one target omitted and confirm the bot returns the controlled
  duplicate-name response without persisting changes.

### Residual risks

- Live Telegram and LLM behavior remains manual because automated tests use
  controlled boundaries and do not call external services.
- The behavior intentionally continues to depend on stripped, case-folded
  member identity semantics.
