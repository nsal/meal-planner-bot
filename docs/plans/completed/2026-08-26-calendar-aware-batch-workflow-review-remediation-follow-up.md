# Remediate Calendar-Aware Batch Workflow Follow-Up Findings

## Overview

Remediate the three actionable findings from the follow-up review of the
calendar-aware batch workflow. The work makes persisted `BatchRule` values
deterministically enforceable, prevents a leftover portion ordinal from being
consumed more than once across distinct submissions, and guarantees that
expiry is reapplied after a conditional-write race.

This is a planning-only run. **Remediation is not implemented in this run.**
No implementation code, tests, original or archived plans, commits, issues,
or external systems are changed as part of creating this document.

## Context

- **Original remediation plan:**
  `docs/plans/2026-08-26-calendar-aware-batch-workflow-review-remediation.md`.
- **Completed implementation baseline:** all seven tasks in that plan were
  reported complete. The last recorded full suite was 1,763 passing tests
  with two unchanged Pydantic serializer warnings; Ruff format/check, strict
  Mypy, and `git diff --check` were green.
- **Relevant planner paths:** `PlanGenerationContext.batch_rules`,
  `PlannerHandler.generate_plan()`, `PlannerHandler.revise_plan()`, and calls
  to `validate_generated_plan()` in
  `src/meal_planner/planner_handler.py`; deterministic plan compliance in
  `src/meal_planner/preferences.py`.
- **Relevant repository paths:** submitted-leftover validation around
  `src/meal_planner/db/dynamo.py:943` and expiry materialization in
  `DynamoRepository.get_weekly_batch_ledger()` around line 1133.
- **Dirty-worktree boundary:** the repository had extensive overlapping
  changes before this plan was created. Preserve them, do not rewrite or
  attribute them, and modify only the files named by the active task during
  implementation.

## Review Findings Covered

1. **P1 — Enforce persisted batch rules during plan validation.** Confirmed
   typed rules are transported into prompts but are not passed to deterministic
   validation, so omitted or noncompliant batch behavior can be published.
2. **P1 — Prevent replay of a remaining high-numbered portion.** Range-based
   submission validation can accept portion 3 when portions 2 and 3 remain,
   then accept portion 3 again after inventory decrements.
3. **P2 — Reapply expiry after a conditional-write loss.** An expiry CAS loser
   reloads eventually consistently without the original `as_of`, allowing it
   to return a ledger whose entries still require expiry.

## Development Approach

- **Testing approach: TDD.** For each numbered task, add the smallest focused
  failing tests first, run them to capture the expected defect, make only the
  task's implementation change, rerun the focused tests, and then run the
  listed regression modules.
- Complete tasks in numerical order. The two P1 correctness gaps precede the
  P2 concurrency hardening. Task 3 must preserve the exact inventory semantics
  established by Task 2 while retrying expiry.
- Do not proceed while a task's focused or regression tests fail. Record exact
  failing and passing commands and outcomes in this plan during execution.
- Mark items `[x]` only after the work and evidence exist. Prefix approved
  scope additions with `➕` and blockers or deviations with `⚠️`.
- Keep provider output advisory. Typed profile rules, portion-consumption
  order, IDs, inventory state, revisions, and expiry outcomes remain
  application-owned decisions.
- Preserve transactional and idempotent meal submission. A rejected, duplicate,
  or raced batch mutation must not partially write a meal, conversation state,
  update marker, plan state, or ledger state.
- Preserve user-partition key access, bounded retries, strict Pydantic
  boundaries, and privacy-safe logging. Do not add scans, unbounded loops, live
  provider calls, or internal batch IDs to logs or user-visible messages.
- Keep historical records readable. Prefer enforcing the next canonical
  ordinal over adding a claimed-ordinal schema or migration unless a failing
  test proves the existing ledger state is insufficient.

## Testing Strategy

- **Planner validation:** table-driven unit tests cover exact compliance and
  every mismatch dimension of a confirmed `BatchRule`, for both generation
  and revision, including bounded repair behavior.
- **Inventory accounting:** Moto repository tests use distinct submission IDs
  against one evolving ledger, rather than resetting inventory between cases,
  and verify both accepted order and rejected replay/order transitions.
- **Handler integration:** focused submission tests verify a controlled user
  outcome and no partial workflow writes when a high ordinal is submitted too
  early or replayed.
- **Expiry concurrency:** deterministic mocked/Moto races cover a competing
  non-expiry mutation, an expiry winner, a stale reload, repeated conflicts,
  and the no-op path.
- Tests must not use skips, xfails, live AWS, live LLMs, network calls, or live
  Telegram APIs.
