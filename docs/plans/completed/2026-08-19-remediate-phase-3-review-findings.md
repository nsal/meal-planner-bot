# Remediate Phase 3 Review Findings

## Overview

Remediate all five findings from the read-only Phase 3 review of the completed
meal-plan preference follow-up. The work first bounds requirement-derived
Telegram output and accounts for the complete success-path delivery budget.
It then preserves exact meal slots in repair feedback, corrects ambiguous
`-ies` normalization, makes preference-clarification overflow recoverable,
and sanitizes the model label used in failure logs.

This is a planning-only document. No remediation implementation is included.
Implementation must use TDD: add and run each missing test first, record the
expected failure that proves the defect, make only the scoped implementation
change, and rerun the new and regression tests before starting the next task.

## Context

### Completed implementation baseline

- The implementation immediately preceding this review is archived at
  `docs/plans/completed/`
  `2026-08-19-remediate-review-findings-follow-up.md`.
- Its completed results were:
  1. exact table-scoped `TransactWriteItems` permission for
     `PlannerFunction`, with deployed-role resolution and simulation for both
     Bot and Planner;
  2. bounded `repair_id` invariants and atomic plan-plus-marker publication
     with `PUBLISHED`, `STALE`, and `DUPLICATE` outcomes, unchanged UUID
     forwarding, and silent duplicate replay;
  3. rejection of normalized-empty food alternatives in schemas and parsing;
  4. named clarification limits and an application-owned bounded fallback;
  5. propagation of generation start time into terminal validation telemetry;
     and
  6. propagation of the active attempt through recovery and notification
     telemetry.
- Focused verification reported 465 passed and 2 skipped. Full verification
  reported 589 passed and 2 skipped. Ruff format, Ruff lint, strict Mypy, and
  `git diff --check` all passed, and no implementation blocker was reported.
- The current working tree contains that completed implementation. Preserve
  it and do not rewrite any archived plan.
- The relevant archived plan hashes at planning time are:
  - Phase 3 follow-up:
    `d86fd6d54b40bd98b0bb1fd17b982677a20d463f7993b38ddf1d20418300b52c`;
  - preceding review remediation:
    `0a7e76d3d89eb99fba15c937b8e8f6df82eca62aa8959a27ea14cc700a4b7443`;
    and
  - evidence-validation plan:
    `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`.

### Current implementation patterns

- `format_satisfaction_summary()` renders every alternative for every
  requirement without an aggregate bound. `TelegramAPI.send_message()` splits
  text at 4,096 characters, so one call can become many sequential Telegram
  HTTP requests.
- A successful requirement-aware generation persists first, then calls
  `send_plan()`, sends the satisfaction summary, and sends the review prompt.
  The Planner configuration currently budgets only two Telegram requests.
- Completeness validation retains a typed `MealType`, but
  `_validation_location()` reduces every meal-specific issue to
  `days[n].meals` before repair feedback is constructed.
- The generation prompt shows a JSON example but does not explicitly state the
  complete daily meal, ingredient, and calorie contract enforced after the
  model returns.
- `_normalize_plural()` rewrites every `-ies` token to `-y`, which handles
  `berries` but corrupts regular `-ie` words such as `cookies` and `pies`.
- Clarification replies are joined to the saved preference with `"; "`.
  A saved preference near the 500-character limit therefore cannot accept any
  non-empty answer, and the current rejection does not explain how to reset
  and restate the full preference.
- `_model_name()` documents a bounded label but returns every non-empty string
  unchanged, including oversized and line-breaking values.

### Project constraints

- Follow `AGENTS.md` and `pyproject.toml`: Python 3.14, Ruff at 80 columns,
  strict Mypy, and Pytest.
- Use `uv run` for all Python tools and tests and Ruff for formatting and
  linting.
- Do not add a dependency. The standard library and existing packages are
  sufficient.
- Preserve transactional publication, repair replay suppression, tracked
  request ownership, confirmed-plan protection, and the one-provider-call per
  Planner invocation invariant.
- Keep user-facing output and logs bounded and privacy-safe. Do not log raw
  prompts, preferences, plans, meals, ingredients, user IDs, chat IDs,
  credentials, raw events, or provider exception text.
- Do not combine unrelated cleanup with these remediations.
- Never push or merge directly to `master`. Implementation must use a
  dedicated branch and pull request.
- A later commit must use Conventional Commits and include the associated
  issue number. After implementation, comment on that issue with the commit or
  pull request link.

## Complete Review Findings

1. **P2 — Bound requirement-derived Telegram output and account for every
   send.** In `src/meal_planner/preferences.py:306`, 20 requirements can each
   contain 20 alternatives of 100 characters. The current summary can exceed
   41,000 characters and become about 20 Telegram requests. Terminal
   compliance messages in `src/meal_planner/planner_handler.py:324` can also
   exceed one message. Ordinary success now makes three Telegram calls after
   persistence and state clearing: plan, summary, and follow-up. The Planner
   budget in `src/meal_planner/config.py:176` reserves only two, which can
   consume the deadline after durable state changes. Add a compact,
   application-owned aggregate bound that fits one Telegram chunk, budget at
   least three success-path sends, update README calculations, and add
   max-shape summary, terminal, and configuration-boundary tests.
2. **P2 — Preserve exact meal slot in repair feedback.** In
   `src/meal_planner/planner_handler.py:435`, `_validation_location()` checks
   `meal_type` and then discards it. Breakfast, lunch, dinner, and snack
   failures all become `days[n].meals`; an empty day produces three
   indistinguishable entries. The repair invocation does not include the
   rejected plan, and `src/meal_planner/llm/prompts.py:275` does not explicitly
   require breakfast, lunch, and dinner every day or complete ingredients and
   positive calories. Include only the safe `MealType` enum in repair
   locations and state the complete generated-plan contract in both initial
   and repair prompts. Test distinct slots and prompt invariants.
