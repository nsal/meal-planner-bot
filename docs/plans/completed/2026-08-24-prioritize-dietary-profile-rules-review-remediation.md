# Remediate Dietary Profile Rule Review Findings

## Overview

Remediate the seven actionable findings from the independent review of the
completed dietary-profile rule implementation. The work must close four P1
rule-enforcement gaps and three P2 correctness or concurrency gaps without
weakening the fail-closed planner workflow.

The remediation will make legacy constraints semantically enforceable, keep
structured rules out of the legacy exact-count channel, assign stable
application-owned rule IDs, interpret raw stored preferences before planning,
preserve explicit parsing semantics, protect profile confirmation against
concurrent writes, and enforce strict rules on every named weekday.

## Context

- **Completed original plan:**
  `docs/plans/completed/2026-08-24-prioritize-dietary-profile-rules.md`.
- **Original workflow result:** Tasks 1-12 completed. Ruff format and lint,
  Mypy, the full 1,112-test Pytest suite, and `git diff --check` passed. The
  suite emitted two existing Pydantic serializer warnings.
- **Reviewed areas:** `src/meal_planner/models/schemas.py`,
  `src/meal_planner/bot_handler.py`, `src/meal_planner/llm/parser.py`,
  `src/meal_planner/db/dynamo.py`, and
  `src/meal_planner/preferences.py`.
- **Supporting rule boundary:** `src/meal_planner/dietary_rules.py` owns the
  reviewed term registry, normalization, expansion, and rule-resolution
  behavior that should remain the canonical source of dietary semantics.
- **Primary tests:** `tests/test_schemas.py`, `tests/test_dietary_rules.py`,
  `tests/test_bot_handler.py`, `tests/test_parser.py`,
  `tests/test_dynamo.py`, `tests/test_preferences.py`, and
  `tests/test_planner_handler.py`.
- The implementation worktree predates this remediation plan. Preserve all
  existing implementation, test, documentation, and completed-plan changes
  until an authorized remediation execution begins.

## Review Findings Covered

1. **P1:** Fail closed for unstructured legacy constraints.
2. **P1:** Stop converting generalized rules into exact-count requirements.
3. **P1:** Assign application-owned IDs before combining rule tiers.
4. **P1:** Interpret raw stored preferences before planning.
5. **P2:** Preserve explicit operators and flexible wording during parsing.
6. **P2:** Guard the profile snapshot inside the confirmation transaction.
7. **P2:** Enforce every named weekday for strict weekday rules.

## Scope and Deviations

- **In scope:** focused production fixes and regression tests for all seven
  findings, followed by repository-wide quality gates.
- **Out of scope:** new dietary features, prompt redesign beyond the parsing
  defect, profile UI changes, deployment, data backfills not required for safe
  reads, GitHub issue creation, and unrelated refactoring.
- The tasks are ordered by severity and dependency. Task 4 is P2 but precedes
  Task 5 because stored raw preferences must be interpreted with corrected
  operator and strength precedence.
- Preserve compatibility only where it remains safe. Unknown semantic legacy
  constraints and uninterpretable stored strict preferences must block plan
  generation instead of being guessed, ignored, or treated as literal terms.
- Do not modify the completed original plan. Record implementation-time scope
  deviations only in this remediation plan.

## Development Approach

- **Testing approach: TDD.** For each task, add the smallest regression first,
  run it against the current implementation, and record the expected failure.
- Implement only after the new test demonstrates the finding. Rerun the
  focused test and relevant regression modules before starting the next task.
- Use application-owned, deterministic semantics at persistence and dispatch
  boundaries. Do not trust provider-generated IDs or raw prose as validated
  dietary contracts.
- Maintain fail-closed behavior from profile ingestion through planner
  publication. Controlled clarification or retry states must not invoke the
  planner with incomplete safety data.
- Keep structured rules in `effective_rules`; retain legacy
  `PreferenceRequirement` values only for genuinely legacy planner events.
- Preserve atomic DynamoDB writes, replay safety, unrelated profile fields,
  stable ordering, and existing controlled user-facing failures.
- Use `uv run` for Python tools, Ruff as the only formatter, type all Python
  changes, and keep formatted text within the project 80-column standard.
- Mark a task complete only after its focused and regression tests pass.

## Testing Strategy

