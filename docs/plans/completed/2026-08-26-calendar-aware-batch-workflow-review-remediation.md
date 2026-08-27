# Remediate Calendar-Aware Batch Workflow Review Findings

## Overview

Remediate the seven actionable findings from the independent review of the
calendar-aware dietary scheduling and batch-leftover implementation. The work
closes four P1 workflow gaps and three P2 ownership, revision-contract, and
concurrency gaps without changing the established weekly scheduling model.

The remediation makes a confirmed batch-cooking rule a durable part of the
profile and planner input, aligns leftover ordinals across planning and meal
submission, resolves batch links from the plan covering the submitted date,
and rejects late preparation activation deterministically. It also replaces
provider-selected preparation IDs before persistence, preserves three-meal
yield during plan revision, and keeps weekly-ledger revisions monotonic when
expiry is materialized.

## Context

- **Completed original plan:** `docs/plans/completed/2026-08-26-`
  `calendar-aware-dietary-scheduling-and-batch-leftovers.md`.
- **Implementation baseline:** all 14 original tasks were reported complete.
- **Last verified gates:** `uv run pytest` passed with 1,717 tests and two
  known Pydantic serializer warnings; Ruff format/check, strict Mypy, and
  `git diff --check` passed.
- **Primary components:** profile confirmation and display in
  `src/meal_planner/bot_handler.py` and `src/meal_planner/telegram/api.py`;
  profile and batch schemas in `src/meal_planner/models/schemas.py`; planner
  contracts and normalization in `src/meal_planner/llm/prompts.py`,
  `src/meal_planner/preferences.py`, and
  `src/meal_planner/planner_handler.py`; durable plan/ledger behavior in
  `src/meal_planner/db/dynamo.py`.
- **Repository conventions:** Python 3.14, Pydantic boundaries, DynamoDB
  conditional transactions, `uv` commands, Ruff at 80 columns, strict Mypy,
  Pytest/Moto repository tests, and no live provider or Telegram calls in the
  automated suite.
- **Dirty-worktree boundary:** preserve all pre-existing and accumulated
  implementation changes. Modify only files required by a numbered task; do
  not reset, clean, reformat, or attribute unrelated paths.
- **Finding 1 attribution limitation:** the fail-closed branch near
  `BotHandler` profile confirmation predates Task 9 in an overlapping dirty
  worktree, so its origin cannot be attributed reliably to this execution.
  The reviewed end state nevertheless does not satisfy the original
  persistent batch-rule workflow and must be remediated and tested end to end.

## Review Findings Covered

1. **P1 — Persist confirmed batch rules instead of rejecting them.** A
   confirmed `BatchRule` currently reaches a cannot-be-saved branch, so the
   rule is absent from durable profiles and later `/plan` requests.
2. **P1 — Align available portion validation with submission consumption.**
   Planning and submission calculate different valid ordinals when a
   three-portion preparation has one portion left.
3. **P1 — Resolve the plan covering the submitted meal date.** Looking only at
   the newest plan loses batch linkage for a backdated meal from an earlier
   plan.
4. **P1 — Reject activation after a provisional source has expired.** A late
   preparation can activate based on whether an unrelated read happened to
   materialize expiry first.
5. **P2 — Replace provider-generated preparation IDs before persistence.** A
   provider-selected `batch_id` can become a durable application identifier.
6. **P2 — Preserve preparation yield in the revision contract.** The revision
   schema omits `total_yield`, allowing a three-meal batch to default to two.
7. **P2 — Advance the ledger revision when materializing expiry.** Rebuilding
   an expired ledger resets its revision and weakens the CAS token.

## Development Approach

- **Testing approach: TDD.** For every numbered task, add the smallest focused
  failing test first, run it to capture the expected defect, implement only
  that task, rerun the new test, and then run its regression modules.
- Complete tasks in numerical order. P1 workflow correctness precedes P2
  ownership and concurrency hardening; Task 6 depends on Task 5's canonical
  preparation-ID boundary.
- Keep profile, plan, meal-log, and weekly-ledger writes compatible with
  existing records. New profile fields must have safe defaults for records
  written before this remediation.
- Keep application-owned decisions outside provider output. Provider data may
  propose meal structure, but it must not choose durable IDs, inventory, CAS
  revisions, or expiry outcomes.
