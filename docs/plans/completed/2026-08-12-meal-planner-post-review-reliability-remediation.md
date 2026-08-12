# Meal Planner Post-Review Reliability Remediation

## Overview

- Address the three actionable findings from the read-only branch review:
  keep LiteLLM retries inside the configured Lambda deadlines, reject meal
  check-ins for superseded overlapping plans, and prevent LLM output from
  setting server-owned draft lifecycle fields.
- Preserve the existing Telegram callback payload, DynamoDB item shape,
  application-owned retry policy, and asynchronous Planner Lambda design.
- Keep the remediation narrow: no dependency, infrastructure, persistence
  migration, or user-facing command changes are required.
- Associated GitHub issue:
  [#20](https://github.com/nsal/meal-planner-bot/issues/20).

## Context (from discovery)

- **Files/components involved:**
  `src/meal_planner/llm/client.py`,
  `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`,
  `tests/test_llm_client.py`,
  `tests/test_bot_handler.py`,
  `tests/test_planner_handler.py`, and `README.md`.
- **Related patterns found:** `LLMClient` owns bounded transient retries;
  `DynamoRepository.get_active_plan` selects the newest confirmed plan
  covering today; `PlannerHandler.generate_plan` already overwrites status and
  grocery state after parsing provider output.
- **Dependencies identified:** the locked LiteLLM/OpenAI adapter defaults to
  internal provider retries unless `max_retries` is set explicitly; no package
  changes are needed.
- **Review evidence:** the current 151-test suite passes, but its LiteLLM mock
  checks only the timeout argument, callback tests model only one confirmed
  plan, and generated-plan fixtures use trusted lifecycle defaults.

## Development Approach

- **Testing approach:** TDD. Add a focused failing regression test before each
  production fix.
- Complete each task fully before moving to the next and keep changes small.
- Every code task must add or update tests for all modified behavior, including
  success and rejection paths.
- All focused tests must pass before the next task begins.
- Update this plan immediately if implementation discoveries change scope or
  the selected design.
- Maintain backward compatibility for callback data, persisted plans, retry
  configuration, and valid provider responses.
- Use `uv run` for project tools, Ruff for formatting and linting at 80
  columns, and strict mypy for Python typing.

## Testing Strategy

- **LLM client tests:** assert every LiteLLM call disables adapter-owned
  retries while application retries, transient classification, backoff, and
  JSON mode continue to work.
- **Bot handler tests:** store or mock two overlapping confirmed plans, prove a
  callback for the superseded week is rejected without persistence, and prove
  the selected active week's callback still succeeds and is acknowledged.
- **Planner handler tests:** pass schema-valid but noncanonical revision,
  status, grocery state, and meal outcomes from the mocked provider, then
  assert only normalized draft state reaches persistence and Telegram.
- **Regression tests:** preserve timeout propagation, current-plan check-ins,
  generated-plan validation, optimistic revisions, and grocery lifecycle
  behavior.
- **Release verification:** run pytest, Ruff lint, Ruff format check, strict
  mypy, lockfile validation, SAM validation/build, and required artifact tests.
- This repository has no UI/browser end-to-end suite; handler and repository
  tests cover the relevant workflows.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document issues or blockers with a `⚠️` prefix.
- Keep this plan synchronized with implementation and verification results.

## Solution Overview

- Make `LLMClient` the sole retry owner by passing `max_retries=0` to every
  `litellm.acompletion` call. The existing outer loop and configuration budget
  then describe the actual maximum number of timed attempts and waits.
- Resolve callbacks through `get_active_plan` and require its exact
  `week_start_date` to match the callback. A confirmed plan that still covers
  today but has been superseded by a newer overlapping plan is no longer
  mutable through an old button.
- Normalize all server-owned lifecycle fields after parsing a generated plan:
  force draft status, revision zero, grocery state `not_requested`, an empty
  grocery list, and every meal outcome to `unreported` before persistence or
  display.
- Keep these changes at existing trust and selection boundaries rather than
  introducing new schemas, callback versions, repository methods, or retry
  abstractions.

## Technical Details

### Retry ownership and deadline enforcement

- Add `max_retries: 0` to the LiteLLM keyword arguments built in
  `LLMClient._execute_with_retry`.
- Retain `timeout`, the configured application attempt count, transient-status
  detection, provider retry-after handling, and the five-second backoff cap.
- Do not add another configuration variable: provider retries are always
  disabled because the application budget cannot safely model nested retries.
- Update the timeout test to verify both the per-attempt timeout and retry
  ownership contract.

### Active-plan callback validation

- Replace the exact-week plan lookup used as the activity check with
  `get_active_plan(user_id)`.
- Require a selected active plan whose `week_start_date` equals the parsed
  callback week before calling `update_meal_outcome`.
- Preserve malformed, expired, draft, missing-meal, persistence-failure, and
  callback-query acknowledgement behavior.
- Do not change the existing `checkin:<week>:<day>:<meal>:<outcome>` payload.

### Generated-plan normalization

- Treat status, revision, grocery fields, and meal outcomes as application
  state even though `WeeklyPlan` must continue accepting them when loading
  persisted data.
- Normalize the parsed plan in `PlannerHandler.generate_plan` before
  `save_generated_draft` and before `send_plan` observes the object.
- Keep the exact requested week check and all structural Pydantic validation.
- Prefer direct, explicit normalization in the handler over a second model or
  reusable abstraction because there is one generation boundary.

## What Goes Where

- **Implementation Steps:** regression tests, focused production fixes, full
  verification, documentation updates, and plan tracking in this repository.
- **Post-Completion:** manual Telegram/provider checks and GitHub issue/PR
  coordination that require external systems.

## Implementation Steps

### Task 1: Make the application the sole LLM retry owner

**Files:**

- Modify: `src/meal_planner/llm/client.py`
- Modify: `tests/test_llm_client.py`

- [x] add a failing test proving text completion passes both the configured
  timeout and `max_retries=0` to LiteLLM
- [x] add or update a JSON completion test proving the same retry contract is
  used in structured-output mode
- [x] set `max_retries=0` in the common LiteLLM call arguments so provider
  adapters cannot add attempts outside the application budget
- [x] retain and verify application-level transient retries, bounded backoff,
  permanent-failure handling, and fallback responses
- [x] run `uv run pytest tests/test_llm_client.py` and fix every failure before
  Task 2

### Task 2: Reject check-ins for superseded overlapping plans

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a failing test with overlapping confirmed plans proving a callback
  for the older week is rejected as inactive
- [x] assert the superseded callback does not call `update_meal_outcome` and is
  still acknowledged with a controlled failure result
- [x] update callback selection to resolve the repository's active plan and
  require an exact callback-week match
- [x] add or update a success test proving the selected active plan still
  accepts every supported outcome and uses the exact week, day, and meal type
- [x] retain tests for malformed, expired, missing, draft, and persistence
  failure cases
- [x] run `uv run pytest tests/test_bot_handler.py` and fix every failure
  before Task 3

### Task 3: Normalize generated draft lifecycle state

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] add a failing test whose otherwise valid provider plan contains a
  nonzero revision and cooked, skipped, or swapped meal outcomes
