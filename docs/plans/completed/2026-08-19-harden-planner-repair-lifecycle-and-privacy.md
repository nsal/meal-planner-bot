# Harden Planner Repair Lifecycle and Privacy

## Overview

Remediate the six actionable findings from the read-only review of the
completed Phase 3 remediation. The work removes provider exception text from
LLM retry logs, prevents stale tracked Planner events from reaching the
provider, makes untracked repair identity stable across redelivery, bounds and
budgets asynchronous Lambda repair dispatch, enforces the 20-requirement
interpretation contract at the parser boundary, and fixes exact-boundary
terminal message compaction.

This is a planning-only document. No implementation, test, README,
configuration, or archived-plan change is included. Implementation must use
TDD for every finding: add and run the missing test first, record the expected
failure that demonstrates the defect, make only the scoped change, rerun the
new test to green, and run the listed regression tests before continuing.

## Context

### Completed baseline

- The original implementation plan is archived at
  `docs/plans/completed/2026-08-19-remediate-phase-3-review-findings.md`.
- Its SHA-256 hash at planning time is
  `f3b19b4920008a617173fc1bb09d14422eaf300d50d898a3cc25764fbe1e46cd`.
- Task 1 bounded requirement summaries and terminal messages to 1,000
  characters, budgeted three Telegram sends and 290 seconds, and passed all
  focused and full gates.
- Task 2 preserved exact meal slots in repair feedback and added the complete
  generated-plan prompt contract.
- Task 3 corrected `-ies` normalization across matching, schemas, and parsing.
- Task 4 added an actionable reset for 498-500 character clarification
  overflow.
- Task 5 sanitized invalid model labels to `unknown`.
- Task 6 passed focused suites, Ruff formatting and linting, strict Mypy, full
  Pytest with 645 passed and two existing SAM-artifact skips,
  `git diff --check`, and archived-plan hash checks.

### Relevant implementation patterns

- `LLMClient._execute_with_retry()` currently interpolates `str(exc)` into a
  warning before classifying the failure as transient or permanent.
- `PlannerHandler.generate_plan()` builds the prompt and calls
  `_generate_once()` before strongly checking ownership for tracked requests;
  the existing post-provider check protects publication races and must remain.
- `_schedule_repair()` creates a new UUID for every untracked attempt-one
  execution and creates an unconfigured boto3 Lambda client immediately before
  asynchronous dispatch.
- `PlanGenerationContext` and Planner event handling already carry an optional
  `repair_id`, and repaired publication already has a duplicate outcome. The
  missing guarantee is a stable identity beginning with the original
  untracked attempt-one event.
- Planner generation currently budgets a 240-second provider call, three
  10-second Telegram sends, and a 20-second margin. Repair dispatch is not an
  explicit budgeted tail.
- `parse_preference_interpretation()` validates individual requirements but
  does not reject a list longer than the `ConversationState` limit of 20.
- The terminal clause formatter counts its fixed suffix twice while deciding
  which complete clauses fit in the 1,000-character application limit.

### Project constraints

- Follow `AGENTS.md` and `pyproject.toml`: Python 3.14, Ruff at 80 columns,
  strict Mypy, and Pytest.
- Use `uv run` for repository tools and Ruff for all Python formatting and
  linting. Add type hints to every Python change.
- Add no dependency; boto3/botocore and the standard library are sufficient.
- Preserve one provider call per Planner invocation, the existing post-call
  ownership check, transactional publication, confirmed-plan protection,
  repaired-publication duplicate suppression, and bounded user output.
- Never log raw prompts, preferences, plans, meals, ingredients, identifiers,
  raw events, credentials, or provider exception text.
- Preserve the current 645-passed/two-skipped baseline. The same two
  SAM-artifact-dependent skips may remain in a workspace without `sam build`
  output; no new skip or xfail is allowed.
- Do not modify an archived plan. During implementation, update only this
  active plan's progress and verification records, then archive it only after
  all final gates pass.
- Implementation must occur on a dedicated branch and through a pull request,
  never directly on `master`. A later commit must use Conventional Commits and
  include the associated issue number.

## Complete Review Findings

1. **P1 — Provider exception text leaks preference data.**
   `LLMClient._execute_with_retry()` logs the provider exception verbatim. A
   real-client probe emitted `preference=PRIVATE_LOW_SODIUM`. This is the
   highest-priority finding because provider failures can copy private dietary
   or medical preferences into durable CloudWatch records.
