# Close Remaining Preference Defaulting Classifier Gaps

## Overview

Follow up on the independent review of the minimum-one preference remediation.
Two explicit quantity forms can still be weakened into a strict
`at_least 1` rule when the provider omits `operator`, `count`, and
`exact_count`:

- comparative limits such as `less than 3 eggs`
- compact frequencies such as `eggs 3x a week` and `eggs 3/week`

The change must keep the application-owned minimum-one default for genuinely
unqualified positive food requests while making these remaining quantity
forms fail closed through the existing malformed-requirement clarification.
It must preserve numeric food-name support, explicit provider fields, safe
diagnostics, and all previously verified parser behavior.

## Context (from discovery)

- **Primary implementation:** `src/meal_planner/llm/parser.py`
  - `_has_explicit_frequency_wording` recognizes bounded recurrence and
    quantity expressions.
  - `_is_positive_request` blocks negative, limiting, operator, and frequency
    intent before `_prepare_rule_payload` synthesizes `at_least 1`.
- **Primary tests:** `tests/test_parser.py`
  - existing mode-parity tests cover negative wording, expanded frequency
    wording, and numeric food names.
- **Completed predecessor plans:**
  - the archived minimum-one implementation plan
  - the archived minimum-one review-remediation plan
- **Repository requirements:** use `uv`, Ruff with an 80-column limit, strict
  mypy, and a passing full `uv run pytest` suite.
- **Known verification behavior:** ignored `.aws-sam` artifacts may need the
  established SAM rebuild before template tests reflect parser changes; two
  existing Pydantic serializer warnings are known baseline warnings.

## Review Findings Covered

### P1: Comparative limits can be inverted into minimums

`less than 3 eggs` currently reaches the minimum-one default because neither
the negative/limiting gate nor the frequency detector classifies the
comparative quantity. This changes upper-bound intent into a lower bound.
Numeric and number-word forms of `less than`, `fewer than`, and `under` need
mode-parity coverage and bounded classification.

### P2: Compact frequency forms can be weakened into minimums

`eggs 3x a week` and `eggs 3/week` currently reach the minimum-one default.
Compact `Nx`, slash, and `N a/each period` forms express frequency and must
fail closed without reviving the unrestricted digit rejection that broke
numeric food names.

## Development Approach

- **Testing approach:** TDD. Add focused tests and record the expected red
  phase before changing production code.
- Complete each numbered task before beginning the next.
- Keep classification local to the existing bounded intent helpers; do not
  introduce a general natural-language parser.
- Prefer small, named patterns or helpers when that makes quantity context
  auditable. Do not reject a clause merely because it contains a digit.
- Preserve explicit provider `operator`, `count`, and `exact_count` values as
  supplied so normal Pydantic validation remains authoritative.
- Update this plan if implementation discovers a material scope change.
- Mark checklist items complete only after the associated behavior and tests
  are verified.

## Testing Strategy

- Add parameterized parser tests in `tests/test_parser.py` for both
  `stored_preference` and `current_plan_preference`.
- For every new blocked expression, assert that no rule is returned and the
  existing malformed-preference clarification is unchanged.
- Keep positive controls for numeric food names and ordinary text containing
  digits so contextual detection cannot regress into a global digit guard.
- Keep regression controls for explicit provider fields, legacy
  `exact_count`, daily/weekly wording, best-effort strength, ambiguous
  operators, and privacy-safe diagnostics.
- Run focused tests after each implementation task, then parser, integration,
  formatting, linting, typing, and full repository gates.

## Progress Tracking

- Mark completed items with `[x]` immediately after verification.
- Record focused red- and green-phase commands and outcomes in this file.
- Add newly discovered work with a `➕` prefix.
- Record blockers or verified pre-existing failures with a `⚠️` prefix.
- Keep implementation changes limited to the parser, parser tests, and plan
  tracking unless a verified discovery requires a documented scope change.

## Solution Overview

Extend the existing bounded intent classification in two stages. First,
recognize comparative limiting phrases only when the comparative marker is
connected to a numeric or number-word quantity. Second, recognize compact
frequency syntax only when a quantity participates in an explicit recurrence
shape such as `3x a week`, `3x/week`, `3/week`, `3 a week`, or `3 each week`.

Both checks must run before the minimum-one default. They should return only
an eligibility decision; they must not synthesize an exclusion, infer an
`at_most` rule, or modify provider payloads. Missing-field payloads then
continue into existing schema validation and produce the established
clarification.

## Technical Details

