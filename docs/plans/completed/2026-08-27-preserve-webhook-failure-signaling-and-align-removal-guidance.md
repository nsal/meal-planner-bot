# Preserve Webhook Failure Signaling and Align Removal Guidance

## Overview

Correct two findings from the review of the uncommitted numbered-profile
removal work:

- return an explicit HTTP 500 response when an unexpected exception reaches
  the Telegram update boundary, so Telegram can retry without exposing the
  exception contents; and
- align stale-removal guidance and README documentation with the implemented
  behavior, where successful removals refresh the numbered removal list rather
  than returning to the category menu.

Known persisted-profile validation failures and Telegram delivery failures
remain controlled HTTP 200 paths. The change is deliberately narrow and does
not alter callback formats, transaction guards, profile ordering, or removal
persistence.

## Context (from discovery)

- `BotHandler.handle_update` currently catches every unexpected `Exception`,
  emits a bounded diagnostic, optionally sends a generic retry message, and
  then falls through to the normal HTTP 200 response.
- Returning HTTP 200 acknowledges an infrastructure or programming failure as
  successfully handled, preventing Telegram from retrying that update.
- Existing tests distinguish profile `ValidationError` from an unexpected
  repository failure, but the unexpected-failure test does not assert the
  response status.
- Numbered-removal success remains in `AWAITING_PROFILE_INPUT` and renders the
  refreshed list. The README instead says every successful change returns to
  the category menu.
- Stale-removal responses say that nothing changed but do not provide the
  `/profile` recovery instruction currently promised by the README.

## Development Approach

- **Testing approach:** TDD. Add or update each regression test first, confirm
  it fails against the reviewed implementation, then make the smallest code or
  documentation change that satisfies it.
- Complete each task fully and pass its focused tests before starting the next
  task.
- Preserve bounded, redacted diagnostics. Do not log exception messages,
  profile contents, callback payloads, or persisted values.
- Preserve HTTP 200 for unknown or denied updates, expected profile validation
  recovery, and Telegram delivery failures.
- Keep callback wire formats, removal transaction behavior, revision guards,
  presentation ordering, and add/change workflows unchanged.
- Keep this plan synchronized with implementation progress and mark an item
  `[x]` only after its behavior and tests are complete.

## Testing Strategy

- **Update-boundary tests:** prove unexpected command, callback, and
  conversational handler exceptions return HTTP 500 while known validation and
  delivery failures retain HTTP 200.
- **Privacy tests:** prove the bounded unexpected-error reason code remains in
  logs while exception text and update contents remain absent.
- **Delivery fallback tests:** prove failure to send the generic user-facing
  retry message does not convert the unexpected-error response back to HTTP
  200.
- **Removal guidance tests:** cover stale conversation state, stale profile
  revision, and conditional transaction conflict, plus successful refreshed
  removal rendering.
- **Documentation verification:** compare README claims with the tested
  handler behavior.
- **Regression gates:** run Ruff formatting, Ruff linting, strict mypy, and the
  complete pytest suite. The project has no separate browser/UI end-to-end
  suite.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Add newly discovered work with a `➕` prefix.
- Record blockers or deviations with a `⚠️` prefix.
- Do not proceed to the next task while the current task's tests fail.
- Move this plan to `docs/plans/completed/` only after every gate passes.

## Solution Overview

Keep the existing expected-error branches in `handle_update`, but return an
explicit `{"statusCode": 500, "body": "error"}` response from the final
unexpected-exception branch after bounded logging and the best-effort generic
user notification. This preserves privacy and deterministic API behavior while
allowing Telegram to retry. The Lambda invocation itself remains controlled,
so tests must establish the response contract directly.

Use one shared stale-removal recovery message for revision and transaction
conflicts, and ensure stale conversation-state handling gives the same recovery
direction when the callback is no longer valid. Keep invalid-index wording
distinct. Update README text to say successful removals refresh the active
numbered list, while add/change operations continue returning to their existing
menus.

## Technical Details

- The explicit 500 response applies only when an unexpected exception escapes
  the selected command, callback, or conversational handler into
  `BotHandler.handle_update`.
- `ProfileLoadValidationError` remains a redacted recovery path ending in HTTP
  200, including callback acknowledgement when applicable.
- `TelegramAPIError` remains a controlled delivery path ending in HTTP 200;
  this plan does not change existing Telegram delivery semantics.
- The generic retry message sent after an unexpected failure is best effort.
  If that send raises `TelegramAPIError`, log only the existing bounded
  delivery reason and still return HTTP 500.
