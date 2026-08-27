# Remediate Final Calendar-Aware Batch Workflow Follow-Up Findings

## Overview

Remediate the two actionable findings from the independent review of
`docs/plans/completed/2026-08-26-calendar-aware-batch-workflow-final-review-remediation.md`.
The changes will prevent an available-inventory plan from skipping the
ledger's next required portion, and will normalize retained preparation
yields before confirmed-rule validation consumes a revision repair attempt.

This document is a remediation plan only. **Do not implement remediation in
this planning phase.** Creating this plan does not authorize implementation
code or test changes, modification of the original completed plan or archived
plans, issue creation, commits, pushes, deployment, external actions, schema
changes, or destructive repository operations. A later executor run may
implement only the numbered tasks below.

## Context

- `validate_generated_plan()` delegates available-inventory batch checks to
  logic in `src/meal_planner/preferences.py` around lines 1129-1174.
- That logic correctly computes `first_available_portion` from
  `total_portions - remaining_portions + 1`, but constructs
  `expected_portions` from `actual_portions[0]`. A candidate can therefore
  skip the ledger's next required portion while remaining internally
  contiguous.
- `PlannerHandler.revise_plan()` in
  `src/meal_planner/planner_handler.py` supplies confirmed-rule validation to
  `_generate_with_bounded_repair()` around lines 1538-1557, then calls
  `_preserve_revision_yields()` only after bounded candidate validation.
- A retained preparation may legally omit optional `total_yield`; the
  application is expected to restore it from the current draft. Validating
  first makes the omission look like confirmed-rule noncompliance and can
  consume the single repair opportunity.
- Existing focused coverage lives in `tests/test_preferences.py` around
  `test_batch_leftovers_require_chronological_canonical_ordinals()` and in
  revision tests in `tests/test_planner_handler.py` around
  `test_revise_plan_preserves_three_meal_preparation_yield()`.
- The original three remediation tasks and their quality gates were completed.
  Review found no actionable defect in final-conflict expiry handling.
- The repository has an extensively overlapping dirty worktree. Preserve all
  pre-existing changes and attribute edits by task-scoped hunk; do not use
  broad reset, checkout, clean, stash, or formatting operations.

## Review Findings Covered

1. **P1 — Anchor available-inventory ordinals to the ledger's next
   portion.** With portions 2 and 3 remaining, a candidate that links only
   portion 3 currently validates because its expected sequence starts at 3.
   Submission still requires portion 2, so the published plan is
   unfulfillable.
2. **P2 — Normalize retained yields before revision candidate validation.**
   A confirmed-rule revision retaining a preparation may omit its optional
   yield and rely on application normalization. Rule validation currently sees
   `None` before normalization, incorrectly spends the repair attempt, and may
   reject an otherwise valid revision.

## Development Approach

- **Testing approach: TDD.** For each task, add the smallest focused failing
  test first and run it to capture the reviewed defect before changing source
  code.
- Complete Task 1 before Task 2 because it is the P1 publication-integrity
  issue. The tasks have no required implementation dependency and must remain
  independently reviewable.
- Do not start the next task until the current task's focused and regression
  tests pass. Record exact commands, exit results, and any demonstrably
  pre-existing failures in this plan during execution.
- Mark `[x]` only after the corresponding implementation and verification are
  complete. Prefix approved scope additions with `➕` and blockers or
  deviations with `⚠️`.
- Keep changes minimal: preserve existing validator boundaries and the
  one-repair lifecycle rather than introducing a second repair loop, new
  ledger fields, migrations, provider calls, or abstractions unrelated to the
  two findings.
- Preserve strict Pydantic parsing, immutable typed-rule snapshots,
  chronological meal ordering, user/week partitioning, transactional and
  idempotent submission, durable-write ordering, and bounded privacy-safe
  feedback.
- Do not log or expose profile prose, meal descriptions, food text, provider
  payloads, ledger contents, request IDs, or internal batch IDs.

## Testing Strategy

- Direct validator tests cover a partially consumed available batch whose
  ledger-derived next portion is 2, with a portion-3-only candidate as the
  failure and a portion-2-only candidate as the passing control.
- Planner tests prove the same skipped ordinal cannot pass bounded generation
  or revision validation into reservation or publication.
- Revision tests cover a retained preparation with an omitted optional yield
  under a confirmed typed rule. They assert one provider call, restored yield,
  successful publication, and no unnecessary repair feedback.
- Negative revision controls prove normalization does not permit a new
  preparation with no yield or a retained preparation with an explicitly
  changed yield.
