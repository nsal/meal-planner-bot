# AI-Powered Draft Plan Revisions

## Overview

Add an AI-backed revision loop for generated draft meal plans. After `/plan`
creates a draft, a user can describe whole-plan amendments in natural language,
such as changing breakfast frequencies or excluding an ingredient. The bot will
regenerate the complete draft asynchronously, preserve all accumulated
request-specific instructions, show the replacement, and invite further edits
or confirmation.

This fixes the mismatch between the current user-facing promise to "request
edits" and the existing mutation contract, which can only update one meal when
the conversational LLM supplies an exact day and meal type. Revision
instructions remain attached to the current weekly plan and never update the
household profile.

- GitHub issue:
  [#32](https://github.com/nsal/meal-planner-bot/issues/32).
- Selected approach: dedicated asynchronous whole-draft revision workflow.
- Testing approach: TDD; add failing behavior tests before implementation in
  every task.

## Context (from discovery)

- **Files/components involved:** `src/meal_planner/models/schemas.py`,
  `src/meal_planner/llm/prompts.py`, `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`, `src/meal_planner/db/dynamo.py`,
  `tests/factories.py`, and their focused test modules.
- **Current conversational path:** `build_conversational_prompt` asks the LLM
  for an intent and untyped entity dictionary. `BotHandler._edit_plan` expects
  `day` and `meal_type`, so draft-wide constraints fail validation and surface
  as the misleading generic persistence error.
- **Current planning path:** `/plan` stores a durable request state, invokes the
  Planner Lambda asynchronously, validates a complete `WeeklyPlan`, persists it
  with optimistic revision checks, and clears matching state before delivery.
- **Persistence patterns:** conversation state uses revision-checked writes;
  generated drafts use exact-week status and revision conditions; DynamoDB
  transactions are already used where multiple items must change atomically.
- **Lifecycle constraints:** only eligible drafts are revised. Existing targeted
  edits to active confirmed plans and their grocery refresh behavior remain
  unchanged.
- **Project standards:** Python 3.14, strict Mypy, Ruff at 80 columns, `uv` for
  execution and dependencies, and full Pytest verification before completion.

## Development Approach

- **Testing approach:** TDD. Start each task with failing tests for the stated
  success and error behavior, implement only enough to pass them, and run the
  focused tests before proceeding.
- Complete each task fully before moving to the next task.
- Make small, focused changes and retain backward-compatible defaults for
  already persisted plans and conversation state.
- Every task that changes Python behavior must include new or updated tests for
  its code paths, including success and error scenarios.
- All focused tests must pass before starting the next task.
- Update this plan immediately if scope or architectural decisions change.
- Use Ruff for formatting and lint fixes; do not introduce another formatter.
- Add no dependency unless it is essential; if one is added, use `uv add` and
  update `uv.lock` in the same task.
- Keep all implementation work off `master`; publish it through a dedicated
  feature or bug-fix branch and pull request.

## Testing Strategy

- **Schema tests:** validate backward-compatible plan loading, bounded planning
  instructions, revision event context, and workflow-specific state invariants.
- **Prompt tests:** prove draft requests are classified into a narrow
  `revise_plan` contract and the revision prompt contains the permanent profile,
  complete current draft, accumulated instructions, and latest amendment.
- **Repository tests:** use Moto to prove the plan replacement and matching
  conversation-state cleanup are atomic and reject stale plan revisions,
  changed request state, confirmation races, and duplicate events.
- **Bot handler tests:** reproduce the reported amendment verbatim, check the
  asynchronous payload and messages, exercise retry state, and ensure a running
  revision blocks confirmation and additional edits.
- **Planner handler tests:** cover success, bounded repair, provider failure,
  stale events, server-owned field normalization, conditional conflicts, and
  Telegram delivery failure after persistence.
- **Regression tests:** retain initial `/plan`, confirmed-plan targeted edits,
  grocery generation, cancellation, profile updates, and meal logging.
- **End-to-end UI tests:** not applicable; this repository has no browser UI.
  Handler-level Telegram update tests provide workflow coverage.
- **Required gates:** `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, and `uv lock --check`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document issues or blockers with a `⚠️` prefix.
- Update the plan if implementation deviates from the selected design.
- Keep this document synchronized with tests and implementation.
- Move it to `docs/plans/completed/` only after every required gate passes.

## Solution Overview

Add a `revise_plan` conversational intent whose only mutable input is a bounded
natural-language `amendment`. The conversational model identifies and extracts
the request; it does not manufacture meal structures. `BotHandler` starts a
durable `PLAN_REVISION` workflow tied to an exact week and plan revision, then
invokes a new Planner Lambda action.

The Planner Lambda consistently reads the current draft and profile, verifies
the request and plan snapshots, and sends the full draft plus accumulated
temporary instructions to the planner model. The model returns a complete
seven-day replacement through the existing strict JSON and bounded repair path.
Server code normalizes status, revision, grocery fields, outcomes, week, and
planning instructions before persistence.

The revised plan and matching workflow cleanup are committed atomically so a
cancelled, replaced, duplicated, or stale request cannot update the draft.
Successful revisions are displayed with another review/edit/confirm prompt.
Provider or validation failures leave the original draft unchanged and move the
matching request to a retry-ready state. Confirmation and new amendments are
blocked while a revision is generating so the user cannot accidentally confirm
the prior draft or create ambiguous ordering.

Rejected alternatives:

- Reusing `/plan` generation overloads initial creation and revision semantics,
  weakens user-facing error messages, and complicates stale-request handling.
- Returning meal-level patch operations cannot reliably express aggregate rules
  such as "three egg breakfasts" and adds a larger validation surface.

## Technical Details

### Domain contracts

- Add `ConversationIntent.REVISE_PLAN`.
- Add a bounded instruction type and a backward-compatible
  `WeeklyPlan.planning_instructions` list with an empty default. Enforce limits
  on individual entries and the collection so prompt and item sizes stay
  bounded.
- Store the initial non-empty `/plan` preference as the first server-owned plan
  instruction after generation.
- Add `ConversationWorkflowKind.PLAN_REVISION`. Its valid states are
  `GENERATING` and `RETRY_READY`, and it requires an amendment, target week,
  expected plan revision, and request ID while forbidding meal-log fields.
- Add a typed revision event context requiring the amendment, request ID,
  conversation-state revision, and expected plan revision together.

### Conversational contract

- When the current plan is an eligible draft, classify any request to alter the
  plan as `revise_plan` and return only `{"amendment": "..."}`.
- Keep `confirm_plan` distinct. Retain `edit_plan` for targeted changes to an
  active confirmed plan, with its required entity contract documented clearly.
- Invalid or empty amendment metadata produces a specific rephrase response,
  never the generic database-save message.

### Revision prompt

- Include the permanent household profile and safety constraints.
- Include the complete current plan, not only meal names.
- Include all prior plan-specific instructions in order and append the newest
  amendment with highest priority.
- Require a complete plan for the same week using the existing output schema.
- Tell the model to satisfy all compatible instructions and preserve sensible
  unaffected choices, while profile allergies, restrictions, calorie targets,
  and safety rules retain precedence.

### Asynchronous event and persistence flow

```text
Telegram amendment
  -> conversational LLM: revise_plan + amendment
  -> Bot Lambda: create PLAN_REVISION / GENERATING state
  -> async Planner Lambda event with week and both revisions
  -> consistent draft/profile/state reads
  -> full-plan LLM generation and bounded repair
  -> atomic conditional plan replacement + matching state deletion
  -> revised draft + review/edit/confirm prompt
```

- The replacement revision is exactly `expected_plan_revision + 1`.
- The persisted replacement remains `draft`, uses `not_requested` grocery
  status, has an empty grocery list, and resets all meal outcomes to
  `unreported`.
- Persistence requires the existing plan to remain a draft at the expected
  revision and conversation state to retain the expected request ID and state
  revision.
- A conditional conflict changes nothing. Duplicate asynchronous delivery is
  therefore harmless.
- Provider or schema failure transitions only the matching conversation state
  to `RETRY_READY`; the original draft remains intact.
- A retry reuses the stored amendment, target week, and latest expected plan
  revision. `/cancel` continues to clear the unfinished workflow.

### User-visible behavior

- Accepted amendment: "I’m revising your draft now."
- Revision in progress: explain that the bot is still working and ask the user
  to wait before confirming or sending another amendment.
- Missing or expired draft: direct the user to `/plan`.
- Invalid amendment: ask the user to describe the desired plan change again.
- Planner failure: explain that revision failed, the original draft is
  unchanged, and the user can reply `retry` or use `/cancel`.
- Conflict: explain that the plan or request changed and the stale result was
  discarded.
- Success: send the full revised draft, then say, "Review this revised draft,
  request more edits, or tell me to confirm it."
- Delivery failure after an atomic save is logged as delivery failure and never
  rolls back or labels the saved revision as generation failure.

## What Goes Where

- **Implementation Steps:** schema, prompts, repository transaction, Bot Lambda
  workflow, Planner Lambda revision action, focused regression coverage, and
  project documentation are implemented in this repository.
- **Post-Completion:** test-stack Telegram exercises, cloud deployment, and
  provider-quality observation require external systems and are listed without
  implementation checkboxes.

## Implementation Steps

### Task 1: Define persisted instruction and revision workflow contracts

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/factories.py`

- [x] Write failing schema tests for default and populated plan instructions,
  per-entry and collection bounds, and JSON round trips of existing plans.
- [x] Write failing tests for valid revision-generating and retry-ready states,
  plus missing, mixed-workflow, and inconsistent revision fields.
- [x] Write failing tests for complete and partial revision event contexts.
- [x] Add the `revise_plan` intent, bounded plan instruction field,
  `PLAN_REVISION` workflow kind, workflow fields, and typed revision context.
- [x] Update shared factories to build plans with optional instructions without
  changing existing callers.
- [x] Run `uv run pytest tests/test_schemas.py`; it must pass before Task 2.

### Task 2: Specify conversational extraction and full revision prompts

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_prompts.py`

- [x] Write failing tests that require draft-wide edits to emit the
  `revise_plan` intent with only a faithful natural-language amendment.
- [x] Add the explicit `revise_plan`, `confirm_plan`, and confirmed-plan
  `edit_plan` entity rules to the conversational prompt.
- [x] Write failing tests for a revision prompt containing the full current
  draft, permanent profile, prior instructions, newest amendment, exact week,
  constraint precedence, and complete JSON schema.
- [x] Implement `build_plan_revision_prompt` using bounded, clearly separated
  trusted context and user-provided instruction sections.
- [x] Include the reported egg, waffle, crepe, open-day, and cauliflower request
  verbatim in prompt contract coverage.
- [x] Run `uv run pytest tests/test_prompts.py`; it must pass before Task 3.

### Task 3: Persist a revised draft and workflow cleanup atomically

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] Write a failing Moto test for atomically replacing the expected draft and
  deleting the matching revision conversation state.
- [x] Write failing tests for stale plan revision, confirmed-plan race, changed
  request ID, changed state revision, missing state, and duplicate delivery;
  every failure must leave both items unchanged.
- [x] Implement a typed repository method using one DynamoDB transaction with
  conditions on plan status/revision and state request ID/revision.
- [x] Reuse the repository's expected transaction-conflict classification while
  allowing service and malformed cancellation failures to propagate.
- [x] Verify stored planning instructions and normalized lifecycle fields round
  trip through the existing plan readers.
- [x] Run `uv run pytest tests/test_dynamo.py`; it must pass before Task 4.

### Task 4: Start, block, retry, and cancel Bot-side draft revisions

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write failing conversational tests proving the reported amendment starts
  one revision workflow and sends an exact-week asynchronous event containing
  the amendment, request ID, state revision, and expected plan revision.
- [x] Write failing tests for empty or oversized amendment, missing or expired
  draft, invocation failure, duplicate Telegram update, and competing starts.
- [x] Implement revision-state creation and invocation with specific user-facing
  responses instead of the generic persistence error.
- [x] Write failing tests proving generating state blocks confirmation and new
  amendments, while retry-ready state accepts `retry` and `/cancel` remains
  effective.
- [x] Implement revision-state routing and retry without changing initial
  `/plan`, meal-log, or confirmed-plan targeted-edit semantics.
- [x] Extend `_invoke_planner` with the validated revision payload and keep
  action-specific fields out of unrelated events.
- [x] Run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 5.

### Task 5: Generate and conditionally publish complete revised drafts

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing event-dispatch tests for valid and malformed revision
  payloads, including mismatched or missing revision context.
- [x] Write failing success tests that consistently load the exact draft,
  profile, and matching state; call the revision prompt; and atomically publish
  revision `expected_plan_revision + 1`.
- [x] Implement the new revision action and refactor only the reusable bounded
  plan-generation/repair logic needed by initial generation and revision.
- [x] Normalize week, status, revision, groceries, outcomes, and accumulated
  instructions from server-owned context before persistence and delivery.
- [x] Write failing tests for provider timeout, permanent failure, invalid output
  after bounded repair, absent or expired draft, confirmed draft, stale state,
  plan/state transaction conflict, and duplicate event.
- [x] Retain retry-ready state only for a matching recoverable request; preserve
  the original draft and emit revision-specific failure messages.
- [x] Write tests proving successful persistence precedes delivery and Telegram
  failure does not roll back or retry the saved revision.
- [x] Run `uv run pytest tests/test_planner_handler.py`; it must pass before
  Task 6.

### Task 6: Cover iterative behavior and protect existing workflows

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_prompts.py`
- Modify: `README.md`

- [x] Add a workflow regression proving an initial preference becomes the first
  plan instruction and one successful amendment is appended only to that plan.
- [x] Add a two-revision regression proving all accumulated instructions reach
  the second planner call and neither revision mutates `UserProfile`.
- [x] Add race coverage proving confirmation or cancellation wins atomically
  against a stale planner worker without modifying the plan.
- [x] Retain focused regressions for initial `/plan`, draft confirmation,
  confirmed-plan targeted edit and grocery refresh, meal logging, and callbacks.
- [x] Update README workflow documentation with revision, retry, confirmation,
  and plan-specific instruction behavior.
- [x] Run the focused workflow suite with `uv run pytest tests/test_bot_handler.py
  tests/test_planner_handler.py tests/test_dynamo.py tests/test_prompts.py`; it
  must pass before Task 7.

### Task 7: Verify acceptance criteria and project quality gates

**Files:**

- Verify: `src/meal_planner/`
- Verify: `tests/`
- Verify: `README.md`
- Verify: `uv.lock`

- [x] Verify the reported natural-language amendment produces a replacement
  seven-day draft and another review/edit/confirm prompt.
- [x] Verify temporary instructions remain on the current plan only and profile
  persistence is untouched.
- [x] Verify stale, duplicate, failed, cancelled, and confirmation-race requests
  cannot overwrite the wrong plan revision.
- [x] Run `uv run ruff check .` and resolve every lint failure.
- [x] Run `uv run ruff format --check .` and resolve every formatting failure
  with `uv run ruff format .` when needed.
- [x] Run `uv run mypy` and resolve every strict typing failure.
- [x] Run `uv lock --check` and confirm the lockfile is synchronized.
- [x] Run `uv run pytest` and confirm the complete suite passes.

### Task 8: Finalize documentation and issue tracking

**Files:**

- Modify if needed: `README.md`
- Modify if new project-wide rules emerge: `AGENTS.md`
- Move: `docs/plans/2026-08-15-ai-powered-draft-plan-revisions.md` to
  `docs/plans/completed/2026-08-15-ai-powered-draft-plan-revisions.md`

- [x] Confirm every implementation and verification checkbox above reflects the
  finished work.
- [x] Record any final architectural deviation or operational caveat here.
- [x] Ensure implementation commits use Conventional Commits and reference the
  associated GitHub issue number.
- [x] Ensure changes are on a dedicated branch and proposed through a pull
  request rather than pushed or merged directly to `master`.
- [x] Comment on the associated GitHub issue with a concise implementation
  summary and a link to the commit or pull request.
- [x] Move this plan to `docs/plans/completed/` only after all gates pass.

Final note: the implementation adds compatibility aliases for revision
context and instruction names so existing persisted state and callers remain
readable while the new workflow uses explicit typed contracts.

## Post-Completion

**Manual verification**

- In a test Telegram chat, generate a draft using the reported breakfast rules,
  request `Avoid cauliflower`, and confirm the bot displays a revised complete
  draft before accepting confirmation.
- Send two amendment messages close together and verify only one revision starts
  while the other receives the in-progress response.
- Ask to confirm during revision and verify the older draft is not confirmed.
- Force one planner timeout, reply `retry`, and verify the original draft remains
  visible until the successful replacement is saved.
- Confirm the final revision and verify grocery generation uses that exact plan.

**External system updates**

- Deploy the Bot and Planner Lambda changes together because the new event
  action and payload contract span both functions.
- Observe CloudWatch logs for revision conflicts, invalid model output, retries,
  and post-persistence Telegram delivery failures.
- Review real provider outputs for instruction adherence without logging private
  household profile or amendment contents unnecessarily.
