# Remediate Meal-Plan Preference Validation Review Findings

## Overview

Remediate all nine actionable findings from the read-only review of the
evidence-based meal-preference implementation. The work closes two paths that
can bypass core correctness guarantees, hardens four workflow and validation
boundaries, and resolves three lower-severity consistency and observability
defects.

The remediation preserves the original architecture: an LLM interprets user
wording, application code validates measurable evidence, Planner performs at
most one fresh asynchronous repair invocation, and only an owned, compliant
draft may be persisted and displayed. No remediation implementation begins as
part of this plan.

## Context (from discovery)

- The completed source plan is under `docs/plans/completed/` as
  `2026-08-19-enforce-meal-plan-preferences-with-evidence-based-validation.md`.
  Its ten tasks and all 60 checkboxes are complete.
- The implementation baseline passed Ruff formatting and linting, strict Mypy,
  and `uv run pytest` with 493 passed and 2 skipped.
- `parse_preference_interpretation()` currently accepts an empty successful
  interpretation and does not reject directly conflicting normalized rules.
- `PlannerHandler.generate_plan()` saves a draft and then separately clears
  state, while `DynamoRepository` already contains transaction patterns that
  can conditionally mutate a plan and conversation state together.
- Food normalization currently lives privately in
  `src/meal_planner/preferences.py`; schema duplicate validation and parser
  conflict detection need the same equivalence rules without a circular
  import.
- Repair ownership failures are conflated with stale requests, and several
  repair or recovery logs measure elapsed time from `0.0` instead of the start
  of the affected operation.
- Project constraints remain Python 3.14, Ruff at 80 columns, strict Mypy,
  Pytest, and `uv` for all Python commands, as configured in `pyproject.toml`.

## Development Approach

- **Testing approach:** TDD. For each remediation task, add the specified
  failing or missing test first, run it to demonstrate the defect, make the
  smallest implementation change, and rerun the focused and regression tests.
- Complete tasks one at a time in the listed order. Do not begin the next task
  until the current task's new and existing focused tests pass.
- Keep each finding independently reviewable. Do not combine unrelated cleanup
  with remediation work.
- Preserve direct Planner calls that have no tracked conversation request, old
  stored `WeeklyPlan` records, no-preference generation, plan revisions, and
  the one-provider-call-per-invocation limit.
- Reuse the repository's DynamoDB transaction and conditional-failure patterns.
  Do not emulate atomicity with additional reads or sequential writes.
- Introduce one dependency-neutral food-normalization helper because schema
  validation, evidence matching, and parser conflict detection must agree.
- Keep operational logs privacy-safe and bounded. New tests must not assert or
  expose raw preferences, plans, meals, ingredients, user IDs, or chat IDs.
- Mark checkboxes immediately as work completes. Record scope changes with a
  `➕` prefix and blockers or deviations with a `⚠️` prefix.
- Do not modify the archived original plan.

## Testing Strategy

- **Parser tests:** cover vacuous interpretations, directly conflicting rule
  signatures, reordered alternatives, normalized equivalents, and compatible
  rules.
- **Bot workflow tests:** prove invalid interpretations cannot dispatch Planner
  and duplicate clarification updates receive state-accurate replies.
- **Schema and matcher tests:** enforce one normalization contract and the
  attempt-two feedback invariant while preserving matching behavior.
- **Repository tests:** exercise successful and conditionally rejected
  transactional publication with Moto and verify nonconditional AWS errors are
  re-raised.
- **Planner tests:** simulate ownership loss at publication, repair ownership
  read failures, malformed repair events, and realistic elapsed-time logging.
- **Completeness tests:** validate every present meal, including optional
  snacks, while still requiring breakfast, lunch, and dinner each day.
- **Regression tests:** run the focused module set after every task and the full
  Ruff, Mypy, and Pytest checks in the final verification task.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Add newly discovered work with a `➕` prefix.
- Add blockers or deviations with a `⚠️` prefix and include evidence.
- Keep this plan synchronized with implementation and test results.
- Move this plan to `docs/plans/completed/` only after every remediation and
  final verification item passes.

## Solution Overview

