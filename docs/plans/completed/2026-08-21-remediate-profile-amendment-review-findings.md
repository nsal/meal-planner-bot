# Remediate Profile Amendment Consistent-Read Review Finding

## Overview

Remediate the single P1 finding from the independent review of commit
`1250d62`. A profile amendment currently loads the profile with DynamoDB's
eventually consistent default and later replaces that profile in a transaction
whose condition protects only the conversation state. Two quick sequential
amendments can therefore let the second amendment overwrite data committed by
the first when its profile read returns a stale item.

Make amendment profile reads strongly consistent before deriving the
replacement document. Keep the existing conversation-state transaction as the
concurrency authority, retain eventual reads for unrelated profile call sites,
and add a deterministic regression that simulates DynamoDB returning stale
data unless `ConsistentRead=True` is requested.

## Context

- **Completed original plan:** `docs/plans/completed/` +
  `2026-08-20-remediate-profile-amendment-review-findings.md`.
- **Reviewed implementation:** commit `1250d62`.
- **Finding location:** `DynamoRepository.get_profile()` in
  `src/meal_planner/db/dynamo.py` performs an eventually consistent read, and
  `BotHandler._handle_profile_edit_input()` in
  `src/meal_planner/bot_handler.py` uses that result to build a complete
  replacement profile.
- **Existing protection:**
  `DynamoRepository.save_profile_and_transition_state()` atomically replaces
  the profile and transitions only the exact observed profile-edit state. It
  does not condition the profile write on an observed profile snapshot.
- **Repository convention:** `get_conversation_state()` and `get_plan()`
  already expose a keyword-only `consistent_read: bool = False` option and add
  `ConsistentRead=True` only when requested.
- **Verified baseline:** the independent review reported 800 tests passing,
  2 pre-existing skips, and green Ruff, mypy, and `git diff --check` results.
- **Scope boundary:** cover only the stale profile-read finding. Do not change
  either completed original plan, reintroduce profile revisions, or broaden
  the transaction and workflow design.

## Review Finding Covered

### P1: Read the profile consistently before replacing it

A completed amendment can be lost when the next amendment receives a stale
profile from an eventually consistent read. The second transaction may still
pass its current conversation-state condition and unconditionally replace the
profile with a document derived from the stale snapshot.

## Development Approach

- **Testing approach:** TDD. Add each regression first, run it, and record the
  expected failure before modifying implementation.
- Extend `DynamoRepository.get_profile()` with the same keyword-only
  `consistent_read` option used by existing repository reads. Keep its default
  `False` so unrelated call sites retain their present consistency and cost
  characteristics.
- Make only `BotHandler._handle_profile_edit_input()` request a strongly
  consistent profile read before applying an amendment.
- Keep `save_profile_and_transition_state()` and its exact workflow-state
  condition unchanged unless a failing test proves an integration adjustment
  is necessary. Do not add profile revision/CAS machinery for this fix.
- Complete each task and its focused tests before starting the next task.
- Use `uv run` for Python tools and Ruff as the only formatter. Keep Python and
  Markdown text within the configured 80-column limit.
- Update this plan when implementation results or scope differ from the
  expected sequence. Mark checkboxes only after the stated evidence exists.

## Testing Strategy

- **Repository contract:** prove ordinary profile reads omit
  `ConsistentRead`, while an explicit consistent read sends
  `ConsistentRead=True` to DynamoDB and returns the current item.
- **Sequential amendment regression:** use the real handler/repository flow
  and deterministically simulate a profile read that returns the pre-amendment
  snapshot unless strong consistency is requested. Commit amendment A, start
  amendment B, and prove B preserves A while adding its own change.
- **Handler contract:** assert the amendment input path explicitly requests a
  consistent profile read and continues to use the atomic profile/state
  transaction with controlled stale and unexpected-failure behavior.
- **Regression gate:** run focused repository and handler tests, then Ruff
  format and lint, mypy, the full pytest suite, and `git diff --check`.
- **No skipped proof:** the new consistency and sequential-amendment tests must
  pass without skip, xfail, network access, or reliance on test ordering.

## Solution Overview

Add `consistent_read: bool = False` as a keyword-only argument to
`DynamoRepository.get_profile()`. Build the `get_item` arguments explicitly
and include `ConsistentRead=True` only when the caller opts in. This matches
existing repository APIs and avoids increasing read cost for profile display,
onboarding, or other unrelated paths.

In `BotHandler._handle_profile_edit_input()`, call
`get_profile(user_id, consistent_read=True)`. DynamoDB then returns a profile
that reflects every successful write completed before the read. The existing
conversation-state condition serializes competing input for the active edit:
only one transaction can consume an observed edit state. Together, the strong
read and state transition prevent a later amendment from being built on the
profile snapshot that preceded an earlier completed amendment.

