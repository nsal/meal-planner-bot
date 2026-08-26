# Remediate Unqualified Positive Preference Review Findings

## Overview

- Correct the minimum-one fallback so provider requirements with negative,
  exclusionary, limiting, or explicit natural-language frequency intent fail
  closed when their operator and count are missing.
- Preserve the intended strict `at_least 1` default for genuinely unqualified
  positive preferences, including foods whose names contain digits.
- Make privacy-safe interpretation mode and validation reason diagnostics
  visible in the standard text log output used by the deployed Lambda.
- Cover all four actionable findings from the independent review without
  changing the public rule schema, planner dispatch contract, or fail-closed
  clarification behavior.
- This workflow creates the remediation plan only. It does not implement any
  remediation, modify tests or production code, deploy, create an issue,
  commit, push, or perform external verification.

## Context (from discovery)

- **Project:** Python 3.14 Telegram meal-planning bot using Pydantic, LiteLLM,
  AWS Lambda/SAM, pytest, Ruff, and strict mypy.
- **Original implementation:**
  `docs/plans/completed/2026-08-26-default-unqualified-positive-preferences-to-minimum-one.md`
  added parser normalization, prompt guidance, diagnostics, workflow tests,
  and user documentation.
- **Current normalization boundary:**
  `src/meal_planner/llm/parser.py::_prepare_rule_payload` calls
  `_is_positive_request` before model validation and supplies
  `operator="at_least"` and `count=1` when both fields are absent.
- **Classifier gaps:** `_is_positive_request` uses a narrow negative-word
  blacklist and rejects every digit through an unrestricted `\d+` pattern.
  It misses some negative or limiting phrases and natural-language
  recurrence, while incorrectly rejecting numeric food names.
- **Diagnostic gap:** schema warnings attach `interpretation_mode` and
  `reason_code` through `logging`'s `extra` dictionary, but `template.yaml`
  does not configure JSON logging or a formatter that renders those fields.
- **Existing coverage:** `tests/test_parser.py` covers basic bare-positive,
  negative, malformed, explicit-count, and `caplog` metadata cases, but does
  not cover the reviewed language variants or the final formatted message.
- **Verified baseline:** the completed implementation passed Ruff format and
  lint checks, strict mypy, and `1399` pytest tests. Two existing Pydantic
  serializer warnings remain and are not remediation failures.

## Review Findings Covered

1. **P1:** Reject all negative clauses before applying the positive default.
2. **P2:** Fail closed when source wording contains an explicit frequency.
3. **P2:** Do not classify digits inside food names as counts.
4. **P2:** Render safe diagnostic fields in deployed standard-text logs.

The first finding is handled before frequency classification because
preventing inverted negative intent is the highest-severity behavior change.
The two frequency-related findings share one contextual detector and are
implemented together to avoid replacing the unrestricted digit check twice.
Logging is independent and follows the parser-classification corrections.

## Development Approach

- **Testing approach:** TDD. For each remediation task, add the focused tests
  first, run them against the reviewed implementation, and record the
  expected red result before changing production code.
- Complete each numbered task and make its focused tests pass before starting
  the next task.
- Keep normalization at the interpretation boundary in
  `_prepare_rule_payload`; do not add defaults to `DietaryRule` or planner
  fallback behavior.
- Prefer small, explicit wording helpers over a general natural-language
  parser. Match only the bounded intent required by the findings.
- Preserve provider-owned explicit `operator`, `count`, and legacy
  `exact_count` fields exactly as supplied so malformed values still reach
  schema validation.
- Keep user-facing clarification text unchanged and never include source
  text, food values, provider payloads, or Pydantic input values in logs.
- Follow `AGENTS.md`: use `uv run`, Ruff at 80 columns, strict mypy, and a
  passing full pytest run before completion.
- Update this plan if implementation discoveries materially change scope,
  test expectations, or sequencing.

## Testing Strategy

- **Negative-intent parser tests:** parameterize exclusion verbs, limiting
  verbs, ASCII apostrophes, Unicode apostrophes, and representative existing
  supported negatives in both interpretation modes where useful.
- **Frequency parser tests:** cover number words, daily and weekly recurrence,
  and quantified day/meal phrases when provider frequency fields are absent.
- **Numeric-food parser tests:** prove digit-bearing food names receive the
  same minimum-one default as other bare positive foods.
- **Diagnostic tests:** assert both safe `LogRecord` metadata and the rendered
  text returned by `LogRecord.getMessage()` or captured by `caplog.text`.
- **Privacy tests:** retain assertions that no raw preference or validation
  values appear in either the formatted message or the record dictionary.
