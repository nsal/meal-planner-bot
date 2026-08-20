# Remediate Meal-Plan Review Findings Follow-Up

## Overview

Remediate all six findings from the Phase 3 review of the completed
meal-preference validation work. The release-blocking deployment permission is
fixed first. The remaining work makes untracked asynchronous repair publication
idempotent, rejects impossible normalized food alternatives, bounds provider
clarifications before Telegram delivery, and corrects Planner timing and
attempt telemetry.

This is a planning-only document. No remediation implementation is included.
Implementation must follow TDD: add and run each missing test first, record the
expected failure that proves the defect, make the smallest change, and rerun
the focused and regression suites before starting the next task.

## Context

### Completed implementation baseline

- The implementation plan completed immediately before this review is
  `docs/plans/completed/`
  `2026-08-19-remediate-meal-plan-preference-validation-review-findings.md`.
- That plan completed Tasks 1-11:
  1. vacuous parser clarification;
  2. atomic tracked DynamoDB draft/state publication;
  3. shared food normalization;
  4. direct conflict detection;
  5. repair ownership-read failure recovery;
  6. validation of optional snacks;
  7. state-aware duplicate replies;
  8. the attempt-two feedback invariant;
  9. operation-local elapsed timing;
  10. full verification; and
  11. README stale-publication documentation, `tests/test_readme.py`, and
      archival of the completed plan.
- Every original task reported passing focused tests with no unresolved
  implementation blocker.
- Final baseline quality results were `uv run pytest` with 543 passed and 2
  skipped, `uv run ruff format --check .` passed, `uv run ruff check .`
  passed, `uv run mypy` passed, and `git diff --check` passed.
- The earlier evidence-validation plan remained byte-for-byte unchanged with
  SHA-256
  `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`.
  The completed implementation plan named above starts this follow-up with
  SHA-256
  `0a7e76d3d89eb99fba15c937b8e8f6df82eca62aa8959a27ea14cc700a4b7443`.

### Review assessment

- The review assessed the implementation as not release-ready until the P1
  Planner permission defect is fixed.
- Core preference validation and tracked ownership behavior were otherwise
  covered.
- The working tree already contains the completed implementation. Preserve all
  existing changes and do not rewrite either archived plan.

### Project constraints

- Follow `pyproject.toml`: Python 3.14, Ruff with an 80-column limit, strict
  Mypy, and Pytest.
- Use `uv run` for Python tools and tests and Ruff for formatting and linting.
- Do not add a dependency unless the remediation cannot be completed with the
  standard library and existing packages. No dependency is expected.
- Never push or merge directly to `master`. Use a dedicated branch and pull
  request for implementation.
- A later commit must use Conventional Commits and include the associated issue
  number. After implementation, comment on that issue with the commit or pull
  request link.

## Complete Review Findings

- **P1, Task 1 — Grant the Planner role transactional write permission.** Add
  table-scoped `dynamodb:TransactWriteItems` to `PlannerFunction`, reverse the
  test that currently forbids it, and verify both deployed Lambda roles.
- **P2, Task 2 — Make untracked asynchronous repairs idempotent.** Carry a
  durable repair token and conditionally publish an untracked attempt-two draft
  exactly once.
- **P2, Task 3 — Reject food terms that normalize to no tokens.** Reject every
  alternative for which `normalize_food()` returns `()`.
- **P2, Task 4 — Bound interpreter clarification output before delivery.**
  Validate the provider envelope and cap every rendered clarification before
  Telegram.
- **P3, Task 5 — Measure terminal validation failures from the actual
  operation.** Propagate the generation/validation start into terminal failure
  telemetry.
- **P3, Task 6 — Preserve the actual attempt number in recovery telemetry.**
  Pass the active attempt through retry recovery and failure notification
  paths.

All six findings must remain independently traceable to a focused test and an
acceptance criterion. Do not combine unrelated cleanup with these fixes.

## Development Approach

- **Testing approach:** TDD.
- Complete tasks in severity and dependency order: deployment permission,
  untracked repair idempotency, normalization rejection, clarification bounds,
  terminal timing, attempt propagation, then final verification.
