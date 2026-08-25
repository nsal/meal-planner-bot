# Remediate Dietary Profile Rule Follow-Up Review Findings

## Overview

Remediate the six actionable findings from the independent review of the
completed dietary-profile rule remediation. The follow-up must close four P1
safety, identity, and concurrency defects and two P2 weekday correctness
defects without weakening the existing fail-closed planner workflow.

The work will replace permissive legacy-constraint classification, prevent
incomplete semantic expansions from being treated as safe, preserve unique
effective-rule IDs for partial-scope maxima, make ordinary profile writes
revision-conditional, reject impossible per-weekday rules before generation,
and retain missing weekdays in repair locations.

## Context

- **Completed original plan:**
  `docs/plans/completed/2026-08-24-prioritize-dietary-profile-rules.md`.
- **Completed first remediation plan:**
  `2026-08-24-prioritize-dietary-profile-rules-review-remediation.md` in
  `docs/plans/`.
- **Current implementation result:** Tasks 1-7 of the first remediation
  completed on their first attempts. Ruff format and lint, Mypy, the full
  1,158-test Pytest suite, and `git diff --check` passed. The suite emitted two
  known Pydantic serializer warnings.
- **Constraint boundary:** `src/meal_planner/dietary_rules.py` owns semantic
  phrase expansion, literal-term classification, fail-closed outcomes, and
  priority resolution.
- **Dispatch boundary:** `src/meal_planner/bot_handler.py` prepares stored and
  current rules, snapshots effective rules, persists retry state, and invokes
  the asynchronous planner.
- **Persistence boundary:** `src/meal_planner/db/dynamo.py` uses
  `profile_revision` for guarded confirmation transactions, but ordinary
  `save_profile` writes are not conditional.
- **Validation boundary:** `src/meal_planner/models/schemas.py` validates rule
  scope, `src/meal_planner/preferences.py` evaluates weekday evidence, and
  `src/meal_planner/planner_handler.py` serializes bounded repair locations.
- The worktree contains the original implementation and first-remediation
  changes. Preserve that dirty baseline and the completed-plan hash during any
  separately authorized execution.

## Review Findings Covered

1. **P1:** Reject unregistered dietary labels instead of treating them as
   literal foods.
2. **P1:** Do not mark incomplete semantic expansions as safe.
3. **P1:** Preserve unique IDs when a scoped maximum remains beside its stored
   rule.
4. **P1:** Make ordinary profile writes participate in revision concurrency
   control.
5. **P2:** Reject impossible per-weekday counts before invoking the planner.
6. **P2:** Retain the weekday in repair locations for meal-unscoped rules.

## Scope and Deviations

- **In scope:** focused production fixes and regression tests for all six
  findings, followed by repository-wide quality gates.
- **Out of scope:** new dietary features, probabilistic food classification,
  nutritional or religious certification, cross-contamination claims, data
  backfills, deployment, GitHub issue creation, commits, pushes, and unrelated
  refactoring.
- The tasks are ordered by dependency and severity. Tasks 1 and 2 establish a
  closed constraint vocabulary before the remaining P1 defects. Task 5 fixes
  rule feasibility before Task 6 improves downstream repair metadata.
- Semantic label support is all-or-nothing. Keep a label dispatchable only if
  its reviewed expansion fully represents the application's bounded,
  declared-ingredient contract. Otherwise mark it uninterpretable and request
  clarification before planner dispatch.
- In particular, keep finite, reviewed phrases such as a peanut allergy and
  dairy-free only when their complete expansions are asserted in tests. Add
  direct `gluten` evidence to the gluten-free contract. Treat broad `vegan`
  wording as uninterpretable until the application can provide a complete
  reviewed expansion; do not retain a knowingly partial denylist.
- Preserve compatibility only where it is safe. Short unknown labels must not
  become literal food exclusions merely because they contain few words.
- Do not modify either preceding plan while implementing this follow-up.
  Record implementation-time scope deviations only in this plan.

## Development Approach

- **Testing approach: TDD.** For each numbered task, add the smallest focused
  regression first, run it against the current implementation, and record the
  expected failure before changing production code.
- Complete one task and its acceptance criteria before starting the next.
  Rerun the focused test and the listed regression modules at each boundary.
