# Remediate Final Calendar-Aware Batch Workflow Review Findings

## Overview

Remediate the three actionable findings from the independent review of
`docs/plans/2026-08-26-calendar-aware-batch-workflow-review-remediation-follow-up.md`.
The changes will prevent publication of chronologically unfulfillable batch
plans, make confirmed-rule violations in revisions use the existing bounded
repair lifecycle, and ensure an expiry CAS loser evaluates the winner loaded
after the final allowed conflict before reporting exhaustion.

This document is a remediation plan only. Creating it does not modify
implementation code, tests, the original plan, archived plans, or external
systems. It does not authorize an issue, commit, push, deployment, schema
migration, or destructive repository operation.

## Context

- The follow-up implementation added typed `BatchRule` validation, exact
  transactional leftover ordinal claims, and bounded iterative expiry CAS
  retries.
- Independent review found one P1 integration defect and two P2 lifecycle or
  retry-boundary defects in that accumulated implementation.
- Relevant planner paths are `validate_generated_plan()` in
  `src/meal_planner/preferences.py` and `PlannerHandler.generate_plan()`,
  `PlannerHandler.revise_plan()`, and `_generate_with_bounded_repair()` in
  `src/meal_planner/planner_handler.py`.
- Relevant repository code is
  `DynamoRepository.get_weekly_batch_ledger()` around
  `src/meal_planner/db/dynamo.py:1174`.
- Relevant tests are `tests/test_preferences.py`,
  `tests/test_planner_handler.py`, `tests/test_bot_handler.py`, and
  `tests/test_dynamo.py`.
- The repository has extensive overlapping pre-existing changes. Preserve
  them, avoid broad cleanup or restoration, and modify only the files named
  by the active task.

## Review Findings Covered

1. **P1 — Enforce canonical ordinal order before publishing batch plans.**
   Batch validation checks the expected ordinal set and dates after
   preparation, but does not require chronological leftovers to use portions
   `2, 3, ...` in that order. A reversed plan can therefore publish and later
   be rejected by transactional submission validation.
2. **P2 — Route noncompliant revisions through bounded repair.** A
   structurally valid revision is returned before deterministic confirmed-rule
   validation. The later invalid branch immediately notifies the user instead
   of giving the existing bounded repair lifecycle a compliant second
   candidate.
3. **P2 — Reevaluate the final conflict reload before raising.** The expiry
   retry loop strongly reloads after its final failed CAS but raises without
   evaluating whether that reload contains a winner that already materialized
   the required expiry.

## Development Approach

- **Testing approach: TDD.** In each numbered task, add the smallest focused
  failing tests first, run them to capture the stated defect, make only that
  task's implementation change, rerun the focused tests, and then run its
  regression commands.
- Complete tasks in order. Task 2 depends on Task 1's canonical typed-rule
  validator behavior. Task 3 is independent of planner behavior but follows
  the P1 and planner-lifecycle work because it is P2.
- Do not proceed to the next task while focused or regression tests fail.
  Record exact commands, exit results, and any confirmed pre-existing failure
  in this plan during execution.
- Mark `[x]` only after implementation and verification exist. Prefix approved
  scope additions with `➕` and blockers or deviations with `⚠️`.
- Keep provider output advisory. Typed profile rules, chronological portion
  order, IDs, inventory state, revisions, and expiry outcomes remain
  application-owned decisions.
- Preserve strict Pydantic boundaries, user-partition key access,
  transactional and idempotent meal submission, privacy-safe diagnostics,
  and bounded retries.
- Do not add table scans, unbounded loops, recursion, live provider calls,
  network-dependent tests, claimed-ordinal schema, or migrations.
- Do not log or expose profile prose, meal descriptions, food text, provider
  payloads, ledger contents, or internal batch IDs.

## Testing Strategy

- Direct validator tests cover confirmed rules and available inventory with
  canonical and reversed chronological leftover ordinals.
- Planner tests cover generation and revision publication boundaries, plus
  invalid-then-compliant and invalid-then-invalid revision repair sequences.