2. **P2 — Tracked ownership is checked after the provider call.** Canceled,
   replaced, completed, or duplicate tracked events can consume up to the full
   provider allowance before their output is discarded. This wastes model
   cost and Lambda capacity and unnecessarily exposes stale inputs to a
   provider.
3. **P2 — Untracked attempt-one redelivery creates a fresh repair identity.**
   Replaying the original event can queue multiple distinct repairs, allowing
   more than one repaired publication and Telegram delivery despite
   attempt-two duplicate suppression.
4. **P2 — Asynchronous repair dispatch is neither bounded nor budgeted.** The
   default botocore connect/read policy can consume the remaining Planner
   deadline after a long provider request, preventing retry-state recovery and
   user notification.
5. **P2 — More than 20 parsed requirements cross a schema boundary.** The
   parser accepts an out-of-contract provider response, then durable state
   validation fails and the Bot misleadingly tells the user that a valid
   at-most-500-character preference is too long. The update can be retried
   without useful progress.
6. **P3 — Terminal compaction reserves the suffix twice.** Complete clauses
   are omitted even when the final message would fit exactly at 1,000
   characters. This loses useful diagnostics but does not threaten state or
   confidentiality, so it follows the P1 and P2 work.

## Development Approach

- **Testing approach:** TDD.
- Complete tasks strictly in this order:
  1. remove provider exception text from LLM retry logs;
  2. preflight tracked request ownership;
  3. establish stable untracked repair identity;
  4. bound and budget repair dispatch;
  5. enforce the parser's 20-requirement contract;
  6. correct terminal-message exact-boundary accounting;
  7. run final acceptance and repository verification; and
  8. record results and archive this plan.
- Task 1 leads by severity. Tasks 2-4 then follow the repair lifecycle in
  dependency order: stale work is rejected before generation, valid work gets
  a stable identity, and the final dispatch path receives bounded transport
  behavior and budget accounting. Task 5 is independent but remains P2. Task
  6 is the P3 output-quality correction.
- Each remediation task must contain a focused failing test, a recorded
  before-fix result, the minimal implementation, a passing focused rerun, and
  passing regression tests. Do not proceed with an unexplained failure.
- Do not weaken assertions, replace realistic integration seams with tests of
  mocks alone, or redefine a finding to avoid its affected path.
- Mark completed checklist items with `[x]` immediately. Record scope changes
  with `➕` and blockers with `⚠️`, including commands and evidence.

## Solution Overview

The shared LLM retry layer will classify failures once and log only fixed,
bounded operational fields such as attempt number and transient/permanent
category. It will never interpolate the exception object or provider payload.

Tracked generation will strongly read conversation state and validate
`request_id` plus `state_revision` before prompt construction or provider
invocation. The existing post-call ownership read remains to close races that
occur during generation.

Untracked generation will use one event-supplied `repair_id` as the stable
generation/repair identity from attempt one through attempt two. Repair
dispatch will forward it unchanged. Legacy untracked attempt-one events that
lack identity will fail safely without scheduling an unidentifiable repair;
they will use the existing terminal failure/retry notification path. This
avoids manufacturing a new identity on each redelivery.

Default Lambda repair clients will use explicit short botocore connect/read
timeouts and a single total SDK attempt. Configuration validation will model
the mutually exclusive post-provider tails: either three success-path
Telegram sends, or bounded repair dispatch followed by one failure
notification. The larger tail is included in the Planner deadline check.

The interpretation parser will convert a valid-looking response with more
than 20 requirements into one fixed, bounded over-complexity clarification.
The Bot will claim the update and durably retain a clarification-ready state,
without invoking Planner or blaming the user's character count.

Finally, terminal compaction will count the fixed suffix once and reserve only
the newline plus omission marker while selecting whole clauses. Exact-fit
messages keep every clause; one-character-over messages omit whole clauses
with a deterministic count.

## Technical Details

### Privacy-safe LLM retry records

- In `LLMClient._execute_with_retry()`, determine transient/permanent status
  once per caught exception and reuse it for both logging and retry behavior.
- Emit a fixed warning template containing only bounded values owned by the
  application, for example the one-based attempt, configured maximum, and a
  fixed category. Do not include `exc`, `str(exc)`, `repr(exc)`, exception
  arguments, response bodies, headers, request data, or provider messages.
- Preserve retry count, retry-after parsing, delay behavior, return values,
  and existing permanent/max-retry terminal records.

### Tracked ownership preflight

- Add one narrowly named helper or equivalent block that recognizes a fully
  tracked context only when both `request_id` and `state_revision` are set.
