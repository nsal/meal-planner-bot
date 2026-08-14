# Meal Planner Baseline, Concurrency, and Idempotency Remediation

> Completed and archived on 2026-08-14. Follow-up review remediation is
> recorded in `2026-08-13-concurrency-idempotency-review-remediation.md`.

## Outcome

The release-readiness baseline and the three concurrency/idempotency findings
were implemented together. The repository now protects generated drafts with
revision conditions, protects callback outcomes with an active-plan epoch,
and deduplicates repeated Telegram meal updates by source update ID. Legacy
plans without `PLAN_STATE`, legacy meal keys, direct callers without an update
ID, and the existing five-part callback format remain supported.

## Completed implementation tasks

### Task 1: Restore the release-readiness baseline

- [x] Reconciled handlers, planner workflows, typed models, repository APIs,
  configuration, deployment settings, and their tests.
- [x] Restored exact-week asynchronous plan and grocery lifecycle behavior,
  callback validation/acknowledgement, bounded client behavior, and the
  Python 3.14 ARM64 SAM contract.
- [x] Restored success and error coverage for command, mutation, planner,
  Lambda, configuration, and deployment paths.

### Task 2: Reject stale generated drafts with revision snapshots

- [x] Added absent-snapshot and matching-revision repository coverage.
- [x] Added conflict coverage for edits, confirmation, duplicate workers, and
  non-conditional DynamoDB errors.
- [x] Captured the exact-week revision before LLM work, rejected confirmed
  weeks before the LLM call, and conditionally persisted only current drafts.

### Task 3: Make active callback outcome writes transactional

- [x] Added active-plan snapshots with strongly consistent state and plan reads.
- [x] Confirmation now conditionally updates the draft and increments the
  activity epoch atomically.
- [x] Outcome writes check the expected epoch transactionally, preserve legacy
  absent-state behavior, and reject stale callbacks without partial mutation.
- [x] Added repository and handler coverage for success, conflicts, errors,
  inactive plans, notifications, and callback acknowledgement.

### Task 4: Deduplicate repeated Telegram meal-log updates

- [x] Repeated source update IDs overwrite one stable meal key, while distinct
  IDs remain independent records.
- [x] Callers without a source ID retain timestamp-based keys and legacy meal
  records remain queryable.
- [x] The normalized Telegram update ID is threaded through conversational
  mutations into repository persistence, with fallback coverage.

### Task 5: Verify acceptance criteria and project standards

- [x] Verified release-readiness, stale-worker, stale-callback, legacy-data,
  distinct-meal, callback, and deployment contracts.
- [x] Verified all required quality gates and the complete test suite.

### Task 6: Finalize documentation and plan tracking

- [x] Reviewed README.md and AGENTS.md; no changes were required because the
  final operational and project rules are already documented.
- [x] Reconciled this plan and archived it after verification.

## Final verification

- `uv run pytest`: 166 passed.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 20 passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 41 files already formatted.
- `uv run mypy`: success, 15 source files checked.
- `git diff --check`: passed.
- `uvx --from aws-sam-cli sam build --beta-features`: Build Succeeded.
- The artifact source matches the working tree and both configured Lambda
  handlers import successfully on this compatible host.

## Post-completion

Manual DynamoDB Local/AWS race validation, release-CI enforcement, pull-request
publication, and the issue #21 comment remain release coordination actions
outside this local implementation pass.