The parser will treat a vacuous LLM response as clarification-required rather
than successful interpretation. A shared normalized-food representation will
then make model duplicate checks, meal evidence matching, and direct-conflict
detection agree on Unicode, punctuation, whitespace, and conservative
singular/plural equivalence.

Tracked generation requests will publish through one DynamoDB transaction that
conditionally writes the generated draft and deletes the exact matching
conversation state. A failed ownership condition leaves both records unchanged
and suppresses all delivery. Direct calls without request metadata continue to
use the existing draft compare-and-swap path.

Repair ownership reads will retain the existing tri-state scheduling contract:
scheduled requests succeed, stale requests no-op, and infrastructure failures
enter explicit retry-ready terminal recovery. Optional snacks will be validated
when present, duplicate Telegram updates will report clarification state
accurately, attempt two will require repair feedback, and failure timers will
start at the operation being measured.

## Finding Traceability

| Review finding | Severity | Remediation task |
| --- | --- | --- |
| Reject empty successful interpretations | P1 | Task 1 |
| Make ownership and draft publication atomic | P1 | Task 2 |
| Normalize duplicate alternatives consistently | P3 | Task 3 |
| Reject directly conflicting requirements | P2 | Task 4 |
| Distinguish repair ownership-read failures | P2 | Task 5 |
| Validate optional snacks | P2 | Task 6 |
| Return accurate duplicate clarification replies | P2 | Task 7 |
| Require feedback for attempt two | P3 | Task 8 |
| Record real repair and recovery elapsed times | P3 | Task 9 |

Task 3 precedes Task 4 despite its lower severity because conflict signatures
must use the same normalized-food contract as schema validation and evidence
matching.

## Technical Details

### Vacuous interpretation handling

`parse_preference_interpretation()` must return a focused clarification when
all three wire fields are semantically empty: no requirements, no
clarification, and no unparsed clauses. No-preference phrases remain a bot-level
bypass and never call the interpreter, so a successful interpreter result must
contain at least one `PreferenceRequirement`.

### Atomic tracked publication

Add a dedicated `DynamoRepository` operation for generated drafts with tracked
request metadata. Its `TransactWriteItems` request must contain:

1. A conditional plan `Put` using the same new-plan or current-draft revision
   conditions as `save_generated_draft()`.
2. A conditional conversation-state `Delete` matching both `request_id` and
   state `revision`.

Expected transaction cancellation returns `False`; other DynamoDB errors are
re-raised. `PlannerHandler.generate_plan()` delivers the plan, evidence summary,
and follow-up only after this operation returns `True`.

### Shared normalized-food contract

Move the dependency-neutral normalization primitives to a small module such as
`src/meal_planner/normalization.py`. Expose a typed helper that performs Unicode
normalization and case folding, maps punctuation to token boundaries,
collapses whitespace, and applies the existing conservative singular/plural
normalization. `PreferenceRequirement`, the evidence matcher, and parser
conflict signatures must consume this helper.

### Direct conflict signature

Two interpreted requirements conflict only when they have the same meal scope
and the same set of normalized alternatives but different exact counts.
Alternative ordering, punctuation, case, and supported singular/plural variants
must not alter the signature. Similar but satisfiable rules, such as different
meal scopes or genuinely different alternative sets, remain accepted.

### Repair and recovery outcomes

Preserve `_schedule_repair()`'s existing caller semantics:

- `True`: attempt two was queued.
- `None`: request ownership is stale, so no notification or state mutation is
  allowed.
- `False`: repair infrastructure failed, so the caller performs terminal,
  retry-ready recovery and a bounded user notification.

Capture `time.monotonic()` immediately before each ownership, dispatch,
recovery, or delivery operation whose failure is logged. No failure path may
derive elapsed time from process startup.

## What Goes Where

- **Implementation Steps:** nine finding-specific TDD tasks, one full
  verification task, and one final documentation/archive task.
- **Post-Completion:** reviewed branch and pull request, live AWS/Telegram
  verification, and the required issue update after a commit exists.

## Implementation Steps

### Task 1: Reject vacuous successful preference interpretations