- Use closed, application-owned registries and deterministic classifications.
  Never infer safety from phrase length, provider IDs, or incomplete aliases.
- Keep profile writes atomic and optimistic. Conditional conflicts must have a
  controlled application outcome; non-conditional DynamoDB errors must remain
  visible.
- Validate impossible user intent before planner invocation. Repair remains a
  bounded response to a candidate defect, not a substitute for rejecting an
  impossible request.
- Maintain full type hints, PEP 8, Ruff's 80-column format, strict Mypy, and
  `uv run` for Python tools. No new dependency is expected.
- Mark a checklist item `[x]` only after the work and its tests pass. Add a
  `➕` item for newly discovered in-scope work and a `⚠️` note for blockers
  or approved deviations.

## Testing Strategy

- **Constraint classification:** table-drive known literal foods, finite
  registered phrases, and one- or two-word dietary labels. Pair bot cases with
  planner-spy assertions proving uncertain labels never dispatch.
- **Semantic expansion safety:** test every term in each retained phrase
  expansion and representative previously omitted evidence. Removed broad
  labels must clarify before generation rather than reach publication.
- **Effective-rule identity:** cover a stored Monday-Friday rule plus a current
  Wednesday maximum through the complete bot-to-planner event boundary.
- **Concurrency:** simulate both profile-write interleavings. The new inverse
  case must pause a stale ordinary writer after its read, commit confirmation,
  then prove the stale write loses its condition and preserves the winner.
- **Weekday feasibility:** combine operator, strength, meal scope, and weekday
  scope. Strict impossible minima and exact counts must clarify; maxima,
  best-effort rules, and valid counts retain their existing behavior.
- **Repair metadata:** inspect the actual asynchronous repair event for an
  unmet meal-unscoped weekday rule and preserve existing meal-scoped and
  non-weekday locations.
- Tests must not use skips, xfails, live AWS, live Telegram, network calls, or
  live LLM requests.

## Solution Overview

Replace `_known_literal_term` with a closed classification boundary. A term is
safe only when it is a registered literal food or a semantic phrase with a
reviewed complete expansion. Unknown dietary labels produce an explicit
uninterpretable result that the bot converts into bounded clarification before
planner invocation. Remove broad semantic phrases from the safe registry when
the declared-ingredient matcher cannot represent them completely.

Make `_snapshot_effective_rules` transfer ownership to a current maximum only
when resolution absorbed that current rule. If the scoped current maximum
remains independently in the resolved set, retain both original unique IDs.
Validate snapshot uniqueness before persisting generation state or invoking
the planner.

Change ordinary `save_profile` writes to compare the observed
`profile_revision` in the write condition. New-profile creation must condition
on absence; updates must condition on the exact observed revision. Return a
controlled conflict result to callers while preserving propagation of other
DynamoDB failures.

Finally, validate strict weekday `exactly` and `at_least` counts against the
capacity of one named day before dispatch. A meal-scoped rule has one eligible
slot per day; a meal-unscoped rule uses the application's bounded daily meal
count. When a valid rule later misses a weekday, emit `days[N]` even when no
meal type is present.

## Technical Details

### Closed constraint vocabulary

- Replace phrase-shape guessing with an immutable reviewed literal-food
  registry and the existing semantic phrase registry.
- Normalize lookup keys with the same whole-word food normalizer used by
  matching. Aliases may canonicalize to registered literal or semantic keys,
  but an unknown key must remain unknown.
- Keep `ConstraintExpansionResult.is_safe` true only when every input receives
  a complete deterministic expansion and at least one matchable term exists.
- Audit every retained semantic mapping. Include `gluten` itself in the
  gluten-free expansion. Remove `vegan` from safe expansion and route it to
  clarification until a complete declared-evidence contract exists.

### Effective-rule ownership

- Treat IDs as ownership, not merely sort keys. Resolution output that already
  contains the current maximum proves it was not fully absorbed.
- Transfer the current maximum's ID to a capped stored obligation only when no
  rule with that current ID remains in the snapshot and the resolver produced
  exactly one compatible capped stored result.
- Reject any duplicate-ID snapshot before state transition and planner
  invocation with the existing controlled combination message.

### Profile revision conditions

- Read and canonicalize the observed profile, calculate the next revision, and
  issue one conditional `put_item`.
