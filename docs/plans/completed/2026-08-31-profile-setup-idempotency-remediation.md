# Profile Setup Transaction Idempotency Review Remediation

## Overview

Remediate the single P2 finding from the independent review of the completed
conversational rollout remediation. Profile setup transactions currently retry
ambiguous DynamoDB failures without reusing an explicit idempotency token. A
transaction that committed before its response failed can therefore be replayed
as a new request, rejected by its now-stale condition, and misreported to the
user as a conflict.

The change will preserve one `ClientRequestToken` across all attempts made by a
single repository invocation. It will cover both the intermediate profile-draft
transaction and the final profile-completion transaction without changing their
state ownership conditions, conflict classification, or handler behavior.

## Context

- Affected implementation:
  `src/meal_planner/db/dynamo.py`, specifically
  `DynamoRepository.save_profile_draft_and_transition_state()` near line 412
  and `DynamoRepository.complete_profile_setup()` near line 519.
- Affected tests: `tests/test_dynamo.py`, alongside the existing profile setup
  transaction and operational-error regressions.
- Both methods call `transact_write_items()` from bounded manual retry loops.
  Neither call currently supplies `ClientRequestToken` explicitly.
- `InternalServerError` is classified as retryable. Its outcome can be
  ambiguous because DynamoDB may have committed the transaction before the
  client observed the error.
- The original remediation is complete at
  `docs/plans/completed/2026-08-31-conversational-rollout-review-remediation.md`
  and must remain immutable.
- The repository had an extensively overlapping dirty baseline. Implementation
  must preserve all unrelated and pre-existing work.

## Review-Finding Coverage

The one actionable finding is assigned exactly once:

1. P2 reuse one idempotency token across transaction retries: Task 1.

## Development Approach

- Use strict TDD. Add the commit-then-error regressions first and confirm that
  they fail for the missing or changing request token before editing production
  code.
- Complete the single task as one logical unit because both affected methods
  implement the same transaction-retry contract.
- Generate one token per repository method invocation and reuse it only within
  that invocation's retry loop.
- Preserve the existing two-attempt bound and
  `TransactionConflictKind` classification behavior.
- Use `uv run` for Python tools and Ruff for linting and formatting.
- Keep all Python typed and formatted to the configured 80-column limit.
- Update this plan if implementation scope or verification results change.

## Solution Overview

For each affected method, generate a DynamoDB-compatible UUID token after the
transaction request has been constructed and before entering the retry loop.
Pass that same value as `ClientRequestToken` on every
`transact_write_items()` attempt. Do not share tokens between separate method
invocations.

The regression harness will simulate an ambiguous response by allowing the
first transaction call to commit and then raising `InternalServerError`. It
will model DynamoDB idempotency on the retry: the same token returns the cached
successful outcome without replaying the conditional transaction, while an
absent or changed token attempts a second write and exposes the stale-condition
failure. This proves both token stability and the user-visible success outcome.

## Testing Strategy

- Add focused repository regressions for both transaction methods.
- Assert the first and second attempts receive the same explicit non-empty
  `ClientRequestToken`.
- Assert separate repository invocations do not accidentally reuse a token.
- Verify a committed intermediate transaction returns `True` and retains the
  intended draft and next state after the simulated response failure.
- Verify a committed final transaction returns `True` and retains the profile,
  draft deletion, and setup-state deletion after the simulated response
  failure.
- Retain existing stale-work, duplicate-submission, concurrency, and
  operational-error expectations.
- Run focused tests before the complete repository quality gates.

## Implementation Steps

### Task 1: Make profile setup transaction retries idempotent

**Finding:** P2 retrying an ambiguously committed transaction with a new token
can turn success into a reported conflict.

**Files:**