- Stale numbered-removal messages should direct the user to reopen `/profile`
  without including the removed item label, callback data, or persisted profile
  content.
- Successful numbered removals continue to send the success acknowledgement
  and call `send_profile_operation` with the committed profile snapshot.

## What Goes Where

- `src/meal_planner/bot_handler.py`: explicit unexpected-error response and
  consistent stale-removal recovery guidance.
- `tests/test_bot_handler.py`: TDD response, privacy, fallback-delivery, stale
  guidance, and successful-refresh regression tests.
- `README.md`: accurate successful-removal and stale-button behavior.
- This plan: progress and final verification record. No `AGENTS.md` change is
  expected because no engineering convention changes.

## Implementation Steps

### Task 1: Return HTTP 500 for unexpected update-boundary failures

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] update the unexpected profile-load failure test first to assert an exact
  HTTP 500 response; record that it fails because the reviewed handler returns
  HTTP 200
- [x] add parameterized boundary tests proving an unexpected exception from a
  command, callback, or conversational handler returns the same HTTP 500
  response
- [x] add a failing privacy assertion proving exception text and update content
  are absent while the bounded `reason_code=unexpected` diagnostic remains
- [x] add a failing delivery-fallback test proving a `TelegramAPIError` while
  sending the generic retry message still returns HTTP 500
- [x] add regression assertions proving profile validation recovery, Telegram
  delivery failures, unknown updates, and denied updates still return HTTP 200
- [x] change only the unexpected-exception branch in `handle_update` to return
  the explicit redacted HTTP 500 response after its bounded log and best-effort
  notification
- [x] run the new unexpected-boundary tests by exact node ID; all must pass
- [x] run `uv run pytest tests/test_bot_handler.py -k 'update and (unexpected or validation or telegram or denied)'`; it must pass before Task 2
- [x] run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 2

**Acceptance criteria:**

- Every unexpected exception reaching `handle_update` produces the exact HTTP
  500 response regardless of whether the generic retry message is delivered.
- Expected profile validation and Telegram delivery failures retain their
  current controlled HTTP 200 behavior.
- Logs remain bounded and do not expose exception messages, update text,
  callback data, or stored profile values.
- Unknown and access-denied updates remain silent HTTP 200 no-ops.

### Task 2: Make stale-removal recovery and documentation accurate

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `README.md`

- [x] add or update failing tests first for stale conversation state, stale
  profile revision, and removal transaction conflict; assert each response
  says nothing changed and directs the user to reopen `/profile`
- [x] retain a distinct invalid-index test proving malformed or out-of-range
  selections do not claim a revision conflict and do not mutate either record
- [x] add or strengthen a successful-removal regression proving the handler
  renders the refreshed numbered list from the committed profile and does not
  return to the profile category menu
- [x] introduce one bounded stale-removal recovery message and use it for the
  stale state, revision, and conditional no-commit paths without exposing item
  labels or callback data
- [x] update README removal guidance so successful removals refresh and retain
  the active numbered list, while add/change operations keep their existing
  menu behavior
- [x] verify README stale-button recovery wording matches the tested handler
  response exactly in meaning
- [x] run the new stale-removal and successful-refresh tests by exact node ID;
  all must pass
- [x] run `uv run pytest tests/test_bot_handler.py -k 'numbered and removal and (stale or invalid or refresh)'`; it must pass before Task 3
- [x] run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 3

**Acceptance criteria:**

- Every stale numbered-removal path is a no-op and gives actionable `/profile`
  recovery guidance.
- Invalid indices remain distinguishable from revision or state conflicts.
- Successful removals retain removal mode and render buttons from the committed
  profile snapshot.
- README accurately distinguishes removal refresh behavior from add/change
  navigation.
- Callback formats, profile/state revisions, persistence behavior, and stored
  values remain unchanged.

### Task 3: Verify acceptance criteria and archive the plan

**Files:**

- Modify: `docs/plans/2026-08-27-preserve-webhook-failure-signaling-and-align-removal-guidance.md`
- Move to: `docs/plans/completed/2026-08-27-preserve-webhook-failure-signaling-and-align-removal-guidance.md`

- [x] verify unexpected exceptions return HTTP 500 and all explicitly expected
  recovery/no-op paths retain HTTP 200
- [x] verify unexpected-error and stale-removal diagnostics/messages contain no
  exception text, update contents, callback payloads, or persisted profile data
- [x] verify every stale removal is a no-op with `/profile` guidance and every
  successful removal refreshes from the committed snapshot