The preferred fix is a strong read rather than a profile snapshot condition.
It remains consistent with the completed plan's single-writer model and avoids
reintroducing the profile revision/CAS machinery removed by commit `1250d62`.
If independent profile writers are introduced later, this assumption must be
revisited and a profile version or snapshot condition may become necessary.

## Technical Details

### Repository read contract

`DynamoRepository.get_profile()` should have this effective contract:

- `get_profile(user_id)` sends only the profile key and retains DynamoDB's
  eventual-read default;
- `get_profile(user_id, consistent_read=True)` sends the same key plus
  `ConsistentRead=True`;
- missing and legacy profile behavior remains unchanged;
- validation still returns `UserProfile | None` with no persistence side
  effects.

### Sequential amendment scenario

The regression must model this exact sequence:

1. Persist an initial profile and begin amendment A.
2. Apply amendment A and verify its profile/state transaction commits.
3. Begin a distinct amendment B from the resulting current workflow.
4. Configure the profile read boundary to return the initial pre-A item for an
   eventual read and the actual current item for `ConsistentRead=True`.
5. Apply amendment B through `BotHandler._handle_profile_edit_input()`.
6. Assert the final profile contains both A and B exactly once, the state
   returns to the correct profile category menu, and no LLM path is invoked.

Before the handler requests strong consistency, step 5 should demonstrate the
finding by losing amendment A while amendment B's state transaction succeeds.
After the implementation change, the same test must preserve both amendments.

Moto does not reproduce DynamoDB eventual consistency. The regression must
therefore inject stale/current responses at the repository table-read boundary
based on the presence of `ConsistentRead=True`; it must not claim that moto
itself exercised eventual consistency.

## Implementation Steps

### Task 1: Add opt-in strongly consistent profile reads

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

**Symbols:** `DynamoRepository.get_profile`

**TDD sequence:**

- [x] **Test first:** Add a repository test using a mocked table that calls
  `get_profile()` once with the default and once with
  `consistent_read=True`; assert the default request omits `ConsistentRead`
  and the opted-in request includes `ConsistentRead=True`.
- [x] Add a stale/current response case at the same table boundary, proving
  the opted-in read returns the current profile while the default path can
  receive the simulated stale profile.
- [x] **Expected failure:** Run the new tests before implementation and record
  that `get_profile(..., consistent_read=True)` is rejected because the method
  does not yet accept the keyword argument.
- [x] Modify `DynamoRepository.get_profile()` to accept keyword-only
  `consistent_read: bool = False`, construct typed `get_item` arguments, and
  add `ConsistentRead=True` only for the opted-in path.
- [x] Preserve missing-profile handling, legacy validation, return typing, and
  the exact profile key used by existing reads.
- [x] **Verify new tests pass:** Run the new consistent-profile-read tests with
  `uv run pytest tests/test_dynamo.py -q` using a focused test selector where
  practical.
- [x] **Run regressions:** Run `uv run pytest tests/test_dynamo.py -q` and do
  not proceed until the complete repository test module passes.

**Acceptance criteria:**

- [x] Callers can explicitly request a strongly consistent profile read.
- [x] The opt-in path sends `ConsistentRead=True` to DynamoDB.
- [x] Existing callers that omit the option keep the prior request shape and
  eventual-read behavior.
- [x] Profile validation, legacy compatibility, and missing-item behavior are
  unchanged.

### Task 2: Preserve completed changes across sequential amendments

**Severity:** P1

**Depends on:** Task 1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._handle_profile_edit_input`,
`DynamoRepository.get_profile`,
`DynamoRepository.save_profile_and_transition_state`

**TDD sequence:**

- [x] **Test first:** Add a handler assertion that profile amendment input
  calls `get_profile(user_id, consistent_read=True)` before deriving the
  replacement profile.
- [x] **Test first:** Add the real handler/repository sequential-amendment
  regression described in Technical Details. Simulate the initial profile for
  eventual reads and the current stored profile for strong reads.
- [x] Assert amendment A commits before amendment B starts, B's current
  conversation-state condition succeeds, and the final canonical profile
  contains the initial values plus both amendments exactly once.
- [x] Assert the final state is the expected profile category menu, the atomic
  profile/state transaction remains the only amendment write path, and no LLM
  call occurs.
- [x] **Expected failure:** Run the new tests before changing the handler and
  record that it omits `consistent_read=True`; in the sequential scenario B
  therefore replaces the profile derived from the simulated pre-A snapshot
  and loses amendment A despite a successful state condition.
- [x] Modify `BotHandler._handle_profile_edit_input()` to request
  `get_profile(user_id, consistent_read=True)` for amendment text processing.
- [x] Keep no-profile, validation, controlled stale-conflict, unexpected
  transaction failure, success messaging, and category rendering behavior
  unchanged.
- [x] **Verify new tests pass:** Run the focused strong-read and sequential
  amendment tests in `tests/test_bot_handler.py`.
- [x] **Run regressions:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_router.py tests/test_telegram_api.py -q` and resolve every
  regression before proceeding.

**Acceptance criteria:**