- Modify: `tests/test_dynamo.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify during execution only:
  `docs/plans/2026-08-31-profile-setup-idempotency-remediation.md`
- Move after all checks pass:
  `docs/plans/2026-08-31-profile-setup-idempotency-remediation.md`
  to
  `docs/plans/completed/2026-08-31-profile-setup-idempotency-remediation.md`

- [x] Add a failing `tests/test_dynamo.py` regression for
  `save_profile_draft_and_transition_state()` that commits the first
  `transact_write_items()` call and then raises `InternalServerError` to
  simulate a lost success response.
- [x] In that regression, capture both call argument sets and require one
  explicit, non-empty `ClientRequestToken` to be identical across the initial
  attempt and retry. Model a same-token retry as the cached successful result,
  and assert the method returns `True` with the intended `PROFILE_DRAFT` and
  next `CONVERSATION_STATE` stored.
- [x] Add the analogous failing commit-then-error regression for
  `complete_profile_setup()`. Assert it returns `True`, stores the intended
  `PROFILE`, and leaves both `PROFILE_DRAFT` and `CONVERSATION_STATE` deleted.
- [x] Add a control assertion using separate repository method invocations to
  prove tokens are scoped per invocation rather than reused globally.
- [x] Run the new tests before production changes and record the expected
  failure: the current calls omit an explicit stable token, so the simulated
  retry is treated as a new transaction and the already-committed state makes
  its condition stale instead of preserving the successful result.
- [x] In `src/meal_planner/db/dynamo.py`, add the typed UUID import needed for
  DynamoDB client request tokens.
- [x] In `save_profile_draft_and_transition_state()`, generate one token before
  the retry loop and pass it as `ClientRequestToken` on every
  `transact_write_items()` call.
- [x] Apply the same per-invocation token pattern to
  `complete_profile_setup()`. Do not alter transaction items, condition
  expressions, retry count, conflict classification, or return semantics for
  uncommitted failures.
- [x] Run the focused commit-then-error tests and confirm both now pass.
- [x] Run `uv run pytest tests/test_dynamo.py` and confirm all repository
  regressions pass, including stale state, competing writers, profile revision,
  and repeated operational errors.
- [x] Run relevant profile setup handler regressions with
  `uv run pytest tests/test_bot_handler.py` to confirm repository success and
  error outcomes still map to the existing user responses.
- [x] Run `uv run pytest` and require the complete suite to pass.
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, and `git diff --check`; require every gate to pass.
- [x] Record exact verification results and any resolved failures in this plan,
  confirm prior files under `docs/plans/completed/` are unchanged, and move only
  this plan to `docs/plans/completed/`.

### Verification Results

- TDD red run before production changes:
  `uv run pytest tests/test_dynamo.py -k
  'profile_setup_draft_retry_reuses_token or
  profile_setup_completion_retry_reuses_token or
  profile_setup_transaction_tokens_are_scoped_per_invocation'` failed with
  `3 failed, 79 deselected`; all failures showed the calls omitted an explicit
  `ClientRequestToken`, so the simulated retry replayed the committed write and
  returned `False` from the stale condition.
- Focused green run:
  the same command passed with `3 passed, 79 deselected`.
- Repository regressions: `uv run pytest tests/test_dynamo.py` passed with
  `82 passed`.
- Handler regressions: `uv run pytest tests/test_bot_handler.py` passed with
  `71 passed`.
- Full suite: `uv run pytest` passed with `502 passed, 1 skipped`.
- `uv run ruff check .` passed with `All checks passed!`.
- The first `uv run ruff format --check .` run reported two formatting changes
  in `tests/test_dynamo.py`; `uv run ruff format tests/test_dynamo.py` resolved
  them. The final format check passed.
- `uv run mypy` passed with `Success: no issues found in 16 source files`.
- `git diff --check` passed with no output.
- The existing completed plans were checked before and after this work; the
  prior conversational rollout plan retained SHA-256
  `e217e67574c93ced0c9d4a069823a568ba8b096c66502ccef0f8dd755379a677`.

**Acceptance criteria:**

- Both profile setup transaction methods pass an explicit
  `ClientRequestToken` to every `transact_write_items()` attempt.
- The token remains identical across retries within one repository invocation
  and differs between separate invocations.
- If the first transaction commits but its response surfaces as a retryable
  error, the retry preserves the successful outcome instead of returning a
  stale-work conflict.
- Intermediate setup retains the committed draft and matching next state.
- Final setup retains the committed profile and completed cleanup.
- Existing stale-work, duplicate-submission, optimistic revision, retry-limit,
  and operational-error behavior remains unchanged for transactions that did
  not already succeed.
- Focused tests, the full suite, Ruff checks, mypy, and whitespace validation
  all pass.
- No implementation outside the two affected repository methods and their
  focused tests is changed, and no prior completed plan is modified or moved.

## Post-Completion

The automated regressions model DynamoDB's idempotent retry contract. Live AWS
verification of an actual commit followed by a lost response remains an
external integration check and is not required to complete this remediation.
No GitHub issue, commit, push, deployment, or other external action is part of
this plan.