- Tests must be deterministic and must not use skips, xfails, live AWS, live
  LLMs, live Telegram APIs, thread scheduling, or network calls.
- After both tasks, run the complete suite and project quality gates. Treat
  previously documented Moto concurrency flakes, stale `.aws-sam` comparison
  failures, and Pydantic serializer warnings as limitations only if reproduced
  and demonstrated not to result from these changes.

## Solution Overview

For available inventory, build the expected chronological portion sequence
from the ledger-derived `first_available_portion`, not from provider-supplied
ordinals. Its length should equal the number of linked leftovers in the
candidate. A plan may consume fewer than all remaining portions, but its first
linked portion must be the next one claimable by transactional submission and
any additional links must continue without gaps.

For confirmed-rule revisions, normalize a deep-copied candidate with
`_preserve_revision_yields(candidate, current_plan)` before deterministic
batch-rule validation on every bounded attempt. Return or publish only the
normalized successful candidate. Keep missing yields on new preparations and
explicit changes to retained yields invalid. Reuse the exact current-plan and
typed-rule snapshots captured before the first provider call, preserve one
repair maximum, and perform no durable work until normalization and validation
both succeed.

## Technical Details

### Ledger-anchored available ordinals

- In available-inventory validation in
  `src/meal_planner/preferences.py`, retain the existing calculation:
  `first_available_portion = total_portions - remaining_portions + 1`.
- Sort linked leftovers by the existing `(day, _MEAL_TYPE_ORDER[meal_type])`
  key and collect `actual_portions` as today.
- For a non-empty candidate, construct the expected tuple as
  `range(first_available_portion, first_available_portion + len(leftovers))`.
  Use an empty tuple when no leftovers exist.
- Continue enforcing the existing upper bound, remaining-count, duplicate,
  canonical-ID, source, chronology, and meal-link checks. Do not require a
  candidate to consume all remaining inventory.