- Handler integration tests prove a published three-portion plan can be
  submitted in chronological canonical order and that reversed order cannot
  become a published, unfulfillable workflow.
- Repository tests deterministically drive every expiry write conflict and
  strongly reloaded winner; they must not rely on thread scheduling or Moto
  race timing for the final-conflict scenario.
- Tests must not use skips, xfails, live AWS, live LLMs, live Telegram APIs,
  or network calls.
- After Task 3, run the complete test suite and quality gates. Treat the two
  stale `.aws-sam` comparison failures, two existing Pydantic serializer
  warnings, and reported Moto concurrency flakes as limitations only when
  they are reproduced and shown not to result from the remediation changes.

## Solution Overview

Extend deterministic batch validation so linked leftovers are sorted by
their actual meal date and meal position and must then carry the exact
canonical ordinal sequence `2..total_yield`. Apply this invariant both when a
confirmed rule creates a batch and when the plan consumes available inventory.
The same validation boundary must govern generated and revised candidates
before reservation, publication, display, or any other durable write.

Move confirmed-rule validation for revisions inside the candidate evaluator
used by `_generate_with_bounded_repair()`. A first structurally valid but
rule-invalid revision should produce the existing bounded, privacy-safe repair
feedback and consume the single allowed repair attempt. Only a compliant
candidate may continue to reservation and publication; a second invalid
candidate must terminate with the existing controlled notification and leave
all durable state unchanged.

Restructure the expiry CAS loop so each strong reload is evaluated against the
unchanged original `as_of`, including the reload after the final permitted
write conflict. The write-attempt bound remains unchanged: a final winner
that already applied expiry is returned, while a final reload that still
requires expiry raises the existing retryable concurrency outcome without an
additional write.

## Technical Details

### Chronological canonical ordinals

- In `validate_generated_plan()`, derive a deterministic chronological order
  for linked leftovers using the plan's existing date and meal ordering
  semantics.
- For each confirmed rule, require that ordered leftovers carry exactly
  portions `2..total_yield`; set equality alone is insufficient.
- Apply the same sequence check to links consuming available batch inventory,
  beginning with the ledger's next canonical available portion and continuing
  without gaps or reversals.
- Retain all existing checks for preparation order, source metadata, meal
  types, food, yield, duplicate or ambiguous links, horizon, and batch-ID
  canonicalization.
- Return bounded stable reason-coded feedback without meal content, food text,
  profile prose, provider payloads, or internal IDs.

### Revision bounded repair

- Supply the exact snapshotted typed rules to the validation callback used for
  every revision candidate handled by `_generate_with_bounded_repair()`.
- Treat deterministic rule noncompliance like other repairable candidate
  validation failures on the first attempt; do not reserve, publish, or mutate
  retry/durable workflow state before a candidate passes.
- Reuse the same immutable rule snapshot for both attempts even if profile or
  repository state changes while repair is in progress.
- Preserve the existing one-repair limit and terminal user-facing behavior
  when the repaired candidate remains invalid.

### Final expiry conflict evaluation

- Separate the bounded count of expiry CAS writes from evaluation of the
  ledger returned by each post-conflict strong read.
- After every conditional conflict, strongly reload the same user/week key and
  recompute the pure expiry transformation with the original `as_of`.
- If the final reload is already current for `as_of`, return that winner with
  no duplicate write or revision increment.
- If the final reload still needs expiry and the write budget is exhausted,
  raise the existing retryable conditional-conflict outcome. Do not perform a
  fourth write and do not return known-unexpired state.
- Preserve unrelated winner mutations, exact revision-and-entry CAS
  conditions, malformed-reload handling, owner/week scope, and privacy-safe
  logging.

## Implementation Steps

### Task 1: Enforce chronological canonical ordinals before publication

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `validate_generated_plan()`, confirmed `BatchRule` matching,
available-inventory link validation, `PlannerHandler.generate_plan()`,
`PlannerHandler.revise_plan()`, plan meal ordering, and guided batch meal
submission.

**TDD sequence:**