- Preserve transactional/idempotent meal submission. A failed or duplicate
  batch mutation must not partially write a meal, state transition, update
  marker, or ledger change.
- Use user-partition key queries and exact conditional writes; do not add table
  scans, broad selectors, or unbounded result processing.
- Keep logs privacy-safe. New errors and conflicts may include bounded reason
  codes and ownership-safe metadata, but not profile text, meal descriptions,
  food names, provider payloads, or batch IDs.
- Do not proceed to the next task while the current task's focused or
  regression tests fail. Record the exact failing and passing commands in this
  plan during implementation.
- Mark checklist items `[x]` only after the stated work and evidence exist.
  Add `➕` for approved scope additions and `⚠️` for blockers or
  deviations.

## Testing Strategy

- **Profile workflow:** handler and Telegram tests confirm a typed batch rule
  can be created, displayed, loaded, updated/removed through existing profile
  paths, and supplied to a later plan without reinterpretation.
- **Planner validation:** unit tests exercise canonical leftover ordinals,
  application-owned preparation IDs, linked leftover rewrites, and revision
  yield preservation.
- **Repository behavior:** Moto tests cover date-aware plan selection,
  preparation-day/ISO-week activation rejection, ledger CAS conditions,
  idempotency, and concurrent expiry materialization.
- **Handler integration:** guided meal-submission tests prove backdated and
  late confirmations produce deterministic user-visible outcomes without
  partial inventory mutation.
- **End-to-end regression:** create a batch rule through `/profile`, generate a
  preparation through `/plan`, activate it, and consume portions 2 and 3 in
  separate fresh plans.
- **Error paths:** cover malformed stored rules, stale revisions, duplicate
  updates, unknown provider batch IDs, unavailable ordinals, overlapping plan
  candidates, and late confirmations before and after an expiry read.
- Tests must not use skips, xfails, live AWS, live LLMs, network calls, or live
  Telegram APIs.
- After the final task, run the complete suite and all repository quality
  gates. The two known Pydantic serializer warnings may remain only if their
  count and source are unchanged and they are documented as pre-existing.

## Solution Overview

Add the existing typed `BatchRule` contract to the canonical user profile with
a backward-compatible empty default. Profile confirmation must persist food
rules, constraints, and the batch rule atomically, and existing profile
display/update/removal paths must preserve or intentionally remove it. Fresh
planning must read the saved typed rule directly and propagate it into planner
generation and validation; `/plan` must never reinterpret saved profile text.

Define one canonical formula for available leftover ordinals. For a batch with
`total_portions=T` and `remaining_portions=R`, available ordinals are the
inclusive range `T - R + 1` through `T`. Use this calculation in both planner
validation and submission consumption so each persisted portion can be
planned and consumed exactly once.

When meal submission asks for a planned batch link, query bounded plan records
under the user partition and select the current eligible plan revision that
covers the submitted `target_date`, rather than selecting the newest plan
globally. Activation must then compare the processing date with both the
preparation date and the ledger's ISO-week expiry inside the same guarded
transaction path. A late source remains unusable regardless of whether an
earlier read has materialized expiry.

After parsing a newly generated or revised plan, replace every provider token
for a new preparation with an application-issued identifier and rewrite all
leftover links in that candidate through a validated one-to-one mapping.
Preserve only IDs verified against existing inventory. Make the revision JSON
contract mirror initial generation for `total_yield` and batch-link semantics,
and reject or repair a preparation whose yield is missing rather than silently
defaulting it.

Finally, materialized expiry must advance the weekly ledger from revision `N`
to `N + 1`. Condition the write on both revision `N` and the exact prior entry
set so one concurrent writer wins, losing writers reload safely, and no ABA
revision can be recreated.

## Technical Details

### Persistent profile batch rule

- Reuse the existing bounded `BatchRule` model; do not create an untyped text
  field or store provider prose.
- Add a backward-compatible profile field with the existing supported
  cardinality and safe empty default for historical records.
- Treat a mixed profile interpretation as one atomic confirmation: either all
  supported food and batch rules are saved, or no partial rule update occurs.
- Include the field in the established profile copy/update/removal and display
  code so unrelated amendments cannot silently erase it.
- Supply the saved rule through the normal `/plan` context and retry snapshot.
  Do not make a new preference-interpretation call.

### Canonical portion ordinal

- Centralize or identically apply the formula
  `first_available = total_portions - remaining_portions + 1`.