- For every task:
  1. add or update the missing test;
  2. run it before implementation and confirm the documented failure;
  3. implement only that task;
  4. rerun the new test and confirm it passes;
  5. run the listed regression tests; and
  6. do not proceed while any focused or regression test fails.
- Mark checkboxes immediately as work completes. Add newly discovered work with
  a `➕` prefix and blockers or deviations with a `⚠️` prefix.
- Keep logs bounded and privacy-safe. Tests must not expose raw preferences,
  plans, meals, ingredients, user IDs, chat IDs, credentials, or raw events.
- Preserve tracked request ownership, direct attempt-one generation, plan
  revision compare-and-swap behavior, confirmed-plan protection, and the
  one-provider-call-per-Planner-invocation limit.
- Keep the original completed plans unchanged.

## Solution Overview

`PlannerFunction` will receive the same explicit, table-scoped DynamoDB
transaction permission as `BotFunction`. The read-only deployed permission
verifier will resolve both function outputs, inspect both execution roles, and
fail closed if either role lacks an explicit allow on the exact table ARN.

Untracked asynchronous repair events will carry a random bounded `repair_id`
created once when `_schedule_repair()` builds the attempt-two event. Publication
will transact the plan write with a durable marker under the same user
partition, such as `PLAN_REPAIR#<repair_id>`. A typed publication outcome will
distinguish a successful write, a stale plan conflict, and a replayed repair.
Sequential or concurrent replay of the same event may repeat provider work, but
only one transaction may persist the draft and only that winner may deliver it;
duplicate losers remain silent.

`PreferenceRequirement` will reject normalized-empty alternatives before its
duplicate comparison. `parse_preference_interpretation()` will enforce a small
provider-output envelope and route every clarification through one final
bounded renderer. Oversized or excessive provider content will produce a
concise application-owned fallback instead of truncating ambiguous text or
causing multiple synchronous Telegram requests.

Finally, `_finish_failed_generation()` will log from the start of the affected
generation/validation operation, and the current attempt will be passed through
`_retain_retry_state()` and `_notify_failure()` so attempt-two recovery and
notification incidents are labeled correctly.

## Technical Details

### Planner transaction authorization

`DynamoDBCrudPolicy` does not include `dynamodb:TransactWriteItems`. Add a
separate `Statement` under `PlannerFunction.Properties.Policies` with
`Effect: Allow`, that exact action, and
`Resource: !GetAtt MealPlannerTable.Arn`. Wildcards and broader DynamoDB actions
are not acceptable.

Refactor `scripts/verify_transaction_permission.py` so
`DeploymentResources` represents both Bot and Planner function/role pairs plus
the one table. `resolve_resources()` must require `BotFunctionName`,
`PlannerFunctionName`, and `MealPlannerTableName`; `verify_permission()` must
simulate the transaction action independently for both exact role ARNs and the
exact table ARN. A denial or malformed result for either role fails the whole
verification.

### Untracked repair idempotency

Add `repair_id: RequestId | None` to `PlanGenerationContext` and enforce these
wire invariants:

- attempt one cannot carry `repair_id`;
- a tracked attempt two may continue to use `request_id` and `state_revision`;
- an untracked attempt two must carry a nonblank, bounded `repair_id`; and
- `repair_id` is forwarded unchanged from `handle_event()` through
  `generate_plan()` to publication.

For an untracked attempt one, `_schedule_repair()` creates one UUID repair ID
and places it in the queued attempt-two payload. Lambda asynchronous redelivery
reuses the same payload and therefore the same token.

Add a repository operation such as `save_repaired_draft_once()` that performs
one `TransactWriteItems` call containing:

1. the existing plan `Put` guarded by the current new-plan or draft-revision
   condition; and
2. a marker `Put` at `PK=USER#<user_id>`,
   `SK=PLAN_REPAIR#<repair_id>`, guarded by `attribute_not_exists(PK)`.

Return a typed result such as `PUBLISHED`, `STALE`, or `DUPLICATE` by inspecting
transaction cancellation reasons. Raise unexpected DynamoDB errors. Only
`PUBLISHED` may reach `send_plan()` or follow-up messages. `DUPLICATE` is a
silent success; `STALE` retains the existing stale-result behavior. Tracked
attempts continue to use
`save_generated_draft_and_clear_conversation_state()`.