- Before profile/history retrieval, prompt construction, and `_generate_once`,
  strongly read conversation state and call the existing `_request_matches()`
  predicate. Return silently for missing, replaced, completed, wrong-revision,
  or otherwise stale tracked state.
- Retain the current post-provider ownership read and publication condition;
  it protects against cancellation or replacement after preflight succeeds.
- Do not apply tracked-state ownership rules to legacy untracked generation.

### Stable untracked repair identity

- Treat a valid event-supplied `repair_id` as the stable identity for an
  untracked generation from attempt one through repaired publication.
- Update `_schedule_repair()` to forward `context.repair_id` unchanged for
  untracked work. Remove the per-execution `uuid4()` fallback from this path.
- Ensure every repository-owned producer of a new untracked attempt-one
  `GENERATE_PLAN` event supplies one bounded valid identity before dispatch.
  Tracked requests continue to use their request ownership fields and do not
  need a repair marker.
- For a legacy untracked attempt-one event with no identity, do not enqueue an
  asynchronous repair that cannot be deduplicated. Return the existing
  failure status so `generate_plan()` follows terminal recovery/notification.
- Preserve `save_repaired_draft_once()` as the atomic publication guard. Do
  not weaken its `PUBLISHED`, `STALE`, or `DUPLICATE` semantics.

### Bounded repair dispatch and budget

- Add positive, narrowly bounded Planner repair connect/read timeout settings
  in `config.py`, with short defaults. Keep SDK `total_max_attempts` fixed at
  one so botocore cannot add an unbudgeted retry/backoff sequence.
- Build only the default boto3 Lambda client with
  `botocore.config.Config(connect_timeout=..., read_timeout=...,
  retries={"mode": "standard", "total_max_attempts": 1})`. Preserve injected
  `lambda_client` behavior for unit tests.
- Compute Planner generation budget as provider attempts and retry delay plus
  the handler safety margin plus the larger of:
  - three Telegram allowances for successful plan, summary, and follow-up
    delivery; or
  - repair connect/read allowance plus one Telegram allowance for terminal
    recovery after a failed dispatch.
- Preserve the current 290-second default when the three-send success tail is
  larger, but reject configurations whose repair tail causes the total to
  exceed the Planner function timeout.
- On client creation, connect, read, or non-202 failure, retain the safe
  `repair_dispatch` log category and return failure so the caller reaches
  `_finish_failed_generation()` while time remains. No raw SDK exception may
  be logged.

### Interpretation requirement bound

- Define or reuse one named maximum of 20 at the parser/application boundary;
  avoid duplicating an unexplained literal where a shared schema constant is
  available.
- After validating that the response contains a list but before constructing
  durable state, treat a list longer than 20 as an over-complex
  interpretation. Return the existing clarification result shape with one
  fixed, bounded message that asks the user to combine or prioritize rules.
- Do not describe this case as a character-length error. Exactly 20 valid
  requirements remain accepted.
- Bot handling must claim the update once, retain the preference and request
  identity in its normal clarification state, send the bounded clarification,
  and avoid Planner dispatch.

### Exact terminal-message accounting

- In the bounded unmet-clause formatter in `preferences.py`, count the fixed
  terminal suffix exactly once.
- When an omission line is needed, reserve only its separator/newline and the
  deterministic omission marker while selecting complete clauses.
- Never slice a clause or application-owned recovery suffix. Preserve
  whitespace normalization, the 1,000-character aggregate maximum, and
  deterministic omission counts.

## Testing Strategy

- **Privacy:** exercise the real `LLMClient.chat_sync()` wrapper with mocked
  provider failures and inspect all captured records through `caplog`.
- **Ownership:** use tracked attempt-one and attempt-two events whose durable
  state is absent, replaced, completed, or revision-mismatched. Assert the LLM,
  repair dispatcher, persistence, and Telegram are untouched.
- **Repair idempotency:** redeliver one untracked attempt-one event twice,
  process both emitted attempt-two payloads, and assert one atomic publication
  and one delivery.
- **Dispatch deadline:** inspect the botocore client configuration, test exact
  budget boundaries, and simulate connect/read timeout failures through the
  complete tracked recovery path.
- **Parser/Bot integration:** compare exactly 20 with 21 valid requirements,
  including user-visible wording, durable state, update claiming, and Planner
  call count.
- **Message boundaries:** construct an exact 1,000-character terminal message
  and a corresponding one-character-over input.
- **Regression:** run focused files after each task, then all repository gates
  in Task 7.

## Progress Tracking

