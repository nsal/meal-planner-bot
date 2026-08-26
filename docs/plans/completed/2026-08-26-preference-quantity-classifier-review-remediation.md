# Preference Quantity Classifier Review Remediation

## Overview

Remediate the two actionable findings from the independent review of the
bounded preference quantity classifiers. Hyphenated compound number words,
such as `twenty-one`, and quantities separated from a recurrence slash by
whitespace currently bypass the intent gates. When provider count and
operator fields are omitted, those clauses can be weakened into strict
`at_least 1` rules.

Extend only the application-owned bounded quantity grammar and compact
frequency pattern. Preserve the minimum-one default for genuinely
unqualified positive food requests, fail explicit quantity intent through
the existing malformed-preference clarification, and keep numeric and
hyphenated food names eligible when they do not express a quantity rule.

This is a remediation implementation plan only. Do not implement it during
the planning phase.

## Context

- **Reviewed implementation:** `src/meal_planner/llm/parser.py`, especially
  `_NUMBER_TOKEN`, `_COMPARATIVE_LIMITING_PATTERN`,
  `_COMPACT_FREQUENCY_PATTERN`, `_has_explicit_frequency_wording()`, and
  `_is_positive_request()`.
- **Reviewed tests:** `tests/test_parser.py` contains mode-parity coverage for
  comparative quantities, compact frequencies, malformed-preference
  clarification, and numeric-food positive controls.
- **Completed source plan:**
  `docs/plans/completed/2026-08-26-default-unqualified-positive-preferences-to-minimum-one-review-remediation-follow-up.md`.
  It is an immutable input to this follow-up and must not be edited or moved.
- **Verified baseline:** the prior execution reported 54 focused parser
  tests, 167 parser tests, 418 integration/template tests, and 1,482 full
  repository tests passing. Two existing Pydantic serializer warnings were
  unchanged.
- **Attribution limitation:** `parser.py` and `test_parser.py` already contain
  protected uncommitted work. Preserve all pre-existing changes and keep this
  remediation limited to clearly attributable classifier and test hunks.
- **Repository constraints:** use `uv`; keep Ruff-formatted Python within the
  configured 80-column limit; pass Ruff lint, strict Mypy, Pytest, and
  `git diff --check`.

## Review Findings Covered

### P1: Recognize hyphenated compound number words

`less than twenty-one eggs` and `eggs twenty-one a week` bypass the bounded
classifiers because `_NUMBER_TOKEN` ends before the hyphen and the surrounding
lookarounds prevent matching the trailing component. An explicit upper bound
can therefore be inverted, and an explicit frequency can be weakened. The
shared bounded number grammar must accept valid tens-plus-unit compound words
without matching compound-looking text embedded in hyphenated food names.

The review also noted that the existing `under` cases use `meals`, which the
older frequency detector already rejects. Add `under 4 eggs` and a number-word
equivalent so the comparative detector itself has direct regression coverage.

### P2: Allow whitespace before compact frequency slashes

`eggs 3 / week` becomes a strict `at_least 1` rule because the slash branches
require `/` immediately after the quantity. Optional bounded whitespace before
the slash must be accepted for numeric, simple number-word, and supported
compound-number forms without broadening the recognized recurrence periods or
reviving global digit rejection.

## Development Approach

- **Testing approach: TDD.** For each finding, add focused mode-parity tests,
  run them against the current implementation to record the expected red
  behavior, make the smallest production change, and rerun focused and parser
  suites before continuing.
- Complete each numbered task in order. Task 2 depends on Task 1's corrected
  shared number grammar. Do not start a later task while an earlier task or
  its required tests are incomplete.
- Keep quantity recognition local to the existing bounded regex constants and
  intent helpers. Do not introduce a general natural-language number parser.
- Do not infer or synthesize provider `operator`, `count`, or `exact_count`
  fields. The classifiers only decide whether minimum-one defaulting is safe.
- Preserve the exact malformed-preference clarification and normal Pydantic
  validation for explicit provider fields.
- Preserve unrelated and pre-existing work. Do not commit, push, stash,
  reset, clean, broadly format, or check out existing changes.