**Finding:** P1 — Reject empty successful interpretations.

**Files:**

- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_bot_handler.py`

1. [x] **Test first:** add a parser test for
   `{"requirements": [], "clarification": null, "unparsed_text": []}` and a
   bot workflow test where a nonempty preference receives that interpreter
   response.
2. [x] **Expected failure:** run the new tests and confirm the parser currently
   returns `([], None)` and the bot can transition to `GENERATING` and invoke
   Planner with no requirements. Both new tests failed as expected.
3. [x] **Implementation:** update `parse_preference_interpretation()` to return
   a focused, bounded clarification when a response has neither requirements
   nor clarification/unparsed text. Keep no-preference bypass behavior in
   `BotHandler` unchanged.
4. [x] **Verify the fix:** rerun the new tests and confirm the workflow remains
   `AWAITING_PREFERENCE`, sends the clarification, and does not invoke Planner.
   The focused tests pass: 2 passed.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_parser.py tests/test_bot_handler.py`.
   Result: 104 passed.

**Acceptance criteria:**

- Every successful interpreted preference contains at least one validated
  requirement.
- A vacuous interpretation cannot dispatch Planner or discard the user's raw
  preference.
- Explicit no-preference answers still bypass interpretation and generate as
  before.

### Task 2: Publish tracked drafts and release ownership atomically

**Finding:** P1 — Make state ownership and draft publication atomic.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`

1. [x] **Test first:** add repository tests for a transactional generated-draft
   write plus matching state deletion, plan-revision conflict, request/state
   ownership conflict, and nonconditional DynamoDB failure. Add a Planner race
   test that loses ownership immediately before publication. Result: added four
   repository tests and one tracked-publication race test; the pre-fix run
   failed with the missing repository method and missing transactional planner
   call, as expected.
2. [x] **Expected failure:** confirm the race test demonstrates the current
   sequential behavior can save a draft even though state clearing fails, and
   that the stale worker can proceed to display, summary, or follow-up calls.
   Result: the race fixture modeled a failed ownership clear; before the fix,
   the sequential planner path still persisted and reached delivery because
   the cleanup result was not part of publication success.
3. [x] **Implementation:** add a repository method such as
   `save_generated_draft_and_clear_conversation_state()` using one DynamoDB
   transaction with both the existing draft revision condition and matching
   `request_id`/state-revision deletion. Use it for tracked requests in
   `PlannerHandler.generate_plan()`; retain `save_generated_draft()` for direct
   untracked calls. Result: implemented the dedicated conditional transaction
   and tracked/untracked planner split.
4. [x] **Verify the fix:** confirm transaction success writes one draft and
   removes the state, while either ownership or plan conflict writes nothing,
   clears nothing, and triggers no delivery. Result: transactional success,
   both conditional conflicts, nonconditional error propagation, and planner
   delivery suppression all pass.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_dynamo.py tests/test_planner_handler.py`.
   Result: 112 passed.

**Acceptance criteria:**

- No tracked draft can be persisted unless the exact request still owns the
  matching state at the same atomic commit point.
- A failed transaction produces no plan display, satisfaction summary, or
  review follow-up.
- Conditional conflicts are treated as stale work; infrastructure errors remain
  visible to existing failure handling.
- Direct calls and plan revision compare-and-swap behavior remain compatible.

### Task 3: Share food normalization across schemas and evidence matching

**Finding:** P3 — Normalize duplicate alternatives consistently.

**Files:**

