# Optimize Submit Meals Confirmation UX

Associated issue: [#55](https://github.com/nsal/meal-planner-bot/issues/55)

## Overview

Replace the current multi-turn, LLM-assisted `/submit_meals` flow with a
deterministic single-meal submission and explicit confirmation workflow. The
command first shows actual meals logged for UTC today and yesterday, then asks
for one comma-separated entry containing when, meal type, and description.
The bot validates and reviews that entry before writing anything.

Confirmation atomically saves one meal and offers `Add more` or `Done`.
Cancellation discards the unconfirmed draft and ends the workflow. This makes
the persistence boundary visible, removes unnecessary LLM latency and cost,
and gives stale or retried Telegram callbacks safe, idempotent behavior.

## Context (from discovery)

- **Primary stack:** Python 3.14, Telegram Bot API webhooks, AWS Lambda,
  DynamoDB, Pydantic, and LiteLLM.
- **Workflow handling:** `src/meal_planner/bot_handler.py` currently starts an
  empty `MealLogDraft`, calls the conversational LLM for every meal reply,
  collects missing fields across multiple messages, and saves as soon as the
  draft becomes complete.
- **State contracts:** `src/meal_planner/models/schemas.py` supports one meal
  draft and durable revision-checked conversation state, but has no explicit
  pre-save review step. The existing `request_id` can identify a staged meal
  after its validation rules are extended for meal workflows.
- **Routing and presentation:** `src/meal_planner/router.py` validates check-in
  callbacks, while `src/meal_planner/telegram/api.py` already renders inline
  keyboards and acknowledges callback queries.
- **Persistence:** `src/meal_planner/db/dynamo.py` can query a bounded meal
  history window and atomically write one meal while transitioning workflow
  state. The transaction contract needs submission-ID and expected-step
  conditions for confirmation callbacks.
- **Existing tests:** schema, router, Telegram API, repository, and handler
  suites already cover the relevant layers and use mocks or Moto rather than
  live services.
- **Project standards:** follow `pyproject.toml`; keep typed Python and
  80-column Ruff formatting; run all tools with `uv`; finish with Ruff, strict
  Mypy, and the complete Pytest suite.

## Development Approach

- **Testing approach:** TDD. Add focused failing tests before implementing each
  behavior.
- Complete each task fully before moving to the next and make small, focused
  changes.
- Every task that changes code must add or update tests for successful and
  unsuccessful paths.
- Run the specified focused tests and then `uv run pytest` after every code
  task. Do not proceed while tests fail.
- Keep this plan synchronized with implementation. Mark completed items
  immediately, add newly discovered tasks with a `➕` prefix, and record
  blockers with a `⚠️` prefix.
- Preserve plan generation, profile management, groceries, `/checkin`, access
  control, and other conversational behavior.
- Preserve backward readability of old 24-hour meal workflow state. If an old
  field-by-field state is encountered, clear it conditionally and ask the user
  to restart `/submit_meals`; do not resume it under changed save semantics.
- Do not add dependencies or alter deployment infrastructure.

## Testing Strategy

- **Schema tests:** cover the new input/review steps, submission identifiers,
  complete versus empty draft shapes, post-confirmation state, and legacy state
  compatibility.
- **Parser tests:** use table-driven cases for the first-two-commas grammar,
  UTC aliases, ISO dates, seven-day boundaries, invalid dates and types, and
  non-empty descriptions.
- **Router tests:** validate every meal callback action, submission identifier,
  byte limit, malformed payload, and continued check-in callback behavior.
- **Telegram API tests:** inspect serialized keyboard labels, action payloads,
  `success`/`danger`/`primary` styles, emoji fallbacks, and callback lengths.
- **Repository tests:** use Moto to prove atomic confirm success, state
  transition, duplicate prevention, conditional contention, and rollback on a
  failed condition.
- **Handler tests:** exercise the complete command, input, review, confirm,
  cancel, add-more, done, stale-button, retry, old-state, and delivery-error
  paths without live Telegram, DynamoDB, or LLM calls.
- **Regression gates:** run `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `uv run pytest`.
- **End-to-end scope:** this repository has no browser UI or E2E framework. A
  post-deployment Telegram smoke test is listed under Post-Completion.

## Progress Tracking

- Mark completed checklist items with `[x]` immediately.
- Add newly discovered work with a `➕` prefix in the relevant task.
- Add blockers with a `⚠️` prefix and describe their impact.
- Update the design sections before implementing an approved scope change.
- Move this document to `docs/plans/completed/` only after all acceptance gates
  pass.

## Solution Overview

Keep one `MealLogDraft` in the existing durable conversation-state record. A
new submission starts in an input-awaiting step with a unique request ID. The
next ordinary text message is parsed locally by splitting only its first two
commas. Valid input replaces the empty draft with a complete draft and moves
the state to an awaiting-confirmation step; it does not write meal history.

Review, confirmation, cancellation, add-more, and done actions use compact
inline callback payloads containing the action and staged submission ID. The
handler accepts an action only when the current workflow ID and step match.
Confirm conditionally writes the meal and advances state in one DynamoDB
transaction. Cancel conditionally removes an unconfirmed state. Add more
creates a fresh empty draft and request ID only after the prior meal is saved;
Done clears the completed state.

The `/submit_meals` command queries a two-day history ending on the command
message's UTC calendar date and sends it as the first response. It sends the
structured input instructions as a distinct second response. Meal input uses
the input message's Telegram Unix timestamp interpreted in UTC to resolve
`today`, `yesterday`, and the inclusive seven-calendar-day range. If a usable
message timestamp is absent, processing time in UTC is the fallback.

## Technical Details

### User-visible flow

1. `/submit_meals` replaces any unfinished workflow using the existing
   revision-safe behavior.
2. The first bot message groups actual meal history beneath `Today` and
   `Yesterday`, explicitly showing `No meals submitted.` for an empty group.
3. The second bot message asks for `when, meal type, what you ate`, documents
   both date aliases, strict `YYYY-MM-DD`, the last-seven-days rule, all four
   valid meal types, and one example.
4. A valid reply is echoed in a review message with `✅ Confirm` and
   `❌ Cancel`. The review may show normalized parsed fields as well, but must
   retain the user's exact submitted text in the immediate response.
5. Confirm sends a clear saved-meal message with `➕ Add more` and
   `✅ Done`. Add more repeats the input prompt; Done reports completion and
   ends the workflow.
6. Cancel reports that the meal was not saved, removes the draft, and ends the
   workflow. Later ordinary messages receive normal conversational handling.

### Structured input and dates

- Split on the first two commas only. Trim the three resulting values and keep
  all later commas in the description.
- Match `today` and `yesterday` case-insensitively. Otherwise require a strict
  ISO `YYYY-MM-DD` value.
- Treat the allowed range as seven UTC calendar dates including today:
  `[reference_date - 6 days, reference_date]`.
- Accept only `breakfast`, `lunch`, `snack`, and `dinner`, case-insensitively,
  then store the canonical enum value.
- Reject missing separators, empty fields, malformed dates, dates outside the
  range, and unknown meal types with field-specific messages. Append the full
  system input prompt to every validation error.
- Invalid input must not mutate the durable draft or call the LLM.

### Workflow state and compatibility

- Add explicit steps for awaiting one structured meal input and awaiting its
  confirmation. Retain legacy meal steps so old records remain deserializable.
- Permit and require `request_id` for new meal input, review, and
  post-confirmation states. Continue enforcing its existing plan-workflow
  meaning independently.
- Require an empty draft in the input step, a complete draft in review and
  post-confirmation steps, and a step-specific state shape in Pydantic.
- On Add more, transition conditionally to a new empty draft with a new request
  ID and incremented revision.
- Detect legacy meal states in the handler, conditionally delete them, and send
  a restart message rather than saving or silently translating partial input.

### Callback and persistence safety

- Use a namespaced payload such as `meal:<action>:<request-id>`, where action
  is one of `confirm`, `cancel`, `add`, or `done`. Validate the complete payload
  and Telegram's 64-byte UTF-8 limit before dispatch.
- Use standard emoji in button text. Set Confirm and Done to `success`, Cancel
  to `danger`, and Add more to `primary`; clients that ignore styles still show
  meaningful symbols and labels.
- Always acknowledge valid and invalid callback queries, including exceptions.
- Confirm must condition the state write on workflow kind, request ID, expected
  revision, and awaiting-confirmation step. Its meal key must include the
  stable submission ID and prevent replacement of an existing item.
- If two Confirm callbacks race, only one transaction may save. Reload state
  after an expected conditional loss and report an already-confirmed result
  when appropriate; never report a duplicate meal as newly saved.
- A non-conditional DynamoDB failure leaves the complete review draft intact
  and returns a retryable confirmation error.
- Cancel, Add more, and Done use revision- and request-ID-checked state changes
  so old buttons cannot affect a newer submission or another workflow.

## Implementation Steps

### Task 1: Define the single-meal workflow state contracts

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`

- [x] write failing tests for new meal-input and awaiting-confirmation steps
- [x] write failing tests requiring the correct empty or complete draft and
  submission ID for each new step
- [x] write regression tests proving legacy meal states and all plan workflow
  states still deserialize and validate
- [x] add the new workflow steps and extend shape validation without changing
  `MealLogDraft` or `MealLogEntry`
- [x] run `uv run pytest tests/test_schemas.py`, then `uv run pytest`; both must
  pass before Task 2

### Task 2: Add deterministic meal input and callback parsers

**Files:**
- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_router.py`

- [x] write failing parameterized tests for first-two-commas parsing, trimming,
  case normalization, and descriptions containing commas
- [x] write failing tests for UTC aliases, exact seven-day boundaries, malformed
  ISO dates, future or old dates, invalid meal types, and empty fields
- [x] write failing tests for all four meal callback actions, malformed or
  oversized payloads, and valid UUID submission identifiers
- [x] implement typed deterministic input and callback parsing with specific
  validation results suitable for user-facing error messages
- [x] preserve all existing command, conversational, and check-in routing tests
- [x] run `uv run pytest tests/test_router.py`, then `uv run pytest`; both must
  pass before Task 3

### Task 3: Render meal review and continuation keyboards

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_telegram_api.py`

- [x] write failing tests for Confirm/Cancel labels, callback payloads,
  `success` and `danger` styles, and one-row layout
- [x] write failing tests for Add more/Done labels, callback payloads, `primary`
  and `success` styles, and one-row layout
- [x] write failing boundary tests proving every generated callback payload is
  at most 64 UTF-8 bytes
- [x] add small typed helpers that send review and post-confirmation messages
  through the existing plain-text `send_message` boundary
- [x] keep ordinary message splitting and existing check-in keyboards unchanged
- [x] run `uv run pytest tests/test_telegram_api.py`, then `uv run pytest`; both
  must pass before Task 4

### Task 4: Make one-meal confirmation atomic and idempotent

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] write failing repository tests for atomically inserting one meal and
  advancing the matching review state
