# Submitted-Meal Date Boundary Review Remediation

## Overview

Remediate the independent-review P2 finding that the documented submitted-meal
date range does not match the active `/submit_meals` parser. Preserve the
intended product contract: UTC today through the previous seven dates,
inclusive, for eight accepted calendar dates. Change the active parser to
accept `today - 7 days`, continue rejecting `today - 8 days`, and prove both
boundaries through the live Bot routing path.

This remediation replaces the ineffective boundary coverage of the inactive
field-by-field compatibility helper. It does not revert the correct README
wording, redesign meal submission, change persistence, modify unrelated review
work, or move the active simplification plan.

## Context

- `src/meal_planner/router.py::parse_meal_input()` is the active structured
  parser. It currently rejects dates before `reference_date - 6 days`, so it
  accepts only seven dates despite the eight-date contract.
- `src/meal_planner/bot_handler.py::BotHandler.handle_conversational()` routes
  an `AWAITING_MEAL_INPUT` state to `_handle_structured_meal_input()`, which
  calls `parse_meal_input()` with the route's UTC reference date.
- `src/meal_planner/bot_handler.py::_handle_meal_workflow()` is retained only
  for legacy field-by-field compatibility. Active legacy states are restarted
  rather than routed through that helper.
- `tests/test_bot_handler.py::test_submitted_meal_date_window_is_inclusive_at_`
  `seven_days` tests the inactive helper and therefore cannot protect the live
  contract.
- `tests/test_router.py::test_parse_meal_input_validates_dates_and_meal_types`
  currently characterizes the parser's seven-date behavior.
- `README.md` and `tests/test_readme.py::test_readme_documents_inclusive_`
  `submitted_meal_date_window` already specify the intended eight-date range.
- The completed review-remediation plan records Task 4 as complete, but its
  integration acceptance criterion was not met because its boundary test used
  the inactive helper.
- The worktree contains substantial pre-existing changes. Preserve them and
  do not commit, push, stash, reset, clean, or perform external actions while
  executing this plan.

## Development Approach

- **Testing approach:** TDD. Add or replace the parser and active-route
  assertions first, run them against the current implementation, and record
  the expected `today - 7 days` failures before changing production code.
- Complete Task 1 fully before starting final verification.
- Keep the implementation narrow: adjust the active parser's lower bound and
  align its docstring, validation message, and user-facing input prompt.
- Do not change future-date validation, aliases, meal-type validation,
  description parsing, review transitions, persistence, or the legacy helper.
- Remove or replace the legacy-only date-boundary test; do not retain it as
  evidence for the active `/submit_meals` contract.