3. **P2 — Handle regular `-ie` plurals without changing the stem.** In
   `src/meal_planner/normalization.py:19`, every `-ies` token becomes `-y`.
   This turns cookies, brownies, and smoothies into cooky, browny, and smoothy
   and prevents singular matching and duplicate/conflict alignment. Use a
   conservative shared representation for both `-y`/`-ies` and
   `-ie`/`-ies`. Add matcher, schema-duplicate, and parser-conflict coverage
   for cookie/cookies, brownie/brownies, smoothie/smoothies, pie/pies, and the
   existing berry/berries case.
4. **P2 — Recover when clarification exceeds the preference cap.** In
   `src/meal_planner/bot_handler.py:730`, an initial preference of 499 or 500
   characters can enter clarification, but every non-empty answer adds the
   `"; "` separator and exceeds the 500-character schema cap. The handler
   rejects each answer without changing state and gives no actionable reset or
   full-restatement path. Detect combined overflow without semantic
   truncation and direct the user to an explicit reset and complete
   restatement, or reserve room before clarification. Add two-turn 498-500
   boundary tests with a short answer and actionable recovery.
5. **P3 — Actually bound and sanitize the model label in failure logs.** In
   `src/meal_planner/planner_handler.py:472`, `_model_name()` claims to return a
   bounded value but accepts any non-empty configuration string. An oversized
   or newline-bearing value can inflate or spoof CloudWatch records. Enforce a
   fixed maximum and safe character set with an `unknown` fallback. Add tests
   for oversized, whitespace-only, control-character, and newline values.

All five findings must remain independently traceable to a focused test,
documented expected failure, implementation step, passing verification, and
acceptance criterion.

## Development Approach

- **Testing approach:** TDD.
- Complete tasks in severity and dependency order:
  1. bound requirement output and correct the Planner delivery budget;
  2. preserve meal slots and make the generation contract explicit;
  3. correct plural equivalence used by matching, schemas, and parsing;
  4. make clarification-cap overflow recoverable;
  5. sanitize the model label; and
  6. run final verification.
- Task 1 comes first because unbounded delivery happens after persistence and
  can exhaust the Planner deadline. Task 2 precedes the other semantic fixes
  because it improves the one allowed repair attempt. Task 3 precedes Bot
  recovery because its normalization output is consumed by schemas and the
  parser. Task 5 has no dependency on the P2 behavior and remains last by
  severity.
- For every remediation task:
  1. add or update the missing test;
  2. run it before implementation and confirm the expected defect;
  3. make only the task's implementation change;
  4. rerun the new test and confirm it passes; and
  5. run all listed regression tests before proceeding.
- Record the exact failing and passing commands and results in this plan during
  implementation. Do not weaken assertions merely to make a test pass.
- Mark checkboxes immediately. Add newly required work with a `➕` prefix and
  blockers or deviations with a `⚠️` prefix.
- Keep the archived plans unchanged and update only this active plan's
  progress record during implementation.

## Solution Overview

Requirement-derived output will have one named aggregate limit of 1,000
characters, comfortably below Telegram's 4,096-character limit. Summary
formatting will normalize display whitespace, show only a compact bounded set
of alternatives and requirement lines, and use an application-owned omitted
count instead of splitting or blindly truncating user/model text. Terminal
compliance formatting will use the same aggregate limit and omit whole clauses
with a deterministic count when they cannot fit. Each requirement-derived
message will therefore be one Telegram chunk. Planner generation budgeting
will reserve three sequential Telegram request allowances, making the default
budget 290 seconds: 240 seconds for LiteLLM, three 10-second Telegram
allowances, and the 20-second safety margin.

Repair locations will include the validated enum value, for example
`days[0].meals.breakfast`. Empty-day feedback will therefore distinguish
breakfast, lunch, and dinner without including meal names or other model
content. The plan prompt will explicitly require exactly one breakfast, lunch,
and dinner on every one of seven days, make snack optional, and require every
present meal to have at least one non-empty ingredient item and a positive
`est_calories`. The contract will appear whether or not bounded repair
feedback is present; the rejected plan will not be copied into the repair
event.

Plural normalization will canonicalize the ambiguous family to its shared
plural spelling instead of guessing a singular stem. Existing `-ies` tokens
will remain `-ies`; consonant-plus-`y` singulars will canonicalize to `-ies`;
and `-ie` singulars will append `s`. Thus berry/berries both become
`berries`, while cookie/cookies both become `cookies`. Existing conservative
handling for ordinary terminal `s` remains in place.

When a saved clarification preference plus `"; "` and the new answer would
exceed 500 characters, the Bot will not truncate, append, reinterpret,
transition state, or invoke Planner. It will send one application-owned
message explaining that the answer was not appended and instructing the user
to run `/plan` and send one complete preference of 500 characters or fewer.
The existing `/plan` replacement behavior is the explicit reset path.

Finally, model labels will be accepted only when they are 1-64 characters and
fully match an allowlist suitable for common provider/model identifiers:
ASCII letters, digits, `.`, `_`, `-`, `/`, `:`, and `+`, beginning with an
alphanumeric character. Any non-string, blank, whitespace-bearing,
control-bearing, newline-bearing, oversized, or otherwise invalid value will
be logged as `unknown`.

## Technical Details

### Requirement output and delivery budget

- Define `MAX_REQUIREMENT_MESSAGE_LENGTH = 1000` in
  `src/meal_planner/preferences.py` and keep it below
  `meal_planner.telegram.api.MAX_MESSAGE_LENGTH` by test.
- Refactor `format_satisfaction_summary()` to render deterministic compact
  lines. Normalize embedded whitespace, cap alternatives shown per
  requirement, and append an application-owned `(+N alternatives)` marker.