- [x] verify README behavior matches the tested update-boundary and removal
  workflows
- [x] run `uv run ruff format --check .`
- [x] run `uv run ruff check .`
- [x] run `uv run mypy`
- [x] run `uv run pytest`
- [x] record exact focused and full verification results, including any
  demonstrably pre-existing warnings or external limitations, in this plan
- [x] confirm no new engineering convention requires an `AGENTS.md` update
- [x] after all checks pass, move this plan to `docs/plans/completed/`

**Acceptance criteria:**

- Both actionable review findings have failing-first regression coverage and
  pass after implementation.
- Ruff formatting, Ruff linting, strict mypy, and the complete pytest suite all
  pass.
- No profile compatibility, numbered-removal, transaction, access-control, or
  Telegram delivery behavior regresses outside the specified changes.

### Task 3 verification record

- Focused acceptance verification: `uv run pytest
  tests/test_bot_handler.py::test_unexpected_profile_load_failure_is_distinguished_in_logs
  tests/test_bot_handler.py::test_unexpected_handler_failures_return_http_500
  tests/test_bot_handler.py::test_unexpected_boundary_diagnostic_is_bounded_and_redacted
  tests/test_bot_handler.py::test_unexpected_failure_delivery_fallback_still_returns_http_500
  tests/test_bot_handler.py::test_unknown_update_is_silent_http_200
  tests/test_bot_handler.py::test_numbered_profile_removal_rejects_invalid_without_revision_conflict
  tests/test_bot_handler.py::test_numbered_profile_removal_stale_conversation_state_directs_to_profile
  tests/test_bot_handler.py::test_numbered_profile_removal_stale_profile_revision_directs_to_profile
  tests/test_bot_handler.py::test_numbered_profile_removal_transaction_conflict_directs_to_profile
  tests/test_bot_handler.py::test_real_numbered_preference_removal_refreshes_from_committed_profile
  tests/test_bot_handler.py::test_real_numbered_constraint_removal_preserves_unrelated_profile_data
  tests/test_bot_handler.py::test_numbered_removal_preserves_add_and_change_workflow_rendering`:
  15 passed in 1.74s.
- Expected-path and documentation verification: `uv run pytest
  tests/test_bot_handler.py::test_residual_profile_validation_error_gets_reply_at_update_boundary
  tests/test_bot_handler.py::test_profile_validation_diagnostics_are_bounded_and_redacted
  tests/test_bot_handler.py::test_telegram_failure_is_controlled_at_update_boundary
  tests/test_bot_handler.py::test_unauthorized_private_update_is_silent_and_has_no_side_effects
  tests/test_bot_handler.py::test_allowlisted_group_update_is_silent_and_has_no_side_effects
  tests/test_bot_handler.py::test_allowlisted_update_without_chat_id_has_no_side_effects
  tests/test_bot_handler.py::test_denied_callback_does_not_acknowledge_or_mutate
  tests/test_bot_handler.py::test_denial_log_omits_update_contents`:
  10 passed in 1.66s.
- Workflow and README regression verification: `uv run pytest
  tests/test_bot_handler.py tests/test_readme.py`: 419 passed in 3.38s.
- `uv run ruff format --check .`: passed; 112 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: failed with 1,880 passed and 2 failed in
  `tests/test_template.py::test_built_artifact_imports_lambda_handler` for
  `BotFunction` and `PlannerFunction`. Both failures assert that
  `.aws-sam/build/BotFunction-Shared/src/meal_planner/bot_handler.py` is stale
  compared with `src/meal_planner/bot_handler.py`; the mismatch is shown at
  byte index 3727. `command -v sam` found no executable, so the generated SAM
  artifacts could not be rebuilt in this environment.
- `⚠️` Task 3 is blocked: the mandatory full pytest gate is nonzero because of
  stale generated `.aws-sam` artifacts and the required `sam` rebuild tool is
  unavailable. The plan remains in place and is not archived.

#### Retry attempt 2 verification record

- Focused acceptance verification: the 25 exact Task 3 acceptance nodes
  covering unexpected HTTP 500 handling, expected HTTP 200 paths, bounded
  diagnostics, stale-removal no-op guidance, invalid selections, committed
  snapshot refresh, and add/change workflow rendering passed: `25 passed in
  1.71s`.
