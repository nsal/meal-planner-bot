# Finalize Variable-Length Meal Plan Work

## Overview

Complete and verify every remaining unchecked requirement in the primary
variable-length meal-plan plan, bring generated SAM artifacts back in sync
with source, and archive the primary plan plus its two completed remediation
plans. The feature must support one through seven consecutive days consistently
from user input through persistence and downstream workflows, while preserving
legacy seven-day requests.

## Context

- The primary plan is
  `docs/plans/2026-08-25-support-variable-length-meal-plans.md`; its Tasks 3,
  4, and 6 are complete, while Tasks 1, 2, 5, and 7 retain focused gaps.
- The completed remediation plans are
  `docs/plans/2026-08-25-variable-length-plan-review-remediation.md` and
  `docs/plans/2026-08-25-variable-length-plan-review-remediation-follow-up.md`.
  Both have every implementation checkbox marked complete but are not archived.
- `PlanDays`, `ConversationState`, `PlanGenerationContext`, and `WeeklyPlan`
  already support dynamic horizons. `tests/factories.py::make_plan()` remains
  fixed at seven days, which prevents concise exhaustive downstream coverage.
- `BotHandler._parse_initial_plan_response()` owns the one-time `N, preference`
  syntax. It retains the text after the first comma, and the bot has several
  remaining user-visible references to a "weekly meal plan."
- `tests/test_template.py` compares `.aws-sam/build` artifacts byte-for-byte
  with source. The full suite currently reports two stale-artifact failures for
  changed prompt source; rebuild using the documented SAM command before final
  release verification.

## Scope and Constraints

- **In scope:** missing model/factory tests, initial `/plan` input matrix,
  duration-neutral user-facing wording, short-plan downstream integration
  coverage, SAM artifact regeneration, final quality gates, plan reconciliation,
  and archival.
- **Out of scope:** new duration values, data migrations, changes to profile or
  dietary-rule semantics, deployment, commits, pushes, pull requests, GitHub
  issues/comments, or external messaging.
- Use TDD: add each missing focused test before the production change it
  requires and record the expected failure when one is applicable.
- Preserve all pre-existing dirty work. Do not modify a contextual plan except
  to mark verified remaining items or record final evidence in the primary
  plan. Do not reinterpret fully checked remediation tasks.
- Use `uv run` for Python tools, Ruff with the configured 80-column limit,
  strict Mypy, and no live Telegram, AWS, or LLM calls.
- A full Pytest pass includes the SAM artifact checks; do not call the release
  gate green while source/artifact comparisons fail.

## Development Approach

- Complete tasks in numerical order. Each task must have its focused tests
  passing before the next starts.
- Keep `PlanDays` as the sole duration contract. Derive plan bounds from the
  requested or persisted plan length rather than adding a short-plan schema.
- Test public workflow boundaries where possible: command input, state
  transition, planner event, active-plan lookup, callbacks, and Telegram
  rendering. Avoid test-only shortcuts that bypass those boundaries.
- Use duration-neutral language for generated plan workflows. Retain explicit
  seven-day wording only where the behavior is intentionally fixed, such as
  meal-history windows.
- Update checkboxes immediately after their evidence is complete. Add a `⚠️`
  note for a real blocker and a `➕` item for newly discovered in-scope work.

## Testing Strategy

- **Schema/factory:** parameterize all valid horizons 1 through 7 and invalid
  day arrays; test dynamic `week_end` and factory-generated plans.
- **Input workflow:** table-drive valid whitespace and no-preference aliases,
  plus invalid missing, empty, numeric, textual, and out-of-range inputs with
  no interpreter, Lambda, repository-transition, or Telegram-success effects.
- **Short-plan integration:** assert first-day, last-day, and post-end active
  lookups; target-day actions; confirmation; display; meal outcomes; and
  grocery behavior for one- and intermediate-day plans.
- **Release:** run focused suites after each task, then Ruff, Mypy, the full
  suite, `REQUIRE_SAM_ARTIFACTS=1` artifact checks, and `git diff --check`.