- [x] **Failing tests first:** Add table-driven direct validator cases with
  preparation on Monday, portion 3 on Tuesday, and portion 2 on Wednesday.
  Cover both a confirmed three-portion rule and an available-inventory batch;
  add canonical `2` then `3` controls that continue to pass.
- [x] Add planner generation and revision cases in
  `tests/test_planner_handler.py` proving a reversed candidate is rejected
  before reservation or publication and a canonical candidate is accepted.
  Assert bounded privacy-safe validation feedback for the reversed sequence.
- [x] Add an end-to-end handler regression in `tests/test_bot_handler.py` that
  publishes a canonical three-portion plan and submits portions 2 then 3
  successfully through their dated workflows. Pair it with a reversed-order
  provider candidate that never becomes a published/submittable plan.
- [x] **Expected failure:** Before implementation, reversed direct-validator
  cases and planner candidates pass because `{2, 3}` matches the expected set
  and both dates follow preparation; the resulting Tuesday portion 3 is then
  incompatible with the repository's exact next-ordinal requirement.
- [x] **Implementation change:** In `validate_generated_plan()`, order linked
  leftovers by the plan's chronological meal ordering and require their
  portions to equal the canonical sequence. Enforce it for confirmed rules
  and available inventory, and route the same bounded reason code through
  generation and revision validation before any durable write.
- [x] Preserve existing rule dimensions, empty-rule behavior, inventory
  bounds, canonicalized IDs, and privacy-safe validation messages. Do not
  weaken transactional submission checks or add a ledger field.
- [x] **Passing verification:** Run the focused reversed/canonical validator,
  generation, revision, and handler cases. Require reversed plans to remain
  unpublished and canonical plans to submit portions 2 then 3 exactly once.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_preferences.py tests/test_planner_handler.py \
  tests/test_bot_handler.py tests/test_dynamo.py` and resolve every new
  failure before Task 2.

⚠️ Regression note: the final listed regression run passed 862 tests and
reproduced one pre-existing Moto/thread-scheduling failure in
`tests/test_dynamo.py::test_simultaneous_fresh_plan_state_starts_have_one_winner`.
The full `uv run pytest` run passed 1,795 tests and reproduced only the two
documented stale `.aws-sam` artifact comparison failures; its two documented
Pydantic serializer warnings were unchanged. All Task 1 focused tests passed.

**Acceptance criteria:**

- [x] Confirmed-rule and available-inventory leftovers increase
  chronologically in the exact canonical ordinal sequence; set equality alone
  cannot validate a reversed plan.
- [x] Generation and revision reject or repair reversed ordinal candidates
  before reservation, publication, display, or workflow mutation.
- [x] A canonical three-portion plan remains publishable and supports the only
  valid submission sequence: preparation, portion 2, then portion 3.
- [x] Validation feedback is bounded and privacy-safe, and all existing rule,
  inventory, transaction, and no-rule behavior remains compatible.
- [x] Focused and listed regression tests pass without skips or xfails.

### Task 2: Route confirmed-rule revision failures through bounded repair

**Severity:** P2

**Depends on:** Task 1's complete deterministic batch-rule validator,
including canonical chronological ordinals.

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `PlannerHandler.revise_plan()`,
`_generate_with_bounded_repair()`, revision candidate validation,
`validate_generated_plan()`, `PlanGenerationContext.batch_rules`, retry state,
plan reservation, ledger reservation, and publication.

**TDD sequence:**

- [x] **Failing test first:** Add an invalid-then-compliant revision test in
  which the first structurally valid candidate violates the snapshotted
  confirmed rule and the repaired candidate complies. Capture both provider
  calls and prove the second call receives bounded rule-validation feedback.
- [x] Add an invalid-then-invalid revision test proving exactly one repair is
  attempted, the existing controlled terminal notification is emitted, and
  neither candidate replaces the current plan or ledger reservation.
- [x] In both tests, mutate or replace the profile rule source between attempts
  and assert validation and repair use the exact typed rule snapshot captured
  before the first revision prompt. Assert no premature retry-state,
  conversation-state, plan-state, reservation, ledger, or publication write.
- [x] Add a no-rule and initially compliant revision control to prove existing
  successful revision behavior and provider-call count remain unchanged.
- [x] **Expected failure:** Before implementation,
  `_generate_with_bounded_repair()` returns the first structurally valid
  candidate before rule validation; the later branch immediately notifies the
  user and never asks for a compliant repaired revision.
- [x] **Implementation change:** Integrate typed batch-rule validation into
  the revision candidate evaluator passed to
  `_generate_with_bounded_repair()`. Return bounded repair feedback on the
  first violation, reuse the immutable rule snapshot on the second attempt,
  and allow durable revision work only after full validation succeeds.
- [x] Preserve the single-repair bound, existing structural parse repair,
  terminal notification, optimistic reservation/conflict behavior, and
  privacy-safe logs. Do not create a second repair loop or duplicate durable
  writes.
- [x] **Passing verification:** Run the invalid-then-compliant,
  invalid-then-invalid, snapshot, no-write, no-rule, and compliant revision
  cases. Require exact provider call counts and durable-state assertions.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_planner_handler.py tests/test_preferences.py \
  tests/test_prompts.py tests/test_parser.py` and resolve every new failure
  before Task 3.