- Reuse the bounded `batch_noncanonical_ordinal_order` validation category (or
  the repository's established equivalent) and its privacy-safe location
  metadata. Do not include internal IDs or meal content in feedback.

### Yield normalization before revision validation

- In `PlannerHandler.revise_plan()`, create a revision candidate evaluation
  boundary that first calls `_preserve_revision_yields()` against the
  snapshotted current plan and then calls `validate_generated_plan()` with the
  snapshotted `batch_rules` and `available_batches`.
- Apply this ordering to both the initial candidate and the single repaired
  candidate handled by `_generate_with_bounded_repair()`.
- Ensure the normalized candidate—not the pre-normalization provider object—is
  the object that proceeds to reservation, replacement, publication, and batch
  entry creation. Avoid mutating the provider object or current plan in place.
- Preserve current invalid behavior for a missing yield on a new preparation
  and an explicit yield change on a retained preparation. Convert such
  failures into the existing bounded candidate-failure lifecycle without
  leaking raw exception text.
- Do not add provider calls for a valid omitted retained yield. Keep exactly
  one repair for genuinely invalid candidates and preserve existing terminal
  notification and retry-state behavior after a second invalid candidate.

## What Goes Where

- **Implementation Steps:** the two numbered tasks below are the only
  implementation scope for a later executor run.
- **Post-Completion:** bookkeeping, manual checks, and operational observation
  are informational and do not authorize external actions during planning.

## Implementation Steps

### Task 1: Anchor available-inventory validation to the next ledger portion

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** available-inventory branch used by `validate_generated_plan()`,
`first_available_portion`, `ordered_leftovers`, `actual_portions`,
`expected_portions`, `PlannerHandler.generate_plan()`, and
`PlannerHandler.revise_plan()`.

**TDD sequence:**

- [x] **Failing test first:** Add a focused direct validator case in
  `tests/test_preferences.py` with an available entry having
  `total_portions=3` and `remaining_portions=2`, and a candidate containing
  only chronological portion 3. Assert rejection with bounded canonical
  ordinal feedback.
- [x] Add passing controls proving that a candidate containing only portion 2
  is valid and that chronological portions 2 then 3 remain valid. Preserve
  the existing reversed 3 then 2 rejection.
- [x] Add a planner-boundary regression in `tests/test_planner_handler.py`
  proving a portion-3-only available-inventory candidate cannot be reserved or
  published. Exercise the existing bounded repair path with a compliant
  portion-2 candidate, and assert exact provider call and no-premature-write
  counts.
- [x] **Expected failure:** Run the new focused tests before implementation.
  The portion-3-only cases must fail because current code derives
  `expected_portions == (3,)` from `actual_portions[0]`, even though the ledger
  and submission path require portion 2 next.
- [x] **Implementation change:** In the available-inventory branch of
  `src/meal_planner/preferences.py`, build the expected tuple from
  `first_available_portion` for exactly `len(ordered_leftovers)` positions.
  Keep the existing chronological sort and all inventory bounds and reason
  codes.
- [x] Verify that empty links remain a no-op, partial consumption beginning at
  the next portion remains allowed, excessive or out-of-range consumption is
  rejected, and confirmed-rule ordinal behavior is unchanged.
- [x] **Passing verification:** Run the new direct and planner-focused tests.
  Require the portion-3-only candidate to enter bounded repair before any
  durable write, and require portion 2 and portions 2-then-3 controls to pass.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_preferences.py tests/test_planner_handler.py` and
  resolve every new failure before Task 2.
- [x] Run `uv run ruff format --check src/meal_planner/preferences.py \
  tests/test_preferences.py tests/test_planner_handler.py`,
  `uv run ruff check src/meal_planner/preferences.py \
  tests/test_preferences.py tests/test_planner_handler.py`, and
  `git diff --check` before marking Task 1 complete.

**Acceptance criteria:**

- [x] Available-inventory candidates begin with the ledger-derived next
  canonical portion; provider-supplied `actual_portions[0]` cannot redefine
  the claim sequence.
- [x] With portions 2 and 3 remaining, portion 3 alone is rejected before
  reservation, publication, display, or workflow mutation, while portion 2
  alone and chronological portions 2 then 3 remain valid.
- [x] Existing upper-bound, remaining-count, chronological ordering,
  canonical-ID, source-link, confirmed-rule, and no-inventory behavior remains
  compatible.
- [x] Validation and repair feedback remains bounded and privacy-safe.
- [x] Focused tests, listed regressions, Ruff checks, and diff checks pass with
  no new failures or warnings.

### Task 2: Normalize retained yields before bounded revision validation

**Severity:** P2

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

**Symbols:** `PlannerHandler.revise_plan()`,
`PlannerHandler._preserve_revision_yields()`,
`PlannerHandler._generate_with_bounded_repair()`, revision
`candidate_validator`, `validate_generated_plan()`, `PlanValidationResult`,
and revision reservation/publication boundaries.

**TDD sequence:**

- [x] **Failing test first:** Extend or add a focused revision test in
  `tests/test_planner_handler.py` where the current draft has a retained
  preparation with `total_yield=3`, the snapshotted confirmed rule requires
  yield 3, and the otherwise compliant provider candidate retains the batch
  ID but omits optional `total_yield`.
- [x] Assert that the valid omitted-yield revision makes exactly one provider
  call, receives no repair prompt, restores `total_yield=3` in the replacement
  and batch entries, and publishes only the normalized candidate.
- [x] Add negative controls for a new preparation that omits `total_yield` and
  a retained preparation that explicitly changes the yield. Assert that each
  remains invalid, receives at most the existing single bounded repair, and
  performs no reservation, replacement, ledger, publication, or other durable
  write before a fully valid candidate exists.
- [x] Add an invalid-then-valid control proving both attempts normalize before
  confirmed-rule validation and reuse the exact current-plan, typed-rule, and
  available-inventory snapshots captured before the first provider call.
- [x] **Expected failure:** Run the omitted-yield test before implementation.
  It must show an unnecessary second provider call or terminal rejection
  because `validate_generated_plan()` compares the unnormalized `None` yield
  with the confirmed rule before `_preserve_revision_yields()` runs.
- [x] **Implementation change:** Reorder revision candidate processing so a
  deep-copied candidate is normalized with `_preserve_revision_yields()`
  before confirmed-rule validation on each bounded attempt, and ensure the
  normalized successful object is returned to the durable revision path.
- [x] Map normalization failures for missing new yields or changed retained
  yields into existing bounded, privacy-safe candidate feedback. Preserve the
  one-repair limit, terminal notification, retry-state behavior, and existing
  parse/structural validation lifecycle without introducing another loop.
- [x] Verify no-rule and initially explicit-yield revisions retain their
  current provider-call count and publication behavior, and that neither the
  provider candidate nor current draft is mutated in place.
- [x] **Passing verification:** Run the omitted-yield, changed-yield, new
  missing-yield, invalid-then-valid, snapshot, no-write, no-rule, and explicit
  yield revision cases. Require exact provider and repository call counts.
- [x] **Regression tests:** Run
  `uv run pytest tests/test_planner_handler.py tests/test_preferences.py \
  tests/test_prompts.py tests/test_parser.py` and resolve every new failure.
- [x] **Full verification:** Run `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and
  `git diff --check`. Record exact results and distinguish any reproduced,
  demonstrably pre-existing Moto, `.aws-sam`, or Pydantic limitation.
- [x] Review the accumulated changed-path list against both findings and
  confirm no implementation path outside the files authorized by Tasks 1 and
  2 changed during remediation.

**Acceptance criteria:**

- [x] Every confirmed-rule revision candidate is normalized against the
  snapshotted current draft before deterministic batch-rule validation,
  including both bounded attempts.
- [x] A valid retained preparation that omits optional `total_yield` uses one
  provider call, does not consume repair, restores the prior yield, and
  publishes the normalized revision.
- [x] A new preparation with no yield and a retained preparation with an
  explicitly changed yield remain invalid and cannot reach any durable write.
- [x] A genuinely invalid first candidate still receives at most one repair,
  and the repaired candidate uses the same immutable current-plan, rule, and
  inventory snapshots.
- [x] Existing no-rule, explicit-yield, parse-repair, terminal-notification,
  reservation-conflict, and publication behavior remains compatible.
- [x] Focused tests, listed regressions, the complete suite, Ruff, strict Mypy,
  and diff checks pass with no remediation-introduced failures or warnings.

## Risks and Limitations

- `first_available_portion` is application-owned ledger state. Deriving the
  expected sequence from candidate data again would preserve the reviewed
  defect; requiring all remaining portions would introduce a different
  regression by disallowing partial planning.
- Revision normalization and validation must use deep copies. In-place
  mutation could contaminate the current draft, the second attempt, or durable
  state before validation succeeds.
- Extending `_generate_with_bounded_repair()` more broadly than needed could
  alter initial-generation behavior. Prefer a revision-scoped normalization
  boundary unless a small typed result contract is demonstrably necessary to
  return the normalized candidate.
- Converting `_preserve_revision_yields()` failures to repair feedback must not
  expose exception text, provider payloads, batch IDs, or meal content.
- The dirty worktree overlaps both source and test paths, limiting attribution.
  Compare task-start snapshots by hunk and preserve unrelated user changes.
- Previous execution recorded one nondeterministic Moto/thread-scheduling
  failure, two stale `.aws-sam` comparison failures, and two known Pydantic
  serializer warnings. Do not hide them or broaden remediation unless a new
  change demonstrably causes the result.

## Post-Completion

### Execution evidence (2026-08-27)

- Focused remediation regression: `uv run pytest
  tests/test_preferences.py tests/test_planner_handler.py -q` — 338 passed.
- Initial full-suite run: 1,806 passed, 2 stale `.aws-sam` artifact comparison
  failures, and 2 unchanged Pydantic serializer warnings.
- Rebuilt deployment artifacts with `uvx --from aws-sam-cli sam build
  --beta-features` — Build Succeeded.
- Final full suite: `uv run pytest -q` — 1,808 passed and 2 unchanged Pydantic
  serializer warnings.
- `uv run ruff format --check .` — 109 files already formatted.
- `uv run ruff check .` — all checks passed.
- `uv run mypy` — no issues found in 20 source files.
- `git diff --check` — passed.
- The implementation and tests are part of the accumulated issue-69 worktree;
  the original plan and previously archived plans were preserved unchanged.

### Repository bookkeeping

- After every implementation and acceptance checkbox passes, record exact
  test and quality-gate results in this plan and move it to
  `docs/plans/completed/` with the same filename.
- Preserve the original completed plan and all archived plans unchanged.
- Review the final changed-path list and distinguish remediation-attributable
  hunks from the pre-existing dirty worktree.
- No issue creation, commit, push, deployment, migration, destructive reset,
  or external action is authorized by this plan.

### Manual verification

- With a ledger exposing portions 2 and 3, generate a candidate that links
  only portion 3. Confirm it is repaired or rejected and never displayed or
  submitted; confirm a portion-2-only candidate remains valid.
- Revise a confirmed-rule plan while retaining an existing preparation but
  omitting its optional yield. Confirm one provider call, restored yield, and
  successful replacement without repair feedback.
- Repeat with a new yield-less preparation and an explicit changed retained
  yield. Confirm bounded rejection and unchanged durable state.

### Operational checks

- Confirm validation and revision logs contain only bounded categories and no
  profile text, meal descriptions, food names, provider payloads, ledger
  contents, request IDs, or internal batch IDs.
- Confirm available-inventory claims remain scoped to one user and ISO week,
  and revision metrics show no repair call for a valid normalized omission.