- Stop adding whole requirement lines before the aggregate limit would be
  exceeded. Add one final application-owned line reporting how many
  requirements were omitted. Never slice through a food or clause in a way
  that can change its meaning.
- Add a formatter for unmet saved clauses and call it from
  `_generation_failure_message()` for compliance failures. Include whole,
  whitespace-normalized clauses only while they fit, then report the omitted
  count. Preserve the existing no-draft, retained-preference, and `/plan`
  recovery text inside the same aggregate bound.
- Set `telegram_request_count=3` for `planner_generation_budget`. Do not change
  grocery budgeting, Bot budgeting, request timeout defaults, the Planner
  deadline, or the Lambda timeout.
- Update README calculations from two Telegram allowances and 280 seconds to
  three allowances and 290 seconds. Explain that the three success-path sends
  are plan, bounded summary, and review follow-up.

### Exact repair location and prompt contract

- Change `_validation_location()` to return
  `days[<zero-based day>].meals.<meal_type>` when both day and typed meal slot
  are present. Values must come only from `MealType.value`; do not interpolate
  meal names, ingredient text, raw model locations, or arbitrary strings.
- Keep requirement issues at `requirements` and plan-wide issues at `plan`.
  Preserve the 800-character feedback transport bound and existing issue
  codes.
- Add an explicit generated-plan contract to `build_plan_prompt()` before the
  optional repair block so initial and repair prompts receive identical
  invariants. The contract must match application validation rather than add a
  stricter undocumented rule.
- Keep the repair event bounded and do not add the rejected plan to it.

### Conservative plural equivalence

- Update only the token-level plural normalization needed for this finding.
- Apply rules in this order so the general terminal-`s` rule cannot corrupt an
  `-ies` token:
  1. retain a token already ending in `ies` as the canonical form;
  2. convert a consonant-plus-`y` singular to `ies`;
  3. append `s` to an `-ie` singular; and
  4. apply the existing conservative terminal-`s` handling to other tokens.
- Keep normalization dependency-neutral, Unicode/case/whitespace behavior
  unchanged, and continue returning `NormalizedFood` tuples. This allows
  `matches_food()`, schema duplicate detection, and parser signatures to align
  without separate special cases.

### Clarification-cap recovery

- Distinguish an oversized first preference from overflow caused by joining a
  saved preference and clarification answer.
- For combined overflow, preserve the current durable state and saved original
  preference. Do not call the interpreter, Planner, or persistence transition
  for the rejected answer.
- Send a bounded deterministic response stating that the answer was not
  appended and directing the user to `/plan` to restart with one complete
  preference of at most 500 characters.
- Retain normal two-turn combination at or below 500 characters and existing
  duplicate-update and state-conflict behavior.

### Safe model labels

- Define a private fixed maximum and compiled full-match pattern in
  `planner_handler.py`.
- `_model_name()` must return the original label only when the entire value is
  valid. It must not truncate, replace characters, or strip a malicious suffix
  into validity; invalid values become exactly `unknown`.
- Continue using `_model_name()` at all existing `_log_safe_failure()` and
  `_log_llm_failure()` call sites so both the formatted warning and structured
  `extra` metadata receive the same safe value.

## Testing Strategy

- **Maximum shapes:** construct all 20 requirements with all 20 alternatives
  at their 100-character bound and source clauses at 500 characters. Verify
  summary and terminal outputs are deterministic, at most 1,000 characters,
  and produce one `split_text()` chunk.
- **Delivery budget:** test the exact 290-second default generation budget and
  the pass/fail boundary around it. Keep the 275-second grocery calculation
  independently covered.
- **Repair feedback:** test breakfast, lunch, dinner, and snack locations plus
  all three missing slots on an empty day. Verify repair payloads remain
  bounded and contain no meal names or ingredient text.
- **Prompts:** assert the complete daily contract in an initial prompt and a
  prompt with repair feedback. Keep existing raw preference, exact-rule,
  schema, profile, and repair-feedback assertions.
- **Plural handling:** use both singular-to-plural and plural-to-singular
  matcher directions. Test duplicate alternatives in Pydantic schemas and
  conflicting counts in parser output for every required word pair, plus
  non-equivalent controls.
- **Clarification overflow:** exercise genuine two-turn state transitions for
  initial lengths 498, 499, and 500 followed by a short answer. Confirm no
  semantic truncation or second interpretation and verify `/plan` can replace
  the pending state.
- **Logging:** exercise `_model_name()` boundaries and a real failure-log path.
  Assert one bounded record, `model == "unknown"` for unsafe input, and absence
  of injected lines or control text from captured logs.
- **Regression:** run each task's focused files before moving on, then all
  project quality gates in Task 6.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Add `➕` tasks only when required for an acceptance criterion.
- Record blockers or deviations with `⚠️` and include the failing command.
- Update this plan if implementation scope changes.
- Move this plan to `docs/plans/completed/` only after Task 6 passes.

## Implementation Steps

### Task 1: Bound requirement output and budget all success-path sends

**Finding:** P2 — Maximum requirement shapes can create more than 41,000
characters, about 20 Telegram requests, and oversized terminal compliance
messages. The persisted success path performs plan, summary, and follow-up
sends while configuration budgets only two Telegram allowances.

**Files and symbols:**

- Modify: `src/meal_planner/preferences.py` —
  `MAX_REQUIREMENT_MESSAGE_LENGTH`, compact requirement rendering,
  `format_satisfaction_summary()`, and a bounded unmet-clause formatter
- Modify: `src/meal_planner/planner_handler.py` —
  `_generation_failure_message()` and requirement-summary delivery
- Modify: `src/meal_planner/config.py` —
  `_SharedSettings.validate_function_budgets()`
- Modify: `README.md` — Planner external-call budget calculation
- Modify: `tests/test_preferences.py` — maximum-shape summary tests
- Modify: `tests/test_planner_handler.py` — maximum-shape success and terminal
  compliance delivery tests