⚠️ Regression note: the exact Task 2 regression run passed all 597 tests. The
full `uv run pytest` run passed 1,798 tests and reproduced only the two
documented stale `.aws-sam` artifact comparison failures; its two documented
Pydantic serializer warnings were unchanged. Ruff format/check, Mypy, and
`git diff --check` passed. The initial focused TDD run failed the two new
invalid-revision tests as expected because the pre-change path made one
provider call; after implementation all focused cases passed.

**Acceptance criteria:**

- [x] A structurally valid but confirmed-rule-invalid revision receives one
  bounded repair opportunity through the same lifecycle as other repairable
  candidate failures.
- [x] An invalid-then-compliant sequence publishes only the compliant repaired
  revision; an invalid-then-invalid sequence preserves the current plan and
  ends with the existing controlled notification.
- [x] Both attempts validate against the exact typed rule snapshot captured
  for the revision, regardless of intervening profile changes.
- [x] No retry, workflow, reservation, ledger, plan, or publication state is
  durably changed before a candidate passes deterministic validation.
- [x] Existing initially compliant and no-confirmed-rule revisions retain
  their behavior, and focused and listed regression tests pass.

### Task 3: Evaluate the winner loaded after the final expiry conflict

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `DynamoRepository.get_weekly_batch_ledger()`, bounded expiry CAS
attempt accounting, `_put_weekly_batch_ledger_conditionally()`, strong
post-conflict reload, pure expiry transformation, and conditional-conflict
classification.

**TDD sequence:**

- [x] **Failing test first:** Add a deterministic repository test where the
  first two expiry CAS conflicts strongly reload ledgers that still require
  expiry, while the final allowed CAS conflict strongly reloads a competing
  winner that has fully materialized expiry for the original `as_of`.
- [x] Assert the final winner is returned, its exact revision and unrelated
  mutations are preserved, every reload uses `ConsistentRead=True` for the
  same user/week key, no extra CAS is attempted, and no conflict is raised.
- [x] Add the paired exhaustion case where the final strong reload still
  requires expiry. Assert the existing retryable conflict is raised after the
  same bounded write count, with no fourth write and no known-unexpired
  success.
- [x] Add a handler-facing regression proving a final-reload expiry winner is
  observed as current and its expired inventory is neither offered to fresh
  planning nor accepted for submission.
- [x] **Expected failure:** Before implementation, the final conflict path
  reloads the valid expiry winner but exits the retry loop and raises the last
  conditional conflict without evaluating that ledger.
- [x] **Implementation change:** Restructure the bounded loop so every
  post-conflict strong reload is validated and transformed with the unchanged
  original `as_of` before exhaustion is decided. Keep the existing write
  budget; return an already-current final winner or raise only when the final
  evaluated ledger still needs expiry.
- [x] Preserve exact revision-and-entry CAS, malformed-reload handling,
  unrelated winner state, one increment per successful transition, no-op
  behavior, owner/week scoping, and privacy-safe conflict logging.