## Solution Overview

First make test construction duration-aware, then use it to close the input
and downstream coverage gaps. Update the limited set of user-facing labels
that incorrectly promise a weekly plan. Once source and tests are stable,
perform a clean SAM build so generated Lambda artifacts exactly mirror source.
Finally, reconcile only validated checkboxes, record verification evidence, and
move the fully completed plan documents into `docs/plans/completed/`.

## Technical Details

- Add an optional `plan_days: PlanDays = 7` to `make_plan()` and construct
  `PlanDay(day=value)` for `range(1, plan_days + 1)`.
- Keep `_parse_initial_plan_response()` as the single parser. Tests must verify
  accepted aliases (`anything`, `no preference`, `no preferences`, `none`,
  `whatever`) after normalization, and reject malformed initial syntax before
  any side effect.
- Replace progress text such as "Working on your weekly meal plan" with either
  a concrete `{plan_days}-day meal plan` when that value is available or
  duration-neutral "meal plan" text. Update command/help and README wording
  that describes generated plans; do not alter fixed seven-day history rules.
- Active-plan queries already rely on `WeeklyPlan.week_end`. Integration tests
  must demonstrate they include the final short-plan day and exclude the next
  date. Callback and rendering tests must ensure no target day beyond
  `len(plan.days)` is offered or accepted.
- Build artifacts with the repository-documented `uvx --from aws-sam-cli sam
  build --beta-features` command (adding only necessary architecture/runtime
  flags if host compatibility requires them), then run the artifact tests in
  required mode.

## What Goes Where

- **Implementation Steps:** source, tests, documentation, artifact generation,
  verification evidence, and archival inside this repository.
- **Post-Completion:** manual Telegram checks and optional version-control or
  GitHub actions, none of which are authorized by this plan.

## Implementation Steps

### Task 1: Make variable-horizon fixtures and schema coverage exhaustive

**Files:**

- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`

- [x] Add failing parameterized `WeeklyPlan` tests accepting every contiguous
  horizon from one through seven, and rejecting empty, gapped, duplicate,
  non-one-starting, and overlong sequences.
- [x] Add failing tests proving `week_end` equals `week_start + plan_days - 1`
  for one, intermediate, and seven-day plans.
- [x] Extend `make_plan()` with a typed optional `PlanDays` duration and add
  controls proving it produces exactly contiguous day numbers for each horizon.
- [x] Keep existing default factory callers seven-day compatible and preserve
  meal, grocery, status, revision, and instruction defaults.
- [x] Run `uv run pytest tests/test_schemas.py` and mark the remaining primary
  Task 1 checkboxes only after it passes.

**Acceptance criteria:**

- [x] Tests directly cover every valid horizon and every specified invalid day
  shape.
- [x] Shared test construction can create a typed one- through seven-day plan.
- [x] The primary plan's Task 1 is fully evidenced and checked.

**Task 1 verification evidence (2026-08-25):**

- Added exhaustive `WeeklyPlan` horizon and invalid-shape coverage plus
  dynamic `week_end` assertions in `tests/test_schemas.py`.
- Added typed, duration-aware contiguous construction to
  `tests/factories.py::make_plan()` and verified legacy defaults.
- `uv run pytest tests/test_schemas.py`: passed (248 tests).
- Repository checks: `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `git diff --check` passed. The full `uv run pytest` run
  passed 1331 tests and retained two pre-existing stale-SAM-artifact failures
  in `tests/test_template.py` for `src/meal_planner/llm/prompts.py`; artifact
  regeneration is reserved for Task 4.