- Update this plan with exact red/green evidence, scope changes, blockers,
  warnings, and final verification results as work proceeds.

## Testing Strategy

- Parameterize all new behavioral cases over `stored_preference` and
  `current_plan_preference`.
- For every blocked expression, omit count/operator fields and assert that no
  rule is returned and the clarification remains exactly `One or more
  preference requirements are malformed.`
- For every detector expansion, retain positive controls that assert strict
  `at_least 1`, count `1`, and the correct meal scope for genuinely
  unqualified requests.
- Cover valid hyphenated tens-plus-unit words at both comparative and compact
  frequency call sites. Cover numeric, simple number-word, and compound-word
  quantities with whitespace before a slash.
- Keep boundary controls for `5-spice chicken`, `7-layer salad`, and a
  tens-plus-unit-looking hyphenated food name so the shared grammar cannot
  match a valid numeric prefix inside a larger food token.
- Re-run existing explicit-field, legacy `exact_count`, expanded-frequency,
  ambiguity, flexible-strength, diagnostic, and privacy tests through the
  complete parser suite.
- Do not add skips or xfails, call live services, or alter the warning and
  clarification contracts.

## Progress Tracking

- Mark an item `[x]` only after its behavior and required tests are verified.
- Record focused red and green commands and outcomes under the relevant task.
- Add newly discovered in-scope work with a `➕` prefix.
- Record blockers or confirmed pre-existing failures with a `⚠️` prefix.
- Keep this plan synchronized with implementation and archive it only after
  all acceptance criteria and quality gates pass.

## Solution Overview

First, split or refactor the existing number-token expression just enough to
recognize the currently supported simple forms plus valid hyphenated
tens-plus-unit words such as `twenty-one`. Keep both leading and trailing
boundaries strict so a quantity cannot match inside a larger word or continue
into another hyphenated segment. Because comparative and compact-frequency
patterns share this token, verify both call sites in the same task.

Second, update only the slash alternatives in
`_COMPACT_FREQUENCY_PATTERN` to accept optional whitespace between the bounded
quantity and `/`. Keep the period immediately bounded to the existing
application-owned `day`, `week`, and `meal` vocabulary and plural forms.

Both changes continue to produce only an eligibility decision. They must not
translate text into an `at_most`, exact, exclusion, or frequency rule. Missing
provider fields proceed through existing validation and return the established
clarification.

## Technical Details

### Bounded compound-number grammar

- Preserve digit forms and the currently supported simple number words.
- Add only valid hyphenated tens-plus-unit combinations for the supported tens
  vocabulary (`twenty` through `ninety`) and units (`one` through `nine`).
- Do not accept arbitrary word-word compounds, repeated hyphens, a trailing
  hyphen, or a valid compound followed by another word character or hyphen.
- Keep the token reusable by `_COMPARATIVE_LIMITING_PATTERN` and
  `_COMPACT_FREQUENCY_PATTERN`; avoid independently maintained number
  vocabularies.
- Ensure direct `under <quantity> <food>` cases are classified by the
  comparative path even when the food is not a recurrence period.

### Whitespace-before-slash grammar

- Accept optional bounded whitespace between a recognized quantity and `/` in
  both direct slash and `x`-slash compact forms where applicable.
- Continue to require an allowed recurrence period after the slash.
- Do not classify arbitrary fractions, recipe quantities, slash punctuation,
  or unsupported periods as recurrence solely because they contain digits or
  number words.
- Preserve all existing token lookarounds so matches cannot begin or end
  inside food names or larger words.

## What Goes Where

- **Implementation Steps:** focused parser TDD for the P1 and P2 findings,
  repository-wide verification, evidence recording, and archival of this
  plan.
- **Post-Completion:** deployment, Telegram verification, CloudWatch review,
  and branch or pull-request operations. These are external activities and
  have no implementation checkboxes.

## Implementation Steps

### Task 1: Recognize bounded hyphenated compound number words

**Severity:** P1

**Files:**

- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: this plan with red/green evidence