- [x] assert persistence and Telegram receive revision zero and only
  `unreported` generated meal outcomes
- [x] expand the regression input as needed to prove status, grocery state,
  and grocery contents remain normalized at the same boundary
- [x] explicitly reset every server-owned lifecycle field after parsing and
  before `save_generated_draft`
- [x] retain tests for exact-week validation, malformed plans, duplicate meal
  types, and confirmed-week write conflicts
- [x] run `uv run pytest tests/test_planner_handler.py` and fix every failure
  before Task 4

### Task 4: Verify remediation acceptance criteria

**Files:**

- Modify: relevant test files only if an acceptance gap is discovered
- Modify:
  `docs/plans/2026-08-12-meal-planner-post-review-reliability-remediation.md`

- [x] verify each configured LLM attempt maps to exactly one provider attempt
  and that application retry/backoff behavior remains bounded
- [x] verify only the repository-selected active plan accepts check-ins when
  confirmed plan date ranges overlap
- [x] verify no provider-supplied lifecycle field can make a new draft look
  revised, confirmed, grocery-ready, or previously consumed
- [x] run `uv run pytest` and fix every failure
- [x] run `uv run ruff check .` and `uv run ruff format --check .`
- [x] run `uv run mypy` and fix every type error
- [x] run `uv lock --check` and confirm no dependency changes were introduced
- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] rebuild with `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` against the
  fresh artifacts
- [x] record exact final verification evidence in this plan before Task 5

Final verification evidence (2026-08-12):

- `uv run pytest`: 153 passed.
- `uv run ruff check .`: all checks passed.
- `uv run ruff format --check .`: 40 files formatted.
- `uv run mypy`: success, no issues in 15 source files.
- `uv lock --check`: resolved 82 packages; lockfile is synchronized.
- SAM validation: template is valid.
- SAM build: succeeded with fresh artifacts in `.aws-sam/build`.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 20 passed.

### Task 5: [Final] Update documentation and close plan tracking

**Files:**

- Modify: `README.md`
- Modify: `AGENTS.md` only if a reusable engineering convention is introduced
- Modify:
  `docs/plans/2026-08-12-meal-planner-post-review-reliability-remediation.md`
- Move: this plan into `docs/plans/completed/`

- [x] document that application retries are the sole retry layer and remain
  within configured function deadlines
- [x] clarify that check-in buttons for superseded overlapping plans are
  rejected even while the older plan's date range still covers today
- [x] update `AGENTS.md` only if implementation establishes a new reusable
  project rule
- [x] verify every checkbox and recorded test result matches completed work
- [x] move the completed plan to `docs/plans/completed/`
- [x] rerun `uv run pytest` after the documentation move and fix every failure

## Post-Completion

### Manual verification

- Simulate a transient provider outage and confirm the Bot Lambda returns or
  fails over within its configured deadline without hidden provider retries.
- Confirm plans on consecutive days, then press a check-in button from the
  older plan and verify it is rejected while the newer plan's button succeeds.
- Generate a provider response containing noncanonical lifecycle fields and
  verify the displayed and persisted plan is a clean, unreported draft.

### External system updates

- Add a completion comment to the associated GitHub issue after
  implementation, linking the Conventional Commit and pull request.
- Publish through a dedicated branch and pull request; never push or merge
  directly to `master`.
- Monitor Lambda duration and timeout metrics after deployment to verify the
  bounded retry assumptions under real provider failures.