- Mark checklist items complete immediately after evidence exists.
- Add exact failing and passing commands and counts beneath each task.
- Add `➕` only for work required by an acceptance criterion.
- Add `⚠️` for blockers, unexpected failures, or deviations and preserve the
  relevant evidence.
- Do not start a later task until the current task's focused and regression
  commands pass, unless a recorded blocker makes the later task both safe and
  independent.

## Implementation Steps

### Task 1: Remove provider exception text from LLM retry logs

**Finding:** P1 — Raw provider exception text can contain private preference
or request data and is currently written to durable logs.

**Files and symbols:**

- Modify: `src/meal_planner/llm/client.py` —
  `LLMClient._execute_with_retry()`
- Modify: `tests/test_llm_client.py` — retry logging through
  `LLMClient.chat_sync()`
- Modify: `tests/test_bot_handler.py` — preference-interpreter integration if
  needed to prove the real Bot call path

1. [x] **Test first:** monkeypatch the provider call used by a real
   `LLMClient` to raise transient and permanent exceptions whose text contains
   `preference=PRIVATE_LOW_SODIUM`, a newline, and request-like content. Call
   `chat_sync()` and capture every record with `caplog`.
2. [x] **Test first:** assert the sensitive marker and exception text are
   absent from `record.getMessage()`, structured fields, and all captured log
   output; require only bounded application-owned attempt and failure-category
   metadata. Assert retry counts and terminal behavior remain unchanged.
3. [x] **Expected failure:** run the focused tests before implementation and
   record that the warning contains `preference=PRIVATE_LOW_SODIUM` because
   `_execute_with_retry()` interpolates the exception.
4. [x] **Implementation:** classify the exception once, log only fixed bounded
   metadata, and reuse the classification for the unchanged retry decision.
   Never pass the exception object or any derivative of its text to logging.
5. [x] **Verify the fix:** rerun the focused tests and confirm all records are
   privacy-safe for both transient and permanent failures while the expected
   number of provider attempts still occurs.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_llm_client.py tests/test_bot_handler.py` and do not
   begin Task 2 until it passes.

**Acceptance criteria:**

- No provider exception text or sensitive marker appears in any retry or
  terminal log record reached through `chat_sync()`.
- Retry warnings contain only bounded application-owned metadata.
- Transient classification, retry-after handling, backoff, maximum attempts,
  permanent failure handling, and return values remain unchanged.
- Preference interpretation still uses the real shared client path and gains
  the same privacy guarantee without Bot-specific exception logging.

**Verification record:** The pre-fix focused run showed provider exception text
in retry warnings. The fixed focused run passed both transient and permanent
privacy cases, and `uv run pytest tests/test_llm_client.py
tests/test_bot_handler.py` passed 106 tests.

### Task 2: Preflight tracked ownership before Planner generation

**Finding:** P2 — Stale tracked events currently consume a provider call
before ownership is checked.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` —
  `PlannerHandler.generate_plan()`, `_request_matches()`, and a focused
  ownership-preflight helper if introduced
- Modify: `tests/test_planner_handler.py` — stale tracked attempt-one and
  attempt-two event coverage

1. [x] **Test first:** add parametrized tracked attempt-one and attempt-two
   cases for absent state, a replacement request ID, a changed revision, and a
   state that no longer represents the active generation. Use a strongly read
   repository result in each case.
2. [x] **Test first:** assert stale events do not construct/call any LLM
   method, schedule a repair, save or replace a plan, mutate conversation
   state, or send Telegram output. Retain a current-owner control that makes
   exactly one provider call.
3. [x] **Expected failure:** run the focused tests before implementation and
   record that each stale event reaches `_generate_once()` before the existing
   post-call ownership check discards it.
4. [x] **Implementation:** perform a strong ownership read and existing
   `_request_matches()` validation immediately after context validation and
   before profile/history reads, prompt construction, or provider invocation.
   Return silently for stale tracked work and retain the post-call check for
   races during generation.