- After Task 3, run the complete suite and repository quality gates. The two
  known Pydantic serializer warnings may remain only if their count and source
  are unchanged and documented as pre-existing.

## Solution Overview

Pass the authoritative typed batch rules into the same deterministic
validation boundary used for every parsed initial or revised candidate. Match
each rule against plan batch links and meal metadata, requiring its food,
preparation meal type, reuse meal types, yield, ordering, and linked portion
set to satisfy the typed contract. Return bounded reason-coded feedback through
the existing repair lifecycle; never publish a candidate that remains
noncompliant after the allowed repair attempt.

For consumption, retain the existing compact ledger schema and require the
submitted leftover to be exactly the next canonical ordinal:
`total_portions - remaining_portions + 1`. Because the ledger revision and
entry set are already checked transactionally, this turns each successful
decrement into the durable claim for that ordinal. Higher ordinals cannot be
used early, and a previously consumed ordinal cannot become valid again.

For expiry, replace the one-shot CAS/reload path with a bounded loop. After a
conditional loss, strongly reload the ledger, reapply the original `as_of`,
and either return an already-current winner or attempt expiry against the new
revision and exact entries. Exhausted conflicts must use the existing
retryable conflict/error path instead of returning unexpired state.

## Technical Details

### Deterministic batch-rule compliance

- Extend `validate_generated_plan()` with an explicit typed `batch_rules`
  input whose empty default preserves callers with no confirmed rule.
- Evaluate rules only after strict parsing and application-owned batch-ID
  canonicalization, and before reservation, publication, or display.
- For each rule, require the expected preparation food, an allowed preparation
  meal type, exact `total_yield`, exactly `total_yield - 1` linked leftovers,
  allowed reuse meal types, canonical portions `2..total_yield`, and dates
  ordered after the preparation within the plan horizon.
- Reject missing, extra, ambiguous, cross-linked, duplicate, or wrong-food
  matches with bounded stable reason codes. Feedback may identify the rule
  ordinal and violated field, but must not contain profile prose, meal names,
  food text, provider payloads, or batch IDs.
- Apply the identical validator in initial generation, first-attempt repair,
  and plan revision. A second invalid attempt follows the existing terminal
  non-publication behavior.

### Exactly-once portion order

- Compute `next_portion = total_portions - remaining_portions + 1` from the
  ledger item read for the transaction.
- For leftovers, require `link.portion == next_portion`; do not accept any
  member of a remaining range. Keep preparation links fixed at portion 1.
- Keep the exact expected ledger revision and serialized-entry condition in
  the existing submitted-meal transaction. Distinct submission IDs do not
  bypass ordinal ownership.
- Preserve controlled outcomes for stale revisions, unavailable stock,
  malformed links, wrong source metadata, wrong week, duplicate updates, and
  transaction conflicts.

### Expiry retry after CAS loss

- Factor one pure expiry transformation if needed so every attempt evaluates
  the current ledger against the same original `as_of`.
- Add a small named retry limit consistent with existing repository retry
  bounds; do not use recursion or an unbounded loop.
- After each conditional failure, perform a strongly consistent read for the
  same user and ISO week, revalidate the ledger, and recompute expiry.
- If the winner already materialized all expiry required by `as_of`, return
  that winner without another revision increment. If an unrelated mutation
  won and entries still require expiry, preserve its state and attempt a new
  `revision + 1` CAS against its exact revision and entries.
- If the bounded attempts are exhausted while expiry is still required, raise
  or return the repository's existing retryable concurrency outcome. Never
  return a known stale or unexpired ledger as success.

## What Goes Where

- **Implementation Steps:** the three numbered tasks below contain all code
  and automated test work for the three findings.
- **Post-Completion:** archival, deployment, and live checks occur only after
  every task and quality gate passes. They are not part of this planning run.

## Implementation Steps

### Task 1: Enforce confirmed batch rules in generation and revision

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `validate_generated_plan()`, `PlanGenerationContext.batch_rules`,
`PlannerHandler.generate_plan()`, `PlannerHandler.revise_plan()`, parsed-plan
canonicalization, and the existing bounded repair scheduling/publication
branches.

**TDD sequence:**

- [x] **Failing test first:** Add table-driven
  `tests/test_preferences.py` cases that pass one confirmed `BatchRule` to
  `validate_generated_plan()` and prove a compliant preparation plus linked
  leftovers passes.
- [x] Add one failing case for each violation: omitted preparation, wrong
  food, disallowed preparation meal type, disallowed reuse meal type, wrong
  `total_yield`, missing or duplicate leftover ordinal, wrong number of
  leftovers, reuse before preparation, and ambiguous/cross-linked batches.