- **Regression tests:** preserve explicit counts/operators, `exact_count`,
  best-effort strength, ambiguous-operator clarification, empty-food
  rejection, malformed-value rejection, saved/current mode parity, prompt
  contracts, bot workflows, and SAM template tests.
- **Repository gates:** Ruff format check, Ruff lint, strict mypy, full pytest,
  and `git diff --check` after all focused tests pass.

## Progress Tracking

- Mark completed checklist items with `[x]` immediately after verification.
- Record the exact red-phase command and failure evidence in the relevant
  task before production changes.
- Prefix newly discovered work with `➕` and blockers with `⚠️`.
- Do not mark a behavioral item complete merely because an existing test was
  already green; retain it as regression evidence and identify which new
  cases demonstrated the red phase.
- Do not archive this plan until every implementation and verification item
  is complete.

## Solution Overview

Retain the application-owned minimum-one default, but make eligibility more
conservative. A rule may receive the default only when it has valid food
candidates, has no provider-supplied count/operator fields, and its bounded
source clause contains neither blocked negative/limiting intent nor explicit
frequency intent.

Normalize common apostrophe forms for intent classification, or use patterns
that explicitly accept both ASCII `'` and Unicode `’`. Extend the blocked
intent vocabulary to cover the reviewed exclusion and limiting forms. This
guard must execute before the positive default and must cause missing-field
payloads to continue into normal validation, producing the existing malformed
rule clarification rather than synthesizing opposite intent.

Replace the unrestricted digit rejection with contextual frequency detection.
Recognize explicit operators, count words, recurrence terms, and numbers only
when they participate in frequency phrases such as days, meals, times, or
per-week wording. Digits embedded in names such as `5-spice chicken` and
`7-layer salad` are then ordinary food text and remain eligible for the
minimum-one default.

For diagnostics, retain the allowlisted `interpretation_mode` and
`reason_code` record attributes, and also interpolate those safe values into
the warning message. The standard Lambda text logger will then render the
diagnostic fields without requiring a template-wide logging-format migration.
No raw user or provider values may be interpolated.

## Technical Details

- Refactor or extend `_is_positive_request` in
  `src/meal_planner/llm/parser.py` with narrowly scoped helpers for blocked
  negative/limiting wording and explicit frequency wording.
- Treat reviewed forms such as `omit`, `limit`, `don't include`, and
  `don’t include` as ineligible for defaulting. Include existing forms such as
  `avoid`, `without`, and `exclude` in regression coverage.
- Recognize number words used with frequency units, daily/weekly recurrence,
  `every day`/`every meal` forms, and numeric counts tied to `times`, `days`,
  or `meals`.
- Do not treat a digit as a count solely because it appears anywhere in
  `source_text`. Keep hyphenated and otherwise digit-bearing food names
  eligible when no actual frequency expression is present.
- Do not overwrite explicit provider fields, even when their values are
  malformed. This remains enforced by `_prepare_rule_payload`'s existing
  explicit-field checks and Pydantic validation.
- Format the schema warning with only the selected interpretation mode and
  the allowlisted result of `_preference_validation_reason`; preserve the
  fields in `extra` for structured in-process consumers.
- Keep `template.yaml` unchanged unless implementation proves standard-text
  formatting cannot expose the safe message. Any discovery requiring a
  template logging change must be recorded as a plan scope change before
  editing the template and must add `tests/test_template.py` coverage first.

## What Goes Where

- **Implementation Steps:** parser TDD for negative and frequency intent,
  numeric-food regression coverage, deployed-text diagnostic coverage, and
  repository-wide verification.
- **Post-Completion:** deployment, Telegram behavior checks, CloudWatch log
  inspection, and normal branch/pull-request operations. These external items
  have no checkboxes in this plan.

## Implementation Steps

### Task 1: Fail closed for negative, exclusionary, and limiting clauses

**Severity:** P1

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`

- [x] Add parameterized parser tests first for `omit eggs`, `limit eggs`,
  `don't include eggs`, and `don’t include eggs` when the provider omits
  `operator` and `count`; assert no rule is produced and the existing
  malformed-requirement clarification is returned.
- [x] Add regression cases for currently supported negative forms such as
  `avoid eggs`, `without eggs`, and `exclude eggs`, and exercise both
  `stored_preference` and `current_plan_preference` where mode behavior could
  diverge.
- [x] Run the focused new tests before production changes and record the red
  phase: at minimum the currently missed exclusion/limiting/Unicode forms
  should be inverted into `at_least 1`; already-recognized ASCII forms may
  remain green as regression controls.