**Symbols:** `_NUMBER_TOKEN`, `_COMPARATIVE_LIMITING_PATTERN`,
`_COMPACT_FREQUENCY_PATTERN`,
`test_comparative_quantities_without_fields_fail_closed`,
`test_compact_frequency_without_fields_fails_closed`

**TDD sequence:**

- [x] **Failing or missing tests first:** Extend or add parameterized tests for
  `less than twenty-one eggs`, `fewer than thirty-two eggs`, and an
  `under <compound-number> eggs` case in both preference modes. Assert no rule
  and the exact existing malformed-preference clarification.
- [x] Add direct numeric and simple number-word comparative cases for
  `under 4 eggs` and `under four eggs`. These close the review gap by avoiding
  `meal` as the target noun and must fail closed in both modes.
- [x] Add compound-number compact-frequency cases including
  `eggs twenty-one a week`, `eggs twenty-one each week`, and
  `eggs twenty-one/week` in both modes. Assert no synthesized minimum,
  maximum, exact, or exclusion rule.
- [x] Add positive boundary controls for existing numeric food names and a
  food phrase with a valid-looking compound-number prefix followed by another
  hyphenated segment. Assert the unqualified request still receives strict
  `at_least 1` with the correct food and meal scope.
- [x] **Expected red failure:** Run the new compound-number and direct-`under`
  tests before production changes. Record that compound-number cases currently
  produce strict `at_least 1` rules. Record the direct `under` behavior
  separately; it may already pass because the new comparative detector exists,
  but it must prove coverage independent of the frequency detector.
- [x] **Implementation change:** Refine `_NUMBER_TOKEN` or small named
  component constants in `src/meal_planner/llm/parser.py` so supported
  tens-plus-unit words joined by one hyphen are a single bounded quantity.
- [x] Preserve the existing simple words and digits, strict token lookarounds,
  supported recurrence periods, and shared use by comparative and frequency
  patterns. Do not accept arbitrary hyphenated text or match a numeric prefix
  inside a food name.
- [x] **Verify green:** Rerun the focused compound-number, direct-`under`, and
  positive-control tests. Require all selected tests to pass in both modes
  without skips or xfails.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_parser.py`; resolve every failure attributable to
  this task before Task 2.
- [x] Run `uv run ruff format --check src/meal_planner/llm/parser.py
  tests/test_parser.py`, `uv run ruff check src/meal_planner/llm/parser.py
  tests/test_parser.py`, `uv run mypy`, and `git diff --check`. Record exact
  results in this plan.

**Acceptance criteria:**

- [x] Valid supported hyphenated compound quantities fail closed in
  comparative and compact-frequency clauses in both preference modes.
- [x] `under 4 eggs`, `under four eggs`, and a supported compound-word
  equivalent exercise the comparative detector directly and return the
  unchanged malformed-preference clarification.
- [x] Simple number words and digits retain their existing behavior.
- [x] Numeric and hyphenated food names are not blocked when no limiting or
  recurrence shape is present.
- [x] Explicit provider fields, the minimum-one contract, existing recurrence
  periods, and diagnostics remain unchanged.

#### Task 1 evidence

- Red focused run: `uv run pytest tests/test_parser.py -k
  'comparative_quantities_without_fields_fail_closed or
  compact_frequency_without_fields_fails_closed or
  comparative_detector_preserves_unrelated_positive_text'` produced 10
  failures and 56 passes (119 deselected). The six compound comparative
  cases and four compound compact-frequency cases produced strict
  `at_least 1` rules. `under 4 eggs` and `under four eggs` already failed
  closed, independently of recurrence-period wording. The
  `eggs twenty-one each week` cases were already caught by the existing
  expanded `each week` detector.
- Green focused run: `uv run pytest tests/test_parser.py -k
  'comparative_quantities_without_fields_fail_closed or
  compact_frequency_without_fields_fails_closed or
  comparative_detector_preserves_unrelated_positive_text or
  compact_frequency_detector_preserves_numeric_food_controls'` passed:
  72 passed, 113 deselected, in both preference modes.