- Modify: `tests/test_config.py` — three-send budget boundary tests
- Modify: `tests/test_readme.py` — documented budget calculation
- Use for assertions only: `src/meal_planner/telegram/api.py` —
  `MAX_MESSAGE_LENGTH` and `split_text()`

1. [x] **Test first:** add a maximum-shape satisfaction-summary test with 20
   requirements, 20 alternatives per requirement, and 100-character food
   values. Assert deterministic application-owned compaction, an explicit
   omitted count where needed, total length at most 1,000, and exactly one
   `split_text()` chunk. Retain the existing small-summary wording test.
   Added before implementation; the focused TDD command is recorded below.
2. [x] **Test first:** add Planner tests for a successful maximum-shape
   requirement set and a terminal compliance failure with twenty
   500-character source clauses. Assert the summary and terminal message each
   fit the same aggregate bound and one Telegram chunk. On success, assert the
   ordered delivery sequence remains plan, summary, then review follow-up.
   Added before implementation; the focused TDD command is recorded below.
3. [x] **Test first:** add configuration boundary tests proving Planner
   generation reserves three 10-second Telegram requests: 290 seconds passes
   exactly with the default 240-second LLM request and 20-second margin, while
   any configured deadline below 290 seconds fails. Add a README test for
   `three` allowances, the three named sends, and the 290-second total. Added
   before implementation; the focused TDD command is recorded below.
4. [x] **Expected failure:** run the new tests and confirm the current summary
   exceeds Telegram's one-message boundary, terminal compliance output grows
   with every 500-character clause, the configured calculation returns 280
   seconds and accepts a deadline below 290, and README still documents two
   allowances and 280 seconds. Command: `uv run pytest` with the seven new
   test node IDs listed in the Task 1 TDD command below. Result: 6 failed,
   1 passed; summary was 5,362 characters, terminal output was 10,142
   characters, the 289-second deadline was accepted, and README assertions
   failed.
5. [x] **Implementation:** add deterministic bounded formatters for accepted
   and unmet requirements, route success and terminal compliance output
   through them, set Planner generation `telegram_request_count` to 3, and
   update README calculations. Do not alter persistence order, publication
   outcomes, Telegram client chunking, grocery budgets, or timeout defaults.
6. [x] **Verify the fix:** rerun each new test and confirm maximum schema
   shapes produce one bounded requirement-derived chunk, ordinary success has
   exactly three budgeted sends, and the exact 290-second boundary behaves as
   documented. Command: the seven new test node IDs listed in the Task 1 TDD
   command below. Result: 7 passed.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_preferences.py tests/test_planner_handler.py
   tests/test_config.py tests/test_readme.py tests/test_telegram_api.py` and do
   not begin Task 2 until it passes. Result: 171 passed.

**Task 1 verification record (2026-08-19):**

- TDD command: `uv run pytest tests/test_preferences.py::test_satisfaction_summary_compacts_maximum_requirement_shape tests/test_preferences.py::test_unmet_clause_message_compacts_maximum_source_clauses tests/test_planner_handler.py::test_generate_plan_maximum_requirements_sends_one_bounded_summary tests/test_planner_handler.py::test_terminal_compliance_delivery_compacts_maximum_source_clauses tests/test_config.py::test_config_planner_generation_budget_requires_three_telegram_sends tests/test_config.py::test_config_rejects_planner_generation_budget_below_290_seconds tests/test_readme.py::test_readme_documents_three_planner_success_sends_and_budget`. Before implementation: 6 failed, 1 passed. After implementation: 7 passed.
- Regression command: `uv run pytest tests/test_preferences.py tests/test_planner_handler.py tests/test_config.py tests/test_readme.py tests/test_telegram_api.py`. Result: 171 passed.
- `uv run ruff format src/meal_planner/preferences.py tests/test_preferences.py tests/test_planner_handler.py`: 3 files reformatted; subsequent `uv run ruff format --check` passed for 7 Python files.
- After tightening the maximum-shape fixture to 100-character food values,
  `uv run ruff format tests/test_preferences.py` reformatted 1 file; the
  focused seven-test command still returned 7 passed.
- `uv run ruff check --fix src/meal_planner/planner_handler.py`: 1 import-order issue fixed; subsequent scoped `uv run ruff check` passed.
- `uv run mypy && uv run pytest && git diff --check`: Mypy success with no
  issues in 19 source files; Pytest 596 passed, 2 skipped; diff check passed.
  The two retained skips are the existing SAM-artifact-dependent tests.

**Acceptance criteria:**

- Every satisfaction summary and terminal compliance message is at most 1,000
  characters and strictly below Telegram's 4,096-character limit.
- Maximum valid requirement shapes produce one Telegram chunk, never a fanout
  proportional to requirement or alternative count.
- Compaction is deterministic and application-owned. It normalizes display
  whitespace and omits whole details with an explicit count instead of
  blindly slicing semantic text.
- The accepted plan, bounded summary, and review follow-up remain ordered after
  successful persistence; no new delivery occurs before publication.
- Planner generation reserves at least three sequential Telegram request
  allowances. With current defaults its external-call budget is 290 seconds
  and still fits the 300-second application deadline.
- README and executable configuration validation agree at the exact boundary.
- Bot and grocery budgets, Lambda timeout, provider-call count, and publication
  semantics are unchanged.

### Task 2: Preserve exact meal slots and state the generation contract

**Finding:** P2 — Repair feedback discards a validated meal slot, making
breakfast, lunch, dinner, and snack failures indistinguishable. Empty days
produce three identical locations, while initial and repair prompts do not
explicitly state the complete contract enforced after generation.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` —
  `_validation_location()` and `_validation_feedback()` behavior
