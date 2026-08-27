# Calendar-Aware Dietary Scheduling and Batch Leftovers

## Overview

- Replace the fragile `/plan`-time reinterpretation of legacy dietary text
  with confirmed, typed profile rules created only through `/profile`.
- Treat persistent food frequencies as ISO-week quotas. Project only the
  portion due inside the requested 1-7 day horizon after counting actual
  submitted meals from earlier in the week.
- Assign unspecified weekly occurrences to stable, Monday-anchored,
  evenly-spaced target days. Preserve user-specified weekdays exactly.
- Add an enforceable batch-cooking rule and a durable leftover ledger so one
  preparation can cover two or three lunch/dinner meals across separate plan
  requests.
- Fix the reported regression in which `1, no preference` blocks on an old
  saved profile with: `I couldn't safely interpret a saved dietary
  preference. I could not parse the preference interpretation.`
- Perform an explicitly authorized development reset that clears only the
  single profile's dietary preferences and constraints. Preserve household
  details, nutrition targets, plans, conversation state, and all meal history.

## Context (from discovery)

- **Tracking issue:** https://github.com/nsal/meal-planner-bot/issues/69
- **Files/components involved:**
  `src/meal_planner/models/schemas.py`,
  `src/meal_planner/llm/prompts.py`,
  `src/meal_planner/llm/parser.py`,
  `src/meal_planner/dietary_rules.py`,
  `src/meal_planner/preferences.py`,
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`,
  `src/meal_planner/db/dynamo.py`,
  `src/meal_planner/telegram/api.py`, `scripts/`, and their tests.
- **Regression source:** `BotHandler._prepare_stored_preference_rules()`
  loops over `DietaryPreferenceEntry(rule=None)` values during `/plan` and
  calls the plain-text `LLMClient.chat_sync()` path. Provider fallback text is
  then handed to `parse_preference_interpretation()` and fails JSON parsing.
- **Why the 2026-08-26 patch is insufficient:** commit `a41cc85` repairs a
  well-formed provider rule that omits `count`; it does not make the provider
  response structured and cannot represent cooking-effort or leftover reuse.
- **Existing patterns to retain:** Pydantic boundary validation, conditional
  profile revisions, durable conversation snapshots, source-update
  idempotency, DynamoDB transactions, normalized whole-word meal evidence,
  and bounded planner repair feedback.
- **Dependencies:** submitted meal history is already queryable by date;
  generated plans already have stable dates and meal types; one DynamoDB table
  stores profile, plan, conversation, and meal items under the user partition.
- **Selected approach:** typed weekly rule ledger plus durable batch inventory.
  Rejected prompt-only enforcement and description-based batch inference
  because neither is deterministic or safely verifiable.
- **Authorized compatibility boundary:** do not migrate historic dietary
  fields. Clear and recreate them. Retain backward compatibility for all
  non-dietary profile data and existing meal-history records.

## Development Approach

- **Testing approach:** TDD. Write the failing success, rejection, boundary,
  and race tests before each production change.
- Complete each task fully before moving to the next.
- Make small, focused changes and keep application-owned decisions outside
  prompts.
- Every task that changes code must add or update tests for every changed path.
- All scoped tests for a task must pass before starting the following task.
- Update this plan immediately if implementation discovers a scope change.
- Preserve current behavior unless this plan explicitly replaces it. The
  intentional exceptions are removal of legacy raw dietary entries and the
  one-time dietary-field reset.
- Use `uv run` for all Python commands, Ruff for formatting/linting, and strict
  mypy before completion.

## Testing Strategy

- **Schema tests:** validate weekly rules, explicit/generated scheduling,
  dated obligations, batch metadata, ledger limits, and malformed payloads.
- **Parser/prompt tests:** require strict JSON at profile entry, represent all
  clauses or clarify, recognize frequency versus batch rules, and reject
  unsupported mixed output without a partial write.
- **Scheduling unit tests:** cover every weekday, 1-7 day horizons, ISO-week
  crossings, submitted evidence, explicit weekdays, overdue carry-forward,
  stable spacing, upper bounds, and capacity caps.
- **Repository tests:** use Moto to verify exact item shapes, conditional
  writes, atomic meal-plus-ledger mutation, duplicate Telegram updates,
  provisional replacement, and expiry.
- **Handler tests:** prove `/profile` is the sole interpretation boundary,
  `/plan` makes no preference-interpretation call, retries reuse snapshots,
  and fresh plans recalculate from new submissions.
- **Planner tests:** validate scheduled obligations, constraints, batch source
  and reuse links, bounded repair, terminal failure, stale events, and draft
  publication ownership.
- **End-to-end regression:** on Wednesday, recreate the agreed profile and send
  `1, no preference`; previously submitted breakfasts affect eggs, a
  Saturday pancake is absent, mushrooms remain forbidden, and planning starts.
- **No browser E2E suite exists:** Telegram/API integration tests are the
  repository's end-to-end boundary.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document issues or blockers with a `⚠️` prefix.
- Record commands and relevant outcomes under the task that ran them.
- Keep the plan synchronized with the actual implementation and move it to
  `docs/plans/completed/` only after every required gate passes.

## Solution Overview

Profile creation will interpret dietary input once via strict structured JSON.
Confirmed food rules are stored as weekly rules; confirmed batch wording is
stored as a separate batch-reuse rule. Unqualified persistent positive food
preferences default to at least once per week. Explicit weekly counts and
weekday scopes are preserved. Rules without weekdays receive stable,
Monday-anchored, evenly-spaced target days during confirmation.

Before each fresh `/plan`, a deterministic scheduler splits the requested date
range by ISO week. It loads only submitted meal evidence before each segment,
computes the quota due through the segment, carries missed generated target
slots forward within that week, caps the obligation by eligible plan slots,
and emits dated horizon obligations. Explicit weekday scopes never move to a
different weekday. Matching submitted meals count as actual evidence; draft or
confirmed plan meals do not. A retry reuses its existing obligation snapshot.

Batch reuse is stored separately from food-frequency obligations. Generated
meals carry application-owned batch metadata identifying a preparation or
leftover use. Draft publication writes provisional reservations tied to the
owning request and plan revision. Submitting the preparation meal activates
the remaining portions. Submitting a linked leftover consumes one portion.
Unsubmitted provisional sources expire after their preparation date; all
remaining portions expire at the ISO-week boundary.

The planner receives only typed, horizon-specific obligations, constraints,
and batch availability. Application validation—not the model—decides whether
the resulting draft complies. No `/plan` path reinterprets profile text.

## Technical Details

### Rule and obligation contracts

- Keep request-specific current-plan preferences scoped to the requested plan.
- Add an explicit period/cadence to stored food rules so persistent counts mean
  per ISO week rather than per arbitrary plan request.
- Distinguish user-specified weekday scopes from application-generated target
  weekdays. This prevents an explicit Saturday rule from being carried onto
  Wednesday while allowing a missed generated Monday target to be caught up.
- Represent a projected obligation with exact ISO-week and horizon bounds,
  source rule ownership, eligible dates, operator/count, foods, meal type, and
  strength. Do not overload a bare weekday list across a cross-week plan.
- Keep constraints as independent, unscaled `ConstraintEntry` values.
- Store batch reuse as its own typed rule with eligible preparation/reuse meal
  types and a bounded total yield of two or three meals.

### Calendar-aware quota projection

For each ISO-week segment:

1. Query submitted meals from Monday through the day before the segment.
2. Count distinct matching submitted meals using normalized meal descriptions,
   food alternatives, meal type, and explicit weekday scope where applicable.
3. Count generated target slots due through the segment end. For a generated
   Monday/Wednesday/Friday rule, a Wednesday segment has two due slots.
4. Subtract submitted evidence from the due amount.
5. Place overdue generated obligations into the earliest eligible open dates
   in the segment, followed by target dates inside the segment.
6. Cap positive obligations at the segment's meal-slot capacity. Leave excess
   due for a later fresh `/plan` rather than rejecting a short horizon.
7. For `at_most`, subtract prior matching submissions from the weekly allowance
   and prohibit additional matches when the allowance reaches zero.
8. Produce independent obligations for every ISO week crossed by the plan.

### Batch ledger and submission linkage

- Store a bounded weekly ledger item in the user's DynamoDB partition. Each
  entry contains batch ID, source plan/request/revision, preparation date and
  meal type, food/meal identity, total portions, remaining portions, state,
  and week-end expiry.
- Generated `PlannedMeal` values may contain batch metadata with role
  `preparation` or `leftover`. The source creates 1 consumed plus 1-2 remaining
  portions; a leftover references the same batch ID.
- A replaced draft removes only provisional entries owned by that draft.
  Available portions are actual submitted history and are never rolled back by
  plan replacement.
- Extend the reviewed meal-submission flow with one bounded confirmation when
  date and meal type match a planned batch meal. Persist `batch_id` and role on
  `MealLogEntry` only after confirmation.
- Write the meal item, conversation-state transition, source-update marker,
  and conditional ledger mutation in one DynamoDB transaction.

### Development data reset

- Add a narrowly scoped, idempotent reset command requiring the exact user ID,
  table, AWS profile, and region. It updates only `dietary_preferences` and
  `dietary_constraints` to empty lists and advances the profile revision.
- Refuse broad targets, scans, missing profile keys, or malformed profiles.
- Verify with tests that every other profile attribute and all separate meal,
  plan, and conversation items remain byte-for-byte unchanged.
- Run the reset once after deploying the compatible code. It is not a recurring
  deployment stage and is not a semantic migration.

## What Goes Where

- **Implementation Steps:** repository code, tests, reset utility, docs, and
  local verification are tracked with checkboxes below.
- **Post-Completion:** the one-time targeted reset, live deployment, and manual
  Telegram verification require external credentials and are listed without
  checkboxes.

## Implementation Steps

### Task 1: Define typed weekly, obligation, batch, and meal-link models

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`