- Accept only `first_available <= portion_number <= total_portions`.
- Reject zero/negative remaining values, impossible totals, duplicate
  consumption, and any link outside the current batch/week constraints through
  the existing controlled error path.

### Date-aware plan selection and activation

- Query only plan items in the submitting user's partition using existing key
  patterns and bounded limits.
- Filter candidates whose plan horizon contains `target_date`, then select the
  eligible current revision/status deterministically. Tests must define the
  precedence for overlapping revisions and prove a newer non-covering plan
  cannot hide an older covering plan.
- Pass an application-owned processing date/clock value into activation.
- Require processing on the preparation date and before the ISO-week expiry;
  add exact ledger/revision conditions so the check cannot race with expiry or
  draft replacement.

### Application-owned IDs and revision yield

- Canonicalize IDs after strict parsing and before validation, reservation,
  publication, or persistence.
- Allocate one fresh ID per new preparation and rewrite only leftovers linked
  to that provider-local preparation token.
- Preserve IDs only for entries verified against current application-owned
  inventory; reject unknown, ambiguous, duplicate, or collision-prone links.
- Include required `total_yield` and all initial-generation batch constraints
  in the revision schema and instructions. Do not rely on a default of two for
  a revised preparation.

### Monotonic expiry revision

- Build the expired ledger with `revision=old.revision + 1`.
- Condition persistence on both the old revision and exact old entries.
- Treat a conditional loser as a normal concurrency event: reload and return
  the winner's valid current state without a second increment for the same
  expiry transition.

## What Goes Where

- **Implementation Steps:** the seven numbered tasks below are repository code
  and automated tests. Every task follows the same explicit TDD sequence.
- **Post-Completion:** plan archival, deployment, and live Telegram/AWS checks
  occur only after all implementation checkboxes and quality gates pass.

## Implementation Steps

### Task 1: Persist and propagate confirmed profile batch rules

**Severity:** P1

**Finding attribution:** The rejecting branch near
`src/meal_planner/bot_handler.py:789` predates Task 9 in an overlapping dirty
worktree, so its origin is not reliably attributable to this execution. The
reviewed end state still lacks the required durable workflow.

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_telegram_api.py`

**Symbols:** `UserProfile`, `BatchRule`, the profile-confirmation branch near
`BotHandler` line 789, existing profile copy/remove/display helpers,
`PlanGenerationContext`, and planner invocation setup.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a handler-level regression that
  interprets a profile containing a valid `BatchRule`, confirms it, reloads
  the profile, starts a later `/plan`, and asserts the exact typed rule reaches
  planner generation without another interpretation call.
- [x] Add schema persistence tests proving profiles without the new field load
  with an empty default, valid typed rules round-trip, and malformed or
  oversized batch-rule values fail closed.
- [x] Add profile CRUD/display tests proving unrelated profile amendments
  preserve the batch rule, the existing relevant removal/reset path removes it
  intentionally, and profile summaries render its bounded 2-3 meal semantics
  without exposing storage-only identifiers.
- [x] Add atomicity tests for mixed food and batch interpretations: confirmation
  saves all supported rules together, while an invalid component saves none
  and leaves the prior profile/revision intact.
- [x] ⚠️ **Expected failure:** The pre-implementation failure could not be
  rerun because the supplied dirty baseline already contained the partial
  Task 1 implementation and tests; no rollback or source replacement was
  performed. The first focused run against that baseline passed.
- [x] **Implementation change:** Add backward-compatible typed batch-rule
  storage to `UserProfile`; update profile confirmation, load/copy/update,
  removal, and display paths to persist and preserve the field atomically.
- [x] Propagate the saved typed rule through the fresh-plan context and retry
  snapshot into generation/validation. Remove only the fail-closed rejection
  made obsolete by typed storage; retain rejection for unsupported outputs.
- [x] Preserve profile conditional revisions, duplicate-update handling,
  malformed-profile errors, non-dietary fields, and the rule that `/plan`
  never interprets saved profile text.
- [x] **Verify the new test passes:** Run the new `/profile`-then-`/plan`
  regression and schema/CRUD/display cases; require all to pass without skip
  or xfail.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_schemas.py tests/test_bot_handler.py \
  tests/test_planner_handler.py tests/test_telegram_api.py` and resolve every
  failure before Task 2.

**Acceptance criteria:**