- **Boundary-first integration tests:** cover normalization, profile
  persistence, bot dispatch, planner payload construction, validation, repair,
  and terminal publication rather than testing only isolated models.
- **Safety tests:** pair each accepted legacy or structured rule with a meal
  that violates it and prove publication is blocked.
- **Identity tests:** cover sequential confirmations, stored/current
  collisions, stable persistence, and canonical-profile uniqueness.
- **Parser tests:** combine positive-request wording, explicit operators and
  counts, flexible wording, malformed input, and ambiguity handling.
- **Concurrency tests:** simulate a profile mutation after the confirmation
  read but before the transaction while conversation state remains unchanged.
- **Weekday tests:** cover full and partial named-day satisfaction and preserve
  meal-scope, non-weekday, and best-effort behavior.
- New tests must not use skips, xfails, live AWS, live Telegram, network calls,
  or live LLM requests.

## Solution Overview

Normalize recognized legacy constraints through the reviewed dietary-term
registry and represent unknown semantic prose as uninterpretable. A profile
with uninterpretable safety input must enter controlled clarification and must
not dispatch or publish a plan.

At the application boundary, replace provider IDs with stable IDs owned by the
profile or request tier. Enforce uniqueness when canonicalizing profiles and
again when combining stored and current rules. Interpret raw stored
preferences before priority resolution so every strict preference has a typed
rule or blocks planning.

Remove the compatibility conversion from generalized positive rules to exact
requirements. Structured maxima, minima, exact values, exclusions, and
best-effort rules flow only through `effective_rules`; legacy requirements are
accepted only when an incoming event truly uses the old contract.

Preserve explicit parser intent by applying the default `at_least 1` rule only
when no operator or count was supplied, and let explicit omission/flexibility
language determine best-effort strength. Protect profile confirmation with a
revision or equivalent snapshot condition in the same DynamoDB transaction.
Finally, evaluate each strict weekday rule independently for every named day.

## Implementation Steps

### Task 1: Fail closed for semantic legacy constraints

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dietary_rules.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** legacy constraint normalization, `ConstraintEntry`, profile
collection, planner dispatch, constraint validation

**TDD sequence:**

- [x] **Failing or missing test first:** Add parameterized normalization tests
  for `"allergic to peanuts"`, `"gluten-free"`, and `"vegan"`. Assert that
  recognized phrases resolve through the reviewed registry to enforceable
  forbidden terms rather than being copied verbatim.
- [x] Add end-to-end profile-to-planner cases whose generated meals contain
  peanuts, wheat, or chicken. Assert each corresponding legacy constraint
  blocks publication and enters the existing repair or terminal safety path.
- [x] Add unknown semantic-constraint cases. Assert unknown phrases are marked
  uninterpretable, trigger controlled clarification, and never dispatch the
  planner or validate a plan as safe.
- [x] **Expected failure:** Run the new focused cases before implementation and
  record that the current code stores raw phrases in `forbidden_terms`, misses
  the violating ingredient, or proceeds with generation.
- [x] **Implementation change:** Route legacy and onboarding constraint strings
  through the canonical dietary-term registry. Expand only recognized terms
  and aliases; preserve alternatives according to the existing registry
  contract.
- [x] Represent unrecognized constraint prose explicitly as uninterpretable at
  the normalization boundary, and make profile collection or planning fail
  closed with bounded clarification. Do not guess a forbidden term from raw
  text.
- [x] Preserve canonical constraints, stable deduplication, legacy safe literal
  terms already recognized by the registry, and raw-text privacy boundaries.
- [x] **Verify the new test passes:** Run the new normalization and end-to-end
  safety tests and require every case to pass without skip or xfail.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_schemas.py
  tests/test_dietary_rules.py tests/test_bot_handler.py
  tests/test_planner_handler.py` and resolve all failures before Task 2.

**Acceptance criteria:**

- [x] Recognized semantic legacy constraints become canonical, matchable
  forbidden terms.
- [x] Plans containing a declared violating ingredient cannot be published.
- [x] Unknown constraint prose causes clarification and no planner dispatch.
- [x] No raw semantic phrase is treated as safety evidence merely because it
  was copied into `forbidden_terms`.
- [x] Existing canonical constraint and alias behavior remains green.

### Task 2: Assign stable application-owned rule IDs

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_schemas.py`