- [x] Write failing model tests for weekly cadence, explicit versus generated
  schedules, exact-date obligations, valid batch rules, planned batch links,
  weekly ledgers, and optional submitted-meal batch links.
- [x] Write failing rejection tests for duplicate/invalid dates, counts beyond
  weekly capacity, invalid 2-3 meal yields, inconsistent preparation/leftover
  roles, cross-week ledger entries, and oversized ledgers.
- [x] Add the bounded enums and Pydantic models, preserving compatibility for
  non-dietary `WeeklyPlan` and `MealLogEntry` records through optional defaults.
- [x] Remove `DietaryPreferenceEntry.rule=None` from the canonical profile
  contract; raw dietary strings must no longer validate as saved preferences.
- [x] Update factories and exports without weakening strict validation.
- [x] Run `uv run pytest tests/test_schemas.py`; it must pass before Task 2.

Task 1 verification: `uv run pytest tests/test_schemas.py` passed (258 tests);
`uv run ruff format --check` and `uv run ruff check` passed for all assigned
files; `uv run mypy` passed. The full-suite failures are downstream legacy
tests and stale build artifacts that belong to later task migrations.

### Task 2: Interpret profile rules once with strict structured output

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_dietary_rules.py`

- [x] Write failing prompt/parser tests for weekly egg, explicit Saturday
  pancake/crepe alternatives, weekly fish dinner, and one 2-3-meal
  lunch/dinner batch rule.
- [x] Write failing tests proving mixed frequency/batch clauses are all emitted
  or produce one focused clarification, never a partial accepted result.
- [x] Write failing tests for deterministic Monday-anchored spacing, including
  counts 1-7, explicit weekday preservation, and meal-scope capacity errors.
- [x] Update the structured contract so persistent profile interpretation
  returns typed food and batch rules, while constraint mode remains separate.
- [x] Use application code—not provider output—to assign generated target days
  and stable canonical IDs after successful parsing.
- [x] Retain privacy-safe bounded error codes and reject fallback prose, empty
  responses, schema mismatches, or unsupported subjective clauses.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_parser.py
  tests/test_dietary_rules.py`; it must pass before Task 3.