- Modify: `src/meal_planner/llm/prompts.py` — `build_plan_prompt()`
- Modify: `tests/test_planner_handler.py` — slot-specific repair feedback and
  empty-day repair payload tests
- Modify: `tests/test_prompts.py` — initial and repair prompt invariants
- Use for fixtures/assertions: `src/meal_planner/preferences.py` —
  `ValidationIssue` and `validate_generated_plan()`

1. [x] **Test first:** add parametrized feedback tests for breakfast, lunch,
   dinner, and snack issues. Require exact safe locations such as
   `days[0].meals.breakfast`, distinct values for each slot, the existing
   stable code, and no meal name or ingredient content.
2. [x] **Test first:** generate validation feedback for an empty day and assert
   its missing breakfast, lunch, and dinner issues remain three distinct coded
   locations in the queued repair payload. Assert the payload remains at most
   800 characters and the rejected plan is not forwarded.
3. [x] **Test first:** add one initial-prompt test and one repair-prompt test
   requiring exactly one breakfast, lunch, and dinner on each of seven days,
   optional snacks only, at least one non-empty ingredient item for every
   present meal, and positive `est_calories`.
4. [x] **Expected failure:** run the new tests and confirm every meal issue
   currently renders as `days[n].meals`, all empty-day entries are
   indistinguishable, and neither initial nor repair prompt explicitly states
   the complete validator-aligned contract.
   Result: 7 failed. The four slot cases and the empty-day repair payload all
   rendered `days[0].meals`; both initial and repair prompts lacked the
   complete contract assertions.
5. [x] **Implementation:** append only `MealType.value` to meal-specific
   locations and add the complete generated-plan contract to the shared plan
   prompt. Keep requirements and plan-wide locations, issue codes, feedback
   bounds, repair dispatch, and event shape unchanged.
6. [x] **Verify the fix:** rerun each new test and confirm all four enum slots
   remain distinguishable, empty-day repair feedback identifies three exact
   slots, and both prompt modes contain the same complete contract.
   Result: 7 passed.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py tests/test_prompts.py
   tests/test_preferences.py tests/test_parser.py` and do not begin Task 3
   until it passes.
   Result: 193 passed.

**Task 2 verification record (2026-08-19):**

- TDD command: `uv run pytest tests/test_planner_handler.py::test_validation_feedback_preserves_exact_meal_slot tests/test_planner_handler.py::test_empty_day_repair_feedback_preserves_required_slots tests/test_prompts.py::test_build_plan_prompt_states_complete_generated_plan_contract tests/test_prompts.py::test_repair_plan_prompt_states_same_complete_generated_plan_contract`. Before implementation: 7 failed. After implementation: 7 passed.
- Regression command: `uv run pytest tests/test_planner_handler.py tests/test_prompts.py tests/test_preferences.py tests/test_parser.py`. Result: 193 passed.
- `uv run ruff format --check src/meal_planner/planner_handler.py src/meal_planner/llm/prompts.py tests/test_planner_handler.py tests/test_prompts.py`: passed after Ruff reformatted the planner handler.
- `uv run ruff check src/meal_planner/planner_handler.py src/meal_planner/llm/prompts.py tests/test_planner_handler.py tests/test_prompts.py`: passed.
- `uv run mypy`: passed with no issues in 19 source files.
- `uv run pytest`: 603 passed, 2 existing SAM-artifact skips.

**Acceptance criteria:**

- Meal-specific repair locations contain the zero-based day and exact safe
  enum slot for breakfast, lunch, dinner, or snack.
- An empty day yields distinct missing-slot entries for breakfast, lunch, and
  dinner.
- Repair feedback contains no raw plan, meal name, ingredient, preference, or
  other provider-controlled content and remains bounded to 800 characters.
- Initial and repair prompts explicitly require the same seven-day meal,
  ingredient, and positive-calorie contract that application validation
  enforces.
- The rejected plan is not added to repair events or prompts.
- No persistence, ownership, retry, validation-category, or provider-call
  behavior changes.

### Task 3: Normalize both `-y` and `-ie` singulars with `-ies` plurals

**Finding:** P2 — Unconditionally singularizing `-ies` to `-y` corrupts
cookies, brownies, and smoothies. Matching, duplicate rejection, and parser
conflict detection therefore disagree for ordinary `-ie` words.

**Files and symbols:**

- Modify: `src/meal_planner/normalization.py` — `_normalize_plural()`
- Modify: `tests/test_preferences.py` — `matches_food()` plural-equivalence
  cases
- Modify: `tests/test_schemas.py` — normalized duplicate alternatives
- Modify: `tests/test_parser.py` — normalized direct-count conflicts

1. [x] **Test first:** extend matcher cases in both directions for
   cookie/cookies, brownie/brownies, smoothie/smoothies, pie/pies, and
   berry/berries. Add controls proving unrelated stems and embedded substrings
   still do not match. Added before implementation; the focused TDD command is
   recorded below.
2. [x] **Test first:** add schema cases rejecting each singular/plural pair in
   one `foods_any_of` list as duplicate alternatives, including the existing
   berry pair. Retain valid distinct-alternative controls. Added before
   implementation; the focused TDD command is recorded below.
3. [x] **Test first:** add parser cases in which two requirements use singular
   and plural forms of each pair with the same meal scope but different exact
   counts. Require the existing bounded conflict clarification. Added before
   implementation; the focused TDD command is recorded below.
4. [x] **Expected failure:** run the new tests and confirm berry/berries still
   aligns, while cookies, brownies, smoothies, and pies normalize to incorrect
   `-y` forms and fail singular matching, duplicate rejection, and conflict
   alignment. Result: 16 failed, 45 passed; the berry/berries cases passed and
   the four ambiguous pairs failed in matcher, schema, and parser coverage.
5. [x] **Implementation:** canonicalize existing `-ies` tokens without
   guessing a singular stem, map consonant-plus-`y` and terminal `-ie`
   singulars to that shared plural spelling, and leave all other conservative
   plural, Unicode, punctuation, whitespace, and whole-word behavior intact.
6. [x] **Verify the fix:** rerun every new matcher, schema, and parser case and
   confirm all five required pairs align in both directions without creating a
   match for the unrelated controls. Result: 61 passed.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_preferences.py tests/test_schemas.py
   tests/test_parser.py` and do not begin Task 4 until it passes.
   Result: 215 passed.