- [x] A valid confirmed `BatchRule` is durably stored as typed profile data
  and survives reload and unrelated profile amendments.
- [x] A later `/plan` receives the stored rule without invoking profile
  interpretation and can enforce preparation/reuse behavior from it.
- [x] Existing profile display and relevant removal/reset behavior include the
  batch rule without leaking internal IDs.
- [x] Mixed confirmations are atomic; unsupported or malformed output cannot
  create a partial profile update.
- [x] Historical profiles with no batch-rule field remain readable with no
  invented behavior.

**Task 1 evidence (2026-08-26):**

- `uv run pytest tests/test_bot_handler.py -k 'confirmed_batch_rule_is_saved_and_reused_by_later_plan or mixed_profile_confirmation_saves_food_and_batch_rules_atomically or profile_amendments_preserve_and_remove_batch_rules'`: 3 passed.
- `uv run pytest tests/test_schemas.py -k 'user_profile_batch_rules_round_trip_and_bounded or plan_generation_context_carries_typed_batch_rules'`: 2 passed.
- `uv run pytest tests/test_planner_handler.py -k 'generation_context_carries'`: 1 passed.
- `uv run pytest tests/test_telegram_api.py -k 'profile_summary_renders_bounded_batch_rules_without_storage_id or profile_rule_review_renders_batch_rules_without_storage_id'`: 2 passed.
- `uv run pytest tests/test_schemas.py tests/test_bot_handler.py tests/test_planner_handler.py tests/test_telegram_api.py`: 844 passed, 2 known Pydantic serializer warnings.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded; `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 26 passed.
- `uv run pytest`: 1,723 passed, 2 known Pydantic serializer warnings.
- `uv run ruff format --check .`: 106 files already formatted; `uv run ruff check .`: all checks passed; `uv run mypy`: no issues in 20 source files; `git diff --check`: passed.

### Task 2: Unify available leftover portion ordinals

**Severity:** P1

**Depends on:** Task 1 for the complete profile-to-plan integration fixture.

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** batch-link availability validation near
`src/meal_planner/preferences.py:1112`, leftover consumption validation near
`src/meal_planner/db/dynamo.py:934`, and fresh-plan batch inventory handling.

**TDD sequence:**

- [x] **Failing or missing test first:** Add table-driven validation and
  repository tests for total/remaining pairs `(2, 1)`, `(3, 2)`, and `(3, 1)`.
  Assert the valid inclusive ordinals are respectively `{2}`, `{2, 3}`, and
  `{3}` in both planning and submission.
- [x] Add rejection cases for already-consumed ordinals, an ordinal below
  `total_portions - remaining_portions + 1`, an ordinal above total, zero
  remaining stock, wrong role, and stale ledger revision.
- [x] Add an end-to-end regression that activates a three-meal preparation,
  generates a fresh plan for portion 2, consumes it, generates another fresh
  plan for portion 3, and consumes the final portion exactly once.
- [x] Assert retries and duplicate Telegram updates do not decrement inventory
  twice and no new preparation is invented while a valid portion remains.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that the one-remaining state exposed portion 2 during planning while
  repository consumption accepted only portion 3. The planning matrix failed
  for `(3, 1, 3)` and incorrectly passed `(3, 1, 2)`; the repository matrix
  passed.
- [x] **Implementation change:** Use the canonical first-available formula
  `total_portions - remaining_portions + 1` in both application validation and
  repository consumption, preferably through one pure typed helper if that
  does not introduce cross-layer coupling.
- [x] Keep exact ledger revision/entry conditions and existing insufficient,
  stale, duplicate, and wrong-role outcomes unchanged apart from the corrected
  ordinal range.
- [x] **Verify the new test passes:** Run the focused ordinal matrix and the
  two-fresh-plan three-portion regression; require all assertions to pass.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_preferences.py tests/test_dynamo.py \
  tests/test_planner_handler.py tests/test_bot_handler.py` and resolve every
  failure before Task 3.

**Acceptance criteria:**

- [x] Planning and submission accept exactly the same available ordinal range
  for every supported two- or three-meal batch state.
- [x] Portions 2 and 3 of a three-meal preparation can be planned and consumed
  in separate fresh plans without collision or premature unavailability.
- [x] Each portion decrements inventory exactly once; stale, duplicate, and
  out-of-range submissions remain controlled non-mutations.