- Create: `src/meal_planner/normalization.py`
- Modify: `src/meal_planner/preferences.py`
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_schemas.py`

1. [x] **Test first:** add schema tests rejecting normalized duplicates such as
   `egg`/`eggs`, `red-pepper`/`red pepper`, mixed Unicode forms, case variants,
   and repeated whitespace. Add matcher regressions that pin current whole-word
   and conservative plural behavior through the shared contract. Result: added
   duplicate cases for singular/plural, punctuation, fullwidth Unicode, and
   repeated whitespace, plus matcher regressions for Unicode and whole-word
   conservative plural behavior.
2. [x] **Expected failure:** confirm `PreferenceRequirement` currently accepts
   at least the singular/plural and punctuation-equivalent alternatives even
   though evidence matching treats them as equivalent. Result: the pre-fix run
   failed 3 new schema cases for singular/plural, punctuation, and fullwidth
   Unicode equivalence; existing matcher cases passed.
3. [x] **Implementation:** extract the normalization primitives from
   `preferences.py` into a typed, dependency-neutral helper and consume it from
   both the matcher and `PreferenceRequirement` duplicate validation. Avoid
   importing `preferences.py` from `schemas.py`. Result: added
   `meal_planner.normalization.normalize_food()` and routed both consumers
   through its shared Unicode, punctuation, whitespace, and conservative plural
   token contract.
4. [x] **Verify the fix:** rerun the new schema and matcher tests and confirm
   all equivalent alternatives are rejected without changing valid evidence
   match results. Result: all equivalent duplicate alternatives are rejected and
   existing valid and whole-word evidence matches remain unchanged.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_schemas.py tests/test_preferences.py`.
   Result: 123 passed. `uv run ruff format --check` and `uv run ruff check`
   passed for the changed files; `uv run mypy` passed with no issues; full
   `uv run pytest` passed with 506 passed and 2 skipped.

**Acceptance criteria:**

- Model validation and evidence matching use one normalization implementation.
- Case, Unicode normalization, punctuation boundaries, whitespace, and the
  supported singular/plural forms produce consistent duplicate decisions.
- Whole-phrase matching and false-substring rejection remain unchanged.
- No food ontology, synonym table, or external dependency is introduced.

### Task 4: Clarify directly conflicting interpreted requirements

**Finding:** P2 — Reject directly conflicting interpreted requirements.

**Files:**

- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_parser.py`
- Modify: `tests/test_bot_handler.py`

1. [x] **Test first:** add parser cases for the same foods and meal scope with
   different exact counts, reordered alternatives, singular/plural equivalents,
   and punctuation equivalents. Add compatible controls for equal counts,
   different scopes, and genuinely different alternative sets, plus a bot test
   proving conflicts do not dispatch Planner. Result: added parser coverage
   for all conflict equivalences and compatible controls, plus a bot workflow
   regression proving conflicts do not dispatch Planner.
2. [x] **Expected failure:** confirm the parser currently returns both
   contradictory requirements as a successful interpretation, allowing two
   unsatisfiable Planner attempts. Result: the pre-fix parser tests returned
   both contradictory requirements and the bot test observed a GENERATING
   transition, as expected.
3. [x] **Implementation:** after model validation and ID uniqueness checks,
   build a canonical signature from meal scope and the shared normalized set of
   alternatives. Return a focused clarification when one signature has
   different `exact_count` values. Result: added a parser signature using
   shared `normalize_food()` output and a bounded conflict clarification.
4. [x] **Verify the fix:** confirm direct conflicts return no requirements and
   a focused clarification, while compatible rule sets still parse and proceed.
   Result: normalized, reordered, same-scope conflicts return clarification and
   no requirements; compatible rules remain successful; the bot remains in
   `AWAITING_PREFERENCE` without Planner invocation.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_parser.py tests/test_bot_handler.py`. Result: 112
   passed.

**Acceptance criteria:**

- Alternative order and normalized spelling cannot hide a direct count
  conflict.
- Conflicting rules remain recoverable in `AWAITING_PREFERENCE` and never
  invoke Planner.
- The detector is narrow and does not reject rule sets that can coexist.

### Task 5: Recover explicitly from repair ownership-read failures

**Finding:** P2 — Do not treat repair ownership-read failures as stale
requests.

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

1. [x] **Test first:** add first-attempt structural and/or compliance failure
   tests where `get_conversation_state(..., consistent_read=True)` raises during
   `_schedule_repair()`. Assert no repair is invoked and the request does not
   remain silently stuck in `GENERATING`. Result: added parametrized structural
   and compliance tests asserting no repair invocation, retry-state recovery,
   bounded notification, and privacy-safe logs.