5. [x] **Verify the fix:** rerun the focused matrix and confirm every stale
   attempt exits without side effects while current tracked work still
   generates and publishes normally.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py tests/test_dynamo.py` and do not
   begin Task 3 until it passes.

**Acceptance criteria:**

- Stale tracked attempt-one and attempt-two events make zero provider calls.
- Stale preflight exits make no repair, persistence, state-transition, or
  notification call.
- Current tracked events still make one provider call and retain existing
  publication semantics.
- The post-provider ownership check remains and suppresses a request that
  becomes stale while generation is in progress.
- Untracked generation behavior is not changed by the ownership preflight.

**Verification record:** The pre-fix matrix failed all 8 stale cases because
profile/provider work continued after the state read. The fixed matrix passed
all 8 cases, and the Planner/Dynamo/schema regression run passed 298 tests.

### Task 3: Carry one stable identity through untracked repair redelivery

**Finding:** P2 — A fresh `uuid4()` on every redelivered untracked
attempt-one event can create multiple independently publishable repairs.

**Files and symbols:**

- Modify: `src/meal_planner/planner_handler.py` — `PlanGenerationContext`,
  `PlannerHandler.handle_event()`, and `_schedule_repair()`
- Modify if an untracked producer exists: the repository-owned
  `GENERATE_PLAN` event construction site, supplying one initial `repair_id`
- Modify: `tests/test_planner_handler.py` — complete attempt-one redelivery
  and attempt-two publication flow
- Modify if event schema tests are separate: `tests/test_schemas.py`

1. [x] **Test first:** construct one untracked attempt-one event with one
   stable bounded `repair_id`, deliver that exact event twice, and capture both
   queued attempt-two payloads. Require both payloads to carry the original
   identity byte-for-byte.
2. [x] **Test first:** process both repair payloads against the real repository
   publication seam and assert exactly one `PUBLISHED` result, one duplicate
   suppression, one persisted replacement revision, and one Telegram plan,
   summary, and follow-up delivery sequence.
3. [x] **Test first:** add a legacy untracked attempt-one event without
   identity. Require no asynchronous repair dispatch and the existing bounded
   terminal failure/retry path instead of a generated per-delivery identity.
4. [x] **Expected failure:** run the focused tests before implementation and
   record that duplicate attempt-one deliveries contain different UUIDs and
   can each publish and deliver a repaired replacement.
5. [x] **Implementation:** forward the event-supplied identity unchanged,
   remove the per-execution UUID fallback, update repository-owned untracked
   event producers to set identity once, and fail safely for legacy missing
   identity. Keep tracked request semantics unchanged.
6. [x] **Verify the fix:** rerun the full redelivery flow and confirm duplicate
   attempt-one delivery cannot create a second repaired publication or
   Telegram delivery.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_planner_handler.py tests/test_dynamo.py
   tests/test_schemas.py` and do not begin Task 4 until it passes.

**Acceptance criteria:**

- One untracked generation event has one stable identity across both attempts
  and all redeliveries.
- `_schedule_repair()` never creates a new identity while handling an already
  delivered attempt-one event.
- Replaying the same original event through both repair payloads produces one
  publication and one external delivery sequence.
- Legacy events without identity do not schedule an undeduplicable repair and
  receive the existing bounded failure/retry behavior.
- Tracked ownership, repair marker bounds, stale outcomes, and atomic
  publication remain unchanged.

**Verification record:** The pre-fix focused run showed rejected attempt-one
repair IDs, fresh UUID behavior, and dispatch for legacy events without an
identity. The fixed end-to-end redelivery test passed with one atomic
publication and one delivery sequence; the Planner/Dynamo/schema regression
run passed 298 tests.

**Tasks 1–3 final gates:** `uv run ruff format --check .` passed with 75
formatted files; `uv run ruff check .` passed; `uv run mypy` passed for 19
source files; `git diff --check` passed; and `uv run pytest` passed 657 tests
with the two pre-existing SAM-artifact skips. Tasks 4–8 remain pending.

### Task 4: Bound and budget asynchronous repair dispatch

**Finding:** P2 — Unconfigured botocore connect/read behavior can outlive the
remaining Planner allowance and prevent terminal recovery.

**Files and symbols:**

- Modify: `src/meal_planner/config.py` — Planner repair timeout settings and
  `validate_function_budgets()`
- Modify: `src/meal_planner/planner_handler.py` — default Lambda client
  construction, `_schedule_repair()`, and dispatch failure handling
- Modify: `tests/test_config.py` — default and exact-boundary budget tests
- Modify: `tests/test_planner_handler.py` — botocore configuration and stalled
  dispatch recovery tests

1. [x] **Test first:** intercept default `boto3.client("lambda", ...)`
   construction and assert explicit short connect/read timeouts, standard retry
   mode, and one total SDK attempt. Keep an injected-client control.
2. [x] **Test first:** add configuration tests for the exact formula using the
   larger of the three-send success tail and repair-dispatch-plus-failure-send
   tail. Require the current defaults to remain valid at 290 seconds and a
   repair timeout combination that crosses the function deadline to fail.