- For an existing profile, require the stored revision to equal the observed
  revision. For initial creation, require the profile key or revision
  attribute not to exist.
- Map only DynamoDB conditional-check failure to a normal conflict result.
  Preserve the latest profile and onboarding/profile draft so the user can
  retry safely. Re-raise all other client errors.

### Weekday capacity and repair paths

- Centralize the bounded per-day capacity calculation used by rule validation.
  Meal-scoped capacity is one; meal-unscoped capacity is the number of
  supported daily meal types.
- Reject only strict `exactly` or `at_least` weekday rules whose count exceeds
  per-day capacity. Preserve `at_most`, best-effort, and unscoped aggregate
  semantics.
- Serialize a day-only validation issue as `days[day - 1]`; append the meal
  path only when `meal_type` is present.

## Implementation Steps

### Task 1: Replace permissive literal constraint classification

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `tests/test_dietary_rules.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `_known_literal_term`, `expand_constraint_terms`,
`normalize_constraint_entry`, constraint preparation before planner dispatch

**TDD sequence:**

- [x] **Failing test first:** Add table-driven expansion tests for the short
  labels `vegetarian`, `halal`, `kosher`, and `low sodium`. Assert each is
  unknown or uninterpretable, never a literal forbidden term.
- [x] Add positive literal-food controls such as `peanut`, `shellfish`, and a
  reviewed multiword food. Assert registered literals still normalize and
  match with stable deduplication and whole-word behavior.
- [x] Add bot-level legacy-profile cases for every unknown label. Spy on the
  planner invocation and assert bounded clarification, no generating state,
  and no planner dispatch.
- [x] **Expected pre-fix failure:** Run the focused tests and record that
  `_known_literal_term` accepts the short labels, stores them in
  `forbidden_terms`, and permits generation.
- [x] **Implementation change:** Replace the phrase-length/prose-marker
  heuristic with an immutable reviewed literal-food registry or equivalent
  closed classifier. Keep semantic phrases in their separate reviewed
  registry and return explicit unknown terms for every unregistered input.
- [x] Preserve canonical aliases, punctuation normalization, malformed-input
  handling, bounded unknown values, deterministic order, and privacy-safe
  clarification.
- [x] **Passing-test verification:** Run the new classification and bot
  dispatch tests and require all cases to pass without skip or xfail.
- [x] **Regression tests:** Run `uv run pytest tests/test_dietary_rules.py
  tests/test_bot_handler.py` and resolve every failure before Task 2.

**Acceptance criteria:**

- [x] No one- or two-word dietary label is trusted solely because it is short.
- [x] Only registered literal foods and complete registered semantic phrases
  can become enforceable terms.
- [x] Unknown labels produce controlled clarification before planner dispatch.
- [x] Existing reviewed literal foods, aliases, and whole-word matching remain
  green.

### Task 2: Remove incomplete semantic constraint expansions

**Severity:** P1; depends on Task 1

**Files:**

- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `tests/test_dietary_rules.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `CONSTRAINT_PHRASE_REGISTRY`,
`CONSTRAINT_ALIAS_REGISTRY`, `ConstraintExpansionResult.is_safe`, constraint
validation and publication gate

**TDD sequence:**

- [x] **Failing test first:** Add expansion and publication regressions for a
  gluten-free constraint with a declared `gluten` ingredient. Assert the term
  is matched and the candidate is repaired or rejected without publication.
- [x] Add parameterized vegan safety cases for `cheese`, `butter`, `shellfish`,
  `gelatin`, and `honey`. Under the selected fail-closed contract, assert
  `vegan` is uninterpretable and none of these requests reaches generation or
  publication.
- [x] Add exhaustive table-driven tests over every retained semantic phrase
  and every term in its expansion. Prove each declared term matches and no
  retained phrase has an empty, partial, or malformed expansion.
- [x] **Expected pre-fix failure:** Run the focused tests and record that
  gluten-free omits direct `gluten` and that the partial vegan denylist is
  marked safe, allowing omitted animal-derived terms to pass.
- [x] **Implementation change:** Add direct `gluten` to the reviewed
  gluten-free expansion. Remove `vegan` from the safe phrase registry and
  classify it as uninterpretable until a complete reviewed contract exists.
  Apply the same all-or-nothing rule to any other audited partial mapping.
