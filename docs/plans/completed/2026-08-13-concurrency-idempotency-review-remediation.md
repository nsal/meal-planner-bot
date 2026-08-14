# Concurrency and Idempotency Review Remediation

> Completed and archived on 2026-08-14 for GitHub issue [#21](https://github.com/nsal/meal-planner-bot/issues/21).

## Outcome

The review findings were repaired and verified. Present-epoch outcome
transactions now send `:expected_epoch` only on the `PLAN_STATE`
`ConditionCheck`, exact-week generation snapshots can opt into strongly
consistent reads, and the planner uses that option before any prompt or LLM
work. The SAM artifact was rebuilt from the final source.

## Completed tasks

### Task 1: Scope active-epoch values to the condition check

- [x] Added a request-shape regression test proving the plan update omits
  `:expected_epoch` while the condition check contains it.
- [x] Verified matching present epochs persist outcomes after reloading the
  exact plan, and stale epochs return `False` without mutation.
- [x] Preserved non-conditional transaction error propagation and legacy
  absent-epoch behavior.
- [x] Removed the unused epoch-value merge from the plan update.
- [x] Focused epoch/outcome/transaction tests passed.

### Task 2: Read generated-plan revision snapshots consistently

- [x] Added request coverage for opt-in `ConsistentRead=True` and default
  eventual-consistency behavior.
- [x] Updated planner tests to prove the strong exact-week read precedes
  prompt construction and the LLM call.
- [x] Confirmed confirmed weeks are rejected before LLM or draft persistence,
  and repository read failures use the controlled notification path.
- [x] Added the typed keyword-only `consistent_read` option without changing
  existing callers.
- [x] Focused DynamoDB and planner tests passed: 38 passed.

### Task 3: Establish a fresh release artifact baseline

- [x] Rebuilt with `uvx --from aws-sam-cli sam build --beta-features`.
- [x] Required artifact tests passed: 20 passed.
- [x] Verified source-byte freshness and imports for both configured Lambda
  handlers.
- [x] Inspected the generated `dynamo.py`; it contains both fixes.

### Task 4: Verify review findings and acceptance criteria

- [x] Verified request-shape, persistence, consistency, stale-worker,
  stale-callback, legacy-data, idempotency, and deployment contracts.
- [x] Full suite passed: 166 passed.
- [x] Ruff check, Ruff format check, strict mypy, and `git diff --check`
  passed.

### Task 5: Reconcile documentation and complete plan tracking

- [x] Reconciled the original implementation plan and this review plan using
  verified source, tests, artifact, lint, type, and whitespace results.
- [x] Removed obsolete failure counts and blocker notes.
- [x] Confirmed README.md and AGENTS.md require no changes.
- [x] Archived both plans under `docs/plans/completed/` after the final
  verification pass.

## Final verification record

- `uv run pytest`: 166 passed.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 20 passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 41 files already formatted.
- `uv run mypy`: success, 15 source files checked.
- `git diff --check`: passed.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded.

## Deferred post-completion work

Manual DynamoDB Local/AWS race validation, release-CI enforcement, pull-request
publication, and the issue #21 comment remain external coordination actions;
no CI workflow or GitHub publication was created by this local execution.