**Symbols:** pending interpretation confirmation, profile canonicalization,
effective-rule combination, `DietaryRule.id`

**TDD sequence:**

- [x] **Failing or missing test first:** Add a sequential-confirmation test in
  which the provider returns `r1` for two unrelated profile additions. Assert
  both rules persist with distinct application-owned IDs and stable ordering.
- [x] Add a planner-dispatch test combining a stored `r1` with an unrelated
  current `r1`. Assert the effective snapshot has unique IDs and dispatch does
  not produce the misleading preference-length error.
- [x] Add canonical-profile tests for duplicate IDs in legacy or malformed
  stored data. Assert deterministic repair or controlled rejection, with no
  silent rule loss or cross-rule identity reuse.
- [x] **Expected failure:** Run the collision tests before implementation and
  record duplicate effective IDs, rejected `ConversationState`, or incorrect
  user-facing length feedback.
- [x] **Implementation change:** Generate or namespace stable IDs at the
  application boundary using canonical rule content plus owning tier/profile
  identity as appropriate. Never persist provider-generated IDs unchanged.
- [x] Enforce rule-ID uniqueness during profile canonicalization and when
  combining constraint, stored-preference, and current-preference tiers.
- [x] Preserve IDs across rereads and retries for the same persisted rule;
  unrelated rules must never share an ID even when provider output does.
- [x] **Verify the new test passes:** Run all new sequential-confirmation and
  stored/current collision tests and require unique, stable IDs.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_schemas.py
  tests/test_dynamo.py tests/test_bot_handler.py` before Task 3.

**Acceptance criteria:**

- [x] Provider IDs cannot collide across separate interpretations or tiers.
- [x] Canonical profiles contain unique, stable application-owned rule IDs.
- [x] Retry snapshots retain the same IDs for the same rules.
- [x] ID collisions never surface as a preference-length error.
- [x] Deduplication, deterministic ordering, and replay safety remain green.

### Task 3: Keep generalized rules out of legacy exact requirements

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_preferences.py`

**Symbols:** planner event construction, `effective_rules`, `requirements`,
`ConversationState`

**TDD sequence:**

- [x] **Failing or missing test first:** Add full bot-dispatch-planner tests for
  a structured `at_most 2` rule. Assert zero, one, or two matches satisfy the
  maximum and no exact-two legacy requirement is emitted.
- [x] Add a best-effort omission case. Assert omission is reported through the
  generalized evidence path but does not trigger strict repair or failure.
- [x] Add a stored-rule plus `"no preference"` request. Assert dispatch succeeds
  with `preference=None`, populated `effective_rules`, and no incompatible
  synthesized requirement.
- [x] Add a genuine legacy-event case proving an explicitly supplied legacy
  `PreferenceRequirement` is still accepted and validated as before.
- [x] **Expected failure:** Run the new integration tests before implementation
  and record exact-count enforcement for maxima, repair for omitted
  best-effort rules, or `ConversationState` rejection when preference is
  absent.
- [x] **Implementation change:** Remove generalized-rule conversion into
  `PreferenceRequirement`. Send all new structured rules only through
  `effective_rules` and reserve `requirements` for genuinely legacy events.
- [x] Ensure retry snapshots and repair payloads preserve the same structured
  rules without reconstructing a legacy exact-count channel.
- [x] **Verify the new test passes:** Run the new bot-to-planner boundary tests
  and require correct maximum, best-effort, and no-current-preference behavior.
- [x] **Relevant regression tests:** Run `uv run pytest
  tests/test_bot_handler.py tests/test_planner_handler.py
  tests/test_preferences.py` before Task 4.

**Acceptance criteria:**

- [x] Structured maxima are never interpreted as exact counts.
- [x] Omitted best-effort rules do not trigger strict repair.
- [x] Stored rules can dispatch when the current request has no preference.
- [x] New structured rules exist only in `effective_rules`.
- [x] Genuinely legacy requirements retain backward-compatible validation.

### Task 4: Preserve explicit parser operator and flexibility semantics

**Severity:** P2; prerequisite for Task 5

**Files:**

- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_parser.py`

**Symbols:** dietary-rule post-processing and positive-request normalization

**TDD sequence:**

- [x] **Failing or missing test first:** Add parameterized parser tests for
  `"I'd like eggs exactly twice if convenient"`, `"please include beans at
  most three times"`, and positive requests without an explicit count.