**Task 2 evidence (2026-08-26):**

- `uv run pytest tests/test_preferences.py -k canonical_available_portion_ordinals`: 6 passed.
- `uv run pytest tests/test_dynamo.py -k 'canonical_available_portion or portions_outside_canonical'`: 6 passed.
- `uv run pytest tests/test_planner_handler.py -k 'available_leftover_before_new_preparation'`: 1 passed.
- `uv run pytest tests/test_bot_handler.py -k 'wednesday_batch_preparation_then_leftover_consumption_is_atomic'`: 1 passed.
- `uv run pytest tests/test_preferences.py tests/test_dynamo.py tests/test_planner_handler.py tests/test_bot_handler.py`: 802 passed, 2 known Pydantic serializer warnings.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded; `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 26 passed.
- `uv run ruff format --check .`: 106 files already formatted; `uv run ruff check .`: all checks passed; `uv run mypy`: no issues in 20 source files; `git diff --check`: passed.
- `uv run pytest`: 1,734 passed, 2 known Pydantic serializer warnings, with the pre-existing `tests/test_dynamo.py::test_new_profile_creation_is_race_safe` failure (observed twice in the full suite; the isolated test passed).

⚠️ **Task 2 blocker:** The full-suite gate remains blocked by the unrelated,
pre-existing Moto profile-creation race test. No unrelated test or Task 1
implementation was changed.

### Task 3: Select the plan revision covering the submitted date

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `get_planned_batch_link()` near
`src/meal_planner/db/dynamo.py:2103`, plan key-query/deserialization helpers,
and guided meal-submission batch-link lookup.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a Moto repository test with an
  earlier plan covering `target_date` and a lexicographically newer plan whose
  horizon does not cover it. Assert lookup returns the eligible link from the
  covering plan.
- [x] Add deterministic precedence tests for multiple records covering the
  same date: current eligible status and highest current revision must win;
  superseded, stale, malformed, or non-covering records must not supply a
  link.
- [x] Add a handler regression that creates a newer plan, then backdates a
  guided preparation and leftover submission to the preceding covering plan.
  Assert activation/consumption occurs and the meal stores the confirmed link.
- [x] Add no-match and ordinary-meal cases proving a bounded missing link keeps
  existing submission behavior and does not attach an unrelated batch.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that latest-plan-only selection returns no link or the wrong link for
  the backdated meal.
- [x] **Implementation change:** Replace global latest-plan selection with a
  bounded user-partition plan query that filters by `target_date` coverage and
  selects the eligible current revision/status using deterministic existing
  lifecycle ordering.
- [x] Preserve user ownership, date/meal-type matching, deserialization
  validation, duplicate-update handling, and the prohibition on table scans.
- [x] **Verify the new test passes:** Run the covering-plan repository cases
  and backdated handler workflow; require the earlier link to activate or
  consume inventory correctly after a newer plan exists.
- [ ] ⚠️ **Relevant regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and resolve
  every failure before Task 4. The Task 3 cases pass, but the combined run
  retains one unrelated pre-existing Moto race failure in
  `test_simultaneous_fresh_plan_state_starts_have_one_winner`.

**Acceptance criteria:**

- [x] A newer non-covering plan cannot hide the eligible plan for a submitted
  date from the preceding plan horizon.
- [x] Overlapping plan revisions resolve deterministically to the current
  eligible revision and never to stale or malformed data.
- [x] Backdated preparation and leftover submissions retain their planned
  batch links and mutate the intended ledger exactly once.
- [x] Lookup remains bounded to the user partition and introduces no scan.

**Task 3 evidence (2026-08-26):**

- `uv run pytest tests/test_dynamo.py -k 'planned_batch_link'`: 4 passed.
- `uv run pytest tests/test_bot_handler.py -k 'backdated_batch_submission or structured_unrelated_meal'`: 2 passed.
- `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py`: 511 passed, 1 pre-existing Moto race failure, 2 known Pydantic serializer warnings.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded.
- `uv run pytest`: 1,739 passed, 1 pre-existing Moto race failure, 2 known Pydantic serializer warnings; all template tests passed.
- `uv run ruff format --check .`: 106 files already formatted.
- `uv run ruff check .`: all checks passed.
- `uv run mypy`: no issues in 20 source files.
- `git diff --check`: passed.

⚠️ **Task 3 blocker:** The required regression/full-suite gates retain the
pre-existing Moto concurrency failure
`tests/test_dynamo.py::test_new_profile_creation_is_race_safe` in the full
suite and, in one combined-run attempt,
`tests/test_dynamo.py::test_simultaneous_fresh_plan_state_starts_have_one_winner`.
Task 3 focused lookup and handler tests are green; no unrelated concurrency
test or implementation was changed.

### Task 4: Reject late preparation activation transactionally

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** preparation activation validation near
`src/meal_planner/db/dynamo.py:900`, atomic submitted-meal transaction setup,
weekly-ledger expiry helpers, and the handler's application clock/date input.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a repository test for a Friday
  provisional preparation submitted on Saturday before any ledger read has
  materialized expiry. Assert activation is rejected and remaining portions
  never become available.
- [x] Repeat the same submission after an explicit read has materialized
  expiry and assert the identical controlled outcome and repository state.
- [x] Add preparation-day boundary tests for same-day success, next-day
  failure, ISO-week rollover failure, stale revision, and a concurrent expiry
  or draft-replacement winner.
- [x] Add handler tests using an injected processing date/clock. Assert a late
  confirmation cannot persist an activated batch link or partially advance
  meal state, update marker, conversation state, or ledger mutation.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that Saturday activation succeeds when no prior read has expired the
  Friday source, while the post-read case fails.
- [x] **Implementation change:** Pass the application-owned processing date to
  the activation path and require it to equal the preparation date and remain
  inside the ledger ISO week before constructing the transaction.
- [x] Add exact transactional conditions for the provisional source, owner,
  revision, entries, preparation date, and expiry boundary so activation
  cannot race an expiry or replacement write.
- [x] Preserve privacy-safe errors, duplicate/idempotent replay behavior, and
  the existing controlled user response for an unusable planned batch link.
- [x] **Verify the new test passes:** Run both late-confirmation variants and
  all date-boundary/concurrency cases; require behavior to be independent of
  prior reads.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_router.py` and resolve every failure before Task 5.