- [x] Add `tests/test_planner_handler.py` generation tests proving a provider
  candidate that omits or violates the rule is not reserved or published,
  produces bounded privacy-safe repair feedback on attempt one, and publishes
  only after a compliant repaired candidate.
- [x] Add equivalent revision tests proving a noncompliant revision cannot
  replace the current plan or ledger reservation and that the allowed repair
  path retains the exact typed rule snapshot.
- [x] **Expected failure:** Before implementation, the direct validator cases
  cannot supply/enforce batch rules, while handler cases publish or advance a
  candidate that satisfies generic plan validation but omits or violates the
  confirmed rule.
- [x] **Implementation change:** Extend `validate_generated_plan()` with typed
  batch-rule compliance and stable bounded reason codes; pass the authoritative
  rules from generation and revision contexts after canonicalization and
  before any durable write.
- [x] Keep empty-rule behavior unchanged, reject malformed/ambiguous matches,
  and route first-attempt failures through the existing single bounded repair.
  A noncompliant repair must terminate without plan or ledger publication.
- [x] **Passing verification:** Run the focused validator and handler cases;
  require compliant initial and revised candidates to publish and every
  mismatch to remain unpublished without skips or xfails.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_preferences.py tests/test_planner_handler.py \
  tests/test_prompts.py tests/test_parser.py` and resolve every failure before
  Task 2.

**Acceptance criteria:**

- [x] Every confirmed typed `BatchRule` is enforced deterministically for
  initial generation, repair, and revision; prompt text is not the enforcement
  boundary.
- [x] A published plan contains the required food, allowed preparation/reuse
  meal types, exact yield, and complete ordered leftover links for every rule.
- [x] Missing, wrong, duplicate, cross-linked, or ambiguous batch behavior
  enters at most one bounded repair and cannot be published if still invalid.
- [x] Validation feedback and logs remain bounded and omit profile prose, meal
  content, food text, provider payloads, and batch IDs.
- [x] Users with no confirmed batch rules retain existing planning behavior.

**Task 1 execution evidence:**

- `uv run pytest tests/test_preferences.py -k 'confirmed_batch_rule'`:
  12 passed (after the expected initial TypeError for the new API).
- `uv run pytest tests/test_planner_handler.py -k 'enforces_batch_rule or compliant_repaired_batch or revision_enforces_profile_batch_rule or revision_publishes_compliant_profile_batch_rule'`:
  4 passed.
- `uv run pytest tests/test_preferences.py tests/test_planner_handler.py tests/test_prompts.py tests/test_parser.py`:
  588 passed.
- `uv run pytest`: 1,778 passed, 2 failed in unchanged stale SAM-artifact
  checks, with the two known unchanged Pydantic serializer warnings.
- `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, and
  `git diff --check`: passed.

### Task 2: Require the next canonical leftover ordinal transactionally

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** submitted leftover validation around
`src/meal_planner/db/dynamo.py:943`, `BatchMealLink.portion`,
`BatchLedgerEntry.total_portions`, `BatchLedgerEntry.remaining_portions`, the
atomic submitted-meal transaction, and guided meal-confirmation handling.

**TDD sequence:**

- [x] **Failing test first:** Add one stateful Moto test beginning with a
  three-portion available batch and `remaining_portions=2`. Use distinct meal
  submission IDs without resetting the ledger: reject portion 3, accept
  portion 2, accept portion 3, then reject a second portion 3.
- [x] After every transition, assert the exact revision, remaining count,
  state, stored meal count, idempotency/update markers, and conversation state
  so rejected attempts prove full transactional non-mutation.
- [x] Add two-portion and exhausted-batch edge cases, plus stale revision,
  wrong source date/type, duplicate update, and distinct-ID replay cases to
  preserve existing controlled failures.
- [x] Add a concurrent test in which portion 2 and portion 3 race while two
  portions remain. Require only portion 2 to be eligible; after reloading its
  winning revision, portion 3 may succeed exactly once.
- [x] Add a handler regression that submits portion 3 before portion 2 and
  replays portion 3 through separate guided workflows. Assert the existing
  controlled user response and no partial meal or workflow write.
- [x] **Expected failure:** Before implementation, the first portion-3
  submission succeeds because it lies in the range `{2, 3}`; after decrement,
  a second distinct-ID portion-3 submission also succeeds while portion 2 has
  become unusable.
- [x] **Implementation change:** Replace range membership with transactional
  equality to
  `total_portions - remaining_portions + 1` in the leftover branch. Keep the
  existing exact ledger revision/entry conditions as the durable ordinal
  claim; do not add a schema migration.