- Progress note: Red phase ran
  `uv run pytest tests/test_parser.py -k
  'reviewed_negative_wording_without_fields_fails_closed or
  negative_wording_without_fields_fails_closed'`; 6 reviewed
  exclusion/limiting/Unicode cases failed by producing `at_least 1`, while
  10 existing negative-wording controls passed.
- [x] Refactor the bounded intent check used by `_is_positive_request` so
  negative, exclusionary, and limiting clauses are ineligible for the
  minimum-one default, including both ASCII and Unicode apostrophe forms.
- [x] Preserve the normal fail-closed validation path: do not synthesize an
  exclusion rule, infer an `at_most` count, drop the clause, or change the
  user-facing clarification.
- [x] Re-run the focused new cases and verify they pass, then run
  `uv run pytest tests/test_parser.py`; all parser tests must pass before
  Task 2.
- Progress note: The focused suite passed 16/16 and `uv run pytest
  tests/test_parser.py` passed 96/96 after the parser change. Ruff format,
  Ruff lint, and strict mypy also passed for the repository.
- ⚠️ Repository-wide `uv run pytest` passed 1409 tests but reported two
  template-test failures because the ignored `.aws-sam` artifact is stale for
  `src/meal_planner/llm/parser.py`; no Task 1 parser tests failed, and the
  artifact was not rebuilt because generated-artifact refresh is reserved for
  the plan's later repository-wide verification task.

**Acceptance criteria:**

- No reviewed negative, exclusionary, or limiting phrase can become a
  positive `at_least 1` rule when frequency fields are absent.
- ASCII and Unicode contraction forms behave consistently.
- Existing valid bare-positive, explicit-rule, flexible-strength, malformed,
  and ambiguity behavior remains unchanged.

### Task 2: Detect frequency context without rejecting numeric food names

**Severity:** P2

**Depends on:** Task 1's conservative intent gate.

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`

- [x] Add parser tests first for omitted provider fields with source clauses
  such as `eggs three days a week`, `eggs every day`, `eggs weekly`,
  `eggs in two meals`, and numeric `N days`/`N meals` variants; assert they
  fail closed instead of receiving the minimum-one default.
- [x] Add parser tests first for bare positive numeric food names, including
  `5-spice chicken for dinner` and `7-layer salad for lunch`; assert each
  receives strict `operator="at_least"`, `count=1`, and the correct meal
  scope.
- Progress note: Added 12 mode-parity frequency cases and 4 numeric-food
  cases before the production change.
- [x] Run the focused new tests before production changes and record the red
  phase: natural-language recurrence/number-word cases should currently
  default to one, while numbered food names should currently return the
  malformed-requirement clarification. Numeric frequency controls already
  blocked by `\d+` may remain green.
- Progress note: Red phase ran
  `uv run pytest tests/test_parser.py -k
  'frequency_wording_without_fields_fails_closed or
  numeric_food_names_default_to_minimum_one'`; 8 recurrence/number-word
  cases defaulted to `at_least 1`, 4 numeric-food cases returned the
  malformed-requirement clarification, and 4 digit-frequency controls
  remained green under the existing `\d+` branch.
- [x] Replace the unrestricted digit branch in `_is_positive_request` with a
  contextual explicit-frequency detector that recognizes operator wording,
  number words, daily/weekly recurrence, and numbers tied to frequency units
  such as times, days, meals, or per-week expressions.
- [x] Keep the detector bounded: digits embedded in food names must not count
  as frequency, and explicit provider `operator`, `count`, or `exact_count`
  fields must still bypass all default synthesis and validate as supplied.
- [x] Re-run the focused frequency and numeric-food tests and verify they
  pass, then run `uv run pytest tests/test_parser.py`; all parser tests must
  pass before Task 3.
- Progress note: Green phase ran
  `uv run pytest tests/test_parser.py -k
  'frequency_wording_without_fields_fails_closed or
  numeric_food_names_default_to_minimum_one'` with 16 passed, followed by
  `uv run pytest tests/test_parser.py` with 112 passed. Existing explicit
  operator/count and legacy `exact_count` tests also remained green.
- ⚠️ Repository-wide `uv run pytest` passed 1425 tests and reported the same
  two stale `.aws-sam` artifact failures for `src/meal_planner/llm/parser.py`
  as the Task 1 baseline; the generated artifact was not rebuilt because
  repository-wide artifact verification belongs to Task 4.

**Acceptance criteria:**

- Daily, weekly, number-word, and quantified day/meal intent never silently
  weakens to `at_least 1` when provider frequency fields are absent.