- [x] Assert explicit `exactly` and `at most` operators and counts survive
  positive-request wording, explicit flexibility produces `best_effort`, and
  only an unqualified positive request defaults to strict `at_least 1`.
- [x] Add precedence tests for strict wording, omission/flexibility wording,
  contradictory strength phrases, malformed counts, and ambiguous operators.
- [x] **Expected failure:** Run the new parser tests before implementation and
  record the current forced strict `at_least` rewrite and lost explicit
  operator or flexibility qualifier.
- [x] **Implementation change:** Apply the positive-request `at_least 1`
  inference only when no explicit operator or count exists. Give explicit
  omission/flexibility language precedence when determining strength.
- [x] Preserve bounded clarification for contradictory, malformed, ambiguous,
  unknown, unmatchable, incomplete, or partial interpretations.
- [x] **Verify the new test passes:** Run the new parser parameterization and
  require exact operator, count, and strength preservation.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_parser.py
  tests/test_prompts.py` before Task 5.

**Acceptance criteria:**

- [x] Positive-request wording cannot overwrite an explicit operator or count.
- [x] `"if convenient"` and equivalent explicit omission wording yield
  best-effort strength even when the sentence starts with `"I'd like"`.
- [x] Unqualified positive requests still infer strict `at_least 1`.
- [x] Contradictory or malformed input still requests bounded clarification.

### Task 5: Interpret raw stored preferences before planning

**Severity:** P1; depends on Tasks 2 and 4

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** profile collection, stored-rule preparation, clarification state,
priority resolution, planner dispatch

**TDD sequence:**

- [x] **Failing or missing test first:** Add onboarding-to-plan and
  legacy-profile-to-plan integrations where a stored preference has
  `rule=None`. Assert it is interpreted into a typed, stable stored rule before
  priority resolution and validation.
- [x] Pair a stored strict preference with a generated violating plan. Assert
  the violation triggers repair or terminal failure and cannot be published.
- [x] Add uninterpretable, ambiguous, and partially interpretable stored text.
  Assert bounded clarification is persisted and no planner invocation occurs.
- [x] Add a retry case proving a successful stored interpretation is snapshotted
  and reused without provider reinterpretation or changed IDs.
- [x] **Expected failure:** Run the new integration tests before implementation
  and record that `rule=None` entries are omitted from `stored_rules`, allowing
  a violating plan to pass or dispatching without deterministic resolution.
- [x] **Implementation change:** Interpret raw stored preferences during safe
  profile collection or planning preparation, before combining rule tiers.
  Canonicalize the result with application-owned IDs from Task 2.
- [x] On interpretation failure, persist a bounded clarification state and stop
  before planner invocation. Do not silently downgrade, discard, or infer an
  unsafe strict rule.
- [x] Preserve unrelated stored rules, tier priority, current-rule ownership,
  retry snapshots, and raw-text privacy boundaries.
- [x] **Verify the new test passes:** Run the new onboarding and legacy-profile
  integration tests and require typed enforcement or controlled clarification.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_parser.py
  tests/test_bot_handler.py tests/test_planner_handler.py` before Task 6.

**Acceptance criteria:**

- [x] Every stored preference is typed before deterministic priority resolution
  or blocks planning with clarification.
- [x] A strict legacy/onboarding preference cannot be ignored during plan
  validation.
- [x] Failed interpretation never invokes the planner.
- [x] Successful interpretation remains stable across retries.
- [x] Existing typed stored preferences are not reinterpreted or changed.