### Clarification envelope

Define named limits in `src/meal_planner/llm/parser.py`, with an overall
rendered clarification cap of 500 characters. Also bound raw provider
clarification text, the number of `unparsed_text` clauses, and each clause
before joining. Route provider clarification, rendered unparsed clauses,
vacuous responses, duplicate/conflict messages, and malformed-envelope
messages through one final helper that guarantees the 500-character cap.

When provider output exceeds an envelope limit or the rendered message would
exceed the final cap, return one concise, application-owned fallback asking the
user to rephrase the preference. Do not forward a truncation that could change
meaning. Because 500 is below Telegram's 4,096-character message limit, the
Bot path must produce one synchronous `sendMessage` request for clarification.

## Testing Strategy

- **Template and verifier:** parse the SAM template, assert exact policies, and
  mock CloudFormation, Lambda, DynamoDB, and IAM responses for both roles.
- **Schema and parser:** use parametrized punctuation, symbol, emoji,
  item-count, item-length, and final-render-length boundaries, including valid
  boundary controls.
- **Repository:** use Moto for plan/marker atomicity and a controlled concurrent
  race, plus mocked cancellation reasons for exact outcome classification and
  unexpected-error propagation.
- **Planner:** replay the same untracked attempt-two event sequentially and
  concurrently, then assert exactly one persistence and one delivery sequence.
  Add deterministic monotonic-clock tests for all terminal validation
  categories and attempt-two recovery/notification telemetry.
- **Regression:** run the focused files after every task and all project quality
  gates in Task 7.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Add `➕` tasks only when required to satisfy an acceptance criterion.
- Record blockers or deviations with `⚠️` and include the failing command.
- Update this plan if implementation scope changes.
- Move this plan to `docs/plans/completed/` only after Task 7 passes.

## Implementation Steps

### Task 1: Grant and verify Planner transaction permission

**Finding:** P1 — `PlannerFunction` calls `TransactWriteItems`, but its
current role has only `DynamoDBCrudPolicy` and self-invocation permission.
Normal tracked publication can receive `AccessDenied`, publish no plan, and
leave the workflow in `GENERATING` through the broad exception path.

**Files and symbols:**

- Modify: `template.yaml` — `PlannerFunction.Properties.Policies`
- Modify: `tests/test_template.py` —
  `test_transaction_permission_is_explicit_and_table_scoped`
- Modify: `scripts/verify_transaction_permission.py` —
  `DeploymentResources`, `resolve_resources()`, `verify_permission()`, CLI
  descriptions, and success output
- Modify: `tests/test_verify_transaction_permission.py` — `_clients()` and
  deployed-role permission tests

1. [x] **Test first:** rename or update
   `test_transaction_permission_is_explicit_and_table_scoped` to require
   one exact table-scoped transaction statement on both Bot and Planner, with
   no wildcard resource. Extend verifier fixtures to expose both function
   outputs and distinct role ARNs, require two Lambda configuration lookups and
   two IAM simulations, and add a Planner-denied case.
2. [x] **Expected failure:** run the new tests and confirm the template test
   fails because Planner has no transaction statement, while verifier tests
   fail because only `BotFunctionName` and the Bot role are resolved and
   simulated.
3. [x] **Implementation:** add the exact Planner policy statement and refactor
   the verifier to resolve and fail-closed on both deployed roles. Keep the
   action and resource exact; do not broaden the CRUD policy or use `Resource:
   "*"`.
4. [x] **Verify the fix:** rerun the new tests and confirm both roles are
   checked against `dynamodb:TransactWriteItems` on only the deployed table
   ARN; either role's denial, malformed response, or missing stack output must
   return nonzero.
5. [x] **Regression tests:** run
   `uv run pytest tests/test_template.py
   tests/test_verify_transaction_permission.py` and do not begin Task 2 until
   it passes.

**Acceptance criteria:**

- `PlannerFunction` has an explicit allow for only
  `dynamodb:TransactWriteItems` on `MealPlannerTable.Arn`.
- `BotFunction` retains its existing exact permission.
- The deployed verifier checks both exact execution-role ARNs and fails if
  either role lacks the exact table authorization.
- No wildcard resource or unrelated DynamoDB permission is introduced.
- Tracked publication no longer has a known template-level authorization
  failure.

