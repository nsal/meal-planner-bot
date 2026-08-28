# Simplify Meal Planning to Conversational Draft Generation

Tracking issue: [#74](https://github.com/nsal/meal-planner-bot/issues/74)

## Overview

Replace the current rule-heavy meal-plan system with a small conversational
draft generator. A `/plan` request will combine the household profile, the
previous 21 calendar days of submitted meals, the user's request, and any
temporary follow-up context in one LLM prompt. The returned text will be shown
as a lightly formatted draft without parsing, plan persistence, preference
validation, compliance repair, confirmation, or downstream plan features.

Preserve the user-visible `/submit_meals` workflow. Keep `/start`, `/help`,
`/profile`, `/plan`, and `/submit_meals`. Remove `/today`, `/checkin`,
`/grocery`, and the top-level `/cancel` command. Each retained workflow owns
its exit control: plan chat has an `End planning` button, meal review has its
existing `Cancel` button, and profile editing has its existing `Close`
navigation.

This is a deletion-led refactor. Completion requires removing obsolete code,
models, tests, settings, IAM permissions, deployment outputs, scripts, and
documentation rather than leaving the old implementation dormant.

## Context (from discovery)

- The Python application contains about 17,000 lines, with most complexity in
  `bot_handler.py`, `planner_handler.py`, `db/dynamo.py`, `models/schemas.py`,
  `preferences.py`, and `dietary_rules.py`.
- The test suite contains about 34,000 lines. The two largest files exercise
  bot orchestration and planner persistence rather than the desired draft UX.
- The existing planner already calls an LLM, but surrounds it with natural
  language rule interpretation, obligation projection, evidence matching,
  deterministic validation, asynchronous repair, plan publication, grocery
  state, batch ledgers, and revision concurrency.
- Telegram webhook execution must remain short, so a separate asynchronous
  Lambda is still required for the potentially long LLM call.
- DynamoDB remains the source of profiles, submitted meals, and temporary
  conversation state. Stored plan and batch records become unreachable
  historical data and are not scanned or deleted by the application.
- The current profile format persists interpreted constraint and preference
  objects. The simplified profile retains their original `source_text` as raw
  bounded strings and ignores obsolete batch rules.

## Decisions from Brainstorm

- Use a surgical simplification of the existing application, not a rewrite
  and not a temporary `/plan_v2` workflow.
- Treat every generated menu as a user-reviewed draft. Do not promise that
  calorie targets, dietary restrictions, or preferences were followed.
- Perform no structural or semantic meal-plan validation. Technical transport
  handling for provider errors, empty output, message bounds, and stale work
  is still required so the bot remains operable.
- Do not add RAG, LangChain, a vector database, a food ontology, a rule engine,
  or an LLM reviewer.
- Do not store an official plan. Persist only short-lived context needed to
  support follow-up messages.
- Keep only the latest generated response rather than an unlimited transcript.
- Refresh profile and 21-day meal history context for each follow-up.
- Allow the LLM to ask one focused clarification question instead of producing
  a menu when essential information is missing.
- Preserve `/submit_meals` at the visible behavior boundary while deleting its
  hidden planned-batch matching and batch-role persistence.
- Remove obsolete code completely. Historical completed plan documents remain
  as repository history, but production code and active tests must not import
  or describe the removed system.

## Rejected Alternatives

### Side-by-side `/plan_v2`

This would reduce rollout risk but temporarily increase complexity and create
a material risk that the obsolete planner remains indefinitely. The selected
approach switches `/plan` directly once the new path is covered by tests.

### Full rewrite

This would discard working Telegram authentication, deployment, profile,
meal logging, and DynamoDB behavior. It creates more risk without improving
the core generation model beyond the surgical approach.

### Keep deterministic preference validation

This preserves the exact machinery causing fragility: interpretation,
projection, evidence matching, validation, and repair. It conflicts with the
accepted draft-only product contract.

## Development Approach

- **Testing approach:** TDD. Write or update focused tests before every
  production change. Remove obsolete tests in the same task as their
  production behavior.
- Complete one numbered task and mark its checkboxes immediately before
  beginning the next task.
- Run the focused tests and then the full test suite at every task boundary.
  Do not proceed while either is failing.
- Make deletion part of the definition of done. Compatibility shims are
  allowed only where this plan explicitly requires persisted-profile reading.
- Preserve profile data by accepting legacy strings and mappings containing
  `source_text`. Do not preserve interpreted rules, batch rules, plan records,
  or old conversation state contracts.
- Treat incompatible, short-lived conversation items as expired on read; do
  not maintain the old workflow models for their 24-hour TTL window.
- Keep all Python strictly typed and follow `pyproject.toml`: Python 3.14,
  Ruff at 80 columns, strict Mypy, Pytest, and `uv` for tool execution.
- Update this plan immediately if implementation discoveries change scope or
  architecture.

## Testing Strategy

- Unit-test every new state invariant, prompt section, callback parser,
  repository transition, worker outcome, profile migration, and command path.
- Keep retained `/submit_meals` tests as behavioral characterization tests.
  Update only assertions related to removed batch integration or commands.
- Test stale worker suppression and stale `End planning` buttons; these are
  infrastructure correctness, not meal-plan validation.
- Test provider timeout, provider failure, blank text, conditional-transition
  failure, Telegram delivery failure, and retry-by-new-message behavior.
- Do not assert that generated content follows calories, restrictions,
  preferences, dates, meal counts, JSON schemas, or ingredient rules.
- Update SAM and deployment tests to prove obsolete environment variables,
  outputs, transaction grants, and Lambda self-invocation are absent.
- Use import and reference audits to prove deleted symbols are not retained in
  source, active tests, scripts, template configuration, or current docs.
- Final verification commands:

  ```bash
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy
  ```

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document blockers or deviations with a `⚠️` prefix.
- Keep this file synchronized with actual changes.
- Do not move this file to `docs/plans/completed/` until all implementation,
  verification, documentation, deployment, and issue-comment work is done.

## Solution Overview

The Bot Lambda continues to authenticate and route Telegram updates. `/plan`
creates a short-lived planning-chat state and prompts for a free-form request.
The next message conditionally marks the session as generating and invokes a
dedicated Plan Chat Lambda with only stable identifiers. The worker reloads
the owning state, current profile, and previous 21 days of submitted meals,
builds one prompt, and makes one text LLM request.

The worker conditionally transitions the same state to ready before sending
the raw response. If `/plan` replaced the session or `End planning` deleted it
while the model was running, the conditional transition fails and the result
is not sent. A follow-up repeats the process with the initial request, latest
response, and new user instruction. No generated response is written to a
plan item.

The existing Planner Lambda is renamed to Plan Chat in code, configuration,
SAM resources, deployment outputs, and scripts. It no longer invokes itself.
The Bot Lambda no longer calls an LLM for generic intent classification or
profile interpretation. Profile onboarding and editing become deterministic,
and dietary fields store exactly the user's bounded text.

## Technical Details

### Temporary planning state

The final conversation contract has these workflow kinds:

- `MEAL_LOG`
- `PROFILE_SETUP`
- `PROFILE_EDIT`
- `PLAN_CHAT`

Plan-chat steps are:

- `AWAITING_PLAN_REQUEST`
- `PLAN_CHAT_GENERATING`
- `PLAN_CHAT_READY`

The plan-chat portion of `ConversationState` contains:

- `session_id`: stable UUID string for one `/plan` conversation and its end
  button.
- `request_id`: fresh UUID string for the currently running model call.
- `initial_request`: the first free-form planning request.
- `pending_message`: the initial request or current follow-up being processed.
- `latest_response`: the most recent model response, absent before success.
- `context_date`: UTC date used as the inclusive end of the 21-day history
  window for the current request.
- Existing `revision`, timestamps, expiry, and update-id fields needed for
  conditional ownership and Telegram idempotency.

Bound all text and state size below DynamoDB's item limit. The exact limits
must be centralized constants and tested at their boundaries. Do not add a
plan model, transcript list, validator interface, or no-op validator.

### Plan-chat event

Use one typed event action, `GENERATE_PLAN_CHAT`, containing `user_id`,
`chat_id`, `session_id`, `request_id`, and observed state revision. Do not put
profile content, history, prompts, or generated text in Lambda event payloads
or logs. The worker reloads all context with a strongly consistent state read.

### Prompt contract

`build_plan_chat_prompt()` receives the current `UserProfile`, up to 21 days
of `MealLogEntry` values, the initial request, optional latest response, and
the pending message. It renders clearly delimited sections for:

1. household members and calorie/protein/fibre targets;
2. raw dietary constraints;
3. raw dietary preferences;
4. submitted meal examples grouped by date and meal type;
5. original request, previous response, and current follow-up when applicable.

Tell the model that history is preference evidence, not an obligation. Give
explicit constraints and the current request higher importance. Request plain
Telegram-friendly headings and bullets, short meal details, and calorie
estimates where relevant. Prohibit Markdown tables. Permit one focused
clarifying question. Label output as a draft and avoid medical or nutritional
certainty.

### Worker outcomes

- Success: conditionally replace `pending_message` with `latest_response`, set
  the state to ready, and send the unchanged response with `End planning` on
  the final Telegram chunk.
- Stale state: log bounded operational metadata and send nothing.
- Provider timeout/failure or blank response: conditionally return the session
  to ready (or awaiting request before its first success), retain useful
  context, and send a concise retry message with `End planning`.
- Telegram delivery failure: log bounded operational metadata. Do not invent
  a persisted plan or retry delivery through a plan lifecycle.
- Concurrent follow-up while generating: do not enqueue it; reply that the bot
  is still working.

### Cancellation controls

Remove `/cancel` from `BOT_COMMANDS` and `BotHandler.handle_command`. Add a
validated callback such as `plan_chat:end:<session_id>`. It deletes state only
when the active workflow and session ID match, so a button from a replaced
session cannot end the new one. The callback is acknowledged on success,
staleness, and failure. Existing meal-review `Cancel` and profile `Close`
controls remain scoped to their workflows.

### Profile representation and onboarding

Change `UserProfile.dietary_constraints` and
`UserProfile.dietary_preferences` to bounded lists of bounded strings. A small
input normalizer accepts existing strings and legacy mappings, extracts only a
non-empty `source_text`, removes supported no-value phrases, and deduplicates
case-insensitively while preserving order. Ignore the removed `batch_rules`
field when loading saved profiles. New writes do not include structured rules
or batch rules.

Because generic LLM intent parsing is removed, new-user onboarding must be a
deterministic guided workflow. `/start` collects family name, household size,
one member and nutrition-target line per person, then constraints and
preferences as bounded user text with an explicit `none` option. Existing
`/profile` menus continue to amend family targets and raw dietary lists without
LLM interpretation or rule-review confirmation.

### Concrete deletion inventory

Delete these production modules entirely:

- `src/meal_planner/dietary_rules.py`
- `src/meal_planner/preferences.py`
- `src/meal_planner/normalization.py`
- `src/meal_planner/llm/parser.py`
- `src/meal_planner/planner_handler.py` after its replacement worker is wired

Delete these obsolete test modules entirely:

- `tests/test_dietary_rules.py`
- `tests/test_preferences.py`
- `tests/test_parser.py`
- `tests/test_planner_handler.py`
- `tests/test_reset_profile_dietary_fields.py`

Delete the obsolete one-time repair script:

- `scripts/reset_profile_dietary_fields.py`

Remove these model families and compatibility aliases from
`models/schemas.py` and `models/__init__.py`:

- plan types: `PlanStatus`, `GroceryStatus`, `PlanDays`, `PlanInstruction`,
  `PlanningInstruction`, `PlanGenerationContext`, `PlanRevisionContext`,
  `RevisionEventContext`, `Ingredient`, `PlannedMeal`, `PlanDay`,
  `GrocerySection`, and `WeeklyPlan`;
- rule types: `DietaryRule`, `ConstraintEntry`, `DietaryPreferenceEntry`,
  `DietaryObligation`, `PreferenceRequirement`, `RuleOperator`,
  `RuleStrength`, `RuleCadence`, `ScheduleKind`, `Weekday`, and their aliases;
- batch types: `BatchRule`, `PlannedBatchLink`, `SubmittedMealBatchLink`,
  `BatchLedgerEntry`, `WeeklyBatchLedger`, batch enums, and all batch aliases;
- conversational LLM types: `ConversationIntent`, `LLMResponseMetadata`, and
  the old `ProfileUpdateEntities` intent envelope;
- unused outcome and legacy aliases after reachability verification, including
  `MealOutcome`, `WorkflowKind`, `WorkflowStep`, and `PartialMealLog` when no
  retained caller requires them;
- plan/rule constants, hash-based application-owned ID helpers, food
  normalization validators, and saved-rule canonicalizers.

Remove these `BotHandler` paths and their helpers:

- `_cmd_grocery`, `_cmd_today`, `_cmd_checkin`, and `_cmd_cancel`;
- check-in callback handling and `_get_todays_plan_day`;
- `_handle_plan_preference`, `_parse_initial_plan_response`,
  `_plan_progress_message`, `_collect_stored_preference_rules`,
  `_collect_stored_batch_rules`, `_snapshot_effective_rules`, and
  `_retry_plan_request`;
- `_confirm_plan`, `_edit_plan`, `_handle_plan_revision_state`,
  `_start_plan_revision`, `_retry_plan_revision`, `_is_eligible_draft`, and
  `_is_active_confirmed_plan`;
- generic conversational LLM dispatch, `_apply_intent_metadata`, and the old
  structured `_update_profile` intent path;
- preference interpretation, pending-rule encoding/decoding, confirmation,
  and priority-resolution branches;
- old planner action dispatch constants and payload construction.

Replace the retained `_invoke_planner` concept with a narrowly named
`_invoke_plan_chat` that sends only the typed identifiers above.

Remove these DynamoDB repository methods and their private helpers after all
callers are gone:

- retry/revision: `mark_conversation_retry_ready`, `start_plan_revision`, and
  `has_plan_revision_update_marker`;
- extra history projections used only by validation:
  `get_submitted_meals`, `get_meal_history_between`, and
  `get_meal_history_for_range`;
- plans: `save_plan`, `save_generated_draft`,
  `save_generated_draft_and_clear_conversation_state`,
  `save_repaired_draft_once`, `replace_draft_and_clear_revision_state`,
  `confirm_plan`, `get_plan`, `get_latest_plan`, `get_active_plan`,
  `get_active_plan_snapshot`, `update_meal`, and `update_meal_outcome`;
- grocery: `retry_grocery`, `complete_grocery`, `fail_grocery`, and
  `_update_grocery_state`;
- batch: `_batch_ledger_key`, `_iso_week_bounds`,
  `get_weekly_batch_ledger`, `_materialize_weekly_batch_expiry`,
  `put_weekly_batch_ledger`, `save_weekly_batch_ledger`,
  `_put_weekly_batch_ledger_conditionally`,
  `_batch_ledger_transaction_items`, `get_available_batch_portions`,
  `_batch_submission_ledger_item`, `_repair_marker_key`,
  `_repair_publication_outcome`, and `get_planned_batch_link`;
- transaction-conflict helpers that become unreachable after these methods are
  deleted, as confirmed by reference search and tests.

Retain and simplify `confirm_meal_and_transition` so it atomically saves an
ordinary `MealLogEntry` and advances the meal workflow without batch-ledger
writes.

Remove these Telegram and router surfaces:

- commands `grocery`, `today`, `checkin`, and `cancel`;
- `CheckinCallback`, `parse_checkin_callback`, and check-in callback data;
- batch-role data from `MealCallback`, `parse_meal_callback`,
  `meal_review_keyboard`, and `send_meal_review`;
- `send_plan`, `send_grocery_list`, `send_meal_checkin`, and
  `send_profile_rule_review`;
- batch-rule profile labels and removal entries.

### Infrastructure cleanup

Rename the worker module and deployment concepts to plan chat:

- create `src/meal_planner/plan_chat_handler.py` and point SAM to
  `meal_planner.plan_chat_handler.lambda_handler`;
- rename `PlannerFunction` to `PlanChatFunction`, its function-name output,
  Bot invocation environment variable, deployment output field, and related
  script variables;
- replace `PLANNER_*` and `CONVERSATIONAL_*` model/timeout settings with only
  the `PLAN_CHAT_*` settings actually consumed by the worker;
- expose `LLM_API_KEY` only to the Plan Chat function and keep webhook secret
  and allowlist only on the Bot function;
- remove grocery retry settings, Bot LLM settings, Planner self-invocation
  permission, and Planner `TransactWriteItems` permission;
- update the transaction-permission verifier to check only the Bot role if its
  retained profile and meal transactions still require the explicit grant;
- update deployment orchestration, template tests, `.env` examples, stack
  output resolution, and operational documentation to the new names.

## What Goes Where

- **Implementation Steps:** repository code, tests, SAM configuration,
  deployment scripts, compatibility loading, active documentation, deletion
  audits, and plan tracking.
- **Post-Completion:** deploy through the existing protected-branch and PR
  workflow, refresh Telegram commands, test against the real provider, inspect
  CloudWatch, and comment on the tracking GitHub issue with the commit or PR.

## Implementation Steps

### Task 1: Add the temporary plan-chat state contract

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`

- [x] write failing tests for awaiting-request, generating, and ready plan-chat
  states, including required and forbidden fields for each step
- [x] write failing tests for bounded initial requests, pending messages,
  latest responses, UUID identifiers, timestamps, expiry, and state revision
- [x] add the new workflow steps and temporary plan-chat fields alongside the
  legacy types so unrelated behavior remains green during the transition
- [x] add a bounded typed `GENERATE_PLAN_CHAT` event contract containing only
  identifiers and the observed revision
- [x] update factories for valid initial, generating, and ready states
- [x] write error tests for cross-workflow fields, stale timestamps, malformed
  identifiers, oversized text, and invalid event payloads
- [x] run `uv run pytest tests/test_schemas.py`; then run `uv run pytest`; both
  must pass before Task 2

### Task 2: Build the plain-text plan-chat prompt

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_prompts.py`

- [ ] write failing prompt tests for every household member and available
  calorie, protein, and fibre target
- [ ] write failing tests for raw constraints, raw preferences, and submitted
  meals grouped over the inclusive 21-day window
- [ ] write failing follow-up tests for original request, latest response, and
  current instruction, including the no-previous-response case
- [ ] implement `build_plan_chat_prompt()` and only the small rendering helpers
  it needs, without a JSON schema, rule rendering, or validation language
- [ ] instruct the model about draft status, history semantics, clarification,
  plain headings and bullets, calorie estimates, and no Markdown tables
- [ ] test empty dietary lists, no history, optional nutrition targets,
  Unicode text, boundary lengths, and delimiter-safe rendering
- [ ] run `uv run pytest tests/test_prompts.py`; then run `uv run pytest`; both
  must pass before Task 3

### Task 3: Add session-scoped Telegram controls

**Files:**

- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_api.py`

- [ ] write failing tests for `plan_chat:end:<session_id>` parsing, Telegram's
  64-byte callback limit, canonical UUID spelling, and malformed callbacks
- [ ] add a typed plan-chat callback and a parser that accepts only the end
  action and one session ID
- [ ] add an `End planning` inline keyboard and ensure `send_message()` attaches
  it only to the final chunk of a split response
- [ ] add a small plan-chat send helper only if it removes repeated keyboard
  assembly without coupling message transport to state logic
- [ ] write success tests for initial prompts, generated responses, error
  messages, and split output with the button on the last chunk
- [ ] write error tests for stale-looking data, oversized callback data, and
  Telegram API failures
- [ ] run `uv run pytest tests/test_router.py tests/test_telegram_api.py`; then
  run `uv run pytest`; both must pass before Task 4

### Task 4: Implement the asynchronous Plan Chat worker

**Files:**

- Create: `src/meal_planner/plan_chat_handler.py`
- Create: `tests/test_plan_chat_handler.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `tests/test_llm_client.py`

- [ ] write failing worker tests for a valid event, consistent state load,
  current profile load, and exactly 21 days of meal-history retrieval
- [ ] write failing tests proving the worker makes one text request, does not
  request JSON, and sends the response unchanged with `End planning`
- [ ] implement the typed event entry point, ownership recheck, prompt build,
  one provider request, conditional ready transition, and Telegram delivery
- [ ] configure the client for one application-level attempt and retain only
  bounded technical failure classification needed for user retry messages
- [ ] write stale-result tests for missing, replaced, cancelled, wrong-step,
  wrong-request, and wrong-revision states; none may send model output
- [ ] write provider timeout, transient, permanent, blank-output, persistence,
  and Telegram delivery failure tests without inspecting generated semantics
- [ ] test first-request failure versus follow-up failure state restoration and
  verify prompts, profile content, meals, IDs, and generated text are not logged
- [ ] run `uv run pytest tests/test_plan_chat_handler.py
  tests/test_llm_client.py`; then run `uv run pytest`; both must pass before
  Task 5

### Task 5: Switch `/plan` to temporary conversational drafts

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_telegram_commands.py`

- [ ] write failing tests for `/plan` creating an awaiting-request session and
  replacing any older workflow with a new session ID
- [ ] write failing tests for an initial request transitioning to generating,
  creating a fresh request ID, and invoking Plan Chat with identifiers only
- [ ] write failing tests for ready-state follow-ups, refreshed context dates,
  retained initial request/latest response, and no follow-up queue while busy
- [ ] route `/plan` and plan-chat conversational messages through the new state
  path and `_invoke_plan_chat`, bypassing preference interpretation entirely
- [ ] handle invocation failure by conditionally restoring a usable state and
  returning a bounded technical retry message
- [ ] write idempotency, duplicate-update, concurrent-transition, expiry,
  malformed-state, and invocation-error tests
- [ ] update `/start` and `/help` text to describe conversational drafts without
  confirmation, tracking, groceries, check-ins, or validation guarantees
- [ ] run `uv run pytest tests/test_bot_handler.py
  tests/test_telegram_commands.py`; then run `uv run pytest`; both must pass
  before Task 6

### Task 6: Replace `/cancel` with scoped workflow buttons

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_telegram_commands.py`
- Modify: `tests/test_router.py`

- [ ] write failing tests for ending awaiting, generating, and ready plan-chat
  sessions through an inline callback
- [ ] conditionally delete only a matching `PLAN_CHAT` session ID and
  acknowledge successful, stale, already-ended, and persistence-failure paths
- [ ] prove a button from a replaced session cannot end the current session or
  suppress its valid worker
- [ ] remove `cancel` from `BOT_COMMANDS`, `SUPPORTED_COMMANDS`, command
  dispatch, help output, and `_cmd_cancel`
- [ ] retain meal-review `Cancel` and profile `Close` behavior with regression
  tests proving they remain scoped to their own state
- [ ] write tests proving `/cancel` is now an unknown command and plan prompts,
  responses, and failure messages carry the end button
- [ ] run `uv run pytest tests/test_bot_handler.py
  tests/test_telegram_commands.py tests/test_router.py`; then run
  `uv run pytest`; both must pass before Task 7

### Task 7: Remove dropped commands and old bot planning paths

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_telegram_commands.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_api.py`

- [ ] write target-surface tests proving only `start`, `help`, `profile`,
  `plan`, and `submit_meals` are registered commands
- [ ] remove `_cmd_grocery`, `_cmd_today`, `_cmd_checkin`, check-in callback
  dispatch, `_get_todays_plan_day`, and their command mappings
- [ ] remove old preference collection, interpretation, obligation snapshot,
  retry-ready, confirmation, edit, revision, and old planner invocation helpers
  listed in the concrete deletion inventory
- [ ] remove generic conversational plan intents so ordinary text is handled
  only by an active setup, profile, meal-log, or plan-chat workflow
- [ ] remove `CheckinCallback`, `parse_checkin_callback`, `send_plan`,
  `send_grocery_list`, and `send_meal_checkin`
- [ ] delete obsolete bot/router/API test cases and add negative route/callback
  tests for every removed command and check-in payload
- [ ] run `uv run pytest tests/test_bot_handler.py
  tests/test_telegram_commands.py tests/test_router.py
  tests/test_telegram_api.py`; then run `uv run pytest`; both must pass before
  Task 8

### Task 8: Store raw profile constraints and preferences

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_telegram_api.py`

- [ ] write failing migration tests for saved raw strings, legacy constraint
  mappings, legacy preference mappings, missing/null `source_text`, no-value
  phrases, duplicates, oversize values, and ignored `batch_rules`
- [ ] change `UserProfile` and its draft type to bounded raw-text dietary lists
  with one small legacy read normalizer and no interpreted-rule models
- [ ] simplify profile repository canonicalization and guarded updates to
  preserve revision concurrency without dietary conflict resolution
- [ ] remove pending-rule encoding, LLM interpretation, confirmation callbacks,
  rule review messages, batch-rule profile presentation, and related helpers
- [ ] make `/profile` add and remove raw constraints/preferences directly while
  retaining indexed removal revision guards and existing family-target edits
- [ ] write success tests for raw add/remove/display, legacy read followed by
  simplified write, concurrent edit rejection, and empty categories
- [ ] write error tests for malformed saved data, invalid list entries, stale
  buttons, duplicate input, and persistence failures
- [ ] run `uv run pytest tests/test_schemas.py tests/test_dynamo.py
  tests/test_bot_handler.py tests/test_telegram_api.py`; then run
  `uv run pytest`; both must pass before Task 9

### Task 9: Replace LLM onboarding with deterministic profile setup

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_telegram_api.py`

- [ ] write failing tests for the new-user sequence: family name, household
  size, one member/target line per person, constraints, preferences, and save
- [ ] add minimal typed setup steps and reuse the profile draft item rather
  than adding another persistence aggregate
- [ ] implement deterministic parsing for member lines and newline-separated
  dietary text, including explicit `none`, bounds, and retry prompts
- [ ] make `/start` begin or resume setup for an incomplete profile and retain
  the welcome/menu behavior for complete profiles
- [ ] remove generic conversational LLM onboarding, intent metadata,
  conversational profile updates, and all Bot LLM calls
- [ ] write tests for optional protein/fibre targets, multiple members,
  duplicate names, invalid counts, malformed targets, cancellation through the
  profile control, stale state, persistence failure, and restart behavior
- [ ] run `uv run pytest tests/test_schemas.py tests/test_bot_handler.py
  tests/test_dynamo.py tests/test_telegram_api.py`; then run `uv run pytest`;
  both must pass before Task 10

### Task 10: Remove batch coupling from `/submit_meals`

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_telegram_api.py`

- [ ] preserve characterization tests for `/submit_meals` history display,
  structured input, review, confirm, cancel, add-more, done, duplicate update,
  stale callback, and conditional persistence behavior
- [ ] remove planned-batch lookup from `_handle_structured_meal_input` and
  batch-role validation/conversion from `_confirm_meal_callback`
- [ ] remove pending/submitted batch fields from meal state and log entries,
  batch suffixes from `MealCallback`, and batch arguments from review UI
- [ ] simplify `confirm_meal_and_transition` to write only the meal item and
  next conversation state atomically, preserving duplicate-meal semantics
- [ ] delete batch-specific meal tests and add negative tests proving old
  four-part batch callbacks are rejected without changing ordinary callbacks
- [ ] verify user-visible meal prompts and success/error messages remain
  unchanged except for text that explicitly described planned batch roles
- [ ] run `uv run pytest tests/test_schemas.py tests/test_router.py
  tests/test_bot_handler.py tests/test_dynamo.py tests/test_telegram_api.py`;
  then run `uv run pytest`; both must pass before Task 11

### Task 11: Delete plan, grocery, batch, and repair persistence

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/factories.py`

- [ ] add retained repository coverage for profile CRUD, profile drafts,
  conversation save/transition/delete, ordinary meal confirmation, and exact
  21-day history retrieval before deleting other tests
- [ ] delete every repository method and helper named in the concrete deletion
  inventory after confirming there are no retained production callers
- [ ] delete plan, grocery, batch, obligation, repair, active-plan, outcome, and
  revision repository tests and fixtures with their implementations
- [ ] remove now-unused imports, exception classifiers, conditional-expression
  fragments, constants, aliases, and transaction helpers proven unreachable
- [ ] add negative source/reference assertions only where they guard an
  architectural boundary rather than duplicating implementation details
- [ ] test history boundaries, ordering, empty history, duplicate meal
  submission, stale state, expired state, profile revision, and Dynamo failures
- [ ] run `uv run pytest tests/test_dynamo.py`; then run `uv run pytest`; both
  must pass before Task 12

### Task 12: Delete the legacy generation and validation stack

**Files:**

- Delete: `src/meal_planner/planner_handler.py`
- Delete: `src/meal_planner/dietary_rules.py`
- Delete: `src/meal_planner/preferences.py`
- Delete: `src/meal_planner/normalization.py`
- Delete: `src/meal_planner/llm/parser.py`
- Delete: `scripts/reset_profile_dietary_fields.py`
- Delete: `tests/test_planner_handler.py`
- Delete: `tests/test_dietary_rules.py`
- Delete: `tests/test_preferences.py`
- Delete: `tests/test_parser.py`
- Delete: `tests/test_reset_profile_dietary_fields.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `src/meal_planner/llm/__init__.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/factories.py`

- [ ] write retained-contract tests that enumerate the small public model and
  LLM surface needed by profile, meal log, plan chat, and Telegram routing
- [ ] delete all production and test modules listed above rather than leaving
  forwarding imports, deprecated aliases, skipped tests, or dead functions
- [ ] remove every obsolete model family, alias, constant, normalizer, and
  application-owned rule ID helper named in the deletion inventory
- [ ] reduce `llm/prompts.py` to the plan-chat prompt and its directly used
  rendering helpers; reduce `llm/__init__.py` to live client exports only
- [ ] prune `models/__init__.py`, factories, schema tests, and prompt tests to
  imports and behavior used by the retained application
- [ ] add import-smoke tests and run `rg` audits proving active source and tests
  contain no imports of deleted modules or deleted symbol families
- [ ] run `uv run pytest tests/test_schemas.py tests/test_prompts.py
  tests/test_llm_client.py tests/test_plan_chat_handler.py`; then run
  `uv run pytest`; both must pass before Task 13

### Task 13: Simplify configuration, SAM, and deployment tooling

**Files:**

- Modify: `src/meal_planner/config.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `template.yaml`
- Modify: `scripts/deploy.py`
- Modify: `scripts/verify_transaction_permission.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_template.py`
- Modify: `tests/test_deploy.py`
- Modify: `tests/test_verify_transaction_permission.py`

- [ ] write failing settings tests for the minimal Bot configuration and the
  separate Plan Chat model, timeout, retry, Telegram, LLM, and Dynamo settings
- [ ] rename the worker module/resource/output/function environment variables
  and deployment data structures from Planner to Plan Chat
- [ ] remove conversational Bot model settings, grocery settings, repair/self-
  invocation settings, unused budget branches, and legacy property aliases
- [ ] scope secrets and IAM: LLM key only on Plan Chat, webhook secret and
  allowlist only on Bot, no worker self-invocation, and no worker transaction
  permission
- [ ] update deployment settings, SAM parameter overrides, stack output
  resolution, command registration, and safe diagnostic tests for renamed data
- [ ] simplify the transaction-permission verifier to the retained Bot
  transaction requirement or delete it if SAM policy tests fully replace its
  external purpose; record the evidence for the selected branch in this plan
- [ ] test invalid budgets, missing secrets, removed environment variables,
  least-privilege resources, stack outputs, build artifacts, and deployment
  command construction
- [ ] run `uv run pytest tests/test_config.py tests/test_llm_client.py
  tests/test_template.py tests/test_deploy.py
  tests/test_verify_transaction_permission.py`; then run `uv run pytest`; both
  must pass before Task 14

### Task 14: Audit the deletion boundary and verify acceptance criteria

**Files:**

- Modify: `tests/test_readme.py`
- Modify:
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`

- [ ] write or update active-documentation boundary tests for the retained
  command set, 21-day history, draft disclaimer, and removed feature names
- [ ] verify `/plan` uses current raw profile data, exactly the previous 21
  calendar days of submitted meals, the initial request, and latest follow-up
  context in one plain-text generation request
- [ ] verify generated output is displayed unchanged with light Telegram
  formatting controls and is never parsed, validated, repaired, confirmed, or
  stored as a plan
- [ ] verify `/submit_meals` visible behavior remains intact and the registered
  command set is exactly `start`, `help`, `profile`, `plan`, and
  `submit_meals`
- [ ] verify `End planning`, meal `Cancel`, and profile `Close` are scoped and
  stale-safe; verify no top-level `/cancel` remains
- [ ] use `rg`, import tests, file counts, and symbol inventories to confirm all
  listed modules, functions, methods, models, tests, scripts, settings,
  permissions, outputs, and docs references are removed or rewritten
- [ ] run `uv run pytest` and fix every failure
- [ ] run `uv run ruff check .` and `uv run ruff format --check .` and fix
  every issue
- [ ] run `uv run mypy` and fix every issue
- [ ] record final source/test line counts and the deletion audit result in this
  plan before Task 15

### Task 15: Finalize active documentation and completion records

**Files:**

- Modify: `README.md`
- Modify: `docs/prompt.md`
- Modify: `.env.example` if present
- Modify:
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`
- Move:
  `docs/plans/2026-08-28-simplify-meal-planning-to-conversational-drafts.md`
  to `docs/plans/completed/`

- [ ] rewrite README architecture, commands, workflows, configuration,
  deployment outputs, failure behavior, privacy notes, and development guidance
  around temporary conversational drafts
- [ ] rewrite `docs/prompt.md` to document the five prompt sections, 21-day
  history semantics, follow-up context, clarification behavior, and lack of
  validation guarantees
- [ ] remove active documentation for plans, confirmation, revision, grocery,
  today, check-in, outcomes, batch leftovers, interpretation, validation,
  repair, and obsolete settings without rewriting historical completed plans
- [ ] update environment examples and operator commands to the Plan Chat names
  and remove unused variables
- [ ] update every checkbox and any recorded deviations, rerun
  `uv run pytest tests/test_readme.py`, then rerun the complete verification
  commands from Task 14
- [ ] move this fully completed plan to `docs/plans/completed/`

## Post-Completion

### Manual verification

- Deploy through a feature branch and pull request; never push or merge
  directly to `master`.
- Refresh the Telegram command menu and verify the four removed commands no
  longer appear.
- With a real complete profile and at least 21 days of mixed meal history,
  request a draft and inspect whether the prompt produces useful output.
- Exercise an LLM clarification, a follow-up edit, concurrent follow-up while
  generating, and `End planning` during a slow generation.
- Confirm `/submit_meals` review, cancel, add-more, done, and duplicate-update
  behavior in Telegram.
- Inspect CloudWatch logs and confirm they contain bounded categories and IDs,
  not profiles, meal history, prompts, user requests, or generated drafts.

### External system updates

- Commit on a dedicated branch with a Conventional Commit including the
  tracking issue number, open a pull request, and add the required issue
  comment linking the commit or PR.
- Confirm the deployment replaces the old Planner resource/output with Plan
  Chat without pointing the Bot at the obsolete function.
- Decide separately whether to delete unreachable historical plan and batch
  DynamoDB items. This implementation intentionally performs no scan or data
  deletion.
- After completion, comment on the tracking GitHub issue with the commit or PR
  link and a concise summary, as required by the repository instructions.