### Task 2: Complete initial duration parsing and duration-neutral messaging

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: `README.md`
- Modify: `tests/test_bot_handler.py`
- Modify: command/help test file identified during discovery
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`

- [x] Add failing table-driven initial-input tests for one and seven days,
  whitespace, first-comma-only splitting, embedded preference commas, and all
  accepted no-preference aliases.
- [x] Add failing invalid-input tests for a missing comma, empty duration,
  empty preference, booleans/textual numbers, fractional numeric text, zero,
  and eight; assert no state transition, interpreter call, planner invocation,
  persistence, or success delivery occurs.
- [x] Update progress and retry messages to use the selected duration or
  duration-neutral "meal plan" wording; update the Telegram command catalogue,
  `/help` rendering assertions, and README generated-plan descriptions while
  leaving fixed meal-history windows unchanged.
- [x] Run the focused bot and command/help tests, then mark every remaining
  primary Task 2 checkbox only when its exact coverage exists.

**Acceptance criteria:**

- [x] Every allowed initial form preserves the exact selected horizon and
  preference text.
- [x] Every prohibited form is side-effect free.
- [x] Generated-plan UI wording does not incorrectly promise seven days.
- [x] The primary plan's Task 2 is fully evidenced and checked.

**Task 2 verification evidence (2026-08-25):**

- Added table-driven initial-input coverage for one- and seven-day requests,
  whitespace, first-comma-only preference retention, embedded commas, and all
  five no-preference aliases.
- Added side-effect-free invalid-input coverage for missing and empty fields,
  booleans, textual and fractional numbers, and out-of-range durations.
- Progress replies now use the retained `{N}-day meal plan` wording, with a
  duration-neutral fallback; command/help and README generated-plan wording
  no longer promises a weekly plan. Fixed seven-day meal-history wording was
  left unchanged.
- `uv run pytest tests/test_bot_handler.py tests/test_telegram_commands.py
  tests/test_readme.py`: passed (352 tests, 2 existing Pydantic serializer
  warnings).
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed.
- The repository-wide `uv run pytest` run passed 1350 tests but retained two
  stale-SAM-artifact failures for the copied `bot_handler.py`; rebuilding
  artifacts is reserved for completion-plan Task 4.

### Task 3: Cover short-plan behavior across downstream workflows

**Files:**

- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_telegram_api.py`
- Modify: `src/meal_planner/db/dynamo.py` only if a focused regression exposes
  a fixed-week assumption
- Modify: `src/meal_planner/bot_handler.py` only if a focused regression
  exposes a fixed-week assumption
- Modify: `src/meal_planner/telegram/api.py` only if rendering coverage exposes
  a fixed-week assumption
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`

- [x] Add failing Dynamo tests for a short confirmed plan being active on its
  first and actual final day, and inactive on the first date after `week_end`.
- [x] Add failing workflow tests for one- and intermediate-day plans covering
  permitted target-day edits, rejected out-of-range target days, confirmation,
  `/today` display, meal outcomes, and grocery generation.
- [x] Preserve date-key, active-epoch, compare-and-swap, atomic publication,
  and stale-callback behavior while fixing only failures demonstrated by the
  new short-plan tests.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_planner_handler.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`.
- [x] Mark all remaining primary Task 5 implementation and regression items
  only after these focused paths pass.

**Acceptance criteria:**

- [x] A short plan is active through its dynamic end date and no later.
- [x] Downstream plan workflows never assume days beyond the persisted length.
- [x] Seven-day plan lifecycle and stale-work protections remain intact.
- [x] The primary plan's Task 5 is fully evidenced and checked.

**Task 3 verification evidence (2026-08-25):**

- Added DynamoDB regressions for one- and three-day confirmed plans covering
  first-day, dynamic final-day, and post-`week_end` active lookups, plus
  final-day edit, outcome, grocery, and out-of-range target-day behavior.
- Added bot workflow regressions for final-day edits, rejected next-day edits,
  confirmation-to-grocery dispatch, `/today`, check-in callbacks, and meal
  outcomes for one- and three-day plans. Added Telegram rendering checks that
  stop at the persisted final day; the fixed weekly title was made neutral.
- Existing date-key, active-epoch, compare-and-swap, atomic publication, and
  stale-callback tests remain green; no Dynamo or bot fixed-week production
  change was required.
- `uv run pytest tests/test_prompts.py tests/test_planner_handler.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`:
  passed (680 tests, 2 existing Pydantic serializer warnings).
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed.