- [x] write failing tests for duplicate submission IDs, stale revisions, wrong
  request IDs or steps, and transaction contention
- [x] write failing tests proving an expected conditional failure writes
  neither a new meal nor a partial state transition
- [x] implement the confirmation transaction with stable submission keys,
  non-overwrite conditions, and exact state preconditions
- [x] retain existing meal-history query ordering and compatibility with legacy
  meal keys
- [x] run `uv run pytest tests/test_dynamo.py`, then `uv run pytest`; both must
  pass before Task 5

### Task 5: Start submission with recent history and structured instructions

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing command tests for UTC today/yesterday grouping, ordering,
  empty groups, and exactly two initial bot messages
- [x] write failing tests for deriving the command reference date from the
  Telegram Unix timestamp and falling back safely when it is absent or invalid
- [x] update `/submit_meals` to query two days of history, replace unfinished
  state safely, create a unique empty single-meal submission, and send history
  before the reusable prompt
- [x] write tests for conditional replacement contention and correct messaging
  when another workflow is replaced
- [x] run `uv run pytest tests/test_bot_handler.py`, then `uv run pytest`; both
  must pass before Task 6

### Task 6: Validate input and present an unpersisted review

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing end-to-end handler tests for valid aliases, explicit dates,
  all meal types, exact input echoing, and transition to review without a meal
  write