- [x] Every deterministic profile amendment is derived from a strongly
  consistent profile read.
- [x] A quick amendment B cannot erase a successfully committed amendment A
  because of a stale profile read.
- [x] The sequential regression deterministically demonstrates the original
  failure and passes only when strong consistency is requested.
- [x] Competing input for one observed workflow remains serialized by the
  existing exact conversation-state condition.
- [x] No profile revision, profile CAS argument, or profile snapshot condition
  is introduced.
- [x] Existing profile amendment success and error behavior remains green.

### Task 3: Verify acceptance criteria and complete plan tracking

**Depends on:** Tasks 1 and 2

**Files:**

- Modify, then move on completion:
  `docs/plans/2026-08-21-remediate-profile-amendment-review-findings.md`
  to `docs/plans/completed/`

**Verification sequence:**

- [x] Re-run the new repository and sequential-amendment tests together and
  confirm neither is skipped, xfailed, or dependent on execution order.
- [x] Inspect the tests and record that eventual consistency is simulated at
  the table-read boundary because moto cannot reproduce it.
- [x] Run `uv run ruff format .` and confirm Ruff is the only formatter used.
- [x] Run `uv run ruff check .` and resolve all diagnostics.
- [x] Run `uv run mypy` and resolve all static typing errors.
- [x] Run `uv run pytest` and require the complete suite to pass, allowing only
  the two documented pre-existing template skips if they remain unchanged.
- [x] Run `git diff --check` and inspect the accumulated diff for unrelated
  implementation, test, or documentation changes.
- [x] Update this plan with actual commands, results, deviations, blockers,
  and retained manual risks. Do not modify either completed original plan.
- [x] Move only this plan to `docs/plans/completed/` after every authorized
  verification check passes.

**Acceptance criteria:**

- [x] The single P1 review finding is covered by repository-contract and
  sequential-amendment regressions.
- [x] Ruff format, Ruff lint, mypy, the full pytest suite, and
  `git diff --check` pass.
- [x] The original eventual-consistency failure is represented honestly and
  does not rely on moto to emulate behavior it does not provide.
- [x] Changes remain limited to `DynamoRepository.get_profile()`, the profile
  amendment read call site, their tests, and this remediation plan.
- [x] Both completed original plans remain unchanged.

### Task 3 verification record

- Focused tests, repository first:
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py -q -k
  'profile_read_consistency or sequential_profile_amendments'` passed with
  3 passed and 206 deselected. The reverse collection order was also run with
  `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py -q -k
  'profile_read_consistency or sequential_profile_amendments'` and passed with
  3 passed and 206 deselected. Neither run reported a skip or xfail.
- The sequential regression patches `repo.table.get_item`. It returns the
  captured pre-amendment item when `ConsistentRead` is absent and delegates to
  the real table for `ConsistentRead=True`. This explicitly simulates stale
  and current responses at the table-read boundary; moto itself does not
  reproduce DynamoDB eventual consistency.
- `uv run ruff format .` passed with 80 files left unchanged. Ruff was the
  only formatter used.
- `uv run ruff check .` passed with all checks passing.
- `uv run mypy` passed with no issues in 19 source files.
- `uv run pytest` passed with 804 passed and 2 skipped. The two skips are the
  unchanged documented template skips.
- `git diff --check` passed. Diff inspection found only the intended changes
  in `src/meal_planner/db/dynamo.py`, `src/meal_planner/bot_handler.py`,
  `tests/test_dynamo.py`, and `tests/test_bot_handler.py`, plus this plan.
- The completed plans
  `docs/plans/completed/2026-08-20-profile-amendment-workflow.md` and
  the completed remediation plan
  `2026-08-20-remediate-profile-amendment-review-findings.md` have no diff.
  No deviations or blockers were found.
- Retained risks: strongly consistent reads may increase DynamoDB latency and
  read capacity cost; moto does not validate the managed service's actual
  consistency behavior; and the single-writer/state-transition assumption
  still requires review if independent profile writers are added.

## Post-Completion

### Manual verification and residual risks

- Live Telegram checks from the completed original plan remain outstanding.
  Complete two amendments back-to-back in one chat and confirm the second
  result retains the first while callbacks and category menus behave normally.
- Moto does not model DynamoDB eventual consistency. The deterministic stale
  response test validates request intent and application behavior, but a live
  AWS smoke test is the only direct check of the managed service boundary.
- Strongly consistent reads can have higher read-capacity cost and latency.
  The plan limits that cost to amendment text processing rather than changing
  all profile reads.
- The fix relies on the documented single-writer model and the exact active
  conversation-state transition. If an administrative API, migration worker,
  or other independent profile writer is added, introduce an explicit profile
  version or snapshot condition before treating concurrent replacement writes
  as safe.
- Deployment, pull-request handling, issue updates, commits, pushes, and merges
  are outside this remediation-planning action and remain subject to repository
  branch protection and delivery policy.