### Task 2: Publish each untracked asynchronous repair at most once

**Finding:** P2 — `_schedule_repair()` currently queues untracked attempt-two
events without an idempotency key. Lambda's at-least-once replay can persist and
display duplicate revised plans or contradictory stale-result messages.

**Files and symbols:**

- Modify: `src/meal_planner/models/schemas.py` — `PlanGenerationContext`
- Modify: `src/meal_planner/db/dynamo.py` — typed repair-publication outcome,
  marker key construction, transaction cancellation classification, and new
  `save_repaired_draft_once()` operation
- Modify: `src/meal_planner/planner_handler.py` — `generate_plan()`,
  `_schedule_repair()`, and `handle_event()`
- Modify: `tests/test_schemas.py` — generation-context repair ID invariants
- Modify: `tests/test_dynamo.py` — atomic marker/publication and race tests
- Modify: `tests/test_planner_handler.py` — payload forwarding and sequential/
  concurrent replay tests

1. [x] **Test first:** add schema tests requiring a `repair_id` for untracked
   attempt two and rejecting it on attempt one. Update the valid untracked
   attempt-two fixture to include a bounded token. Add a scheduling test that
   captures a nonblank token in the queued payload and an event test proving
   the token is forwarded unchanged.
2. [x] **Test first:** add repository tests for one atomic plan-plus-marker
   success, sequential duplicate rejection, plan-revision conflict, exact
   duplicate-versus-stale cancellation classification, and propagation of
   nonconditional DynamoDB errors. Add a barrier-controlled concurrent test in
   which two calls use the same repair ID and exactly one returns `PUBLISHED`.
3. [x] **Test first:** add sequential and concurrent replay tests around
   `PlannerHandler` using the same complete attempt-two event and repair ID.
   Assert one persisted draft, one `send_plan()` call, one satisfaction summary
   when requirements exist, one review follow-up, and no duplicate/stale
   notification from the losing replay.
4. [x] **Expected failure:** run the new tests and confirm untracked attempt-two
   contexts currently validate without a token, `_schedule_repair()` emits no
   token, `handle_event()` cannot forward one, no atomic repair marker exists,
   and sequential/concurrent replays can each reach persistence or delivery.
   Recorded failures: `uv run pytest tests/test_schemas.py -q` failed with 6
   expected repair-id validation failures (104 passed), and
   `uv run pytest tests/test_schemas.py tests/test_dynamo.py
   tests/test_planner_handler.py` stopped at collection with two expected
   `ImportError: RepairPublicationOutcome` failures.
5. [x] **Implementation:** add the validated `repair_id`, create it once in the
   untracked repair payload with `uuid4()`, and forward it through the event and
   generation context. Implement the plan-plus-marker transaction and typed
   `PUBLISHED`/`STALE`/`DUPLICATE` outcomes. Deliver only after `PUBLISHED` and
   make `DUPLICATE` silent.