- `uv run ruff format --check .`: passed; 112 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: failed with `1,880 passed, 2 failed in 14.51s`. The two
  failures were the unchanged
  `tests/test_template.py::test_built_artifact_imports_lambda_handler` cases
  for `BotFunction` and `PlannerFunction`. Both fail because
  `.aws-sam/build/BotFunction-Shared/src/meal_planner/bot_handler.py` differs
  from `src/meal_planner/bot_handler.py` at byte index 3727.
- Safe artifact investigation found no installed `sam` executable, no
  installed `samcli` or `aws_sam_translator` module, no installed UV tools,
  and no repository Makefile or build wrapper. The documented recovery command
  is `uvx --from aws-sam-cli sam build --beta-features`, but it would install a
  missing tool and was not run under the verification constraint. The ignored
  `.aws-sam` artifact remained unchanged.
- The focused tests and all non-artifact gates demonstrate the implementation
  acceptance criteria. Full-suite completion and plan archiving remain blocked
  solely by the stale generated artifact and unavailable permitted rebuild
  tool.
- `AGENTS.md` was reread; no new engineering convention was introduced by
  this plan, so no instruction-file update is required.
- `⚠️` Retry attempt 2 remains blocked on the mandatory nonzero full pytest
  gate. The plan remains in place and is not archived.

#### Retry attempt 3 verification record

- Focused acceptance verification: the exact 20-node command covering
  unexpected HTTP 500 handling, expected HTTP 200 paths, bounded diagnostics,
  stale-removal no-op guidance, invalid selections, committed snapshot
  refresh, and add/change workflow rendering collected 25 parameterized cases:
  `25 passed in 1.62s`.
- Workflow and README regression verification: `uv run pytest
  tests/test_bot_handler.py tests/test_readme.py` passed with `419 passed in
  3.28s`.
- `uv run ruff format --check .`: passed; 112 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: failed with `1,880 passed, 2 failed in 14.70s`. The two
  failures were `tests/test_template.py::test_built_artifact_imports_lambda_handler`
  for `BotFunction` and `PlannerFunction`. Both failures assert that
  `.aws-sam/build/BotFunction-Shared/src/meal_planner/bot_handler.py` is stale
  compared with `src/meal_planner/bot_handler.py`; the mismatch is at byte
  index 3727 (`b'\\n' != b'S'`).
- `command -v sam` found no executable. `uvx` is available, but installing or
  invoking the unavailable SAM CLI was outside this final verification's
  already-available-tools constraint, so the generated artifacts could not be
  rebuilt. No cleanup, reset, install, implementation, test, README, or
  unrelated-file changes were made.
- `AGENTS.md` was read in full; this plan introduces no new engineering
  convention requiring an instruction-file update.
- `⚠️` Retry attempt 3 is blocked: the mandatory full pytest gate remains
  nonzero solely because the pre-existing stale `.aws-sam` artifacts cannot be
  rebuilt in this environment. The plan remains in place and is not archived.

#### Retry attempt 4 verification record

- Rebuilt the ignored stale SAM output with the documented command
  `uvx --from aws-sam-cli sam build --beta-features`; it completed successfully
  for `BotFunction` and `PlannerFunction` using the repository lock file.
- Focused acceptance verification: the exact 20-node Task 3 command covering
  unexpected HTTP 500 handling, expected HTTP 200 paths, bounded diagnostics,
  stale-removal no-op guidance, invalid selections, committed-snapshot refresh,
  and add/change workflow rendering passed: `25 passed in 1.71s`.
- `uv run ruff format --check .`: passed; 112 files already formatted.
- `uv run ruff check .`: passed; all checks passed.
- `uv run mypy`: passed; no issues found in 20 source files.
- `uv run pytest`: passed; `1,882 passed in 20.59s`.
- `AGENTS.md` was reread; this plan introduces no new engineering convention
  requiring an instruction-file update. All Task 3 gates now pass, so the plan
  is archived in `docs/plans/completed/`.

## Post-Completion

**Manual verification:**

1. In the development bot, trigger a bounded simulated repository outage and
   confirm the webhook endpoint returns HTTP 500 without logging exception or
   update contents.
2. Confirm Telegram retries the failed update after the transient condition is
   removed and that revision guards prevent duplicate profile mutations.
3. Use an old numbered-removal button and confirm the response directs the user
   to reopen `/profile` without changing the profile or conversation state.
4. Complete a valid numbered removal and confirm the refreshed numbered list
   remains active.

**External systems:**

- Development deployment, Telegram retry observation, CloudWatch inspection,
  and live DynamoDB failure simulation require the external development
  environment and are not prerequisites for repository completion.