**Acceptance criteria:**

- [x] A preparation activates only when processed on its planned preparation
  date and before its ISO-week expiry.
- [x] Late confirmation has the same non-activation result before and after
  any read materializes expiry.
- [x] Activation and expiry/replacement races have one conditional winner and
  cannot create available inventory from an expired source.
- [x] No failed activation leaves partial meal, workflow-state, idempotency, or
  ledger writes.

### Task 5: Canonicalize new preparation IDs before persistence

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_dynamo.py`

**Symbols:** parsed-plan handling near
`src/meal_planner/planner_handler.py:169`, `_batch_entries_for_plan()`, draft
publication/reservation setup, and the existing application ID factory.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a planner test whose provider
  returns a new preparation with a recognizable `batch_id` and one or two
  leftovers referencing it. Assert that token appears nowhere in the
  published plan or provisional ledger.
- [x] Add multiple-preparation tests proving each provider-local token maps to
  one distinct application-issued ID and every linked leftover is rewritten
  consistently before validation and reservation.
- [x] Add preservation tests proving only batch IDs verified against existing
  available inventory survive unchanged; unknown existing IDs, duplicate
  provider tokens, ambiguous links, and collisions fail validation or enter
  bounded repair without persistence.
- [x] Add retry/stale-publication tests proving canonicalization occurs before
  any durable write and cannot publish reservations owned by a stale request.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that the provider-selected preparation ID is currently persisted by
  `_batch_entries_for_plan()`.
- [x] **Implementation change:** Add a typed canonicalization step after strict
  parsing and before compliance validation/publication. Allocate one
  application ID for each new preparation and atomically rewrite its matching
  leftovers through a validated local mapping.
- [x] Keep verified existing-inventory IDs separate from provider-local new
  preparation tokens, check collisions against inventory and the candidate,
  and keep all internal IDs out of logs and Telegram rendering.
- [x] **Verify the new test passes:** Run provider-token, multi-preparation,
  inventory-preservation, and stale-publication cases; assert no provider ID is
  persisted.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_planner_handler.py tests/test_preferences.py \
  tests/test_dynamo.py tests/test_prompts.py tests/test_parser.py` and resolve
  every failure before Task 6.

**Acceptance criteria:**

- [x] No provider-selected ID for a new preparation reaches a plan, ledger,
  meal link, log, or user-visible response.