Task 2 verification: `uv run pytest tests/test_prompts.py tests/test_parser.py
tests/test_dietary_rules.py` passed (305 tests); scoped Ruff format/check and
strict mypy passed. Full-suite verification remains a downstream migration
gate for later tasks: it currently has 13 failures from Task 1's intentional
raw-dietary contract migration, stale handler/Dynamo fixtures, and stale SAM
artifacts.

### Task 3: Add the targeted development dietary reset

**Files:**

- Create: `scripts/reset_profile_dietary_fields.py`
- Create: `tests/test_reset_profile_dietary_fields.py`
- Modify: `README.md`

- [x] Write failing tests for one exact-user reset that clears only dietary
  preferences and constraints and advances the profile revision.
- [x] Write failing tests proving family data, nutrition targets, plans,
  conversation state, and every meal-history item remain unchanged.
- [x] Write rejection/idempotency tests for missing explicit identifiers,
  missing/malformed profiles, conditional races, repeated execution, broad
  targets, and AWS error redaction.
- [x] Implement the explicit table/profile/region/user command with a
  conditional DynamoDB update and no table scan or recursive target.
- [x] Document that the command is a one-time development reset and must not be
  added to routine deployment orchestration.
- [x] Run `uv run pytest tests/test_reset_profile_dietary_fields.py`; it must
  pass before Task 4.