**Task 3 verification record (2026-08-19):**

- TDD command: `uv run pytest tests/test_preferences.py::test_matches_food_uses_normalized_whole_words_and_phrases tests/test_schemas.py::test_preference_requirement_rejects_invalid_values tests/test_parser.py::test_parse_preference_interpretation_rejects_direct_count_conflicts`. Before implementation: 16 failed, 45 passed. After implementation: 61 passed.
- Regression command: `uv run pytest tests/test_preferences.py tests/test_schemas.py tests/test_parser.py`. Result: 215 passed.
- `uv run ruff format --check src/meal_planner/normalization.py tests/test_preferences.py tests/test_schemas.py tests/test_parser.py`: passed after Ruff reformatted the normalization module.
- `uv run ruff check src/meal_planner/normalization.py tests/test_preferences.py tests/test_schemas.py tests/test_parser.py`: passed.
- `uv run mypy`: passed with no issues in 19 source files.
- `uv run pytest`: 627 passed, 2 existing SAM-artifact skips.
- `git diff --check`: passed.

**Acceptance criteria:**

- cookie/cookies, brownie/brownies, smoothie/smoothies, pie/pies, and
  berry/berries normalize as equivalent terms.
- Each pair matches in either singular/plural direction, is rejected as a
  duplicate alternative, and aligns for parser conflict detection.
- Existing egg/eggs, pancake/pancakes, Unicode, case, punctuation, whitespace,
  whole-word, and normalized-empty behavior remains covered and passing.
- Unrelated words and substrings remain distinct; no broad fuzzy matching or
  dictionary dependency is introduced.
- `normalize_food()` retains its existing public return type and all consumers
  continue to share one implementation.

### Task 4: Provide an actionable reset for clarification overflow

**Finding:** P2 — A 498-500 character preference can enter clarification, but
the `"; "` separator plus any short non-empty answer exceeds the 500-character
cap. The current response leaves the user in the same state without explaining
how to reset and provide one complete preference.

**Files and symbols:**

- Modify: `src/meal_planner/bot_handler.py` —
  `BotHandler._handle_plan_preference()` combined-preference overflow path
- Modify: `tests/test_bot_handler.py` — two-turn 498-500 boundaries and reset
  recovery

1. [x] **Test first:** add parametrized two-turn tests for initial preferences
   of 498, 499, and 500 characters. Make the first interpretation request a
   clarification, then send a short non-empty answer that causes combined
   overflow.
2. [x] **Test first:** assert the answer is not truncated or appended, no
   second interpreter call occurs, no Planner invocation occurs, and no second
   state transition is attempted. Require one bounded message that says the
   answer was not appended and explicitly instructs `/plan` plus one complete
   preference of 500 characters or fewer.
3. [x] **Test first:** exercise `/plan` after the overflow response and assert
   the pending state is conditionally replaced by a fresh
   `AWAITING_PREFERENCE` request with no saved preference. Add an at-or-below
   500 combined-length control that still interprets and transitions normally.
4. [x] **Expected failure:** run the new tests and confirm the current handler
   returns only the generic too-long message. It does not distinguish combined
   clarification overflow, state that the answer was not appended, or provide
   the explicit reset and full-restatement path.
5. [x] **Implementation:** add a dedicated combined-overflow branch before the
   second interpreter call. Preserve the saved state and original preference,
   avoid semantic truncation and side effects, and send the actionable `/plan`
   restart instruction. Keep first-turn oversize handling separate.
6. [x] **Verify the fix:** rerun all new boundary and reset tests and confirm
   lengths 498-500 recover predictably, while an exactly valid combined value
   still completes the normal interpretation path.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_bot_handler.py tests/test_schemas.py
   tests/test_parser.py` and do not begin Task 5 until it passes.

**Task 4 verification record (2026-08-19):**

- TDD command: `uv run pytest tests/test_bot_handler.py -k
  'clarification_overflow_rejects_answer_without_side_effects or
  clarification_overflow_can_reset_with_plan_command or
  exactly_500_character_combined_preference_is_interpreted'`. Before
  implementation: 3 failed, 2 passed; each 498/499/500 case returned the
  generic too-long message.
- Focused command: the same five-test command. Result: 5 passed.
- Regression command: `uv run pytest tests/test_bot_handler.py
  tests/test_schemas.py tests/test_parser.py`. Result: 262 passed.
- `uv run ruff format src/meal_planner/bot_handler.py
  tests/test_bot_handler.py`: one test file reformatted; `uv run ruff check`
  passed for both changed Python files.
- `uv run mypy`: passed with no issues in 19 source files.
- `uv run pytest`: 632 passed, 2 existing SAM-artifact skips.
- `git diff --check`: passed.

**Acceptance criteria:**

- Combined clarification overflow is detected before interpretation,
  persistence, or Planner dispatch.
- The rejected answer is neither truncated nor appended, and the existing
  durable preference/state remains unchanged.
- The user receives one bounded message explaining the outcome and the exact
  `/plan` reset plus full-restatement path.
- Running `/plan` replaces the pending request using existing conditional
  state semantics and starts with no retained partial preference.
- Combined preferences of exactly 500 characters remain valid; 501 remains
  invalid.
- Duplicate-update idempotency, interpreter-failure recovery, state-conflict
  handling, and manual retry behavior remain unchanged.