- Numeric food names remain eligible for the same minimum-one default as
  nonnumeric food names.
- Existing explicit counts/operators, legacy `exact_count`, best-effort
  wording, and ambiguous-operator behavior remain unchanged.

### Task 3: Render safe schema diagnostics in standard Lambda text logs

**Severity:** P2

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`

- [x] Extend the diagnostic tests first to assert that
  `LogRecord.getMessage()` and/or `caplog.text` contains stable rendered
  fields such as `interpretation_mode=stored_preference` and
  `reason_code=count` for representative food, count, scope, and fallback
  schema failures.
- [x] Extend the privacy test first to prove rendered text and record metadata
  omit source text, food values, requirement IDs, provider-only fields,
  Pydantic input values, and the complete provider payload.
- [x] Run the focused diagnostic tests before production changes and record
  the red phase: record attributes should exist, but the formatted warning
  should contain only the current generic schema-validation message.
- Progress note: Red phase ran `uv run pytest tests/test_parser.py -k
  'preference_schema_warning'`; all 5 diagnostic/privacy cases failed because
  `LogRecord` metadata existed but `getMessage()` and `caplog.text` contained
  only `Preference requirement failed schema validation`. The added fallback
  case also confirmed the allowlisted `schema` reason attribute.
- [x] Update the schema warning in `parse_preference_interpretation` to
  interpolate only the validated interpretation mode and allowlisted reason
  code into the message while retaining the existing safe `extra` fields.
- [x] Keep the existing clarification text and `_preference_validation_reason`
  fallback behavior unchanged; do not add raw exception text or provider
  values to any log path.
- [x] Re-run the focused diagnostic and privacy tests and verify they pass,
  then run `uv run pytest tests/test_parser.py`; all parser tests must pass
  before Task 4.
- Progress note: Green phase ran `uv run pytest tests/test_parser.py -k
  'preference_schema_warning'` with 5 passed, followed by `uv run pytest
  tests/test_parser.py` with 113 passed. Ruff format, Ruff lint, strict mypy,
  and `git diff --check` also passed for the Task 3 files. The warning keeps
  the existing clarification and safe record attributes while rendering only
  the validated mode and allowlisted reason code.

**Acceptance criteria:**

- A standard text logger renders both `interpretation_mode` and `reason_code`
  without custom JSON logging configuration.
- Existing consumers can still read the two allowlisted `LogRecord`
  attributes.
- No raw dietary text, food, payload, requirement ID, validation input, or
  exception detail is emitted.
- User-facing fail-closed behavior is unchanged.

### Task 4: Verify all remediation acceptance criteria and quality gates

**Files:**
- Modify: this remediation plan only if verification records results, scope
  changes, or blockers

- [x] Verify every actionable P1 and P2 review finding maps to passing focused
  tests and the corresponding acceptance criteria in Tasks 1-3.
- [x] Run `uv run pytest tests/test_parser.py` and confirm all parser behavior,
  diagnostic, and privacy tests pass.
- [x] Run
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py tests/test_template.py`
  to verify prompt, workflow, and deployment-template regressions.
- [x] Run `uv run ruff format --check .` and fix any formatting failures
  attributable to the remediation.
- [x] Run `uv run ruff check .` and fix any lint failures attributable to the
  remediation.
- [x] Run `uv run mypy` and fix any strict type failures attributable to the
  remediation.
- [x] Run `uv run pytest` and confirm the complete repository suite passes;
  if ignored SAM artifacts are stale, rebuild them with the established SAM
  build command and rerun the suite before marking this item complete.
- [x] Run `git diff --check` and inspect the final diff to confirm no unrelated
  behavior, schema, prompt, documentation, or deployment changes were added.

Progress note: Verification completed without implementation changes. The
four actionable review findings map to passing focused coverage and acceptance
criteria: Task 1 negative-intent coverage passed 16/16; Task 2 frequency and
numeric-food coverage passed 16/16; and Task 3 diagnostic/privacy coverage
passed 5/5. `uv run pytest tests/test_parser.py` passed 113 tests. The prompt,
workflow, and template gate initially reproduced the two known stale-SAM
failures, so ignored artifacts were rebuilt with
`uvx --from aws-sam-cli sam build --beta-features`; the exact gate then passed
418 tests. Repository gates passed: `uv run ruff format --check .` reported
100 files already formatted, `uv run ruff check .` passed, `uv run mypy`
reported no issues in 20 source files, and `uv run pytest` passed 1428 tests.
The full suite emitted only the two unchanged Pydantic serializer warnings.
`git diff --check` passed. Final diff inspection confirmed this task changed
only this plan; the pre-existing implementation, test, README, prompt, and
archived-plan changes remain untouched.