Task 3 verification: `uv run pytest tests/test_reset_profile_dietary_fields.py`
passed (8 tests). Scoped Ruff format/check passed; strict mypy passed for the
new command. The full suite retains the downstream Task 1/2 migration
failures and stale SAM artifacts recorded under those tasks.

### Task 4: Make `/profile` the sole dietary interpretation boundary

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_telegram_api.py`

- [x] Write failing handler tests proving dietary additions call
  `chat_json_strict_sync()`, present the complete typed interpretation, and
  commit only after the existing token/revision confirmation.
- [x] Write failing tests for timeout, transient/permanent provider failure,
  invalid JSON, malformed rules, unsupported wording, stale callbacks, and
  profile revision conflicts; every case must leave the profile unchanged.
- [x] Write failing display/removal tests for food-frequency and batch rules
  without exposing storage-only IDs or losing the user's source wording.
- [x] Replace plain-text dietary interpretation calls with the strict JSON
  boundary and preserve typed failure categories without fallback prose.
- [x] Remove profile-write paths that can create raw or optional-rule dietary
  entries.
- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_llm_client.py
  tests/test_telegram_api.py`; it must pass before Task 5.

Task 4 verification: `uv run pytest tests/test_bot_handler.py
tests/test_llm_client.py tests/test_telegram_api.py -q` passed (398 tests);
`uv run ruff format --check` and `uv run ruff check` passed for all assigned
files; strict `uv run mypy` passed. Saved typed rules are collected directly
by `/plan` without a preference interpreter. Batch rules are parsed and
reviewed as typed rules, but confirmation remains fail-closed because the
Task 1 profile schema has no durable batch-rule field; later batch-ledger work
must add that storage contract before batch rules can be persisted.

### Task 5: Project weekly rules from submitted meal evidence

**Files:**

- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dietary_rules.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_dynamo.py`

- [x] Write failing table-driven tests for all seven start weekdays and all
  1-7 day horizons, including ranges that cross Sunday into a new ISO week.
- [x] Write failing tests for zero/partial/complete/excess submitted evidence,
  distinct-meal counting, food alternatives, meal scopes, explicit weekdays,
  generated-day carry-forward, `at_least`, `exactly`, and `at_most`.
- [x] Prove draft and confirmed plan meals never count; only persisted submitted
  meal history before the segment cutoff is evidence.
- [x] Add a bounded week-range meal query and a pure projection function that
  emits exact-date obligations with evidence IDs and a deterministic order.
- [x] Reuse normalization and whole-word matching primitives while keeping a
  small dedicated `MealLogEntry` matcher rather than coupling logged and
  planned meal models.
- [x] Return explicit infeasibility only for malformed/contradictory rules;
  cap short-horizon catch-up and leave excess due for a later fresh request.
- [x] Run `uv run pytest tests/test_dietary_rules.py tests/test_preferences.py
  tests/test_dynamo.py`; it must pass before Task 6.

Task 5 verification: `uv run pytest tests/test_dietary_rules.py
tests/test_preferences.py tests/test_dynamo.py -q` passed (305 tests).
Targeted Ruff format/check and strict `uv run mypy` passed. The full suite
passed 1622 tests with 2 pre-existing stale `.aws-sam` artifact failures in
`tests/test_template.py`; no artifact files or later-task code were changed.

### Task 6: Snapshot projected obligations in the `/plan` workflow

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing `/plan` tests proving `no preference` loads typed profile
  rules and submitted evidence without calling any preference interpreter.
- [x] Write failing tests for a fresh request recalculating after new submitted
  meals and a retry retaining its original week/evidence/obligation snapshot.
- [x] Write failing tests for cross-week payloads, duplicate updates, stale
  state revisions, malformed stored typed rules, and constraints remaining
  unscaled.
- [x] Replace `_prepare_stored_preference_rules()` with deterministic profile
  rule collection and calendar projection; delete lazy legacy interpretation.
- [x] Add the bounded obligation snapshot to conversation state and
  `PlanGenerationContext`, and pass it unchanged through async invoke/repair.
- [x] Keep request-specific current preferences scoped to their requested plan
  and resolve them against projected stored obligations and constraints.
- [x] Run `uv run pytest tests/test_bot_handler.py
  tests/test_planner_handler.py`; it must pass before Task 7.

Task 6 verification: the scoped suite passed (525 tests); targeted Ruff format,
Ruff lint, and strict mypy passed. The complete suite passed 1632 tests with
two pre-existing stale `.aws-sam` artifact comparison failures in
`tests/test_template.py`; no artifact files were changed.

### Task 7: Enforce dated obligations in generation and repair

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing prompt tests proving only horizon obligations and permanent
  constraints are rendered, with exact dates and no raw legacy profile rule
  reinterpretation instructions.
- [x] Write failing validation tests for success, missing obligations, excess
  matches, wrong dates, wrong meal scopes, cross-week isolation, best-effort
  rules, and independent mushroom constraint violations.
- [x] Write failing planner tests proving violations produce bounded repair
  feedback, the second attempt uses the same snapshot, and terminal failure
  cannot publish a non-compliant draft.
- [x] Update evidence collection and summaries to report obligation ownership
  and dates without leaking provider payloads or private profile text.
- [x] Update planner generation, repair, and stale-event checks to treat the
  obligation snapshot as application-owned immutable input.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_preferences.py
  tests/test_planner_handler.py`; it must pass before Task 8.

Task 7 verification: `uv run pytest tests/test_prompts.py
tests/test_preferences.py tests/test_planner_handler.py -q` passed (320 tests).
Scoped Ruff format/check and strict `uv run mypy` passed. Dated obligations
are the authoritative planner validation input when supplied; legacy broad
rules remain supported only for callers that do not provide a snapshot.
Repair feedback contains bounded issue codes, safe schema locations, and
application-owned obligation IDs/dates only. Full-suite verification is still
required; `uv run pytest` passed 1642 tests and retained the two pre-existing
stale `.aws-sam` artifact comparison failures in `tests/test_template.py`.

### Task 8: Persist provisional and available batch portions

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing repository tests for empty ledger reads, bounded weekly
  entries, available-portion selection, request/revision ownership, and
  week-end expiry.