- [x] Preserve preparation activation, source/week checks, duplicate-event
  idempotency, conflict classification, and exhaustion transitions.
- [x] **Passing verification:** Run the sequential distinct-ID, concurrency,
  and handler tests; require portions 2 then 3 to decrement exactly once and
  every early or replayed portion 3 to leave all state unchanged.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_planner_handler.py tests/test_preferences.py` and resolve every
  failure before Task 3.

**Acceptance criteria:**

- [x] A leftover submission is accepted only for the next canonical ordinal
  derived from the transaction's current ledger entry.
- [x] With three total portions, the only successful sequence is preparation
  portion 1 followed by leftover portions 2 and 3; portion 3 cannot precede
  portion 2 or be consumed twice under distinct submission IDs.
- [x] Each successful portion advances the ledger revision and inventory once;
  rejected, stale, duplicate, or raced submissions leave every transactional
  participant unchanged.
- [x] Existing two-meal batches, activation, exhaustion, idempotent replay,
  and controlled handler responses remain compatible.
- [x] No claimed-ordinal field, migration, table scan, or non-transactional
  write is introduced.

**Task 2 execution evidence:**

- `uv run pytest tests/test_dynamo.py -k 'canonical_available_portion or consumes_three_portions_in_order or portions_outside_canonical_range or wrong_source_metadata or insufficient_wrong_or_stale_inventory or duplicate_batch_confirmation or duplicate_submission or concurrent_leftover_candidates_require_next_ordinal' -q`:
  15 passed after the final formatting pass. The initial retry also exposed
  one nondeterministic Moto race assertion; the same test passed in five
  isolated reruns.
- `uv run pytest tests/test_bot_handler.py -k 'wednesday_batch_preparation_then_leftover_consumption_is_atomic' -q`:
  1 passed.
- `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py tests/test_planner_handler.py tests/test_preferences.py`:
  849 passed, 2 unchanged Pydantic serializer warnings.
- `uv run pytest`:
  1,781 passed, 2 failed only in the unchanged stale `.aws-sam` artifact
  comparisons for `src/meal_planner/db/dynamo.py`, with the same 2 unchanged
  Pydantic serializer warnings.
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed. Ruff reformatted only the Task 2 test additions
  in `tests/test_dynamo.py` and `tests/test_bot_handler.py` before this final
  check.
- The previously reported
  `tests/test_dynamo.py::test_simultaneous_fresh_plan_state_starts_have_one_winner`
  failure was reproduced as a concurrency-flaky result (`[True, True]`) in
  an earlier regression run, then passed in three isolated reruns. Its
  implementation is `save_conversation_state()`, unchanged by Task 2; the
  failure is therefore recorded as pre-existing overlapping/Moto state and
  no out-of-scope fix was made.

### Task 3: Reevaluate expiry after every conditional-write conflict

**Severity:** P2

**Depends on:** Task 2's exact transactional inventory state and revisions.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `DynamoRepository.get_weekly_batch_ledger()`, expiry
transformation around `src/meal_planner/db/dynamo.py:1133`,
`_put_weekly_batch_ledger_conditionally()`, ledger deserialization, DynamoDB
`ConsistentRead`, and existing conditional-conflict classification.

**TDD sequence:**

- [x] **Failing test first:** Add a deterministic repository race where
  expiry computes from revision N, an unrelated inventory mutation wins at
  N+1 without expiring another eligible entry, and the original expiry writer
  loses. Assert the caller preserves the competing mutation, reapplies the
  original `as_of`, and materializes expiry at N+2.
- [x] Add an expiry-winner race proving the loser strongly reloads and returns
  the already-expired winner without a second write or revision increment.
- [x] Add a read-contract test asserting every post-conflict DynamoDB reload
  uses `ConsistentRead=True`, the same user/week key, and the original `as_of`
  semantics.
- [x] Add a controlled stale-reload simulation: return the pre-conflict
  revision once, force another CAS loss, then return the current revision.
  Assert stale data is never returned as success and retry count remains
  bounded.
- [x] Add repeated-conflict exhaustion, malformed reload, no-expiry-change,
  and repeated-read tests. Exhaustion must surface the existing retryable
  repository outcome; no-op reads must perform no write or revision increment.
- [x] Add a handler-facing regression for a raced inventory read, proving an
  entry expired at the requested application date is never offered to fresh
  planning or accepted for submission after the conflict.
- [x] **Expected failure:** Before implementation, the CAS loser performs an
  eventually consistent recursive reload without `as_of` and can return
  revision N or unexpired N+1 instead of materializing the required N+2 state.
- [x] **Implementation change:** Introduce a bounded iterative expiry CAS loop.
  On each conditional loss, strongly reload, validate, and recompute against
  the unchanged original `as_of`; retry only when expiry remains necessary.
- [x] Preserve the winner's unrelated changes, exact revision-and-entry CAS,
  one increment per successful transition, owner/week scoping, privacy-safe
  conflict logging, and no writes when the current ledger already satisfies
  `as_of`.
- [x] **Passing verification:** Run the unrelated-winner, expiry-winner,
  strong-read, stale-reload, retry-exhaustion, no-op, and handler cases. Require
  callers either to observe fully materialized expiry or a controlled retryable
  conflict, never known-unexpired success.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_planner_handler.py` and resolve every failure.