- Update this plan's checkboxes as work completes. Do not edit or move
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`.
- Follow `pyproject.toml`: Python 3.14, Ruff at 80 columns, strict Mypy, Pytest,
  and `uv` for all project commands.

## Solution Overview

Use one inclusive lower-bound comparison in `parse_meal_input()`: reject a
parsed date only when it is earlier than `reference_date - timedelta(days=7)`.
Keep UTC reference-date derivation in the Bot handler unchanged.

Protect the contract at two levels:

1. Parser tests establish that `today - 7 days` succeeds and
   `today - 8 days` fails with the date-window validation error.
2. A Bot integration test starts `/submit_meals`, captures the resulting
   `AWAITING_MEAL_INPUT` state, and sends structured conversational input
   through `handle_conversational()` at both boundaries. The accepted boundary
   must reach meal review; the rejected boundary must remain at input and show
   validation guidance.

The existing README contract remains the source of product wording. The Bot
prompt and parser error should describe the same range without calling it a
seven-day window.

## Technical Details

- Accepted explicit-date predicate:
  `reference_date - timedelta(days=7) <= parsed_date <= reference_date`.
- Rejected lower boundary: any date less than
  `reference_date - timedelta(days=7)`, including `today - 8 days`.
- `today` and `yesterday` continue to resolve against the UTC date supplied to
  `parse_meal_input()`.
- Active-route tests must supply a deterministic Telegram message timestamp in
  `RouteResult.raw_update`, so `_reference_date_from_route()` uses the same UTC
  date for command startup and conversational parsing.
- The accepted active-route assertion must verify the transition to
  `AWAITING_MEAL_CONFIRMATION` and `send_meal_review()` delivery.
- The rejected active-route assertion must verify no state transition and no
  meal-review delivery, while `send_message()` contains the date guidance and
  the normal meal-input prompt.
- No dependency, schema, DynamoDB, README, or deployment change is required.

## Testing Strategy

- Update the existing parser parameterization rather than adding duplicative
  parser tests. Include exact cases for zero, seven, eight, and future dates.
- Replace the legacy-only Bot boundary test with a parameterized integration
  test that enters through `/submit_meals` and then
  `BotHandler.handle_conversational()`.
- Assert observable active behavior, not calls to
  `BotHandler._handle_meal_workflow()`.
- Keep `tests/test_readme.py` in regression coverage to prove documentation
  still states the eight-date contract.
- Run focused tests after the failing-test phase and after implementation.
  Run all project gates only after the focused suite passes.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered in-scope tasks with a `➕` prefix.
- Document blockers or deviations with a `⚠️` prefix.
- Do not proceed while a task's required tests are failing.
- Preserve unrelated worktree changes and attribute only this remediation's
  deltas to this plan.

## Implementation Steps

### Task 1: Align the active parser and `/submit_meals` route boundaries

**Finding:** P2 — README documents eight accepted UTC dates, but the active
structured parser accepts only seven, and the added test covers an inactive
legacy helper.

**Files:**

- Modify: `tests/test_router.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/bot_handler.py`

**Failing or missing test first:**

- [x] In `tests/test_router.py`, update
  `test_parse_meal_input_validates_dates_and_meal_types` so a fixed reference
  date accepts the exact `today - 7 days` ISO date and rejects the exact
  `today - 8 days` ISO date. Retain coverage for today, the future boundary,
  malformed dates, real-calendar validation, and invalid meal types.
- [x] In `tests/test_bot_handler.py`, remove or replace
  `test_submitted_meal_date_window_is_inclusive_at_seven_days`; the replacement
  must not call `_handle_meal_workflow()`.
- [x] Add a parameterized active-route test that invokes `/submit_meals` via
  `handle_command()`, captures the created `AWAITING_MEAL_INPUT` state, then
  submits an ISO-dated meal through `handle_conversational()` using a fixed UTC
  Telegram timestamp for `today - 7 days` and `today - 8 days`.
- [x] For `today - 7 days`, assert the active route conditionally transitions
  the state to `AWAITING_MEAL_CONFIRMATION`, preserves the submitted date in
  the draft, and calls `send_meal_review()`.
- [x] For `today - 8 days`, assert the active route does not transition state,
  does not call `send_meal_review()`, and sends the aligned date error followed
  by the normal meal-input prompt.
- [x] Add or strengthen the `/submit_meals` startup-prompt assertion so it
  describes UTC today through the previous seven dates, inclusive, as eight
  calendar dates.

**Expected failure:**

- [x] Run the new focused tests, for example:

  ```bash
  uv run pytest tests/test_router.py tests/test_bot_handler.py \
    -k "parse_meal_input_validates_dates or submitted_meal_date_window"
  ```

- [x] Confirm the parser and active-route `today - 7 days` cases fail because
  `parse_meal_input()` rejects dates older than `reference_date - 6 days`.
- [x] Confirm the prompt assertion fails because `MEAL_INPUT_PROMPT` still
  describes only the last seven calendar days, including today.
- [x] Confirm the `today - 8 days` rejection still passes, demonstrating that
  the defect is the missing inclusive lower date rather than absent date
  validation.

TDD failure evidence: before the production change, the focused suite reported
four failures. The parser rejected `today - 7 days`, both active-route cases
failed their startup-prompt assertion, and `today - 8 days` was rejected with
the old seven-day error.

**Implementation change:**

- [x] In `src/meal_planner/router.py::parse_meal_input`, change the lower-bound
  comparison to reject only dates earlier than
  `reference_date - timedelta(days=7)`.
- [x] Update the parser docstring and date-validation error to state the
  explicit eight-date contract: UTC today through the previous seven dates,
  inclusive. Avoid ambiguous “last seven days” wording.
- [x] In `src/meal_planner/bot_handler.py`, align `MEAL_INPUT_PROMPT` with the
  same explicit eight-date contract.
- [x] Do not alter `_reference_date_from_route()`, the structured-meal review
  transition, persistence calls, the README contract, or
  `_handle_meal_workflow()` behavior.

**Verification that the new tests pass:**

- [x] Re-run the focused Pytest command and confirm the parser, active-route,
  and startup-prompt boundary assertions all pass.
- [x] Confirm the accepted route reaches meal review with the exact
  `today - 7 days` date and the rejected route remains awaiting input at
  `today - 8 days`.

**Relevant regression tests:**

- [x] Run:

  ```bash
  uv run pytest tests/test_router.py tests/test_bot_handler.py \
    tests/test_dynamo.py tests/test_readme.py
  ```

- [x] Confirm retained parser validation, meal review transitions, repository
  state behavior, and README contract assertions pass before Task 2.

**Acceptance criteria:**

- [x] `parse_meal_input()` accepts exactly the intended lower boundary of UTC
  today minus seven days and rejects today minus eight days.
- [x] The complete active `/submit_meals` route proves both boundaries using
  an `AWAITING_MEAL_INPUT` state and deterministic UTC message dates.
- [x] No boundary test relies on `_handle_meal_workflow()` as evidence for the
  active submitted-meal contract.
- [x] Parser errors, the Bot prompt, README, and executable behavior all agree
  on eight inclusive UTC calendar dates.
- [x] No unrelated behavior or pre-existing worktree change is modified.
- [x] Focused and regression tests pass before final verification begins.

### Task 2: Verify the remediation and repository gates

**Files:**

- Modify for progress tracking only:
  `docs/plans/2026-08-30-submitted-meal-date-boundary-review-remediation.md`

- [x] Review the attributable diff and confirm it is limited to the active
  parser boundary, aligned parser/prompt wording, effective parser and route
  tests, removal or replacement of the legacy-only test, and this plan's
  progress updates.
- [x] Run an `rg` audit of `tests/test_bot_handler.py` to confirm no submitted-
  meal date-boundary test invokes `_handle_meal_workflow()`.
- [x] Run `uv run pytest` and confirm the full suite passes.
- [x] Run `uv run ruff check .` and resolve all findings.
- [x] Run `uv run ruff format --check .` and confirm formatting passes at the
  configured 80-column limit.
- [x] Run `uv run mypy` and confirm strict static typing passes.
- [x] Run `git diff --check` and confirm there are no whitespace errors.
- [x] Confirm
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`
  remains active and unchanged by this remediation.
