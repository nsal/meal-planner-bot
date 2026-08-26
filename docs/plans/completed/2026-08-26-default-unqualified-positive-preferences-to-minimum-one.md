# Default Unqualified Positive Preferences to Minimum One

## Overview

- Restore `/plan` generation for bare positive food preferences such as
  `eggs for breakfast` after profile-rule enforcement began interpreting
  legacy saved preferences before every plan.
- Treat a supported, unqualified positive food requirement with no explicit
  operator or count as a strict `at_least 1` rule.
- Apply the same deterministic normalization to saved profile preferences and
  request-specific plan preferences while preserving explicit counts,
  exclusions, flexibility, ambiguity, and validation failures.
- Improve schema-failure diagnostics without logging raw dietary text, and
  retain fail-closed behavior for genuinely malformed or unsafe rules.

## Context (from discovery)

- **Project:** Python 3.14 Telegram meal-planning bot using Pydantic models,
  LiteLLM, Lambda planner dispatch, DynamoDB conversation state, pytest, Ruff,
  and strict mypy.
- **Regression source:** `feat(profile): prioritize dietary profile rules`
  made `/plan` interpret legacy saved preferences even when the user replies
  `N, no preference`; a failed stored interpretation therefore blocks all
  plan generation for that profile.
- **Parser gap:** `src/meal_planner/llm/parser.py` defaults missing count fields
  only for narrow wording such as `I'd like` and `please include`. A bare
  provider requirement such as `eggs for breakfast` reaches `DietaryRule`
  without its required `count` and fails schema validation.
- **Prompt mismatch:** `src/meal_planner/llm/prompts.py` defines the minimum-one
  default only for `I'd like` while also warning the model not to invent a
  count, so bare positive requests can legitimately produce the invalid wire
  shape.
- **Affected paths:** both `current_plan_preference` and `stored_preference`
  use `parse_preference_interpretation`; the bot resolves those typed rules
  before horizon validation and planner dispatch.
- **Dependencies:** existing `DietaryRule`, wording classifiers, parser
  normalization, dietary-priority resolution, and bot workflow mocks. No new
  package or persistence schema is required.

## Development Approach

- **Testing approach:** TDD. Add focused failing tests before each production
  change, confirm they fail for the expected reason, then implement the
  smallest change that makes them pass.
- Complete each task fully before moving to the next and mark its checkboxes
  immediately as work completes.
- Keep the behavioral change at the interpretation boundary; do not add a
  planner fallback, silently drop saved preferences, or perform a profile-data
  migration in this fix.
- Every task that changes Python behavior includes separate success and error
  tests, and all focused tests must pass before the next task starts.
- Update this plan if implementation discoveries change scope or sequencing.
- Maintain compatibility with explicit operators, counts, legacy
  `exact_count`, best-effort wording, constraints, and persisted profile
  entries.
- Follow `pyproject.toml` and `AGENTS.md`: use `uv run`, Ruff at 80 columns,
  strict mypy, and a final full pytest run.

## Testing Strategy

- **Parser unit tests:** bare positive requirements in both preference modes,
  multiword foods and meal scopes, explicit operator/count preservation,
  flexible strength, negative wording, malformed values, and ambiguous
  operators.
- **Prompt contract tests:** require the unqualified minimum-one rule and
  remove the contradiction that discourages the provider from emitting the
  application-defined default.
- **Diagnostic tests:** capture schema warnings and verify a stable safe reason
  code and interpretation mode are present while raw source text is absent.
- **Bot workflow tests:** cover `1, no preference` with a legacy saved bare
  preference and a one-day request containing breakfast, lunch, and dinner
  clauses whose provider requirements omit frequency fields.