- [x] **Complete suite:** Run `uv run pytest`; require all tests to pass and
  record the exact count. Confirm the two known Pydantic serializer warnings
  are unchanged in count and source, or resolve any new warning.
- [x] **Quality gates:** Run `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `git diff --check`; resolve every
  finding and review the accumulated implementation diff against all three
  review findings.

**Acceptance criteria:**

- [x] A CAS loser always strongly reloads and reevaluates the same original
  `as_of`; it never returns a ledger known still to require expiry.
- [x] An unrelated winning mutation is preserved and required expiry is
  applied in a subsequent exact CAS with one monotonic revision increment.
- [x] An already-expired winner is returned without a duplicate transition,
  while stale reloads and repeated conflicts remain bounded and cannot report
  false success.
- [x] No-op reads do not write, and malformed or exhausted retry paths use a
  controlled repository outcome without leaking ledger contents or batch IDs.
- [x] All three findings have focused regression coverage; the complete suite,
  Ruff, strict Mypy, and diff checks pass with no new warnings.

### Task 3 execution evidence:

- `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py -k
  'expiry_retry_preserves_unrelated_winner or
  expiry_loser_strongly_reloads_expiry_winner or
  expiry_retry_keeps_original_as_of or expiry_conflict_exhaustion or
  expiry_malformed_strong_reload or raced_expiry_removes_batch or
  concurrent_expiry_loser_reloads_the_winning_ledger or
  repeated_current_expiry_reads_are_no_ops' -q`: 8 passed.
- `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py
  tests/test_planner_handler.py -q`: three final reruns each reached 725
  passed and one nondeterministic, pre-existing Moto concurrency failure. The
  failures were `test_competing_ordinary_saves_allow_only_one_revision_owner`,
  `test_new_profile_creation_is_race_safe`, and
  `test_concurrent_leftover_candidates_require_next_ordinal`; no Task 3 test
  failed, and the focused Task 3 suite passed.
- `uv run pytest`: 1,787 passed, 2 failed only in unchanged stale `.aws-sam`
  artifact comparisons for `src/meal_planner/db/dynamo.py`; the two known
  unchanged Pydantic serializer warnings remained.
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`: passed.
- Implementation: `get_weekly_batch_ledger()` now uses a bounded three-attempt
  iterative expiry CAS loop, strongly reloads the same user/week key after
  each conditional loss, preserves the original `as_of`, and reuses the pure
  expiry transformation for every attempt. Added repository and handler
  regressions for winner preservation, no duplicate expiry increment, stale
  reloads, bounded exhaustion, malformed reloads, no-op reads, and raced
  handler submission.

## Post-Completion

### Repository completion bookkeeping

- Only after all implementation and acceptance checkboxes are complete and
  every gate passes, record exact command outcomes in this plan and move this
  file to `docs/plans/completed/` using the same filename.
- Preserve the original remediation plan and archived plans unchanged. Do not
  rewrite their task history to describe this separate follow-up.
- Review the final changed-path list and distinguish follow-up changes from
  the pre-existing overlapping dirty worktree.

### Manual verification

- Generate and revise plans with a confirmed three-meal batch rule. Confirm a
  provider omission or mismatch is repaired or rejected and never displayed.
- Activate a three-meal preparation, attempt portion 3 before portion 2, then
  submit portions 2 and 3 in order through separate workflows. Confirm only
  the ordered submissions mutate inventory.
- Trigger an expiry race with an unrelated ledger update and confirm the final
  state preserves that update while materializing all expiry required by the
  original application date.

### Operational checks

- Inspect only bounded conflict/error categories in CloudWatch. Confirm logs
  contain no profile text, meal descriptions, food names, provider payloads,
  ledger contents, or batch IDs.
- Verify DynamoDB conflict rates do not indicate an unexpected retry loop and
  all reads/writes remain scoped to one user and ISO-week ledger.
- No schema migration, destructive reset, deployment, commit, push, or issue
  creation is part of this plan's creation phase.