3. [x] **Test first:** simulate connect timeout, read timeout, and non-202
   dispatch after a failed tracked attempt-one generation. Assert one safe
   `repair_dispatch` record with no exception text, transition of the owned
   request to its existing retry-ready state where possible, and one bounded
   actionable notification. Assert no repair was reported as queued.
4. [x] **Expected failure:** run the focused tests before implementation and
   record that the default client lacks explicit timeout/retry configuration,
   configuration accepts an unbudgeted dispatch tail, and a stalled dispatch
   can consume the recovery allowance.
5. [x] **Implementation:** configure the default botocore Lambda client with
   bounded connect/read settings and one total attempt, include the alternative
   repair tail in Planner budget validation, and preserve the caller's
   `_finish_failed_generation()` path for every dispatch failure.
6. [x] **Verify the fix:** rerun client, budget, and recovery tests and confirm
   dispatch is bounded early enough for retry-state transition and
   notification under every configured-valid boundary.
7. [x] **Regression tests:** run
   `uv run pytest tests/test_config.py tests/test_planner_handler.py` and do not
   begin Task 5 until it passes.

**Acceptance criteria:**

- The default repair client has explicit short connect/read timeouts and does
  not perform an unbudgeted SDK retry.
- Planner configuration rejects every calculated provider-plus-tail budget
  above the function timeout.
- The default remains 290 seconds when the existing three-send success tail is
  the larger tail.
- Connect, read, client-creation, and non-202 failures are safely logged
  without exception text and reach terminal recovery while budget remains.
- Injected Lambda clients, event shape, one-provider-call behavior, and
  successful 202 dispatch semantics remain unchanged.

**Verification record:** The pre-fix focused run failed 5 tests because repair
settings and the default botocore policy were absent and the new recovery
matrix could not observe a dispatched failure. The fixed focused regression
run passed 157 tests, including connect/read/non-202 recovery, exact 319/320
second-tail boundaries, and the 3-second connect/10-second read client policy.

### Task 5: Enforce the 20-requirement interpretation bound

**Finding:** P2 — The parser accepts more requirements than durable state,
causing a misleading character-limit response and repeat interpretation work.

**Files and symbols:**

- Modify: `src/meal_planner/llm/parser.py` —
  `parse_preference_interpretation()` and the named requirement bound
- Modify: `src/meal_planner/bot_handler.py` — interpretation result handling
  only if needed to durably claim the bounded clarification
- Modify: `tests/test_parser.py` — exactly-20 and 21-requirement parser cases
- Modify: `tests/test_bot_handler.py` — durable Bot integration behavior

1. [x] **Test first:** add parser fixtures containing exactly 20 and 21
   individually valid requirements. Require 20 to parse normally and 21 to
   return one fixed, bounded over-complexity clarification.
2. [x] **Test first:** drive both responses through the Bot. For 20, require
   the normal generating transition and one Planner invocation. For 21,
   require no Planner invocation, one claimed update, the normal durable
   clarification state retaining request identity and preference, and one
   bounded message that asks the user to combine or prioritize rules without
   saying their preference text is too long.
3. [x] **Expected failure:** run the focused tests before implementation and
   record that 21 requirements pass parser validation, fail later during
   `ConversationState` construction, produce the wrong character-limit
   guidance, and leave the update eligible for repeated interpretation.
4. [x] **Implementation:** enforce the named maximum at the parser boundary and
   return the existing clarification result shape with application-owned
   wording. Adjust Bot handling only as required to persist and claim that
   normal clarification outcome.