- Reuse one bounded numeric token definition for digits and supported number
  words where practical, avoiding divergent quantity vocabularies.
- Comparative limiting patterns should cover at least:
  - `less than <quantity>`
  - `fewer than <quantity>`
  - `under <quantity>`
- Compact frequency patterns should cover at least:
  - `<quantity>x a|each|per <period>`
  - `<quantity>x/<period>`
  - `<quantity>/<period>`
  - `<quantity> a|each|per <period>`
- Periods should remain bounded to recurrence units owned by the application,
  such as day, week, meal, and their plural forms where grammatically valid.
- Pattern boundaries must prevent incidental matches inside food names or
  larger words. `5-spice chicken`, `7-layer salad`, and similar digit-bearing
  foods must remain eligible when no frequency or limiting context exists.
- Do not change the user-facing malformed-requirement clarification or the
  schema-warning privacy contract.

## What Goes Where

- **Implementation Steps:** parser TDD for comparative limits and compact
  frequencies, repository-wide verification, and final plan archival.
- **Post-Completion:** deployment, Telegram checks, CloudWatch inspection, and
  branch/PR operations. These external items have no checkboxes.

## Implementation Steps

### Task 1: Fail closed for comparative limiting quantities

**Severity:** P1

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: this plan with red/green evidence

- [x] Add parameterized tests first for numeric and number-word forms such as
  `less than 3 eggs`, `less than three eggs`, `fewer than 2 eggs`,
  `fewer than two eggs`, `under 4 meals`, and `under four meals`, exercising
  both preference modes with omitted count/operator fields.
- [x] Assert each comparative case returns no rule and exactly the existing
  malformed-preference clarification; do not accept a synthesized
  `at_least`, `at_most`, or exclusion rule.
- [x] Add positive controls proving unrelated uses of food text and existing
  numeric food names are not blocked solely by a quantity token or the word
  `under` outside a bounded comparative-quantity shape.
- [x] Run the focused tests before production changes and record the expected
  red phase: reviewed comparative cases should currently produce strict
  `at_least 1` rules while existing controls remain green.
- [x] Add a narrowly scoped comparative-limiting detector in
  `src/meal_planner/llm/parser.py` and call it from `_is_positive_request`
  before minimum-one synthesis.
- [x] Keep the detector bounded to comparative markers tied to supported
  numeric or number-word quantities; do not restore unrestricted digit
  rejection or infer provider fields.
- [x] Re-run the focused comparative tests and verify the green phase, then
  run `uv run pytest tests/test_parser.py`; all parser tests must pass before
  Task 2.

**Task 1 evidence:**

- Red phase command:

  `uv run pytest tests/test_parser.py -k 'comparative_quantities_without_fields_fail_closed or comparative_detector_preserves_unrelated_positive_text'`

  initially produced 8 failures for the numeric and number-word `less
  than`/`fewer than` cases and 12 passing controls. The `under ... meals`
  cases were already blocked by the existing bounded frequency detector.
- Green phase: the same focused command passed all 20 selected tests.
- Parser regression: `uv run pytest tests/test_parser.py` passed all 133
  tests.
- Scoped quality checks: `uv run ruff format --check
  src/meal_planner/llm/parser.py tests/test_parser.py`, `uv run ruff check
  src/meal_planner/llm/parser.py tests/test_parser.py`, and `uv run mypy`
  all passed.
- No Task 1 blocker or scope change was discovered.

**Acceptance criteria:**

- Numeric and number-word comparative upper bounds cannot become positive
  minimum-one rules in either preference mode.
- Comparative payloads fail closed through the unchanged malformed-rule
  clarification path.
- Numeric food names and genuinely unqualified positive requests retain the
  existing minimum-one behavior.
- Explicit provider fields and existing negative, frequency, ambiguity, and
  flexible-strength behavior remain unchanged.

### Task 2: Fail closed for compact frequency syntax

**Severity:** P2

**Depends on:** Task 1's bounded quantity vocabulary and conservative intent
gate.

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: this plan with red/green evidence

- [x] Add parameterized tests first for compact forms including
  `eggs 3x a week`, `eggs 3x/week`, `eggs 3/week`, `eggs 3 a week`, and
  `eggs 3 each week`, with equivalent supported period variants and both
  preference modes.
- [x] Include number-word controls where the grammar permits them, such as
  `eggs three a week` and `eggs three each week`, and assert all reviewed
  forms return the existing malformed-preference clarification.