2. [x] **Expected failure:** confirm the current exception path returns `None`,
   is treated as stale, suppresses terminal recovery and notification, and
   returns successfully from the asynchronous handler. Result: the pre-fix
   tests failed because retry recovery and notification were both suppressed.
3. [x] **Implementation:** reserve `None` for a confirmed ownership mismatch and
   return the existing failure outcome (`False`) for repository read errors so
   the caller runs `_finish_failed_generation()`. Preserve privacy-safe
   `repair_ownership` logging and conditional retry-state recovery. Result:
   `_schedule_repair()` now returns `False` from the repository-read exception
   path; no other repair or stale-request behavior changed.
4. [x] **Verify the fix:** confirm an ownership-read failure results in either a
   matching retry-ready state plus bounded notification, or a silent no-op only
   when conditional recovery proves ownership was concurrently lost. Result:
   ownership-read failures recover a matching request, while existing stale and
   cancelled tests remain silent.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py`. Result: 73 passed. Focused
   new tests: 2 passed; Ruff format/check and strict Mypy also passed.

**Acceptance criteria:**

- Infrastructure failure and stale ownership are distinct control-flow
  outcomes.
- A matching request cannot remain indefinitely `GENERATING` after a handled
  repair ownership-read exception.
- No repair invocation, draft publication, or raw-data logging occurs on this
  path.
- Existing stale and cancelled request behavior remains silent.

### Task 6: Validate every present meal, including optional snacks

**Finding:** P2 — Validate optional snacks for ingredients and calories.

**Files:**

- Modify: `src/meal_planner/preferences.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_planner_handler.py`

1. [x] **Test first:** add completeness tests for a present snack with empty
   ingredients, zero or negative calories, and an invalid snack that would
   otherwise satisfy an unscoped requirement. Add a valid-snack control and a
   Planner publication regression. Result: added completeness coverage for
   empty, zero, and negative snack calories, invalid and valid unscoped snack
   evidence, legacy meal-field defaults, and Planner publication suppression.
2. [x] **Expected failure:** confirm the current completeness validator accepts
   invalid snacks and can count one as preference evidence in an otherwise
   publishable plan. Result: the pre-fix tests showed invalid snacks produced a
   valid result and were persisted by Planner, as expected.
3. [x] **Implementation:** continue requiring breakfast, lunch, and dinner for
   every day, but validate nonempty ingredients and positive calories for every
   meal entry that is present before preference evidence is accepted. Result:
   shared meal completeness checks now cover all present optional snacks while
   required meal presence checks and model defaults remain unchanged.
4. [x] **Verify the fix:** confirm invalid snacks produce stable completeness
   feedback and cannot contribute to a persisted or displayed plan, while valid
   optional snacks remain supported. Result: focused Task 6 tests pass; invalid
   snacks are rejected before publication and valid snacks satisfy unscoped
   evidence.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_preferences.py tests/test_planner_handler.py`.
   Result: 102 passed. Full `uv run pytest` also passed with 523 passed and 2
   skipped; Ruff format/check and strict Mypy passed.

**Acceptance criteria:**

- Every present meal has at least one ingredient and positive calories.
- Breakfast, lunch, and dinner remain the only required daily meal types.
- An invalid snack cannot satisfy an unscoped preference requirement.
- Stored legacy plan deserialization remains unchanged.

### Task 7: Make duplicate clarification replies state-aware

**Finding:** P2 — Return an accurate response for duplicate clarification
updates.

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

1. [x] **Test first:** extend duplicate-update tests to assert the exact reply
   when focused clarification is pending and when interpreter failure leaves a
   retryable `AWAITING_PREFERENCE` state. Preserve a generating-state control.
   Result: added exact-reply tests for focused clarification, interpreter
   failure, and a duplicate `GENERATING` control.
2. [x] **Expected failure:** confirm both clarification cases currently receive
   "Working on your weekly meal plan." even though no Planner invocation
   occurred. Result: the three new tests failed before the implementation;
   both clarification cases received the working response and the generating
   case received the generic in-progress/retry response.
3. [x] **Implementation:** base the idempotent duplicate response on the current
   `ConversationWorkflowStep`; return a concise clarification-pending response
   for `AWAITING_PREFERENCE` and retain the working response for `GENERATING`.
   Result: moved the duplicate guard before the step gate and made the replies
   state-aware without changing transition, revision, or Planner dispatch
   behavior.