### Task 6: Guard profile confirmation with a transactional snapshot condition

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/models/schemas.py` if a profile revision is used
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** guarded profile/workflow confirmation transaction, profile write
condition, stale-confirmation handling

**TDD sequence:**

- [x] **Failing or missing test first:** Add a DynamoDB transaction test that
  reloads a profile, mutates it concurrently before confirmation, leaves the
  conversation state unchanged, and then attempts confirmation.
- [x] Assert the stale transaction fails conditionally, preserves the
  concurrent profile mutation, does not restore removed preferences, and does
  not bypass a newly added constraint.
- [x] Add success, replay, missing-profile, and stale-conversation cases to
  distinguish profile-snapshot failure from existing workflow-state guards.
- [x] **Expected failure:** Run the new concurrency test before implementation
  and record that the unguarded profile write overwrites the concurrent
  mutation while the conversation condition still succeeds.
- [x] **Implementation change:** Add a profile revision or equivalent complete
  snapshot condition to the same transaction that confirms the workflow.
  Increment or refresh the guard atomically with the profile write.
- [x] Map conditional failure to controlled stale-state handling. Reload the
  latest profile for user feedback; do not automatically replay an obsolete
  interpretation against changed safety data.
- [x] Preserve atomic conflict removal, idempotent confirmation, cancellation,
  existing field values, and unrelated concurrent profile updates.
- [x] **Verify the new test passes:** Run the focused concurrent-profile test
  and require the latest profile to survive unchanged.
- [x] **Relevant regression tests:** Run `uv run pytest tests/test_dynamo.py
  tests/test_bot_handler.py` before Task 7.

**Acceptance criteria:**

- [x] The transaction conditions both workflow state and the profile snapshot.
- [x] A concurrent profile change cannot be overwritten by stale confirmation.
- [x] Newly added constraints and removed preferences cannot be bypassed or
  restored by the stale write.
- [x] Successful confirmation and replay behavior remain deterministic.
- [x] Conditional failures produce controlled stale-state behavior.

### Task 7: Enforce strict rules on every named weekday

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** weekday-scoped evidence collection, strict rule evaluation,
validation summaries

**TDD sequence:**

- [x] **Failing or missing test first:** Add a strict `at_least 1` rule scoped
  to Monday and Wednesday with evidence only on Monday. Assert validation
  fails and identifies Wednesday as missing.
- [x] Add a passing case with evidence on every named weekday and cases for
  exact, minimum, maximum, zero, and meal-scoped rules using multiple named
  weekdays.
- [x] Add best-effort and non-weekday regressions. Assert best-effort misses are
  summarized without invalidation and unscoped aggregate semantics are
  unchanged.
- [x] **Expected failure:** Run the missing-weekday test before implementation
  and record that aggregate filtering lets Monday evidence satisfy both named
  days.
- [x] **Implementation change:** Evaluate each strict weekday-scoped rule
  independently for every named weekday, applying its operator and count to
  that day's distinct-meal evidence. Preserve meal-scope filtering within each
  day.
- [x] Report bounded per-day misses without raw source text. If a parsed rule
  cannot express safe per-day semantics, reject it before planning rather than
  accepting aggregate evidence.
- [x] Preserve alias expansion, alternatives, whole-word matching, distinct
  meal counting, strict/best-effort separation, and legacy completeness.
- [x] **Verify the new test passes:** Run the focused weekday tests and require
  all-day coverage for strict rules.
- [x] **Relevant regression tests:** Run `uv run pytest
  tests/test_preferences.py tests/test_planner_handler.py` before final
  verification.

**Acceptance criteria:**

- [x] A strict rule naming multiple weekdays cannot pass unless every named day
  satisfies the rule.
- [x] The operator and count are evaluated against distinct meals on each day.
- [x] Meal scope is respected independently within each named weekday.
- [x] Best-effort weekday misses remain non-blocking and are summarized.
- [x] Non-weekday and legacy validation behavior remains green.

## Verification Requirements

After all seven remediation tasks are complete:

- [x] Confirm every review finding has a passing regression that failed before
  its implementation change.
- [x] Run `uv run ruff format --check .` and require success.
- [x] Run `uv run ruff check .` and require success.
- [x] Run `uv run mypy` and require no type errors.
- [x] Run `uv run pytest` and require the full suite to pass. Record warning
  output and distinguish the two known Pydantic serializer warnings from any
  newly introduced warning.
- [x] Run `git diff --check` and require success.
- [x] Review the accumulated diff against this plan, repository conventions,
  correctness, regressions, security, error handling, and test coverage.
- [x] Confirm the completed original plan remains unchanged.
- [x] Confirm that no unavoidable deviations remain before moving this plan to
  `docs/plans/completed/`.

## Post-Completion

- No GitHub issue, commit, push, deployment, or external action is part of this
  planning task.
- Remediation implementation must be separately authorized and must preserve
  the existing dirty-worktree baseline.