- [x] Add positive controls for `5-spice chicken for dinner`,
  `7-layer salad for lunch`, and digit-bearing food text adjacent to ordinary
  non-frequency punctuation; assert strict `at_least 1` with the correct meal
  scope.
- [x] Run the focused compact-frequency and numeric-food tests before
  production changes and record the expected red phase: compact recurrence
  cases should currently default to one while numeric-food controls remain
  green.
- [x] Extend `_has_explicit_frequency_wording` with bounded patterns for
  quantity-plus-`x`, slash-based recurrence, and quantity-plus-`a`/`each`/
  `per` period syntax.
- [x] Keep recognized periods explicit and preserve word/token boundaries so
  digits or the letter `x` inside food names do not count as frequency.
- [x] Re-run the focused tests and verify the green phase, then run
  `uv run pytest tests/test_parser.py`; all parser tests must pass before
  Task 3.

**Task 2 evidence:**

- Red phase command:

  `uv run pytest tests/test_parser.py -k 'compact_frequency_without_fields_fails_closed or compact_frequency_detector_preserves_numeric_food_controls'`

  produced 20 expected failures for compact recurrence cases and 14
  passing numeric-food controls before production changes. The failures
  showed compact expressions being synthesized as strict `at_least 1`
  rules.

- No Task 2 blocker or scope change was discovered during the red phase.

- Green phase command:

  `uv run pytest tests/test_parser.py -k 'compact_frequency_without_fields_fails_closed or compact_frequency_detector_preserves_numeric_food_controls'`

  passed all 34 selected tests: 28 compact-frequency cases and 6
  numeric-food controls across both preference modes.

- Parser regression: `uv run pytest tests/test_parser.py` passed all 167
  tests.
- Scoped quality checks: `uv run ruff format --check
  src/meal_planner/llm/parser.py tests/test_parser.py`, `uv run ruff check
  src/meal_planner/llm/parser.py tests/test_parser.py`, `uv run mypy`, and
  `git diff --check` all passed.
- No Task 2 blocker or scope change was discovered after implementation.

**Acceptance criteria:**

- Compact `Nx`, slash, and quantity-per-period forms cannot silently weaken
  to `at_least 1` in either preference mode.
- Supported number-word compact frequencies fail closed consistently with
  numeric forms.
- Numeric and hyphenated food names remain eligible for minimum-one
  defaulting when no actual recurrence phrase is present.
- Existing expanded frequency forms, explicit provider fields, legacy
  `exact_count`, and diagnostics remain unchanged.

### Task 3: Verify remediation acceptance criteria and quality gates

**Files:**
- Modify: this plan only to record verification results, scope changes, or
  blockers

- [x] Verify the P1 comparative-limit finding maps to passing focused tests
  and every Task 1 acceptance criterion.
- [x] Verify the P2 compact-frequency finding maps to passing focused tests
  and every Task 2 acceptance criterion.
- [x] Run `uv run pytest tests/test_parser.py` and confirm all parser behavior,
  mode-parity, clarification, diagnostic, and privacy tests pass.
- [x] Run
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py tests/test_template.py`
  to verify prompt, workflow, and deployment-template regressions.
- [x] If template tests identify stale ignored SAM artifacts, run the
  established `uvx --from aws-sam-cli sam build --beta-features` command and
  rerun the affected and full suites.
- [x] Run `uv run ruff format --check .` and fix formatting failures
  attributable to this remediation.
- [x] Run `uv run ruff check .` and fix lint failures attributable to this
  remediation.
- [x] Run `uv run mypy` and fix strict type failures attributable to this
  remediation.
- [x] Run `uv run pytest` and confirm the complete repository suite passes;
  record whether the two known Pydantic serializer warnings remain unchanged.
- [x] Run `git diff --check` and inspect the final diff for unrelated parser,
  test, documentation, schema, prompt, or deployment changes.

**Task 3 evidence:**

- Focused remediation verification:
  `uv run pytest tests/test_parser.py -k
  'comparative_quantities_without_fields_fail_closed or
  comparative_detector_preserves_unrelated_positive_text or
  compact_frequency_without_fields_fails_closed or
  compact_frequency_detector_preserves_numeric_food_controls'` passed all
  54 selected tests, with 113 deselected. The P1 comparative and P2 compact
  frequency cases passed in both preference modes, including unchanged
  malformed-preference clarification and numeric-food positive controls.
- Parser regression:
  `uv run pytest tests/test_parser.py` passed all 167 tests, covering the
  parser behavior, mode parity, clarification, diagnostics, and privacy
  cases required by the acceptance criteria.