6. [x] **Verify the fix:** rerun each new schema, repository, and Planner test.
   Confirm both sequential and concurrent replay have one persistence and one
   delivery sequence, while a true plan conflict still follows the existing
   stale-result path.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_schemas.py tests/test_dynamo.py
   tests/test_planner_handler.py` and do not begin Task 3 until it passes.
   Focused/regression result: 247 passed. Project result: `uv run pytest`
   passed with 560 passed and 2 skipped. Ruff format/check and strict Mypy
   also passed.

**Acceptance criteria:**

- Every queued untracked attempt-two event has a nonblank bounded repair ID,
  and redelivery preserves it unchanged.
- Plan persistence and repair-marker creation are atomic and table-scoped.
- Sequential and concurrent replay of one repair event persist and display at
  most one draft; duplicate workers are silent.
- A marker is never left behind when the plan condition fails, and a plan is
  never written when marker creation fails.
- Tracked attempts keep their state-ownership transaction, direct attempt-one
  generation remains compatible, and each invocation still makes at most one
  provider call.
- Unexpected DynamoDB failures remain visible to existing safe failure
  handling.

### Task 3: Reject normalized-empty food alternatives

**Finding:** P2 — `PreferenceRequirement.reject_duplicate_foods()` currently
accepts a single punctuation-, symbol-, or emoji-only alternative that
`normalize_food()` maps to `()`. `matches_food()` can never match that value, so
the requirement is impossible and can waste two Planner attempts.

**Files and symbols:**

- Modify: `src/meal_planner/models/schemas.py` —
  `PreferenceRequirement.reject_duplicate_foods()`
- Modify: `tests/test_schemas.py` — invalid `foods_any_of` cases
- Modify: `tests/test_parser.py` — malformed interpreted requirement cases

1. [x] **Test first:** add parametrized schema cases for a single `"---"`,
   punctuation-only text, symbols, and emoji, plus mixed lists where one
   otherwise valid alternative normalizes to no tokens. Add valid controls
   containing letters or digits separated by punctuation.
2. [x] **Test first:** add parser cases proving provider requirements with
   punctuation-, symbol-, or emoji-only alternatives return no requirements
   and the existing bounded malformed-requirement clarification.
3. [x] **Expected failure:** run the new tests and confirm a single normalized-
   empty alternative currently passes model validation because the duplicate
   set still has the same cardinality, and the parser therefore accepts an
   impossible requirement. Recorded failure: `uv run pytest
   tests/test_schemas.py tests/test_parser.py -q` failed with 10 expected
   failures (143 passed).
4. [x] **Implementation:** normalize each food once in
   `reject_duplicate_foods()`, reject if any normalized tuple is empty, then
   perform the existing duplicate cardinality check on those results. Keep
   `normalize_food()` and `matches_food()` semantics unchanged.
5. [x] **Verify the fix:** rerun the new schema and parser tests and confirm all
   normalized-empty alternatives fail while valid punctuation boundaries and
   existing normalized duplicate behavior remain accepted/rejected as
   appropriate.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_schemas.py tests/test_parser.py
   tests/test_preferences.py` and do not begin Task 4 until it passes.
   Focused verification: 153 passed for schema/parser tests; regression result:
   181 passed. Project quality gates passed with `uv run ruff format --check
   .`, `uv run ruff check .`, `uv run mypy`, and `git diff --check`. Full
   verification: `uv run pytest -q` passed with 574 passed and 2 skipped.

**Acceptance criteria:**

- Every `foods_any_of` entry normalizes to at least one matchable token.
- A single punctuation-, symbol-, or emoji-only alternative is rejected.
- A mixed list is rejected when any one alternative normalizes to empty.
- Existing Unicode, case, punctuation-boundary, whitespace, plural, duplicate,
  and evidence-matching contracts remain unchanged.
- Parser rejection stays recoverable and cannot dispatch an impossible rule.

### Task 4: Bound interpreter clarifications before Telegram delivery

**Finding:** P2 — `parse_preference_interpretation()` currently accepts
unbounded `clarification` and `unparsed_text`, then joins clauses without a
final cap. Provider output can be split into many synchronous Telegram calls,
undermining the timeout and bounded-clarification contract.

**Files and symbols:**

- Modify: `src/meal_planner/llm/parser.py` — named clarification limits,
  envelope validation, final bounded-render helper, and
  `parse_preference_interpretation()`
- Modify: `tests/test_parser.py` — provider-envelope and final-render limits
- Modify: `tests/test_bot_handler.py` — final clarification delivery bound

1. [x] **Test first:** add parser tests for provider clarification over 500
   characters, more than the allowed number of `unparsed_text` clauses, one
   oversized clause, and individually valid clauses whose joined rendering
   exceeds 500 characters. Add exact-boundary controls.
2. [x] **Test first:** add a Bot workflow test that feeds an oversized
   interpretation response and asserts the saved workflow remains
   `AWAITING_PREFERENCE`, Planner is not invoked, exactly one bounded fallback
   is passed to `send_message()`, and that message is at most 500 characters
   and one Telegram chunk.
3. [x] **Expected failure:** run the new tests and confirm oversized
   clarification is returned verbatim, unlimited clauses are joined, an
   oversized clause survives, and the final Bot message can exceed Telegram's
   single-message boundary and trigger multiple synchronous sends. Recorded
   failure: `uv run pytest tests/test_parser.py tests/test_bot_handler.py -q`
   failed with 5 expected failures (122 passed).