- Parser regression: `uv run pytest tests/test_parser.py` passed 185 tests.
- Quality gates passed: `uv run ruff format --check
  src/meal_planner/llm/parser.py tests/test_parser.py` (2 files already
  formatted), `uv run ruff check src/meal_planner/llm/parser.py
  tests/test_parser.py` (all checks passed), `uv run mypy` (no issues in 20
  source files), and `git diff --check`.
- Repository-wide verification: the initial `uv run pytest` reached 1,498
  passed and 2 failures because ignored `.aws-sam` artifacts were stale for
  `src/meal_planner/llm/parser.py`. The established
  `uvx --from aws-sam-cli sam build --beta-features` refresh completed, and
  the final `uv run pytest -q` passed 1,500 tests with the same two known
  Pydantic serializer warnings.
- Scope: only the shared bounded number grammar, Task 1 parser regressions,
  and this Task 1 evidence were added. Task 2 slash-whitespace behavior and
  Task 3 verification/archive were not started.

### Task 2: Accept whitespace before compact frequency slashes

**Severity:** P2

**Depends on:** Task 1's shared bounded number grammar

**Files:**

- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: this plan with red/green evidence

**Symbols:** `_COMPACT_FREQUENCY_PATTERN`, `_FREQUENCY_PERIOD`,
`test_compact_frequency_without_fields_fails_closed`,
`test_compact_frequency_detector_preserves_numeric_food_controls`

**TDD sequence:**

- [x] **Failing or missing tests first:** Extend or add mode-parity tests for
  direct slash forms with whitespace before the slash, including
  `eggs 3 / week`, `eggs three / week`, and
  `eggs twenty-one / week`. Include supported `day` and `meal` period
  variants without expanding the period vocabulary.
- [x] Add mode-parity coverage for the `x` slash branch with spacing where the
  existing grammar supports it, such as `eggs 3x / week`, and assert the exact
  malformed-preference clarification.
- [x] Retain numeric-food controls for `5-spice chicken` and `7-layer salad`,
  and add ordinary slash or recipe punctuation that does not form
  `<quantity> / <supported-period>`. Assert strict `at_least 1` and the
  expected meal scope.
- [x] **Expected red failure:** Run the new whitespace-before-slash tests
  before production changes. Record that the reviewed forms currently produce
  strict `at_least 1` rules while the non-frequency controls remain green.
- [x] **Implementation change:** Update only the slash alternatives in
  `_COMPACT_FREQUENCY_PATTERN` to permit optional whitespace between the
  bounded quantity and `/`, including the applicable `x` slash form.
- [x] Keep `_FREQUENCY_PERIOD`, quantity boundaries, and trailing lookarounds
  unchanged. Do not interpret arbitrary fractions, recipe punctuation,
  unsupported periods, or digit-bearing food names as recurrence.
- [x] **Verify green:** Rerun the focused whitespace-before-slash and
  numeric-food-control tests. Require every numeric, simple-word, and
  compound-word case to pass in both modes without skips or xfails.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_parser.py`; resolve every failure attributable to
  this task before Task 3.
- [x] Run `uv run ruff format --check src/meal_planner/llm/parser.py
  tests/test_parser.py`, `uv run ruff check src/meal_planner/llm/parser.py
  tests/test_parser.py`, `uv run mypy`, and `git diff --check`. Record exact
  results in this plan.

**Acceptance criteria:**

- [x] Numeric, simple number-word, and supported compound-number quantities
  separated from `/` by whitespace fail closed in both preference modes.
- [x] Applicable `x` slash forms behave consistently with direct slash forms.
- [x] The recognized recurrence period vocabulary remains limited to `day`,
  `week`, `meal`, and valid plural forms.
- [x] Numeric foods, hyphenated foods, ordinary fractions, and unrelated slash
  punctuation do not become frequency clauses.
- [x] Existing no-space compact frequency forms and all Task 1 cases remain
  green.

#### Task 2 evidence

- Initial red focused run, after the first Task 2 test additions and before
  the later exact `/ week` coverage additions: `uv run pytest
  tests/test_parser.py -k
  'compact_frequency_without_fields_fails_closed or
  compact_frequency_detector_preserves_numeric_food_controls'` produced 6
  failures and 44 passes (145 deselected). The six direct-slash cases with
  whitespace before `/` produced strict `at_least 1` rules in both modes.
  The spaced `x`-slash case and all numeric-food and ordinary-slash controls
  remained green.