### Task 4: Rebuild SAM artifacts and run repository quality gates

**Files:**

- Modify: `.aws-sam/build/**` through the SAM build command only
- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`

- [x] Run the focused suites from Tasks 1 through 3 and fix every newly
  introduced failure before artifact generation.
- [x] Rebuild Lambda artifacts with the documented SAM command and confirm the
  generated template and copied `meal_planner` sources match the current
  source tree.
- [x] Run `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` and
  resolve every artifact comparison, import, compatibility, or template
  failure.
- [x] Run `uv run ruff format .`, `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, `uv run pytest`, and
  `git diff --check`; record exact commands and results in the primary plan.
- [x] Mark the primary Task 6/Task 7 gate evidence complete only after every
  quality gate is green, including the artifact checks.

**Acceptance criteria:**

- [x] The full Pytest suite passes without stale SAM artifact failures.
- [x] Ruff, Mypy, and diff checks pass.
- [x] The primary plan contains current, reproducible final verification
  evidence.

### Task 5: Reconcile and archive the completed plans

**Files:**

- Modify: `docs/plans/2026-08-25-support-variable-length-meal-plans.md`
- Move: `docs/plans/2026-08-25-support-variable-length-meal-plans.md` to
  `docs/plans/completed/2026-08-25-support-variable-length-meal-plans.md`
- Move: `docs/plans/2026-08-25-variable-length-plan-review-remediation.md` to
  `docs/plans/completed/2026-08-25-variable-length-plan-review-remediation.md`
- Move:
  `docs/plans/2026-08-25-variable-length-plan-review-remediation-follow-up.md`
  to
  `docs/plans/completed/2026-08-25-variable-length-plan-review-remediation-follow-up.md`
- Move: this plan to
  `docs/plans/completed/2026-08-25-finalize-variable-length-meal-plan-work.md`

- [x] Verify every primary-plan implementation checkbox is checked with direct
  test or implementation evidence; do not mark manual/external Post-Completion
  items as implementation work.
- [x] Verify both remediation plans remain fully checked and unchanged except
  for their archive location.
- [x] Move the three completed feature/remediation plans and this completion
  plan to `docs/plans/completed/` only after Task 4 passes.
- [x] Run `git diff --check` and verify no active `docs/plans/` file remains
  for this variable-length feature family.

**Acceptance criteria:**

- [x] All three feature plans are archived with complete implementation
  checklists and current verification evidence.
- [x] No unrelated plan or pre-existing worktree file is moved or rewritten.
- [x] No commit, push, pull request, GitHub issue/comment, or deployment is
  performed.

**Task 5 verification evidence (2026-08-25):**

- The primary plan has no unchecked implementation items; its Post-Completion
  manual and external items remain intentionally unchecked.
- Both remediation plans had no unchecked items before archival. Their content
  hashes were unchanged across the move:
  `2026-08-25-variable-length-plan-review-remediation.md`:
  `ae16c3dbff603341f1eaa32296caa0523d75c755febb3a41fa5152dbf5abd6ad`;
  `2026-08-25-variable-length-plan-review-remediation-follow-up.md`:
  `6d3785c826566de2e035d2ff2ec93fdfdae989f584a41aa78dcf9968802b3a03`.
- Exactly the four named plans were moved to `docs/plans/completed/`; no
  unrelated plan or pre-existing worktree file was moved.
- `git diff --check`: passed. No active root-level `docs/plans/` file remains
  for this variable-length feature family.

## Post-Completion

**Manual verification**

- In Telegram, start `/plan` with `1, no preference` and
  `3, fish, pasta, and salad`; verify the displayed date range and generated
  content stop on the selected last day.
- Confirm an invalid initial duration and a comma-containing clarification
  cannot start generation or discard the retained horizon.

**External actions**

- If separately authorized, create a Conventional Commit, open a pull request,
  and comment on the related GitHub issue. These actions are intentionally not
  part of this plan.