4. [x] **Implementation:** validate raw clarification length, clause count, and
   clause length before rendering. Route every parser-produced clarification
   through one helper that guarantees the 500-character final cap. Return a
   concise application-owned rephrase fallback for any envelope or rendered
   overflow instead of forwarding or semantically truncating provider text.
5. [x] **Verify the fix:** rerun each new parser and Bot test and confirm all
   overflow paths are bounded, deterministic, recoverable, and single-message,
   while valid focused clarifications and unparsed clauses retain their current
   wording.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_parser.py tests/test_bot_handler.py
   tests/test_telegram_api.py` and do not begin Task 5 until it passes.
   Focused/regression result: 141 passed. Full result: `uv run pytest -q`
   passed with 583 passed and 2 skipped. Ruff format/check and strict Mypy
   also passed.

**Acceptance criteria:**

- Raw clarification text, clause count, each clause, and final rendered output
  all have explicit named limits.
- Every return value in the clarification position is at most 500 characters.
- Oversized provider content is replaced with one concise fallback and is not
  echoed or ambiguously truncated.
- The Bot remains in `AWAITING_PREFERENCE`, saves the user's bounded original
  preference, and never invokes Planner for these cases.
- A clarification causes at most one synchronous Telegram `sendMessage`
  request.

### Task 5: Measure terminal validation failures from generation start

**Finding:** P3 — `_finish_failed_generation()` currently passes
`time.monotonic()` directly as `started_at` to `_log_safe_failure()`. The log
reads the clock immediately and reports near-zero rather than the actual
generation/validation duration.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` — `generate_plan()` call sites
  and `_finish_failed_generation()`
- Modify: `tests/test_planner_handler.py` — deterministic terminal validation
  elapsed tests

1. [x] **Test first:** add deterministic `time.monotonic()` tests for terminal
   `structural`, `completeness`, and `compliance` failures. Assert each matching
   log record reports the known nonzero duration from the start of the affected
   generation operation and retains bounded privacy-safe metadata.
2. [x] **Expected failure:** `uv run pytest
   tests/test_planner_handler.py::test_terminal_validation_failures_measure_from_generation_start
   -q` failed all three cases before implementation: each category logged
   approximately `0 ms` from the fresh logging timestamp instead of the
   arranged `400 ms` generation duration.
3. [x] **Implementation:** make `_finish_failed_generation()` accept the
   relevant operation `started_at` and pass the `generate_plan()` start through
   every terminal structural/completeness/compliance path. Remove the fresh
   timestamp used only at logging time. Do not change timeout/transient/
   permanent/response-format logging already measured by `_generate_once()`.
4. [x] **Verify the fix:** reran the deterministic tests; all three cases pass
   with the arranged generation duration, nonnegative bounded elapsed values,
   and privacy-safe bounded metadata.
5. [x] **Regression tests:** `uv run pytest tests/test_planner_handler.py`
   passed (`91 passed`). Full quality verification also passed: `uv run pytest`
   (`586 passed, 2 skipped`), `uv run ruff format --check .`, `uv run ruff check
   .`, `uv run mypy`, and `git diff --check`.

**Acceptance criteria:**

- Structural, completeness, and compliance terminal logs measure from an
  actual generation/validation start, not a timestamp captured at the log.
- Deterministic tests prove meaningful nonzero elapsed values for all three
  categories.
- Existing repair, recovery, provider-failure, and delivery timers keep their
  operation-local semantics.
- Control flow, user messages, persistence, and privacy-safe metadata are
  unchanged.

### Task 6: Propagate attempt two through recovery and notification telemetry