4. [x] **Verify the fix:** confirm duplicate updates do not reinterpret,
   redispatch, or mutate state and return a response consistent with the saved
   workflow step. Result: focused duplicate tests passed with no interpreter,
   transition, or Planner calls.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_bot_handler.py`.
   Result: 85 passed. Ruff format/check and strict Mypy passed; full
   `uv run pytest` passed with 525 passed and 2 skipped.

**Acceptance criteria:**

- Duplicate clarification updates never claim generation is underway.
- Focused-clarification and interpreter-failure states both prompt the user to
  continue or retry their preference answer.
- Duplicate generating updates retain the existing working response.
- Idempotency and revision guards remain unchanged.

### Task 8: Require bounded feedback for every second attempt

**Finding:** P3 — Require repair feedback for attempt two.

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_planner_handler.py`

1. [x] **Test first:** add `PlanGenerationContext` tests rejecting attempt two
   with missing or blank feedback and retaining rejection of attempt-one
   feedback. Add handler-event tests proving malformed attempt-two payloads are
   rejected before provider invocation. Result: added explicit attempt-one
   no-feedback coverage, missing/blank attempt-two cases, and side-effect-free
   malformed event coverage.
2. [x] **Expected failure:** confirm `attempt=2` with `repair_feedback=None`
   currently validates and can enter Planner as a terminal repair without
   repair context. Result: the pre-fix focused run failed 5 cases because
   omitted or `None` attempt-two feedback was accepted and dispatched through
   `handle_event()`.
3. [x] **Implementation:** make the `PlanGenerationContext` invariant
   bidirectional: attempt one has no feedback, and attempt two requires bounded,
   nonblank feedback. Keep existing attempt bounds and wire validation. Result:
   added the missing attempt-two presence invariant; existing `RepairFeedback`
   constraints continue to enforce trimmed, nonblank feedback of at most 800
   characters.
4. [x] **Verify the fix:** confirm malformed second-attempt events invoke no LLM
   or persistence operation, while valid attempt-one and attempt-two contexts
   still work. Result: malformed events return `False` before generation, and
   valid first- and second-attempt contexts remain accepted.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_schemas.py tests/test_planner_handler.py`.
   Result: 184 passed. Full `uv run pytest` passed with 534 passed and 2
   skipped; `uv run ruff format --check .`, `uv run ruff check .`, `uv run
   mypy`, and `git diff --check` also passed.

**Acceptance criteria:**

- Every attempt-two context contains nonempty bounded repair feedback.
- Attempt one cannot carry repair feedback.
- Invalid internal events fail safely before provider, repository mutation, or
  Telegram delivery.
- Legacy direct attempt-one calls remain valid.

### Task 9: Measure repair and recovery failures from real start times

**Finding:** P3 — Record real elapsed times for repair and recovery failures.

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

1. [x] **Test first:** patch `time.monotonic()` deterministically and assert
   bounded, realistic `elapsed_ms` values for repair ownership-read failure,
   missing/failed/non-202 repair dispatch, and retry-state recovery failure.
   Add an assertion that logged fields remain privacy-safe. Result: added
   deterministic coverage for ownership-read failure, missing/exceptional/non-
   202 dispatch, and both retry-state read and write failures; each asserts
   bounded elapsed values and excludes sensitive metadata.
2. [x] **Expected failure:** confirm paths using `started_at=0.0` report process
   uptime rather than the duration of the failed operation. Result: the new
   tests initially observed 10,000–30,000 ms from the zero sentinels instead
   of the expected operation durations.
3. [x] **Implementation:** capture an operation start immediately before each
   repair ownership, repair dispatch, and state-recovery operation. Audit the
   remaining `_log_safe_failure()` call sites in `PlannerHandler` and replace
   every zero sentinel with an actual local start or an explicit supported
   unknown-duration representation. Result: added local monotonic starts for
   repair ownership, dispatch, retry-state recovery, and all remaining
   Planner failure/delivery paths; control flow and bounded metadata are
   unchanged.
4. [x] **Verify the fix:** rerun deterministic logging tests and confirm elapsed
   values reflect only the measured operation and never become negative or
   process-uptime-sized. Result: deterministic tests report the expected 125 ms
   ownership, 50/200/75 ms dispatch, 40/10 ms recovery durations; all are
   nonnegative and bounded.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py`. Result: 85 passed. Full
   verification also passed: `uv run ruff format --check .`, `uv run ruff
   check .`, `uv run mypy`, `uv run pytest` (540 passed, 2 skipped), and
   `git diff --check`.