- Integration and template regression:
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py
  tests/test_template.py` passed all 418 tests. Template tests did not
  identify stale ignored `.aws-sam` artifacts, so the conditional
  `uvx --from aws-sam-cli sam build --beta-features` rebuild and reruns were
  not required.
- Quality gates passed without fixes:
  `uv run ruff format --check .` reported 101 files already formatted;
  `uv run ruff check .` reported all checks passed; and `uv run mypy`
  reported `Success: no issues found in 20 source files`.
- Full repository regression:
  `uv run pytest` passed all 1,482 tests. The same two known Pydantic
  serializer warnings remained from the existing bot-handler cases
  involving `ConstraintEntry` and `DietaryPreferenceEntry` serialization;
  no new warning was observed.
- Diff and scope audit: `git diff --check` passed. The working tree remains
  limited to the supplied baseline paths plus the three supplied plan files;
  Task 3 made no parser, test, schema, prompt, deployment, or documentation
  changes outside this plan's Task 3 evidence. The unrelated README,
  prompt, handler, and associated test changes were present in the supplied
  baseline and were preserved. No Task 3 blocker or scope change was
  discovered.

**Acceptance criteria:**

- Both independent-review findings have passing focused regression tests.
- All focused and repository-wide quality gates pass.
- Any remaining warnings are confirmed pre-existing and documented.
- The final implementation diff is limited to parser classification, parser
  tests, and plan tracking unless a scope change is explicitly recorded.

### Task 4: [Final] Record completion and archive the follow-up plan

**Files:**
- Modify: this plan with final red/green and gate results
- Move after completion: this plan to `docs/plans/completed/` with the same
  filename

- [x] Record Task 1 and Task 2 red-phase evidence, final focused results, full
  quality-gate results, SAM rebuild status, and confirmed pre-existing
  warnings in this plan.
- [x] Confirm no user-facing documentation change is required because the
  minimum-one contract remains unchanged and this follow-up only closes
  unsafe classifier gaps.
- [x] Confirm every implementation and verification checkbox is complete and
  no unresolved blocker remains.
- [x] Move this plan to `docs/plans/completed/` without modifying or moving
  either predecessor plan.

**Task 4 evidence:**

- Task 1 red phase initially produced 8 failures and 12 passing controls;
  its focused green phase passed all 20 selected tests. Task 1's parser
  regression passed all 133 tests.
- Task 2 red phase initially produced 20 failures and 14 passing controls;
  its focused green phase passed all 34 selected tests. Task 2's parser
  regression passed all 167 tests.
- Final focused remediation verification passed 54 tests. Prompt, bot
  handler, and template regression passed 418 tests; the full repository
  suite passed all 1,482 tests.
- Final quality gates passed: `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `git diff --check`. Template
  tests found no stale ignored SAM artifacts, so the established SAM rebuild
  was not required.
- The two known Pydantic serializer warnings involving `ConstraintEntry` and
  `DietaryPreferenceEntry` remained unchanged; no new warnings were
  observed. No user-facing documentation change is required because the
  minimum-one contract remains unchanged and this follow-up only closes
  unsafe classifier gaps.
- All implementation and verification checkboxes are complete, no blocker
  remains, and the predecessor plans remain unchanged. The plan is ready to
  move to `docs/plans/completed/` with this same filename.

**Acceptance criteria:**

- The completed plan contains auditable red/green and final verification
  evidence.
- Existing user-facing documentation and predecessor plans remain unchanged.
- The plan is archived only after every in-repository requirement passes.

## Post-Completion

*These items require deployment or external systems. They are informational
and have no checkboxes.*

**Manual Telegram verification:**

- Deploy through a dedicated branch and pull request; never push or merge
  directly to `master`.
- Exercise comparative requests such as `less than 3 eggs` and confirm they
  are clarified or explicitly interpreted rather than inverted into a
  mandatory positive rule.
- Exercise compact frequencies such as `eggs 3x a week` and `eggs 3/week`
  and confirm they do not silently default to one.
- Recheck `5-spice chicken for dinner` and `7-layer salad for lunch` to confirm
  numeric food names still produce the documented minimum-one behavior.

**Operational verification:**

- Inspect CloudWatch text logs for representative schema failures and confirm
  only the allowlisted `interpretation_mode` and `reason_code` diagnostics are
  rendered.
- Add a completion comment to the associated GitHub issue with the commit or
  pull-request link after implementation is delivered.