- [x] Ensure retained finite mappings, including peanut-allergy and dairy-free
  phrases, remain immutable, deterministic, de-duplicated, and directly
  enforceable against meal names and declared ingredients.
- [x] **Passing-test verification:** Run all new expansion, no-dispatch, and
  publication-safety tests and require every omitted-term scenario to fail
  closed.
- [x] **Regression tests:** Run `uv run pytest tests/test_dietary_rules.py
  tests/test_bot_handler.py tests/test_planner_handler.py` before Task 3.

**Acceptance criteria:**

- [x] No semantic phrase is marked safe with a knowingly incomplete
  expansion.
- [x] A gluten-free constraint rejects a declared `gluten` ingredient.
- [x] Broad vegan wording requests clarification and never invokes the planner
  while its representation is incomplete.
- [x] Every retained semantic expansion has exhaustive registry coverage.
- [x] A violating candidate is never saved or displayed.

⚠️ **Scope deviation:** Updated the existing `tests/test_schemas.py` legacy
normalization assertions because they encoded the superseded safe `vegan`
denylist and omitted direct `gluten` evidence. No additional production files
were required.

### Task 3: Preserve IDs for partial-scope maximum resolution

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `BotHandler._snapshot_effective_rules`, priority-resolution
dispatch, `ConversationState.effective_rules`, `PlanGenerationContext`

**TDD sequence:**

- [x] **Failing test first:** Add a full bot-to-planner test with a stored egg
  rule scoped Monday-Friday and a current `at_most` rule scoped Wednesday.
  Assert both resolved fragments remain present with distinct stable IDs.
- [x] Assert the persisted `ConversationState`, invoked event, and parsed
  `PlanGenerationContext` contain the same unique IDs and deterministic order.
  Prove generation reaches the asynchronous planner without a misleading
  length or validation error.
- [x] Add controls for a broad current maximum that is fully absorbed into one
  capped stored obligation and for unrelated maxima. Assert ownership transfer
  occurs only in the absorbed case and retries preserve the snapshot.
- [x] **Expected pre-fix failure:** Run the focused tests and record that
  `_snapshot_effective_rules` copies the current ID onto the stored fragment
  while retaining the current rule, creating duplicate effective IDs and an
  invalid planner context.
- [x] **Implementation change:** Detect whether the current maximum remains in
  the resolver output. Rewrite capped ownership only when that current ID was
  fully absorbed and exactly one compatible stored result exists; otherwise
  preserve both original IDs and scopes.
- [x] Add a deterministic uniqueness check before generation state is saved or
  the planner is invoked. Map an impossible duplicate to the existing bounded
  rule-combination clarification, without announcing a started generation.
- [x] **Passing-test verification:** Run the new partial-scope, absorbed-scope,
  and retry tests and require unique IDs at every serialized boundary.
- [x] **Regression tests:** Run `uv run pytest tests/test_bot_handler.py
  tests/test_planner_handler.py` before Task 4.

**Acceptance criteria:**

- [x] A retained current maximum and stored fragment never share an ID.
- [x] Ownership changes only when the current maximum is actually absorbed.
- [x] Effective IDs and ordering remain stable through state, event, planner
  parsing, and retry serialization.
- [x] No invalid context is dispatched after the user is told generation
  started.
- [x] Existing full-scope capping and unrelated-rule behavior remains green.

### Task 4: Make ordinary profile saves revision-conditional

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `DynamoRepository.save_profile`, profile onboarding completion,
`UserProfile.profile_revision`, DynamoDB conditional-error handling

**TDD sequence:**

- [x] **Failing test first:** Add an inverse-interleaving DynamoDB test that
  lets ordinary `save_profile` observe revision N, pauses it, commits a profile
  confirmation at N+1, and then completes the stale ordinary write.
- [x] Assert the stale save reports a controlled conflict and cannot erase the
  confirmed constraint, restore a removed preference, or replace revision
  N+1. Add a mirror case for two competing ordinary saves.
- [x] Add creation, successful update, legacy revision-zero, and non-conditional
  client-error tests. Assert initial creation is race-safe, successful writes
  increment exactly once, conditional conflicts are controlled, and other
  errors propagate.