- Green focused run: `uv run pytest tests/test_parser.py -k
  'compact_frequency_without_fields_fails_closed or
  compact_frequency_detector_preserves_numeric_food_controls'` passed 54
  tests (145 deselected) in both preference modes after the slash
  alternatives accepted bounded whitespace before `/`, including the exact
  `/ week` cases for numeric, simple-word, and compound-word quantities.
- Parser regression: `uv run pytest tests/test_parser.py` passed 195 tests.
- Quality gates passed: `uv run ruff format --check
  src/meal_planner/llm/parser.py tests/test_parser.py` (2 files already
  formatted), `uv run ruff check src/meal_planner/llm/parser.py
  tests/test_parser.py` (all checks passed), `uv run mypy` (no issues in 20
  source files), and `git diff --check`.
- Repository-wide verification: the initial `uv run pytest` passed 1,508
  tests and failed 2 template tests because the ignored `.aws-sam` artifact
  was stale for `src/meal_planner/llm/parser.py`. The established
  `uvx --from aws-sam-cli sam build --beta-features` command completed
  successfully. The subsequent final `uv run pytest` passed 1,514 tests with
  the same two known Pydantic serializer warnings.
- Scope: only direct and `x` slash alternatives in the compact frequency
  pattern, Task 2 mode-parity controls, and this Task 2 evidence were
  changed. Task 3 verification and archival were not started.

### Task 3: [Final] Verify remediation and archive the plan

**Severity:** Release gate

**Depends on:** Tasks 1 and 2

**Files:**

- Modify, then move on completion:
  `docs/plans/2026-08-26-preference-quantity-classifier-review-remediation.md`
  to `docs/plans/completed/` with the same filename

**Verification sequence:**

- [x] Confirm each P1 and P2 scenario has direct mode-parity coverage, exact
  clarification assertions, and positive boundary controls. Add any missing
  regression to its implementation task before continuing.
- [x] **Expected failure:** No new red test is expected solely for this
  verification task. Any failing acceptance test means the relevant earlier
  task is incomplete and must be corrected before finalization.
- [x] Rerun the combined focused tests for compound-number comparatives,
  direct `under` comparatives, compound-number frequencies,
  whitespace-before-slash frequencies, and numeric/hyphenated food controls.
- [x] **Verify green:** Run `uv run pytest tests/test_parser.py` and confirm
  parser mode parity, clarification, explicit-field, legacy `exact_count`,
  diagnostic, and privacy behavior all pass.
- [x] **Relevant integration tests:** Run `uv run pytest
  tests/test_prompts.py tests/test_bot_handler.py tests/test_template.py`.
- [x] If template tests expose stale ignored `.aws-sam` artifacts, run the
  established `uvx --from aws-sam-cli sam build --beta-features` command and
  rerun the affected integration tests before the full suite.
- [x] Run `uv run ruff format --check .` and require all files to remain
  formatted at the repository's configured 80-column limit.
- [x] Run `uv run ruff check .` and resolve all attributable diagnostics.
- [x] Run `uv run mypy` and resolve all attributable strict typing errors.
- [x] Run `uv run pytest` and require the full repository suite to pass.
  Confirm whether the two known Pydantic serializer warnings remain unchanged
  and document any difference.
- [x] Run `git diff --check` and inspect the accumulated diff. Confirm the
  remediation is limited to `parser.py`, `test_parser.py`, and this plan;
  preserve and do not attribute protected baseline changes in overlapping or
  unrelated files.
- [x] Record exact commands, counts, results, SAM rebuild status, warnings,
  scope deviations, blockers, and residual attribution limitations in this
  plan.
- [x] Confirm no user-facing documentation update is required because this
  remediation closes unsafe classifier gaps without changing the documented
  minimum-one contract.
- [x] Verify every implementation and acceptance checkbox is complete, then
  move only this plan to `docs/plans/completed/`. Do not modify or move the
  archived source or predecessor plans.