**Acceptance criteria:**

- Repair ownership, repair dispatch, and state recovery log meaningful elapsed
  durations.
- No new Planner failure path passes `0.0` as a synthetic operation start.
- Safe metadata remains bounded and excludes user and meal content.
- Control flow and user-visible recovery behavior are unchanged by timing
  instrumentation.

### Task 10: Verify every review finding and project quality gate

**Files:**

- Modify if needed: tests and implementation files listed in Tasks 1-9
- Modify: this plan with the final verification record

1. [x] verify all nine findings in the traceability table have passing tests and
   completed acceptance criteria
2. [x] run the reported preference example and confirm exact evidence counts,
   accepted-plan summary behavior, and rejection of invalid snack evidence
3. [x] verify cancellation or replacement immediately before publication leaves
   no draft, display, summary, follow-up, or inappropriate state deletion
4. [x] verify vacuous and conflicting interpretations remain recoverable and
   cannot invoke Planner
5. [x] verify repair ownership failures recover explicitly, stale repairs remain
   silent, attempt two requires feedback, and elapsed logs are realistic and
   privacy-safe
6. [x] run `uv run ruff format --check .` and fix formatting failures
7. [x] run `uv run ruff check .` and fix lint failures
8. [x] run `uv run mypy` and fix all type errors
9. [x] run `uv run pytest` until the full suite passes
10. [x] run `git diff --check` and inspect the complete remediation diff for
    accidental implementation, dependency, configuration, or archived-plan
    changes outside scope

**Verification record (2026-08-19):**

- All nine findings and acceptance criteria were rechecked through the focused
  suites: parser and bot workflows (`uv run pytest tests/test_parser.py
  tests/test_bot_handler.py`: 114 passed), transactional repository and
  Planner behavior (`uv run pytest tests/test_dynamo.py
  tests/test_planner_handler.py`: 126 passed), shared normalization and
  completeness (`uv run pytest tests/test_schemas.py
  tests/test_preferences.py`: 133 passed), repair/recovery and timing (`uv
  run pytest tests/test_planner_handler.py`: 85 passed), snack and publication
  regressions (`uv run pytest tests/test_preferences.py
  tests/test_planner_handler.py`: 113 passed), duplicate replies (`uv run
  pytest tests/test_bot_handler.py`: 85 passed), and attempt-two schema/event
  validation (`uv run pytest tests/test_schemas.py
  tests/test_planner_handler.py`: 190 passed).
- `uv run pytest -q tests/test_preferences.py::test_reported_preference_example_has_exact_evidence_counts tests/test_preferences.py::test_format_satisfaction_summary_uses_validated_evidence_counts tests/test_preferences.py -k 'invalid_snack or optional_snack'`: 5 passed; the reported example asserted exact evidence counts `[1, 3, 1]`, the accepted summary, and invalid/valid snack behavior.
- `uv run pytest tests/test_dynamo.py -k 'tracked_generated_draft'`: 4
  passed; `uv run pytest tests/test_planner_handler.py -k
  'tracked_generation_losing_ownership_before_publication or
  cancelled_request or stale_second_attempt or repair_ownership_failure or
  repair_dispatch_failures or retry_state_recovery or
  handle_event_rejects_malformed_attempt_two'`: 11 passed. These cover
  cancellation/replacement ownership races, no publication side effects,
  explicit repair ownership-read recovery, silent stale repair handling,
  malformed attempt-two rejection, and realistic privacy-safe elapsed logs.