**Acceptance criteria:**

- All focused and full repository gates pass with no new warnings or failures.
- The two known Pydantic serializer warnings may remain only if they are
  confirmed unchanged from the verified baseline.
- The final diff is limited to the parser, parser tests, and plan tracking,
  unless a documented scope change was required by a verified discovery.

### Task 5: [Final] Record completion and archive the remediation plan

**Files:**
- Modify: this remediation plan with final red/green and gate results
- Move after completion:
  `docs/plans/2026-08-26-default-unqualified-positive-preferences-to-minimum-one-review-remediation.md`
  to `docs/plans/completed/`

- [x] Record the red-phase evidence, final focused test results, full quality
  gate results, and any confirmed pre-existing warnings in this plan.
- [x] Confirm no user-facing documentation update is required because the
  documented minimum-one contract is unchanged and this remediation only
  narrows unsafe classifier boundaries and exposes already-designed safe
  diagnostics.
- [x] Confirm every implementation and verification checkbox is complete and
  no unresolved blocker remains.
- [x] Move this plan to `docs/plans/completed/` without modifying or moving the
  archived original plan.

Final completion note: All implementation and verification checklist items in
Tasks 1-4 and this final task are complete, with no unresolved blocker. The
TDD red phases were recorded as follows: Task 1 ran
`uv run pytest tests/test_parser.py -k
'reviewed_negative_wording_without_fields_fails_closed or
negative_wording_without_fields_fails_closed'` and initially had 6 reviewed
exclusion/limiting/Unicode cases fail by producing `at_least 1`, while 10
existing negative-wording controls passed; Task 2 ran
`uv run pytest tests/test_parser.py -k
'frequency_wording_without_fields_fails_closed or
numeric_food_names_default_to_minimum_one'` and initially had 12 new cases
fail (8 recurrence/number-word cases defaulted to one and 4 numeric-food cases
failed closed), while 4 existing digit-frequency controls passed; and Task 3
ran `uv run pytest tests/test_parser.py -k 'preference_schema_warning'` and
initially had all 5 diagnostic/privacy cases fail because safe record metadata
existed but the rendered warning remained generic. The corresponding green
focused suites passed 16/16, 16/16, and 5/5; `uv run pytest
tests/test_parser.py` passed 113 tests.

Final verification passed with `uv run pytest
tests/test_prompts.py tests/test_bot_handler.py tests/test_template.py` after
the initial 416-pass, 2-failure stale-SAM result was resolved by
`uvx --from aws-sam-cli sam build --beta-features`, followed by 418 passed;
`uv run ruff format --check .` reported 100 files already formatted;
`uv run ruff check .` passed; `uv run mypy` reported no issues in 20 source
files; `uv run pytest` passed 1428 tests; and `git diff --check` passed. The
full suite emitted only the two confirmed unchanged pre-existing Pydantic
serializer warnings. The stale `.aws-sam` parser artifact was the source of
the two initial template failures and was regenerated successfully. No
user-facing documentation update is needed: the documented minimum-one
contract is unchanged, while the remediation only narrows unsafe classifier
boundaries and renders already-designed safe diagnostics. The archived
original plan remains unchanged.

## Post-Completion

*These items require deployment or external systems. They are informational,
have no checkboxes, and are not performed by this planning workflow.*

**Manual Telegram verification:**

- Deploy through the normal dedicated branch and pull-request workflow; never
  push or merge directly to `master`.
- Exercise representative negative and limiting requests and confirm they are
  clarified or interpreted explicitly rather than inverted into mandatory
  positive rules.
- Exercise `5-spice chicken for dinner` and `7-layer salad for lunch` and
  confirm valid provider output reaches plan generation with the intended
  minimum-one rules.
- Re-run the original saved and current bare-positive Telegram scenarios to
  confirm the remediation did not reintroduce the reported `/plan` block.

**Manual CloudWatch verification:**

- Trigger a bounded malformed preference in each interpretation mode and
  confirm the deployed warning text visibly contains only the expected mode
  and allowlisted reason code.
- Confirm CloudWatch output does not contain raw source text, food values,
  provider payloads, requirement IDs, Pydantic input values, or exception
  details.

**External workflow:**

- Create a Conventional Commit referencing the associated issue, open a pull
  request from a dedicated bug-fix branch, and report the verification results
  through the repository's normal review process.
- Issue creation, commits, pushes, deployment, Telegram checks, and CloudWatch
  checks are explicitly outside this remediation-planning workflow.