- [x] **Passing verification:** Run the final-winner, final-unexpired,
  strong-read, unchanged-`as_of`, no-extra-write, and handler cases. Require
  current winner success or controlled retryable exhaustion, never false
  failure after completed expiry or known-unexpired success.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_planner_handler.py`; then run `uv run pytest`. Resolve every new
  failure and record the exact full-suite result.
- [x] **Quality gates:** Run `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `git diff --check`. Review the
  accumulated diff against all three findings and confirm no out-of-scope
  path changed.

⚠️ Regression note: the focused Task 3 cases passed 3 tests. The listed
regression command passed 735 tests with the documented Pydantic serializer
warnings. The full `uv run pytest` executor validation passed 1,799 tests and
reproduced the documented Moto/thread-scheduling failure
(`tests/test_dynamo.py::test_two_consumers_cannot_decrement_the_last_batch_portion`)
and the two stale `.aws-sam` artifact comparison failures. Ruff
format/check, Mypy, and `git diff --check` passed.

**Acceptance criteria:**

- [x] Every post-conflict strong reload, including the reload after the final
  permitted write conflict, is evaluated against the original `as_of`.
- [x] A final competing winner that already materialized required expiry is
  returned without an extra write, revision increment, or false conflict.
- [x] A final reload that still requires expiry raises the existing retryable
  outcome without exceeding the bounded CAS write count or returning stale
  state.
- [x] Unrelated winner mutations, exact CAS conditions, malformed-data
  handling, no-op reads, user/week scope, and privacy-safe diagnostics remain
  intact.
- [x] Focused tests, listed regressions, the complete suite, Ruff, strict
  Mypy, and diff checks pass with no new failures or warnings.

## Risks and Limitations

- Planner ordering must use the same date and within-day meal ordering already
  used by publication and submission. A new competing sort convention could
  make validation and execution disagree at day boundaries.
- Available inventory may begin at a portion greater than 2. Its validation
  must start from the ledger-derived next canonical portion and must not
  accidentally require historical portions already consumed.
- Moving revision validation into repair can accidentally duplicate provider
  calls or durable writes. Exact call-count and no-premature-write assertions
  are required.
- Expiry retry accounting has an off-by-one risk. Tests must separately count
  write attempts and post-conflict evaluations and cover both final-winner and
  final-unexpired outcomes.
- The active worktree has overlapping changes, so attribution is limited on
  shared paths. Review task-scoped symbols and preserve unrelated hunks.
- Previous execution reported two stale `.aws-sam` artifact comparison
  failures, two unchanged Pydantic serializer warnings, and nondeterministic
  Moto concurrency failures. Do not hide them, but do not broaden this plan to
  generated artifacts or unrelated concurrency code unless a remediation
  change demonstrably causes the failure.

## Post-Completion

### Repository bookkeeping

- After all implementation and acceptance checkboxes pass, record exact test
  and quality-gate outcomes in this plan and move this remediation plan to
  `docs/plans/completed/` with the same filename.
- Preserve the original follow-up plan and all archived plans unchanged.
- Review the final changed-path list and distinguish task-attributable changes
  from the pre-existing dirty worktree.

### Manual verification

- Generate and revise a three-portion batch plan where a provider initially
  reverses portions 2 and 3. Confirm the candidate is repaired or rejected and
  never displayed in an unfulfillable order.
- Revise a plan with a confirmed rule using invalid-then-compliant and
  invalid-then-invalid provider responses. Confirm one bounded repair and no
  premature visible or durable state change.
- Exercise an expiry race whose final conflict is won by an expiry writer.
  Confirm the loser returns the current winner without a duplicate write or
  false retryable error.

### Operational checks

- Confirm conflict and validation logs contain only bounded categories and no
  profile text, meal descriptions, food names, provider payloads, ledger
  contents, or batch IDs.
- Confirm DynamoDB operations remain scoped to one user and ISO-week ledger,
  and retry metrics do not indicate an extra write after final-conflict
  winner evaluation.
- No issue creation, commit, push, deployment, migration, destructive reset,
  or external action is part of this plan's creation.