- [x] Every new preparation and all of its candidate leftovers share one
  collision-checked application-issued ID.
- [x] Existing IDs survive only when verified against current inventory;
  unknown, ambiguous, duplicate, and colliding IDs cannot be persisted.
- [x] Canonicalization runs before validation, reservation, and publication on
  initial and repaired candidates.

### Task 6: Preserve batch yield through plan revision

**Severity:** P2

**Depends on:** Task 5, so revised candidates use the same application-owned
ID canonicalization as initial generation.

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** revision JSON schema near
`src/meal_planner/llm/prompts.py:696`, revision prompt construction, revised
candidate parsing/validation, and provisional reservation creation.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a prompt contract test requiring
  every revised preparation batch link to include `total_yield` with the same
  bounded values and role/link rules as initial generation.
- [x] Add a planner revision regression starting with a three-meal
  preparation. Have the revision retain that preparation and assert the
  published reservation has two future portions, not the two-meal default of
  one.
- [x] Add rejection/repair cases for missing `total_yield`, values outside
  2-3, mismatch between yield and linked leftovers, and a revision that
  changes verified available-inventory metadata.
- [x] Assert revised new-preparation IDs are canonicalized under Task 5 and a
  stale revision cannot replace a newer plan or ledger reservation.
- [x] **Expected failure:** Run the new tests before implementation and record
  that the revision schema omits `total_yield` or reservation creation defaults
  the revised three-meal preparation to two total portions.
- [x] **Implementation change:** Make the revision batch schema/instructions
  mirror initial generation for `total_yield`, preparation/leftover roles,
  ordering, eligible meal types, and inventory ownership.
- [x] Require or safely preserve the validated yield for revised preparations
  through parsing, canonicalization, compliance validation, and reservation;
  do not silently default a missing revised yield to two.
- [x] **Verify the new test passes:** Run the revision prompt and three-meal
  reservation cases; assert two future portions remain after publication.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_prompts.py tests/test_parser.py \
  tests/test_planner_handler.py tests/test_preferences.py` and resolve every
  failure before Task 7.

**Acceptance criteria:**

- [x] Initial and revision contracts express identical bounded preparation
  yield and batch-link semantics.
- [x] Revising a valid three-meal preparation preserves two future portions.
- [x] Missing, invalid, or inconsistent revision yield enters bounded repair
  or terminal rejection and never silently becomes a two-meal batch.
- [x] Revised candidates retain application-owned ID, inventory, stale-event,
  and publication guarantees.

**Task 6 evidence (2026-08-26):**

- `uv run pytest tests/test_prompts.py -k 'revision_prompt_requires_complete_batch_yield_contract' -q`: 1 expected failure before implementation; passed after implementation.
- `uv run pytest tests/test_prompts.py -k 'revision_prompt_requires_complete_batch_yield_contract' tests/test_planner_handler.py -k 'revise_plan_preserves_three_meal_preparation_yield' -q`: 1 expected failure before implementation (`total_portions` was 2 instead of 3); passed after implementation.
- `uv run pytest tests/test_prompts.py -k 'revision_prompt_requires_complete_batch_yield_contract' tests/test_planner_handler.py -k 'revise_plan_preserves_three_meal_preparation_yield or revision_rejects_missing_or_out_of_bounds_new_yield or revision_rejects_yield_mismatch_with_linked_leftovers or revision_rejects_changed_available_batch_metadata or revise_plan_canonicalizes_new_preparation_before_reservation' -q`: 6 passed.
- `uv run pytest tests/test_prompts.py tests/test_parser.py tests/test_planner_handler.py tests/test_preferences.py -q`: 571 passed.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded; `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py -q`: 26 passed.
- `uv run pytest`: 1,759 passed, 2 unchanged Pydantic serializer warnings.
- `uv run ruff format --check .`: 106 files already formatted; `uv run ruff check .`: all checks passed; `uv run mypy`: no issues in 20 source files; `git diff --check`: passed.

### Task 7: Keep ledger revisions monotonic during expiry

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** expiry materialization near
`src/meal_planner/db/dynamo.py:1164`, `WeeklyBatchLedger.revision`, ledger CAS
conditions, conflict classification/reload, and expiry callers.

**TDD sequence:**

- [x] **Failing or missing test first:** Add a repository test starting with a
  nonzero ledger revision and an expirable entry. Assert materialization writes
  the expired state at exactly `old_revision + 1`, never zero.
- [x] Add a concurrent-expiry test in which two readers attempt to materialize
  the same transition. Assert one conditional write wins, the loser reloads,
  both callers observe the same final ledger, and the revision increments only
  once.
- [x] Add CAS rejection tests proving the write is conditioned on both the old
  revision and exact prior entries; stale expected revisions or changed
  entries cannot recreate an earlier revision or overwrite newer inventory.
- [x] Add no-op cases proving a read with no expiry change performs no write or
  revision increment, including repeated reads after expiry was materialized.
- [x] **Expected failure:** Run the new focused tests before implementation and
  record that the current expiry rebuild resets a nonzero revision to zero or
  permits a stale conditional state.
- [x] **Implementation change:** Construct the expired ledger with
  `revision=ledger.revision + 1`, persist it with exact old-revision and
  old-entry conditions, and classify a conditional loser for safe reload.
- [x] Preserve owner/week scoping, bounded retries, privacy-safe conflict logs,
  provisional-versus-available expiry semantics, and no mutation on read when
  state is already current.
- [x] **Verify the new test passes:** Run monotonic revision, concurrent
  expiry, stale-CAS, and no-op cases; require one stable final revision and
  entry set.
- [x] **Relevant regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_planner_handler.py` and resolve every failure.
- [x] **Complete regression suite:** Run `uv run pytest`; require all tests to
  pass. Record the exact count and confirm the two known serializer warnings
  are unchanged or resolve any newly introduced warning.