**Acceptance criteria:**

- [x] Every P1 and P2 finding has direct passing regression coverage in both
  preference modes.
- [x] The residual direct-`under` coverage gap is closed without relying on a
  recurrence-period noun.
- [x] Focused, parser, integration/template, and full repository suites pass
  with no new skips, xfails, or warnings.
- [x] Ruff format, Ruff lint, strict Mypy, and `git diff --check` pass.
- [x] The final attributable diff is limited to the bounded classifiers,
  focused parser tests, and this plan; protected baseline work remains intact.
- [x] This completed plan is archived without changing any predecessor plan.

#### Task 3 evidence

- Coverage confirmation: Task 1 and Task 2 contain direct mode-parity cases
  for every P1/P2 shape, exact `One or more preference requirements are
  malformed.` assertions for blocked provider payloads, and positive controls
  for `5-spice chicken`, `7-layer salad`, and
  `twenty-one-spice chicken`. Direct `under 4 eggs`, `under four eggs`, and
  `under twenty-one eggs` cases do not rely on a recurrence-period noun.
- Combined focused run: `uv run pytest tests/test_parser.py -k
  'comparative_quantities_without_fields_fail_closed or
  compact_frequency_without_fields_fails_closed or
  comparative_detector_preserves_unrelated_positive_text or
  compact_frequency_detector_preserves_numeric_food_controls'` passed 86
  tests (113 deselected), with both preference modes covered.
- Parser regression: `uv run pytest tests/test_parser.py` passed 199 tests.
  This includes explicit-field, legacy `exact_count`, diagnostic, privacy,
  ambiguity, flexible-strength, clarification, and mode-parity behavior.
- Integration/template regression: `uv run pytest tests/test_prompts.py
  tests/test_bot_handler.py tests/test_template.py` passed 418 tests with
  the two known Pydantic serializer warnings unchanged.
- SAM status: No template test exposed a stale ignored `.aws-sam` artifact,
  so `uvx --from aws-sam-cli sam build --beta-features` was not needed for
  this final verification.
- Formatting and typing: `uv run ruff format --check .` reported 102 files
  already formatted; `uv run ruff check .` reported all checks passed;
  `uv run mypy` reported no issues in 20 source files; and `git diff --check`
  passed.
- Full-suite verification: The first `uv run pytest` run collected 1,514
  tests and reported 1,513 passed, one intermittent failure in the unchanged
  `tests/test_dynamo.py::test_new_profile_creation_is_race_safe`, and the
  same two Pydantic serializer warnings. The isolated command `uv run pytest
  tests/test_dynamo.py::test_new_profile_creation_is_race_safe` then passed
  1 test. The required final `uv run pytest` rerun passed 1,514 tests in
  78.31 seconds with the same two warnings.
- Scope and documentation: No missing in-scope regression was identified and
  no implementation change was made during Task 3. The attributable
  remediation remains limited to the bounded classifier changes, focused
  parser tests, and this plan. Existing protected changes in `parser.py` and
  `tests/test_parser.py`, plus unrelated pre-existing worktree paths, were
  preserved and not attributed to this remediation. No user-facing
  documentation update is required because the documented minimum-one
  contract is unchanged.
- Warnings and blockers: No new skips, xfails, or warnings were introduced.
  The two existing Pydantic serializer warnings remain unchanged. The
  transient Dynamo race failure was not attributable to this remediation and
  passed on isolation and on the final full-suite rerun. No blockers remain.

## Post-Completion

*These activities require external systems or branch authority and are not
part of this implementation plan.*

### Manual verification

- Deploy only through the repository's normal reviewed release process.
- Exercise representative Telegram preference updates for compound-number and
  spaced-slash phrases, confirming the existing clarification is returned and
  no unsafe preference is persisted.
- Inspect bounded CloudWatch diagnostics for unexpected parser warnings while
  preserving the existing privacy contract.

### Version-control and release operations

- Create any branch, commit, pull request, or associated issue only when
  separately authorized.
- Do not include unrelated pre-existing worktree changes in remediation
  commits or review attribution.