- `uv run pytest tests/test_parser.py tests/test_bot_handler.py -k 'vacuous or
  conflicting_preference_interpretation'`: 3 passed; both interpretation
  failures remain retryable and do not invoke Planner.
- `uv run ruff format --check .`: 71 files already formatted. `uv run ruff
  check .`: all checks passed. `uv run mypy`: success, no issues in 19 source
  files. `uv run pytest`: 540 passed, 2 skipped. `git diff --check`: passed
  with no output.
- Diff inspection found only the accumulated remediation/application/test/
  documentation files already in scope; `pyproject.toml` and `uv.lock` have no
  diff, and there are no dependency changes. The archived original plan was
  unchanged: its SHA-256 was `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`
  before and after verification. No implementation fix was needed during
  Task 10.

**Acceptance criteria:**

- All nine review findings are closed by focused regression coverage.
- Ruff formatting, Ruff linting, strict Mypy, and the full Pytest suite pass.
- The original archived plan remains unchanged.
- The final verification record includes exact commands and results.

### Task 11: Update documentation and archive the remediation plan

**Files:**

- Modify if behavior documentation requires it: `README.md`
- Modify: tests covering any changed documentation assertions
- Move after completion from `docs/plans/` to `docs/plans/completed/`:
  `2026-08-19-remediate-meal-plan-preference-validation-review-findings.md`

1. [x] review `README.md` against the remediated clarification, validation,
   repair, and atomic-publication behavior; update only user- or
   operator-relevant contract changes
2. [x] update documentation assertions if the README changes
3. [x] rerun documentation-adjacent tests and all final Ruff, Mypy, and Pytest
   quality gates
4. [x] mark every checkbox complete, record any scope deviations and final test
   results, and move this plan to `docs/plans/completed/`
5. [x] confirm the archived original implementation plan was not modified

**Task 11 completion record (2026-08-19):**

- README review found the remediated clarification, evidence-validation,
  bounded-repair, manual-retry, one-provider-request-per-invocation, and
  privacy-safe diagnostic behavior already documented. Added the user-facing
  stale-publication contract: cancelling or replacing an in-progress request
  prevents its older asynchronous result from being saved or displayed.
- Added `tests/test_readme.py` with three documentation contract tests covering
  clarification continuation, validation and repair behavior, stale-request
  suppression, and operator diagnostics. No implementation files, dependency
  files, or deployment configuration were changed for Task 11.
- `uv run pytest tests/test_readme.py`: 3 passed.
- `uv run ruff format --check .`: 72 files already formatted.
- `uv run ruff check .`: all checks passed.
- `uv run mypy`: success, no issues in 19 source files.
- `uv run pytest`: 543 passed, 2 skipped.
- `git diff --check`: passed with no output.
- Scope deviations: none. The requested no-commit, no-push, Task 11-only
  boundary was maintained; GitHub issue updates and deployment verification
  remain post-completion work outside this task.
- The archived original implementation plan remained byte-for-byte unchanged;
  its SHA-256 was
  `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`
  before and after Task 11.

**Acceptance criteria:**

- Documentation accurately describes user-visible behavior without exposing
  internal implementation detail unnecessarily.
- All plan tasks and acceptance criteria are complete before archival.
- Both completed plans exist under `docs/plans/completed/`, and the original is
  byte-for-byte untouched by remediation work.

## Post-Completion

**Manual verification**

- Deploy from a dedicated branch through a pull request; never push or merge
  directly to `master`.
- Race a cancellation or replacement against successful Planner completion in
  a test environment and confirm no stale plan appears in Telegram or DynamoDB.
- Exercise vacuous, conflicting, and clarification-required interpreter
  responses through Telegram and confirm the bot asks for input accurately.
- Inspect CloudWatch repair and recovery logs for realistic elapsed values and
  absence of raw user or meal content.

**External system updates**

- Use a Conventional Commit message containing the associated issue number.
- Comment on the associated GitHub issue after implementation with a summary and
  link to the commit or pull request.
- Deploy and verify DynamoDB transactional permissions through the normal
  reviewed release process.

No GitHub issue is created during this planning-only phase; external writes are
outside the requested scope.