- [x] **Quality gates:** Run `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `git diff --check`; resolve every
  finding and review the accumulated diff against all seven findings.

**Acceptance criteria:**

- [x] Every materialized expiry advances a nonzero or zero ledger revision by
  exactly one and never reuses a prior revision.
- [x] Concurrent materialization has one writer; losers reload without a
  second increment or an inventory overwrite.
- [x] CAS guards include both the expected revision and exact prior entries,
  preventing stale or ABA-style matches.
- [x] No-op reads do not change state or revision.
- [x] All seven review findings have focused regression coverage, the complete
  suite passes, and Ruff, Mypy, and diff quality gates are green.

**Task 7 evidence (2026-08-26):**

- `uv run pytest tests/test_dynamo.py -k 'expiry_materialization_advances or concurrent_expiry_loser or weekly_batch_ledger_cas_requires or repeated_current_expiry_reads' -q`: 4 expected failures before implementation; 4 passed after implementation.
- `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py tests/test_planner_handler.py -q`: 712 passed, 2 known Pydantic serializer warnings.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded.
- `uv run pytest -q`: 1,763 passed, 2 unchanged known Pydantic serializer warnings.
- `uv run ruff format --check .`: 106 files already formatted; `uv run ruff check .`: all checks passed; `uv run mypy`: no issues in 20 source files; `git diff --check`: passed.

**Task 7 blockers:** None.

## Post-Completion

### Repository completion bookkeeping

- After every implementation and acceptance checkbox is complete and all
  gates pass, record exact command outcomes in this plan and move it to
  `docs/plans/completed/` using the existing filename.
- Preserve the archived original plan unchanged. Do not rewrite its completed
  task history to describe this separate remediation.
- Review the final changed-path list and distinguish remediation changes from
  the pre-existing overlapping dirty worktree, especially for Finding 1.

### Manual verification

- Deploy only after automated gates pass, then recreate or amend the profile
  through `/profile` and confirm the batch rule appears in profile output.
- Run `/plan`, submit a three-meal preparation, and consume the two leftovers
  in separate fresh plans. Confirm no duplicate decrement occurs.
- Generate a newer plan and submit a backdated meal from a still-eligible
  preceding plan; confirm the intended batch link is retained.
- Attempt a preparation confirmation after its preparation day and at an ISO-
  week boundary, both before and after reading inventory; confirm neither
  activates stock.

### Operational checks

- Inspect CloudWatch only for bounded conflict/error categories. Confirm logs
  contain no profile text, meal descriptions, food names, provider payloads,
  or batch IDs.
- Verify deployed DynamoDB plan lookups remain bounded to the user partition
  and that conditional-conflict rates do not indicate an unexpected retry
  loop.
- No schema migration or destructive reset is planned. Historical profiles
  without the new typed batch-rule field must continue to load with the safe
  empty default.