- [x] Add a bot onboarding/profile-save conflict test. Assert the latest
  profile survives, the draft remains retryable, and the user receives bounded
  stale-state feedback instead of a false success message.
- [x] **Expected pre-fix failure:** Run the concurrency cases and record that
  the unconditional `put_item` lets the stale ordinary writer overwrite the
  transaction winner with its own N+1 document. The pre-fix focused run
  produced six failures: `save_profile` returned `None`, creation and update
  writes had no controlled conflict outcome, and the bot reported success
  without preserving a retry draft.
- [x] **Implementation change:** Make `save_profile` perform a conditional
  `put_item`: require absence for creation and exact equality with the observed
  `profile_revision` for update. Return a typed or boolean conflict outcome
  while re-raising non-conditional DynamoDB errors.
- [x] Update every application caller of `save_profile` to handle conflict
  without discarding retry state or claiming success. Keep the guarded
  confirmation transaction and unrelated profile fields unchanged.
- [x] **Passing-test verification:** Run the inverse-interleaving and bot
  conflict tests and require the latest committed profile to survive exactly.
- [x] **Regression tests:** Run `uv run pytest tests/test_dynamo.py
  tests/test_bot_handler.py` before Task 5.

**Acceptance criteria:**

- [x] Every application write to the canonical profile item participates in
  revision concurrency control.
- [x] A stale ordinary save cannot overwrite a later confirmation or ordinary
  save.
- [x] Creation and revision-zero legacy records are handled deterministically.
- [x] Conditional conflicts preserve retryable state and produce controlled
  feedback; other DynamoDB errors remain visible.
- [x] Existing confirmation replay, conflict removal, and profile round trips
  remain green.