- [x] Confirm the P2 is fully covered: the parser, the complete active route,
  user guidance, and README agree at both lower-boundary dates.
- [x] Move only this remediation plan to `docs/plans/completed/` after every
  implementation item and verification gate above passes. Do not move the
  original simplification plan.

**Acceptance criteria:**

- [x] Full Pytest, Ruff lint, Ruff format, strict Mypy, and diff checks pass.
- [x] The ineffective legacy-only boundary coverage has been removed or
  replaced by active-route coverage.
- [x] The original simplification plan remains active and untouched.
- [x] No GitHub issue, issue comment, commit, push, deployment, or other
  external action is performed as part of this remediation.

Task 2 verification completed. The attributable Task 1 changes are limited to
the active parser's inclusive lower boundary, aligned parser and Bot prompt
wording, parser boundary coverage, and active `/submit_meals` route coverage;
the inactive legacy-only boundary evidence was replaced. The route test uses a
deterministic UTC Telegram timestamp, proves `today - 7 days` reaches meal
review, and proves `today - 8 days` remains at input with aligned guidance.

Verification results: `uv run pytest` passes 414 tests with one skipped test;
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
`git diff --check` pass. The original simplification plan remains active at
its original path and retains SHA-256
`b2c03866c50eb295c77ffcde5fd6bb9165c55f769a344f5e33666ea34f2e7e6e`.
No source or test files were changed during Task 2, and no external action was
performed.

## Post-Completion

No external action is part of this remediation. In particular, do not create
or update a GitHub issue, commit, push, deploy, or perform live Telegram, AWS,
or provider verification. Those actions remain governed by the active
simplification plan and separate user authorization.