**Finding:** P3 — `_retain_retry_state()` and `_notify_failure()` hard-code
`attempt=1`, so retry-state recovery and failure-notification logs mislabel
attempt-two incidents.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` — `_retain_retry_state()`,
  `_notify_failure()`, `_finish_failed_generation()`, `generate_plan()`, and
  all grocery/revision call sites that have an explicit attempt-one context
- Modify: `tests/test_planner_handler.py` — attempt-two recovery and
  notification telemetry tests

1. [x] **Test first:** added attempt-two tests for retry-state recovery read
   failure, retry-state recovery write failure, and failure-notification
   delivery failure. Each captures the safe log record, asserts
   `record.attempt == 2`, and retains elapsed and privacy assertions.
2. [x] **Expected failure:**
   `uv run pytest tests/test_planner_handler.py -k
   'attempt_two_recovery or attempt_two_notification' -q` failed with 3
   failures: recovery-read, recovery-write, and notification records each
   reported `attempt == 1` for an attempt-two generation context.
3. [x] **Implementation:** added an explicit current-attempt parameter to
   `_retain_retry_state()` and `_notify_failure()`. The generation attempt now
   flows through `_finish_failed_generation()` and the top-level generation
   exception path. All callers were audited; grocery and revision operations
   pass `attempt=1` explicitly.
4. [x] **Verify the fix:** reran the focused tests; all 3 pass, with attempt-two
   recovery-read, recovery-write, and notification records reporting 2.
   Existing attempt-one and non-repair telemetry remains covered by the
   planner regression suite.
5. [x] **Regression tests:** `uv run pytest tests/test_planner_handler.py -q`
   passed with 94 tests. Full verification also passed: `uv run pytest -q`
   (589 passed, 2 skipped), Ruff format/check, and strict Mypy.

**Acceptance criteria:**

- Recovery-read, recovery-write, and notification failures retain the actual
  Planner generation attempt.
- Attempt-two incidents emit `record.attempt == 2`; attempt-one and non-repair
  operations remain labeled 1.
- Elapsed timing and privacy-safe log fields remain correct.
- No recovery, notification, stale-request, or publication behavior changes
  beyond telemetry metadata.

### Task 7: Verify all findings and project quality gates

**Files and symbols:**

- Modify if needed: only implementation and test files named in Tasks 1-6
- Modify during implementation: this plan's progress and verification record
- Move only after every gate passes:
  `docs/plans/2026-08-19-remediate-review-findings-follow-up.md` to
  `docs/plans/completed/2026-08-19-remediate-review-findings-follow-up.md`

1. [x] verify all six findings have a passing focused test and every acceptance
   criterion in Tasks 1-6 is satisfied
2. [x] run
   `uv run pytest tests/test_template.py
   tests/test_verify_transaction_permission.py` and confirm exact table-scoped
   Bot and Planner transaction authorization
3. [x] run
   `uv run pytest tests/test_schemas.py tests/test_dynamo.py
   tests/test_planner_handler.py` and confirm repair-token validation, atomic
   marker publication, sequential replay, concurrent replay, terminal timing,
   and attempt propagation
4. [x] run
   `uv run pytest tests/test_parser.py tests/test_bot_handler.py
   tests/test_preferences.py tests/test_telegram_api.py` and confirm normalized-
   empty rejection and one-message clarification bounds
5. [x] run `uv run ruff format --check .` and fix all formatting failures with
   Ruff
6. [x] run `uv run ruff check .` and fix all lint failures
7. [x] run `uv run mypy` and fix all strict type errors
8. [x] run `uv run pytest` until the full suite passes; the result must exceed
   the 543-passed baseline, with no new skip or xfail, and the ordinary
   artifact-free workspace may retain only the same 2 SAM-artifact-dependent
   skips
9. [x] run `git diff --check` and inspect the complete diff for accidental
   dependency, unrelated configuration, README, template, test, or archived-
   plan changes outside the stated scope
10. [x] confirm the completed implementation plan at `docs/plans/completed/`
    `2026-08-19-remediate-meal-plan-preference-validation-review-findings.md`
    still has SHA-256
    `0a7e76d3d89eb99fba15c937b8e8f6df82eca62aa8959a27ea14cc700a4b7443`
    and the earlier evidence-validation plan still has SHA-256
    `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`
11. [x] record exact focused/full commands and results in this plan, mark every
    checkbox complete, and archive this plan only after all gates pass

### Task 7 Verification Record

All six findings have focused coverage in the Task 1-6 regression files. The
tests cover both exact Bot and Planner transaction authorization, repair-token
validation and atomic publication/replay, normalized-empty rejection, bounded
single-message clarification delivery, operation-local terminal timing, and
attempt-two recovery/notification telemetry. All Task 1-6 acceptance criteria
remain satisfied by the passing results below.

Exact commands and results:

- `uv run pytest tests/test_template.py
  tests/test_verify_transaction_permission.py` — 33 passed, 2 skipped in
  0.25s. The two skips are the generated SAM artifact import checks; the
  artifact is absent in this ordinary artifact-free workspace.
- `uv run pytest tests/test_schemas.py tests/test_dynamo.py
  tests/test_planner_handler.py` — 263 passed in 5.16s.
- `uv run pytest tests/test_parser.py tests/test_bot_handler.py
  tests/test_preferences.py tests/test_telegram_api.py` — 169 passed in
  2.07s.
- `uv run ruff format --check .` — 73 files already formatted.
- `uv run ruff check .` — all checks passed.
- `uv run mypy` — success: no issues found in 19 source files.
- `uv run pytest` — 589 passed, 2 skipped in 5.37s. This exceeds the
  543-passed baseline and retains only the same two SAM-artifact-dependent
  skips; there were no new skips or xfails.
- `git diff --check` — passed with no output.
- `sha256sum
  docs/plans/completed/2026-08-19-remediate-meal-plan-preference-validation-review-findings.md
  docs/plans/completed/2026-08-19-enforce-meal-plan-preferences-with-evidence-based-validation.md`
  — predecessor hashes remained
  `0a7e76d3d89eb99fba15c937b8e8f6df82eca62aa8959a27ea14cc700a4b7443` and
  `8730053fa5886f79fbb3b63dfe82ffcde2f5104859a56d585a0c36eb735207d8`,
  respectively.

Scope inspection found no `pyproject.toml` or `uv.lock` changes, no dependency
changes, and no unrelated implementation changes. The accumulated Tasks 1-6
files are limited to the review findings, their tests, and the associated
preference-validation documentation and archived plans.

Assumptions and risks retained: generated SAM artifacts were intentionally not
present, so the two artifact-dependent checks remain skipped; live AWS role
authorization, asynchronous Lambda dispatch acceptance, and external Telegram
exactly-once delivery still require post-deployment verification as described
below. No unresolved Task 7 blocker remains.

**Acceptance criteria:**

- All six findings are closed by focused regression coverage.
- Ruff format, Ruff lint, strict Mypy, full Pytest, and `git diff --check` pass.
- The full suite has no new failures, skips, or xfails relative to the 543
  passed/2 skipped baseline.
- Both archived predecessor plans remain byte-for-byte unchanged.
- The verification record contains exact commands, counts, and any retained
  skip reasons.

## Residual Risks

The review identified these residual risks. They remain explicit even where a
task reduces part of the exposure:

- Two tests depend on generated SAM artifacts and were skipped in the baseline
  run. Source/template tests do not replace a clean artifact build and import
  check.
- LLM semantic completeness remains probabilistic beyond the deterministic
  measurable validation contract.
- The asynchronous Lambda 202 handoff lacks a durable dispatch marker and a
  configured failure destination. Task 2 adds a durable publication marker for
  one untracked repair event, but it does not prove dispatch acceptance or
  recover an event that exhausts asynchronous delivery retries.
- Live AWS transaction authorization remains unverified until the updated stack
  is deployed and both roles are checked against the real table.
- Telegram delivery races and partial network failures remain unverified in a
  live environment. Atomic persistence and replay suppression provide
  at-most-once publication/delivery initiation, not an exactly-once external
  Telegram guarantee.

## Post-Completion

**Manual and external verification:**

- Build clean SAM artifacts and run the artifact-required tests on a compatible
  Linux ARM64 Python 3.14 environment so the two baseline skips become passing
  checks.
- Deploy through a reviewed pull request from a dedicated branch, never
  directly to `master`.
- Run the read-only transaction-permission verifier against the deployed stack
  and confirm explicit allows for both Bot and Planner roles on the exact table
  ARN.
- Exercise a tracked plan generation and an untracked repair replay in a test
  environment; inspect DynamoDB, Telegram, and privacy-safe CloudWatch logs.
- Verify the configured Lambda asynchronous retry policy and decide separately
  whether a durable dispatch record or failure destination is required.
- Commit with a Conventional Commit message containing the associated issue
  number, then comment on the issue with the commit or pull request link.

No GitHub issue is created during this planning-only phase because the request
authorizes only creation of this plan document.