- **Regression gates:** run focused test modules after every task, then
  `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `uv run pytest` before completion.
- The project has no separate browser end-to-end suite; bot-handler pytest
  cases provide workflow-level integration coverage.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Prefix newly discovered tasks with ➕ and blockers with ⚠️.
- Record the expected red-phase failure before implementing each task.
- Keep this plan synchronized with actual implementation and test scope.
- Do not archive the plan until all acceptance criteria and repository gates
  pass.

## Solution Overview

Normalize a provider-emitted preference requirement before Pydantic model
validation. When the requirement identifies valid foods, contains neither an
explicit operator nor an explicit count, and its bounded source clause is an
unqualified positive request, supply `operator="at_least"`, `count=1`, and the
existing wording-derived strength. This extends the existing normalization
pattern rather than adding a second interpretation path.

Classify bare positive clauses conservatively. Explicit negative or exclusion
wording must never be inverted into a positive minimum, while malformed count
or operator values must remain failures rather than being overwritten. The
prompt must describe exactly the same application-owned rule so the provider
usually returns a complete contract, while parser normalization remains the
deterministic reliability boundary.

Retain the current fail-closed workflow for errors outside this narrow default.
Saved profile preferences are not silently ignored. Add safe structured log
context for schema failures so operators can distinguish an invalid count,
food, scope, or other contract error without recording user preference text.

## Technical Details

- Extend the wording classification used by `_prepare_rule_payload` so bare
  positive food clauses can receive the existing missing-field default.
- Require a valid, non-empty `foods_any_of` candidate before applying the bare
  default; final food normalization and validation remain owned by
  `DietaryRule`.
- Do not default when either `count` or `operator` is present, including values
  that are malformed. Explicit provider intent must be validated as supplied.
- Exclude bounded negative forms such as `no`, `avoid`, `without`, and
  `exclude` from positive defaulting. Their provider output must include a
  valid explicit zero-count rule or return clarification.
- Preserve `exact_count` compatibility and the existing precedence of
  flexible wording (`if convenient`, `if possible`, and equivalents) when
  deriving `strength`.
- Keep ambiguous-operator checks ahead of model construction so contradictory
  wording still returns bounded clarification.
- Update the interpretation prompt to state that every unqualified positive
  food preference without a frequency means `at_least 1`; qualify the
  no-invention instruction so it does not contradict this application-owned
  default.
- Derive a bounded diagnostic reason from Pydantic validation metadata or a
  small application allowlist. Log only the interpretation mode and reason;
  never include `source_text`, foods, the provider payload, or validation input
  values.
- Do not change `DietaryRule` field defaults. The inference belongs to request
  interpretation and must not silently affect programmatic or persisted model
  construction elsewhere.

## What Goes Where

- **Implementation Steps:** add red parser tests, implement the narrow
  normalization, align the prompt, add safe diagnostics, exercise complete bot
  workflows, and run repository-wide verification.
- **Post-Completion:** manually verify the deployed Telegram workflow and
  CloudWatch diagnostics, then create a Conventional Commit and pull request
  on a dedicated bug-fix branch referencing the associated GitHub issue.

## Implementation Steps

### Task 1: Normalize bare positive requirements to a minimum of one

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`

- [x] Write failing parser tests for `eggs for breakfast`,
  `bean soup for lunch`, and `halloumi for dinner` with omitted operator and
  count in both `stored_preference` and `current_plan_preference` modes.
- [x] Run the focused new tests and record that they fail with the malformed
  preference-requirement clarification before production changes.
- Red phase: the seven new bare-positive and flexible-wording cases returned
  `One or more preference requirements are malformed.` before parser changes;
  the nine negative, malformed, empty-food, conflict, and legacy-boundary
  cases passed as expected.
- [x] Write failing boundary tests proving explicit operators/counts and
  legacy `exact_count` remain unchanged and flexible wording remains
  `best_effort`.
- [x] Write failing error tests proving negative wording with missing fields,
  malformed explicit values, empty foods, and conflicting operators are not
  converted to `at_least 1`.
- [x] Extend the parser's positive-wording classification and rule-payload
  preparation with the conservative minimum-one default.
- [x] Run `uv run pytest tests/test_parser.py`; all tests must pass before
  Task 2.

### Task 2: Align the provider prompt with deterministic normalization

**Files:**
- Modify: `tests/test_prompts.py`
- Modify: `src/meal_planner/llm/prompts.py`

- [x] Write failing prompt tests requiring bare positive food wording to map
  to strict `at_least 1` in both stored and current preference modes.
- [x] Write a failing prompt test proving explicit count/operator intent and
  exclusion wording remain provider-owned rather than defaulted.
- [x] Run the focused new tests and record the expected red phase before
  changing the prompt. Red phase: all three focused tests failed because the
  prompt lacked the generalized minimum-one rule and explicit-intent wording.
- [x] Generalize the minimum-one instruction beyond `I'd like` and qualify the
  no-invention instruction to preserve explicit user intent.
- [x] Review the output-schema example for consistency without expanding the
  wire contract or adding new fields.
- [x] Run `uv run pytest tests/test_prompts.py`; all tests must pass before
  Task 3.

### Task 3: Add privacy-safe schema failure diagnostics

**Files:**
- Modify: `tests/test_parser.py`
- Modify: `src/meal_planner/llm/parser.py`

- [x] Write failing `caplog` tests for representative invalid food, count, and
  scope payloads, requiring a stable safe reason code and interpretation mode.
- [x] Write a failing privacy test proving warnings omit source text, food
  values, provider payloads, and Pydantic input values.