- [x] Write failing transaction tests for atomically publishing a draft with
  provisional reservations, replacing only the prior draft's provisional
  entries, and preserving available portions.
- [x] Write failing planner tests for consuming available leftovers before
  creating a new preparation and for a one-day plan reserving 1-2 future
  portions from a 2-3-meal batch.
- [x] Add conditional weekly-ledger repository methods without table scans and
  extend tracked/untracked draft publication to update plan and ledger
  atomically.
- [x] Tie provisional records to plan request/revision and expire a source
  whose preparation date passed without a matching submission.
- [x] Run `uv run pytest tests/test_dynamo.py tests/test_planner_handler.py`; it
  must pass before Task 9.

Task 8 verification: TDD repository and planner coverage passed with
`uv run pytest tests/test_dynamo.py tests/test_planner_handler.py -q` (283
tests); scoped Ruff format/check and strict `uv run mypy` passed. The complete
suite passed 1650 tests with the two pre-existing stale `.aws-sam` artifact
comparison failures in `tests/test_template.py` for `bot_handler.py` and
`planner_handler.py`; artifacts were not modified.

### Task 9: Generate and validate batch-linked meals

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_telegram_api.py`

- [x] Write failing generation-contract tests for preparation batch ID/yield,
  linked leftover roles, eligible lunch/dinner scopes, ingredient completeness,
  and human-readable Telegram labels.
- [x] Write failing validation tests for missing sources, duplicate sources,
  excessive reuse, wrong batch IDs, reuse before preparation, unavailable
  portions, cross-week reuse, and ordinary meals with no batch metadata.
- [x] Update the planner JSON contract and parser while keeping batch IDs
  application-owned or checked against application-issued reservations.
- [x] Add batch compliance evidence and bounded repair feedback without asking
  the model to infer inventory state.
- [x] Render preparation and leftover intent clearly in the draft without
  exposing storage-only ownership fields.
- [x] Run `uv run pytest tests/test_prompts.py tests/test_parser.py
  tests/test_preferences.py tests/test_planner_handler.py
  tests/test_telegram_api.py`; it must pass before Task 10.

Task 9 verification: the exact scoped command passed (580 tests). Ruff format
check and Ruff lint passed for all assigned files; strict `uv run mypy` passed.
Preparation links now carry a bounded optional `total_yield` of two or three,
source meal types are limited to lunch/dinner, available portion numbers are
checked against application-reported remaining stock, and Telegram draft
labels render preparation yield and leftover intent without batch ownership
fields.

### Task 10: Activate and consume batches through submitted meals

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_api.py`

- [x] Write failing guided-submission tests for a date/meal-type match asking
  one explicit preparation/leftover confirmation and for unrelated meals
  retaining the current workflow.
- [x] Write failing repository tests for atomic meal, state, idempotency marker,
  and ledger activation/consumption; include insufficient portions, stale
  ledger revision, wrong batch role, and duplicate Telegram update cases.
- [x] Add optional pending batch linkage to the meal workflow state and callback
  contract with bounded, signed/application-owned identifiers.
- [x] Activate remaining portions only after preparation submission; decrement
  exactly once after confirmed leftover submission; never mutate inventory for
  a planned or confirmed meal alone.
- [x] Preserve current retry, cancellation, duplicate, and free-text logging
  behavior when no unambiguous planned batch meal matches.
- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py
  tests/test_router.py tests/test_telegram_api.py`; it must pass before Task 11.

Task 10 verification: the exact scoped command passed (611 tests, with two
pre-existing Pydantic serializer warnings in profile-removal tests). Ruff
format/check passed for all assigned files and strict `uv run mypy` passed.
Meal confirmation now atomically persists the submitted meal, continuation
state, idempotency marker, and conditional ledger activation/consumption.

### Task 11: Harden concurrency, retries, expiry, and privacy boundaries

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_dynamo.py`

- [x] Write failing race tests for simultaneous fresh plans, stale planner
  completion, draft replacement versus batch activation, and two submissions
  attempting to consume the last portion.