### Task 5: Reject impossible strict per-weekday counts before dispatch

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify if needed: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_preferences.py`

**Symbols:** `DietaryRule.validate_count_for_scope`, interpretation validation,
planner-dispatch preparation, `_weekday_rule_counts`

**TDD sequence:**

- [x] **Failing test first:** Add schema and parser tests for strict `exactly
  2` and `at_least 2` egg breakfasts on Monday and Wednesday. Assert both are
  rejected because each named day has only one breakfast slot.
- [x] Add bot-level versions of the impossible rules. Assert bounded
  clarification is persisted, generation state is not entered, and the
  planner client is never invoked.
- [x] Add meal-unscoped per-day capacity cases plus controls for strict valid
  counts, `at_most`, best-effort, and rules without weekdays. Assert only
  impossible strict per-day minima or exact counts are rejected.
- [x] Add validation consistency tests proving every rule accepted at the
  input boundary uses the same per-day capacity semantics as
  `_weekday_rule_counts` and does not become an unavoidable
  `impossible_rule_count` after generation.
- [x] **Expected pre-fix failure:** Run the focused tests and record that
  `DietaryRule.validate_count_for_scope` compares a meal-scoped count with the
  number of named weekdays, accepts count two across two days, and dispatches
  a request that no plan can satisfy independently on each day.
- [x] **Implementation change:** Centralize bounded daily capacity and validate
  strict weekday `exactly` and `at_least` rules against one named day's
  capacity. Use one slot for a selected meal type and the supported daily meal
  count when `meal_type` is absent.
- [x] Ensure parser validation errors become the existing focused
  clarification. Preserve valid maxima, best-effort flexibility, unscoped
  aggregate counts, stable IDs, and retry behavior.
- [x] **Passing-test verification:** Run the new schema, parser, bot, and
  preference tests and require every impossible strict rule to stop before
  planner invocation.
- [x] **Regression tests:** Run `uv run pytest tests/test_schemas.py
  tests/test_parser.py tests/test_bot_handler.py tests/test_preferences.py`
  before Task 6.

**Acceptance criteria:**

- [x] A strict weekday rule cannot request more eligible meals per named day
  than the plan contract provides.
- [x] Impossible input produces clarification and no planner invocation.
- [x] Input validation and candidate validation use consistent capacity
  semantics.
- [x] Valid strict counts, maxima, best-effort rules, and non-weekday aggregate
  rules remain accepted.

⚠️ **Scope deviation:** Updated existing cross-weekday priority and planner
boundary fixtures in `tests/test_dietary_rules.py`, `tests/test_bot_handler.py`,
and `tests/test_planner_handler.py` to use rules valid under the new per-day
capacity contract. No production code outside the Task 5 schema and
preference-validation boundaries was changed.

### Task 6: Preserve day-only locations in repair feedback

**Severity:** P2; depends on Task 5

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `PlannerHandler._validation_location`,
`PlannerHandler._validation_feedback`, asynchronous repair-event construction

**TDD sequence:**

- [x] **Failing test first:** Add an unmet strict weekday rule with
  `meal_type=None` and assert `_validation_feedback` emits
  `location=days[N]` for the missing named day rather than `location=rules`.
- [x] Add an asynchronous planner integration test that reaches the one repair
  attempt and inspects the emitted `repair_feedback`. Assert the day-only
  location survives event serialization without raw rule text.
- [x] Add controls for weekday-plus-meal locations, rule issues without a day,
  constraint locations, legacy requirement locations, and plan-level issues.
  Assert their existing bounded paths are unchanged.
- [x] **Expected pre-fix failure:** Run the focused tests and record that
  `_validation_location` requires both day and meal type, so a day-only rule
  falls through to `rules` and loses the missing weekday.
- [x] **Implementation change:** When `issue.day` exists, always begin with the
  bounded zero-based `days[day - 1]` path; append `.meals.<type>` only when a
  meal type exists. Preserve the existing precedence for non-day issues.
- [x] Keep feedback length limits, issue codes, privacy boundaries, and the
  single automatic repair lifecycle unchanged.
- [x] **Passing-test verification:** Run the new unit and asynchronous repair
  tests and require the expected day-only location in both outputs.
- [x] **Regression tests:** Run `uv run pytest
  tests/test_planner_handler.py` before final verification.

**Acceptance criteria:**

- [x] Meal-unscoped weekday failures retain their exact bounded day location.
- [x] Meal-scoped weekday paths remain `days[N].meals.<type>`.
- [x] Non-day rule, constraint, requirement, and plan locations remain stable.
- [x] Repair feedback contains no raw profile wording or meal content.
- [x] The existing one-repair and terminal no-save/no-display behavior remains
  green.

⚠️ **Scope deviation:** None. The focused pre-fix run failed the new day-only
location unit and asynchronous repair tests as expected; no unrelated task
definitions or production boundaries were changed.

## Verification Requirements

After all six tasks are complete:

- [x] Confirm each review finding has at least one regression that failed
  before its implementation change and passes afterward.
- [x] Run `uv run ruff format .`, then `uv run ruff format --check .`, and
  require success with the configured 80-column limit.
- [x] Run `uv run ruff check .` and require no lint findings.
- [x] Run `uv run mypy` and require no type errors.
- [x] Run `uv run pytest` and require the full suite to pass. Record warning
  output and distinguish the two known Pydantic serializer warnings from any
  newly introduced warning.
- [x] Run `git diff --check` and require success.
- [x] Review the accumulated diff against both preceding plans, repository
  conventions, correctness, regressions, security, error handling, privacy,
  concurrency, and test coverage.
- [x] Confirm
  `docs/plans/completed/2026-08-24-prioritize-dietary-profile-rules.md`
  retains hash
  `70e9908c6f1ade37b4c0a2276ff85f22cdf0d7b2679a56510cefc2c29a356fa8`.
- [x] Confirm the first remediation plan was changed only to record its own
  previously completed work and was not altered by this follow-up.
- [x] Confirm that no approved deviations remain before moving this plan to
  `docs/plans/completed/`.

## Post-Completion

**Manual verification**

- In a non-production Telegram environment, attempt plan generation with each
  unsupported short dietary label and verify clarification appears before any
  generation-started message.
- Generate a gluten-free candidate containing a declared `gluten` ingredient
  and verify it is never displayed or saved.
- Exercise a stored multi-weekday rule with a narrower current maximum and
  verify the request reaches generation with unique effective-rule IDs.
- Simulate competing profile updates and verify the stale writer receives a
  retry response while the latest profile remains intact.

**External system updates**

- No GitHub issue, commit, push, pull request, deployment, or external action
  is part of this planning task.
- A separately authorized implementation must use a dedicated branch, a
  Conventional Commit containing issue `#61`, and a pull request rather than
  pushing or merging directly to `master`.
- After implementation, comment on issue `#61` with the commit or pull request
  link and final verification results.