5. [x] **Verify the fix:** rerun parser and Bot cases and confirm the exact
   20/21 boundary, correct message, durable state, update claiming, and call
   counts.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_parser.py tests/test_bot_handler.py
   tests/test_schemas.py` and do not begin Task 6 until it passes.

**Acceptance criteria:**

- Exactly 20 valid requirements remain accepted; 21 are rejected before
  durable-state validation.
- The 21-requirement result is a bounded, actionable over-complexity
  clarification, not a character-limit error.
- The Bot claims the update and persists the ordinary clarification state so
  duplicate delivery does not repeat interpretation.
- No Planner call, partial requirement persistence, or unhandled validation
  exception occurs for the 21-requirement response.
- Existing malformed JSON, conflicting rule, provider failure, and valid
  interpretation behavior remains covered.

**Verification record:** The pre-fix parser/Bot run failed 2 boundary tests
because 21 valid requirements passed the parser and failed during state
construction. The fixed parser/Bot/schema regression run passed 266 tests;
exactly 20 rules dispatch normally, while 21 rules persist one bounded
combine/prioritize clarification with the update claimed and no Planner call.

### Task 6: Preserve every terminal clause that fits the 1,000-character bound

**Finding:** P3 — The fixed suffix is reserved twice, causing unnecessary
clause omission at the exact application-owned boundary.

**Files and symbols:**

- Modify: `src/meal_planner/preferences.py` — bounded unmet-clause formatter
  near the current suffix/reservation calculation
- Modify: `tests/test_preferences.py` — exact-fit and one-character-over cases
- Modify: `tests/test_planner_handler.py` — terminal delivery integration if
  needed to prove one-chunk behavior

1. [x] **Test first:** construct clauses whose complete normalized terminal
   message, including the fixed recovery suffix, is exactly 1,000 characters.
   Require every clause to appear and no omission marker.
2. [x] **Test first:** add a corresponding input one character over the bound.
   Require whole-clause omission, an exact deterministic omitted count, a
   total length at most 1,000, and one Telegram chunk.
3. [x] **Expected failure:** run the focused tests before implementation and
   record that the exact-fit case omits clauses because `len(suffix)` is
   counted in both the candidate and reserved space.
4. [x] **Implementation:** count the suffix once and reserve only the omission
   separator and marker when one is needed. Preserve whole-clause semantics
   and all existing bounds.
5. [x] **Verify the fix:** rerun both boundaries and confirm exact-fit retention
   and correct one-character-over compaction.
6. [x] **Regression tests:** run
   `uv run pytest tests/test_preferences.py tests/test_planner_handler.py
   tests/test_telegram_api.py` and do not begin Task 7 until it passes.

**Acceptance criteria:**

- Every whole clause that fits in an exact 1,000-character terminal message is
  retained.
- A one-character-over input remains at most 1,000 characters, omits only
  whole clauses, and reports the exact omission count.
- The fixed no-draft, retained-preference, and `/plan` recovery suffix remains
  intact and appears once.
- Whitespace normalization is deterministic and all maximum-shape terminal
  messages remain one Telegram chunk.

**Verification record:** The pre-fix boundary run failed both new tests: the
exact-fit message omitted both clauses and the one-character-over case used
the wrong omission count. The fixed preferences/Planner/Telegram regression
run passed 188 tests, retaining the exact 1,000-character message and
deterministically omitting one whole clause for the one-character-over case.

### Task 7: Verify all findings and repository quality gates

**Files and symbols:**

- Modify only if a gate exposes a defect: files already named in Tasks 1-6
- Modify during execution: this active plan's progress and verification record

1. [x] confirm all six findings have a recorded before-fix failure, a passing
   focused test, and satisfied acceptance criteria
2. [x] run `uv run pytest tests/test_llm_client.py tests/test_bot_handler.py`
   and confirm provider text cannot enter preference-interpreter logs
3. [x] run
   `uv run pytest tests/test_planner_handler.py tests/test_dynamo.py
   tests/test_config.py` and confirm ownership preflight, stable repair
   identity, duplicate suppression, dispatch bounds, recovery, and budget
   boundaries
4. [x] run
   `uv run pytest tests/test_parser.py tests/test_schemas.py` and confirm the
   exactly-20/21 interpretation boundary and durable-compatible results
5. [x] run
   `uv run pytest tests/test_preferences.py tests/test_telegram_api.py` and
   confirm exact-fit terminal output and one-character-over compaction
6. [x] run `uv run ruff format --check .`; use Ruff only if formatting fixes
   are required
7. [x] run `uv run ruff check .` and fix every lint failure
8. [x] run `uv run mypy` and fix every strict type error
9. [x] run `uv run pytest` until the full suite passes; require more than the
   645-passed baseline by at least the newly added tests, no new skip or xfail,
   and only the two documented SAM-artifact skips when build output is absent
10. [x] run `git diff --check` and inspect the complete accumulated diff for
    unrelated cleanup, dependency changes, raw-data logging, removed race
    checks, unbounded retry behavior, or archived-plan edits
11. [x] run `sha256sum
    docs/plans/completed/2026-08-19-remediate-phase-3-review-findings.md` and
    confirm it still equals
    `f3b19b4920008a617173fc1bb09d14422eaf300d50d898a3cc25764fbe1e46cd`
12. [x] record exact commands, pass/fail counts, skip reasons, assumptions, and
    residual risks in this plan

**Final verification record:** The focused suites passed with 108 tests for
LLM/Bot privacy, 206 for Planner/Dynamo/configuration, 173 for parser/schema,
and 60 for preferences/Telegram. `uv run ruff format --check .` reported 75
formatted files, `uv run ruff check .` passed, `uv run mypy` passed for 19
source files, and `git diff --check` passed. The full `uv run pytest` run
passed 669 tests and skipped exactly two existing SAM-artifact checks because
`.aws-sam/build/BotFunction` and `.aws-sam/build/PlannerFunction` were absent;
no new skip or xfail was added. The accumulated worktree has no dependency or
lockfile changes, and this plan introduced no additional README, deployment,
or archived-plan edits. The implementation remains on the dedicated
`fix/harden-planner-repair-lifecycle` branch. The original archived plan hash
was confirmed as
`f3b19b4920008a617173fc1bb09d14422eaf300d50d898a3cc25764fbe1e46cd`.

**Acceptance criteria:**

- All six review findings are closed by focused regression coverage.
- Ruff format, Ruff lint, strict Mypy, full Pytest, and `git diff --check` pass.
- No provider exception text appears in logs, no stale tracked event calls the
  provider, duplicate untracked redelivery publishes once, and dispatch fits
  the validated deadline budget.
- Exactly 20 requirements remain valid, 21 enter durable clarification, and
  exact-fit terminal clauses are preserved.
- No dependency, unrelated README, deployment-permission, or archived-plan
  change is introduced unless an acceptance blocker is first recorded.

### Task 8: [Final] Record completion and archive this plan

**Files:**

- Modify: `docs/plans/2026-08-19-harden-planner-repair-lifecycle-and-privacy.md`
- Move after every gate passes:
  `docs/plans/2026-08-19-harden-planner-repair-lifecycle-and-privacy.md`
  to `docs/plans/completed/`

1. [x] verify every Task 1-7 checkbox and acceptance criterion is complete or
   has an explicit unresolved blocker with evidence
2. [x] add the final focused/full test counts, Ruff/Mypy results, diff check,
   skip reasons, and retained risks to this plan
3. [x] update `README.md` or `AGENTS.md` only if implementation introduced a
   user/operator-facing setting or a genuinely new repository convention;
   otherwise record that no documentation update was needed
4. [x] move only this completed active plan into `docs/plans/completed/`
5. [x] confirm the archived original plan hash remains unchanged after the move

**Completion record:** No README or AGENTS.md update was needed for this
remediation plan; its repair timeout settings are already covered by the
accumulated implementation documentation. The active plan was archived after
all executable gates passed, and the original archived plan remained
byte-for-byte unchanged.

**Acceptance criteria:**

- The plan accurately reflects the implementation and verification evidence.
- No incomplete item is silently marked complete.
- The new remediation plan is archived only after all executable gates pass.
- The original archived plan remains byte-for-byte unchanged.

## Residual Risks and Blocker Policy

- A Lambda runtime can still be terminated externally before Python catches an
  exception. The bounded SDK policy and validated budget reduce expected
  timeout paths; they do not provide a guarantee against platform
  termination.
- Asynchronous Lambda delivery can exhaust service retries. A dead-letter
  queue or failure destination is outside these findings unless tests prove it
  is required for an acceptance criterion.
- Legacy untracked events have no stable identity. The deliberately safe
  fallback sacrifices automatic repair for those events instead of allowing
  duplicate repaired publication.
- Atomic publication prevents duplicate persisted repairs, but no application
  can prove exactly-once Telegram delivery after an ambiguous network failure.
- Two baseline tests require generated `.aws-sam/build` artifacts. Their
  existing skips are not blockers in an artifact-free workspace, but no new
  skip is acceptable.
- If a task cannot be completed, record the exact blocker and evidence. Later
  tasks may proceed only when they are independent and safe without the failed
  prerequisite.

## Post-Completion

**Manual and external verification:**

- Build clean SAM artifacts on a compatible Linux ARM64 Python 3.14
  environment and run the two artifact-dependent checks.
- In a non-production environment, inject a provider exception containing a
  synthetic secret and confirm CloudWatch records contain only the fixed
  failure metadata.
- Redeliver one untracked attempt-one event and its repair event and confirm
  one persisted repaired draft and one visible delivery sequence.
- Exercise a repair dispatch timeout and confirm the tracked request becomes
  retry-ready and the user receives bounded guidance before the function
  deadline.
- Deploy only through a reviewed pull request from a dedicated branch. Commit
  with a Conventional Commit message containing the associated issue number,
  then comment on the issue with the commit or pull request link.

No GitHub issue is created during this planning-only request because the user
authorized exactly one new remediation-plan file and no other repository or
external-system change.