### Task 5: Bound and sanitize model labels in failure records

**Finding:** P3 — `_model_name()` passes every non-empty string into the
formatted warning and CloudWatch structured metadata, allowing oversized or
line-breaking configuration values to inflate or spoof records.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` — model-label maximum, safe
  pattern, and `PlannerHandler._model_name()`
- Modify: `tests/test_planner_handler.py` — model-label boundaries and failure
  log sanitization

1. [x] **Test first:** add direct boundary cases proving a representative
   model identifier and an exact 64-character safe label are retained, while
   a 65-character label, empty string, whitespace-only string, non-string, and
   labels containing spaces, tabs, carriage returns, newlines, or other
   control characters return `unknown`.
2. [x] **Test first:** drive a provider failure through the existing safe-log
   path with an oversized label and with a newline-injection label. Assert one
   warning per failure, `record.model == "unknown"`, bounded captured text, and
   absence of the unsafe suffix or injected line from both message and
   structured metadata.
3. [x] **Expected failure:** run the new tests and confirm the current helper
   returns oversized, whitespace-only, and control/newline strings unchanged,
   and those values appear in failure records.
   Result: 9 failed, 4 passed; the exact 64-character and representative safe
   labels passed, while oversized, whitespace, space/control/newline labels,
   and both real failure-log cases were emitted unchanged.
4. [x] **Implementation:** enforce a 1-64 character full-match allowlist for
   common model identifier characters and return exactly `unknown` for every
   invalid value. Do not truncate or character-replace unsafe values into a
   seemingly valid model name.
5. [x] **Verify the fix:** rerun the new unit and logging tests and confirm safe
   labels retain useful diagnostics while every malformed label is represented
   by the fixed fallback in both warning text and structured metadata.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py tests/test_config.py` and do not
   begin Task 6 until it passes.

**Task 5 verification record (2026-08-19):**

- TDD command: `uv run pytest tests/test_planner_handler.py -k
  'model_name_accepts_only_bounded_safe_labels or
  failure_log_sanitizes_unsafe_model_labels'`. Before implementation: 9
  failed, 4 passed. After implementation: 13 passed.
- Regression command: `uv run pytest tests/test_planner_handler.py
  tests/test_config.py`. Result: 141 passed.
- `uv run ruff format --check
  src/meal_planner/planner_handler.py tests/test_planner_handler.py`: passed.
- `uv run ruff check src/meal_planner/planner_handler.py
  tests/test_planner_handler.py`: passed.
- `uv run mypy`: passed with no issues in 19 source files.
- `uv run pytest`: 645 passed, 2 existing SAM-artifact skips.
- `git diff --check`: passed.

No Task 5 assumptions or unresolved issues. The existing two
SAM-artifact-dependent skips remain unchanged.

**Acceptance criteria:**

- Logged model labels are at most 64 characters and use only the documented
  safe ASCII identifier characters.
- Oversized, blank, whitespace-bearing, control-bearing, newline-bearing,
  non-string, and otherwise invalid labels become exactly `unknown`.
- Unsafe content cannot create an extra log line or appear in the warning or
  structured `extra` fields.
- Valid configured labels such as `gpt-5.6-luna` retain their exact value.
- Attempt, elapsed time, category, validation metadata, logging count, and
  failure/recovery behavior remain unchanged.

### Task 6: Verify all findings and project quality gates

**Files and symbols:**

- Modify if needed: only implementation, test, and README files named in Tasks
  1-5
- Modify during implementation: this plan's progress and verification record
- Move only after every gate passes:
  `docs/plans/2026-08-19-remediate-phase-3-review-findings.md` to
  `docs/plans/completed/2026-08-19-remediate-phase-3-review-findings.md`

1. [x] verify all five findings have a passing focused test, documented
   before-fix failure, and satisfied acceptance criteria
2. [x] run
   `uv run pytest tests/test_preferences.py tests/test_planner_handler.py
   tests/test_config.py tests/test_readme.py tests/test_telegram_api.py` and
   confirm bounded one-chunk requirement output, three-send budgeting,
   terminal compaction, and safe model labels
3. [x] run
   `uv run pytest tests/test_prompts.py tests/test_parser.py
   tests/test_schemas.py` and confirm exact repair slots, complete prompt
   invariants, plural matching, duplicate rejection, and conflict alignment
4. [x] run `uv run pytest tests/test_bot_handler.py` and confirm two-turn
   clarification overflow has an actionable reset path without truncation or
   unintended side effects
5. [x] run `uv run ruff format --check .` and fix formatting failures only
   with Ruff
6. [x] run `uv run ruff check .` and fix all lint failures
7. [x] run `uv run mypy` and fix all strict type errors
8. [x] run `uv run pytest` until the full suite passes; it must exceed the
   589-passed/2-skipped baseline by at least the newly added tests, with no new
   skip or xfail, while the same two SAM-artifact-dependent skips may remain in
   an ordinary artifact-free workspace
9. [x] run `git diff --check` and inspect the complete diff for accidental
   dependencies, template changes, unrelated configuration changes,
   implementation cleanup, or archived-plan edits
10. [x] confirm `pyproject.toml`, `uv.lock`, `template.yaml`, and every archived
    plan remain unchanged unless a newly discovered acceptance blocker is
    explicitly recorded before any scope expansion
11. [x] rerun `sha256sum` for the three archived plans listed in Context and
    confirm all hashes are unchanged
12. [x] record exact focused/full commands, counts, skip reasons, and retained
    risks in this plan; mark every checkbox complete and archive this plan only
    after all gates pass

**Task 6 verification record (2026-08-19):**

- Focused requirement, Planner, configuration, README, and Telegram suite:
  `uv run pytest tests/test_preferences.py tests/test_planner_handler.py
  tests/test_config.py tests/test_readme.py tests/test_telegram_api.py` —
  203 passed in 1.64s.