- [x] write failing tests for each input error showing a specific explanation
  followed by the complete reusable prompt with no state mutation
- [x] write failing tests proving active meal input bypasses profile, history,
  prompt-builder, parser, and LLM calls
- [x] implement local parsing and conditional review-state transition using the
  input message's UTC timestamp as its date reference
- [x] handle old field-by-field meal states with conditional cleanup and a clear
  `/submit_meals` restart response
- [x] run `uv run pytest tests/test_bot_handler.py`, then `uv run pytest`; both
  must pass before Task 7

### Task 7: Handle Confirm, Cancel, Add more, and Done callbacks

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing happy-path tests for Confirm saving once, Add more creating
  a fresh draft and prompt, Done clearing state, and Cancel discarding an
  unconfirmed draft
- [x] write failing tests for repeated Confirm, concurrent conditional loss,
  persistence failure and retry, stale buttons, wrong workflow or step, and
  malformed actions
- [x] write failing tests proving every callback path acknowledges Telegram and
  that old buttons cannot mutate a newer workflow
- [x] dispatch meal callbacks before check-in validation and implement each
  revision- and request-ID-safe state transition
- [x] send the designed styled keyboards and clear confirmation, cancellation,
  completion, stale-action, and retry messages
- [x] preserve existing `/cancel` command and `/checkin` callback behavior
- [x] run `uv run pytest tests/test_bot_handler.py`, then `uv run pytest`; both
  must pass before Task 8