- [x] Write failing expiry tests at preparation-day and ISO-week boundaries,
  including a plan spanning two weekly ledgers.
- [x] Write failing logging tests proving provider output, meal descriptions,
  foods, source text, batch IDs, and raw payloads do not appear in warning/error
  records; only bounded reason codes and ownership-safe metadata may appear.
- [x] Add exact conditional expressions and conflict classification so stale
  work is suppressed or retried without overwriting newer actual inventory.
- [x] Ensure cleanup affects only provisional entries owned by stale/replaced
  drafts and never deletes submitted meal evidence or available batches.
- [x] Run `uv run pytest tests/test_bot_handler.py
  tests/test_planner_handler.py tests/test_dynamo.py`; it must pass before
  Task 12.

Task 11 verification: the exact scoped command passed (668 tests, with two
pre-existing Pydantic serializer warnings). Added race, boundary, spanning-
week, owner-scoped cleanup, conflict-classification, retry, and privacy-log
coverage. Ruff format/check and strict `uv run mypy` passed for all assigned
files; `git diff --check` passed.

### Task 12: Prove the complete Wednesday one-day workflow

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_readme.py`

- [x] Add the exact profile fixture: no mushrooms; batch cooking covering 2-3
  lunch/dinner meals; eggs three breakfasts weekly; pancakes/crepes Saturday;
  and fish at least one dinner weekly.
- [x] Add Wednesday `1, no preference` cases with zero, one, and two qualifying
  earlier egg-breakfast submissions and assert the computed obligation changes
  deterministically.
- [x] Assert no Saturday pancake is offered, actual submitted meals are the
  only quota evidence, mushrooms remain prohibited, and no saved-preference
  interpretation call occurs.
- [x] Exercise batch preparation, submission activation, a later one-day plan,
  leftover consumption, retry stability, and a fresh-plan recalculation.
- [x] Add terminal cases for malformed typed profiles and non-compliant planner
  output; neither may publish a draft or weaken the constraint.
- [x] Run `uv run pytest`; the complete suite must pass before Task 13.

Task 12 verification: Added the exact Wednesday profile fixture and complete
projection, no-preference, typed-profile failure, planner safety, retry/fresh
calculation, and Moto-backed batch preparation/activation/leftover workflow
coverage. `uv run pytest` passed (1714 tests, with 2 pre-existing Pydantic
serializer warnings). `uv run ruff format --check .`, `uv run ruff check .`,
and `uv run mypy` passed. The ignored SAM build artifact was refreshed with
`uvx --from aws-sam-cli sam build --beta-features` so the existing artifact
comparison tests pass; no Tasks 13–14 were started.

### Task 13: Verify acceptance criteria and project quality gates

**Files:**

- Modify if required by findings: files already listed in Tasks 1-12 and their
  corresponding tests
- Modify: this plan file with command outcomes and any remediation notes

- [x] Verify `/plan` has no legacy saved-preference interpretation path and the
  original parse error is unreachable after valid profile recreation.
- [x] Verify every approved calendar, submitted-evidence, explicit-weekday,
  batch-inventory, reset, constraint, retry, and privacy requirement.
- [x] Run `uv run ruff format --check .` and fix all findings with Ruff.
- [x] Run `uv run ruff check .` and fix all findings.
- [x] Run `uv run mypy` and fix all findings without suppressing relevant type
  errors.
- [x] Run `uv run pytest` and require a fully passing suite.
- [x] Review the accumulated diff for unrelated changes and verify the reset
  command cannot target a broad directory, table scan, or unspecified user.

Task 13 verification: Audited the `/plan` implementation and confirmed saved
typed rules are collected directly; the only remaining interpretation call is
for new, request-specific preference text. The Wednesday profile regression
tests confirm `1, no preference` performs no interpreter call and does not
emit the original saved-preference parse error. Calendar projection,
submitted-meal evidence, explicit weekdays, constraint isolation, batch
reservation/activation/consumption/expiry, reset scoping, retry snapshots,
stale/race handling, and privacy-safe logging are covered by the complete
passing test suite and the targeted tests added in Tasks 1-12.

Quality gates passed:

- `uv run ruff format --check .` — 105 files already formatted.
- `uv run ruff check .` — passed.
- `uv run mypy` — passed with no issues in 20 source files.
- `uv run pytest` — 1714 passed in 13.43s; 2 pre-existing Pydantic
  serializer warnings remain in profile-removal tests.
- `git diff --check` — passed.

Diff/reset audit: all 28 changed paths are named in the implementation plan;
no unrelated tracked or untracked path was found. The reset command requires
explicit table, AWS profile, region, and user ID arguments, rejects broad
selectors, reads and conditionally updates only the exact `USER#<id>`/
`PROFILE` item, and contains no table scan or recursive target. No Task 13
code remediation was required. The ignored `.aws-sam/` build output remains
outside the tracked diff from the prior verification refresh.