- Focused prompt, parser, and schema suite:
  `uv run pytest tests/test_prompts.py tests/test_parser.py
  tests/test_schemas.py` — 192 passed in 1.36s.
- Focused Bot clarification suite: `uv run pytest
  tests/test_bot_handler.py` — 91 passed in 1.60s.
- Repository formatting: `uv run ruff format --check .` — 74 files already
  formatted.
- Repository lint: `uv run ruff check .` — all checks passed.
- Strict type checking: `uv run mypy` — success, no issues in 19 source files.
- Full suite: `uv run pytest` — 645 passed, 2 skipped in 4.39s; 647 tests
  collected. The same two artifact-dependent skips remain:
  `tests/test_template.py:168` skips BotFunction because
  `.aws-sam/build/BotFunction` is missing, and skips PlannerFunction because
  `.aws-sam/build/PlannerFunction` is missing; both advise running `sam build`
  first. A confirming `uv run pytest -rs` produced the same 645 passed / 2
  skipped result in 4.32s.
- Whitespace validation: `git diff --check` — passed with no output.
- Complete worktree inspection: `git diff --stat` reported 20 tracked changed
  paths (5,351 insertions and 367 deletions), and `git ls-files --others
  --exclude-standard` reported the active plan, three archived baseline plans,
  the new normalization and preferences modules, and their test files. The
  accumulated paths contain only the completed baseline plus Tasks 1–5; no
  Task 6 implementation or unrelated dependency/configuration change was
  introduced. The expected pre-existing `template.yaml` permission additions
  were inspected; `pyproject.toml` and `uv.lock` have no diff.
- Archived-plan integrity: `sha256sum
  docs/plans/completed/2026-08-19-remediate-review-findings-follow-up.md
  docs/plans/completed/2026-08-19-remediate-meal-plan-preference-validation-review-findings.md
  docs/plans/completed/2026-08-19-enforce-meal-plan-preferences-with-evidence-based-validation.md`
  returned, respectively,
  `d86fd6d54b40bd98b0bb1fd17b982677a20d463f7993b38ddf1d20418300b52c`,
  `0a7e76d3d89eb99fba15c937b8e8f6df82eca62aa8959a27ea14cc700a4b7443`, and
  `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8` — all
  unchanged from Context.
- Findings 1–5 each have passing focused coverage, documented TDD failures,
  implementation verification, and acceptance criteria marked complete in
  this plan. No verification-related fixes were necessary.

**Acceptance criteria:**

- All five review findings are closed by focused regression coverage.
- Ruff format, Ruff lint, strict Mypy, full Pytest, and `git diff --check` pass.
- The full suite adds the planned tests without new failures, skips, or xfails
  relative to the 589-passed/2-skipped baseline.
- No dependency, template, deployment-permission, persistence, repair-marker,
  or archived-plan change is introduced.
- The final verification record contains exact commands, results, and reasons
  for any retained artifact-dependent skips.

## Residual Risks

- Two baseline tests depend on generated SAM artifacts. Source tests do not
  replace a clean artifact build and import check.
- The 1,000-character aggregate bound intentionally omits some display detail
  at maximum requirement shapes. Full typed requirements remain in durable
  state and validation; Telegram summaries are not the source of truth.
- The three-request Planner budget assumes the schema-bounded plan itself
  remains one Telegram chunk. Tests should retain a maximum-plan one-chunk
  assertion; a future expansion of plan fields or limits must update both
  output bounds and budgeting.
- `-ies` is orthographically ambiguous: a plural can correspond to a `-y` or
  `-ie` singular. Treating both as one equivalence family fixes the required
  foods but can align rare unrelated spellings. The non-equivalence controls
  limit, but cannot eliminate, this language-level ambiguity without a
  dictionary.
- LLM semantic completeness remains probabilistic beyond deterministic schema,
  completeness, and measurable preference validation. Better prompt wording
  does not replace application validation.
- The asynchronous Lambda handoff still lacks a durable dispatch marker and a
  configured failure destination. Repair publication is idempotent after
  delivery but does not recover an event that exhausts asynchronous retries.
- Atomic persistence and duplicate suppression do not provide exactly-once
  Telegram delivery. A network failure after Telegram accepts a request can
  still leave external delivery status uncertain.
- Replacing unsafe model labels with `unknown` protects logs at the cost of
  diagnostic specificity for a misconfigured model value. Configuration
  validation of model identifiers is outside this remediation.

## Post-Completion

**Manual and external verification:**

- Build clean SAM artifacts on a compatible Linux ARM64 Python 3.14
  environment and run the two artifact-required tests so the baseline skips
  become passing checks.
- Deploy through a reviewed pull request from a dedicated branch, never
  directly to `master`.
- In a test Telegram chat, exercise a requirement set large enough to compact
  and confirm the success path issues exactly one plan request, one summary
  request, and one follow-up request without approaching the Planner deadline.
- Exercise a terminal compliance failure and confirm its message remains one
  Telegram request and provides the retained-preference `/plan` recovery.
- Exercise a near-limit clarification, follow the displayed `/plan` restart
  path, and confirm no partial answer was persisted or semantically truncated.
- Inspect privacy-safe CloudWatch records with a valid model label and with a
  deliberately invalid test configuration. Confirm invalid content appears
  only as `unknown` and cannot split a record.
- Re-run the read-only deployed transaction-permission verifier for both Bot
  and Planner roles after deployment; this remediation must not regress the
  completed table-scoped authorization work.
- Verify the configured Lambda asynchronous retry policy and separately decide
  whether a durable dispatch record or failure destination is required.
- Commit with a Conventional Commit message containing the associated issue
  number, then comment on that issue with the commit or pull request link.

No GitHub issue is created during this planning-only phase because the request
authorizes creation of exactly one new remediation plan and no other change.