### Task 8: Verify acceptance criteria and repository quality gates

**Files:**
- Modify: `docs/plans/2026-08-22-optimize-submit-meals-confirmation-ux.md`
- Modify: test files from Tasks 1-7 if a verification gap is found

- [x] verify every Overview and Technical Details requirement is implemented
- [x] verify edge cases for UTC midnight, seven-day boundaries, embedded commas,
  duplicate callbacks, stale workflow state, and Telegram delivery failures
- [x] verify no dependency or deployment changes were introduced
- [x] run `uv run ruff format .`, then `uv run ruff format --check .`
- [x] run `uv run ruff check .`, `uv run mypy`, and `uv run pytest`; all must
  pass before Task 9

### Task 9: Update documentation and complete delivery

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-08-22-optimize-submit-meals-confirmation-ux.md`
- Move on completion: plan file to `docs/plans/completed/`

- [x] update the README command and user-workflow sections with the exact input
  grammar, UTC date interpretation, seven-day range, and button lifecycle
- [x] update tests for any executable or validated documentation contract
- [x] confirm no new reusable engineering convention requires an `AGENTS.md`
  change
- [x] rerun `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`
- [x] move this plan to `docs/plans/completed/` only after every checklist item
  is complete
- [x] create a Conventional Commit referencing the associated issue, push a
  dedicated non-`master` branch, open a pull request, and comment on the issue
  with the commit or pull-request link when implementation is requested

## Post-Completion

**Manual verification**

- In an authorized non-production Telegram chat, run `/submit_meals` and verify
  history arrives before the separate structured prompt.
- Submit a description containing commas, cancel it, and verify it does not
  appear in a later history summary.
- Submit and confirm meals using `today`, `yesterday`, and both ends of the UTC
  seven-day window; verify each appears once in DynamoDB and in later history.
- Confirm a meal, use Add more for another, then use Done; verify both meals are
  saved and ordinary conversation resumes.
- Tap old Confirm, Cancel, Add more, and Done buttons and verify they cannot
  change current state or duplicate a meal.
- Check button colors on currently supported Telegram clients and verify emoji
  labels remain clear on clients that use default button styling.

**External system updates**

- Deploy through the repository's existing protected-branch pull-request
  workflow; never push or merge directly to `master`.
- Review CloudWatch logs after deployment for unexpected callback validation,
  transaction contention, Telegram API errors, or legacy-state restarts.