### Task 14: [Final] Update documentation and archive the plan

**Files:**

- Modify: `README.md`
- Modify: `docs/prompt.md`
- Modify: `docs/plans/2026-08-26-calendar-aware-dietary-scheduling-and-batch-leftovers.md`
- Move after all gates pass:
  `docs/plans/2026-08-26-calendar-aware-dietary-scheduling-and-batch-leftovers.md`
  to `docs/plans/completed/`

- [x] Document profile-only strict interpretation, weekly calendar projection,
  submitted-meal evidence, explicit weekdays, batch lifecycle, and the exact
  `/plan` retry semantics.
- [x] Document the planner JSON additions and application-owned validation in
  `docs/prompt.md`.
- [x] Update command/documentation tests for every changed user-facing claim.
- [x] Record final verification outcomes and any approved deviations in this
  plan.
- [x] Move the fully completed plan to `docs/plans/completed/`.

Task 14 final verification (2026-08-26): Updated `README.md` with the
profile-only strict interpretation boundary, ISO-week quota projection,
submitted-meal evidence rules, explicit versus generated weekdays, batch
reservation/activation/consumption/expiry lifecycle, and immutable snapshot
retry semantics. Updated `docs/prompt.md` with the 1-7 day planner contract,
optional `batch_link` JSON fields, application-owned batch identifiers, and
post-parse validation responsibilities. Added documentation assertions in
`tests/test_readme.py`; the initial test run failed on the newly required
claims and passed after the documentation changes (`16 passed`).

Final quality gates:

- `uv run pytest` — 1717 passed in 13.55s; two known Pydantic serializer
  warnings remain in profile-removal coverage.
- `uv run ruff format --check .` — passed; 105 files already formatted after
  formatting the new documentation assertions.
- `uv run ruff check .` — passed.
- `uv run mypy` — passed with no issues in 20 source files.
- `git diff --check` — passed.

No approved deviations. The remaining Post-Completion items require live
development credentials or external coordination and were intentionally not
performed. All preceding task checkboxes were verified complete before this
plan was archived.

## Post-Completion

### Manual verification

- Deploy the compatible application code to the development stack.
- Run the targeted reset command once with the exact table, AWS profile,
  region, and sole user ID. Confirm only the profile's dietary fields changed.
- Recreate the dietary constraint and preferences through `/profile`, checking
  each interpreted rule before confirmation.
- On a Wednesday, submit representative earlier breakfasts and run `/plan`
  with `1, no preference`. Confirm egg pacing changes with submitted history,
  no Saturday pancake appears, and no mushroom-containing meal is published.
- Submit a batch preparation, request a later short plan, and verify a linked
  leftover appears and consumes exactly one available portion.

### External system updates

- Create and link the GitHub issue for this plan.
- Inspect CloudWatch logs for bounded failure categories only; do not use raw
  preference or meal content as diagnostics.
- Do not add the reset command to routine deployment or rerun it after the
  profile has been recreated.