- [x] Run the focused diagnostic tests and record the current generic warning
  as the expected red phase. Red phase: the three reason-code cases failed
  because the parser returned a bare-positive rule or emitted a generic
  warning without `reason_code` and `interpretation_mode`; the privacy test
  passed because the existing warning did not include provider values.
- [x] Add a small typed or allowlisted validation-reason mapper and structured
  warning fields without changing user-facing clarification text.
- [x] Preserve bounded fail-closed clarification for every schema error not
  repaired by Task 1.
- [x] Run `uv run pytest tests/test_parser.py`; all tests must pass before
  Task 4.

### Task 4: Cover saved and current `/plan` workflows end to end

**Files:**
- Modify: `tests/test_bot_handler.py`
- Modify: `src/meal_planner/bot_handler.py` only if the regression tests expose
  an integration defect outside parser normalization

- [x] Write a failing regression test where `1, no preference` and a legacy
  saved `eggs for breakfast` entry produce a stored strict `at_least 1` rule
  and reach planner dispatch.
- [x] Write a failing regression test where
  `1, eggs for breakfast, bean soup for lunch, halloumi for dinner` produces
  three current strict minimum-one rules with the correct meal scopes and
  reaches planner dispatch.
- [x] Assert both dispatched events contain the normalized rules in
  `effective_rules`, retain `plan_days=1`, and do not use the legacy
  `requirements` field.
- [x] Add error-path assertions proving a genuinely malformed saved preference
  still blocks dispatch with actionable saved-preference guidance.
- [x] Run the focused new bot tests and confirm the red phase is caused by the
  missing default before relying on the Task 1 implementation.
- [x] Make only the smallest bot integration correction if required; do not
  bypass saved rules or broaden planner fallback behavior.
- [x] Run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 5.

Red phase: the Task 1 parser red-phase evidence established that provider
requirements for these bare positive clauses returned the malformed-rule
clarification before normalization. The focused Task 4 regressions were added
after that implementation was present and passed without a bot integration
change; malformed explicit counts still fail closed.

### Task 5: Verify acceptance criteria and repository quality gates

**Files:**
- Modify: this plan if verification discovers scope changes or blockers

- [x] Verify bare positive requirements normalize identically in stored and
  current preference modes.
- [x] Verify explicit counts, operators, exclusions, ambiguity, and flexible
  wording retain their prior behavior.
- [x] Verify `/plan` dispatches for both reported one-day workflows while
  malformed or unsafe saved rules still fail closed.
- [x] Run `uv run ruff format --check .` and fix any formatting failures.
- [x] Run `uv run ruff check .` and fix any lint failures.
- [x] Run `uv run mypy` and fix any strict type failures.
- [x] Run `uv run pytest` and fix all failures before Task 6.

### Task 6: [Final] Update documentation and archive the completed plan

**Files:**
- Modify: `docs/prompt.md`
- Modify: `README.md` if user-facing preference semantics are documented there
- Modify: `tests/test_readme.py` if README contract assertions change
- Move after completion:
  `docs/plans/2026-08-26-default-unqualified-positive-preferences-to-minimum-one.md`
  to `docs/plans/completed/`

- [x] Document that an unqualified positive food preference means strict
  `at_least 1` for both saved and request-specific plan preferences.
- [x] Document that explicit counts, exclusions, and best-effort qualifiers
  override the default and malformed saved rules remain blocking.
- [x] Update README contract tests for any changed user-facing documentation.
- [x] Run `uv run pytest tests/test_readme.py` when README assertions change.
- [x] Re-run `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest` after documentation changes.
- [x] Confirm every plan checkbox is complete and move this file to
  `docs/plans/completed/`.

## Post-Completion

**Manual verification:**

- Deploy from a dedicated bug-fix branch through the normal pull-request
  workflow; never push or merge directly to `master`.
- In Telegram, create or retain a legacy saved `eggs for breakfast`
  preference, start `/plan`, submit `1, no preference`, and confirm generation
  starts without a malformed-requirement response.
- Start a fresh `/plan`, submit
  `1, eggs for breakfast, bean soup for lunch, halloumi for dinner`, and verify
  a one-day draft contains all three meal scopes.
- Check CloudWatch for the absence of schema warnings on valid bare positive
  preferences and verify intentionally malformed fixtures emit only the safe
  mode and reason fields.

**External workflow:**

- Create a Conventional Commit referencing the GitHub issue created for this
  plan.
- Open a pull request from the bug-fix branch and link both the issue and this
  completed plan.
- After completion, comment on the issue with the commit or pull-request link
  and a concise verification summary, as required by `AGENTS.md`.
