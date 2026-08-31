# Conversational Plan Chat Review Remediation

## Overview

Remediate all six actionable findings from the independent review of the
conversational Plan Chat simplification. The work narrows the Plan Chat
worker's DynamoDB permissions, removes an inert retry setting, aligns active
documentation with retained runtime behavior, and restores the original
implementation plan to the active plan directory until its external
completion gates are satisfied.

The remediation is deliberately narrow. It does not redesign runtime failure
handling, change the submitted-meal date boundary, change delimiter
normalization, deploy AWS resources, contact Telegram or the LLM provider, or
create or update GitHub issues.

## Context

- The project is a Python 3.14 AWS SAM application managed with `uv`.
- Project checks are `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `uv run mypy`.
- Ruff is configured for 80-column formatting and Mypy uses strict mode.
- The current implementation gates pass: 407 tests, Ruff, formatting, and
  Mypy.
- The original implementation plan is currently under
  `docs/plans/completed/`, with filename
  `2026-08-28-simplify-meal-planning-to-conversational-drafts.md`.
- The worktree contains the uncommitted conversational Plan Chat
  implementation. Preserve all pre-existing changes and do not commit, push,
  stash, reset, or clean them while executing this plan.

## Development Approach

- **Testing approach:** TDD. Add or update the narrowest executable assertion
  first and confirm the expected failure before changing implementation or
  documentation.
- Complete tasks in numerical order. Do not start the next task until the
  focused tests for the current task pass.
- Keep behavior unchanged except for the IAM permission reduction and removal
  of the inert retry configuration surface.
- Treat documentation as a tested contract. Pair documentation assertions
  with runtime boundary tests where the review found a mismatch.
- Update only this remediation plan's checkboxes during implementation. The
  original plan may be moved in Task 6, but its contents must remain
  byte-for-byte unchanged.

## Solution Overview

1. Replace the SAM managed DynamoDB CRUD policy with an explicit table-scoped
   allowlist for the four operations the worker performs.
2. Remove `PLAN_CHAT_LLM_MAX_RETRIES` from settings, dependency wiring,
   deployment configuration, documentation, and tests while retaining the
   explicit provider call setting `max_retries=0`.
3. Correct README failure semantics and protect them with tests for the
   provider, persistence, and delivery branches.
4. Document the submitted-meal boundary as eight inclusive UTC calendar dates
   without changing accepted dates.
5. Document dietary text as uninterpreted but delimiter-normalized without
   changing prompt rendering.
6. Move the original plan back to `docs/plans/` until its stated deployment and
   issue-follow-up gates are complete.

## Technical Details

- Plan Chat DynamoDB access is limited to `dynamodb:GetItem`,
  `dynamodb:Query`, `dynamodb:PutItem`, and `dynamodb:DeleteItem` on
  `MealPlannerTable.Arn`. Do not grant scan, update, batch, transaction, or
  wildcard/index permissions.
- `PutItem` remains sufficient for the worker's conditional state writes;
  conditional expressions do not require a separate DynamoDB action.
- The application performs one LLM request. LiteLLM continues to receive
  `max_retries=0`; no application retry loop or replacement setting is added.
- The accepted submitted-meal range remains UTC today minus seven days through
  UTC today, inclusive: eight calendar dates.
- Prompt text remains semantically uninterpreted, while `---` and `===`
  sequences continue to be normalized by `_escape_text()` to protect section
  delimiters.
- The original plan's content is historical execution evidence. Task 6 changes
  only its location, leaving external deployment and issue follow-up as the
  conditions for eventual re-archival.

## Testing Strategy

- SAM policy tests inspect the exact action set and exact table ARN resource,
  and reject managed CRUD, wildcard, index, transaction, scan, update, and
  batch access.
- Configuration tests prove the inert retry setting and constructor plumbing
  are absent while provider retries remain disabled.
- Worker tests establish the failure branches that README wording must match.
- Documentation tests assert the corrected failure, date-window, delimiter,
  and plan-lifecycle contracts.
- Every task runs focused tests before proceeding. Task 6 finishes with the
  complete Pytest, Ruff, formatting, Mypy, and diff checks.

## Implementation Steps

### Task 1: Restrict Plan Chat DynamoDB permissions

**Finding:** P2 — the LLM-facing worker receives broad managed CRUD access.

**Files:**

- Modify: `tests/test_template.py`
- Modify: `template.yaml`

**Failing or missing test first:**

- [x] Add a template test that requires an inline Plan Chat policy with exactly
  `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:PutItem`, and
  `dynamodb:DeleteItem`, scoped exactly to `MealPlannerTable.Arn`.
- [x] In the same test, reject `DynamoDBCrudPolicy`, wildcard or index
  resources, and scan, update, batch, transaction, and other unlisted actions.

**Expected failure:**

- [x] Run `uv run pytest tests/test_template.py` and confirm the new assertion
  fails because `PlanChatFunction` still uses `DynamoDBCrudPolicy` instead of
  an explicit statement.

**Implementation change:**

- [x] Replace the managed CRUD policy in `template.yaml` with one inline
  allow statement containing only the four required DynamoDB actions and
  `Resource: !GetAtt MealPlannerTable.Arn`.
- [x] Do not change the Bot policy, table schema, worker behavior, or add index
  access.

**Verification:**

- [x] Run `uv run pytest tests/test_template.py` and confirm the new least-
  privilege assertions pass.

**Regression tests:**

- [x] Run `uv run pytest tests/test_deploy.py tests/test_template.py
  tests/test_verify_transaction_permission.py`.

**Acceptance criteria:**

- [x] Plan Chat has only GetItem, Query, PutItem, and DeleteItem access to the
  table ARN; no managed CRUD policy, wildcard/index resource, or broader
  DynamoDB action remains.
- [x] Focused and regression tests pass before Task 2 begins.

### Task 2: Remove the inert Plan Chat retry configuration

**Finding:** P3 — `PLAN_CHAT_LLM_MAX_RETRIES` is exposed and wired but has no
behavioral effect.

**Files:**

- Modify: `src/meal_planner/config.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `src/meal_planner/plan_chat_handler.py`
- Modify: `template.yaml`
- Modify: `README.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_plan_chat_handler.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_template.py`
- Modify: `tests/test_readme.py`

**Failing or missing test first:**

- [x] Add boundary assertions that the settings model, LLM client constructor,
  handler wiring, SAM environment, and README contain no
  `PLAN_CHAT_LLM_MAX_RETRIES`, `plan_chat_llm_max_retries`, or inert
  `self.max_retries` surface.
- [x] Retain or strengthen the LLM client assertion that the provider call
  receives `max_retries=0`, proving that provider retries remain disabled.

**Expected failure:**

- [x] Run `uv run pytest tests/test_config.py tests/test_llm_client.py
  tests/test_plan_chat_handler.py tests/test_template.py tests/test_readme.py`
  and confirm the new absence assertions fail against the existing
  configuration chain.

**Implementation change:**

- [x] Make `make_plan_chat_state()` use a current UTC timestamp by default,
  while allowing tests that require a historical timestamp to inject one, so
  handler tests do not receive an already-expired mocked session.
- [x] Remove `plan_chat_llm_max_retries` and its environment alias from
  `PlanChatSettings`.
- [x] Remove the `max_retries` constructor parameter and stored attribute from
  `LLMClient`, and stop passing it from `plan_chat_handler.py`.
- [x] Remove `PLAN_CHAT_LLM_MAX_RETRIES` from the Plan Chat SAM environment and
  the README configuration table.
- [x] Update fixtures and constructor tests to the retained interface; do not
  introduce replacement retry configuration or an application retry loop.

**Verification:**

- [x] Run the focused five-file Pytest command above and confirm all tests pass.
- [x] Run an `rg` audit across `README.md`, `src`, `template.yaml`, and
  `tests` for `PLAN_CHAT_LLM_MAX_RETRIES`,
  `plan_chat_llm_max_retries`, and `self.max_retries`; confirm there are no
  active matches.

**Regression tests:**

- [x] Run `uv run pytest tests/test_config.py tests/test_llm_client.py
  tests/test_plan_chat_handler.py tests/test_template.py`.

**Acceptance criteria:**

- [x] Plan Chat handler tests use live conversation-state lifetimes without
  changing production expiry or transition behavior.
- [x] The inert retry setting is absent end to end, LiteLLM still receives
  `max_retries=0`, and one-request behavior is unchanged.
- [x] Focused and regression tests pass before Task 3 begins.

### Task 3: Correct documented failure behavior

**Finding:** P2 — README promises retry guidance for persistence and delivery
branches that can return silently.

**Files:**

- Modify: `tests/test_plan_chat_handler.py`
- Modify: `tests/test_readme.py`
- Modify: `README.md`

**Failing or missing test first:**

- [x] Add or strengthen worker tests proving that a provider failure produces
  bounded guidance only when state restoration succeeds and that restoration
  failure can return without a user message.
- [x] Add or strengthen the delivery-failure test proving that a Telegram send
  failure is logged without a second send or retry message.
- [x] Add README contract assertions requiring both failure sections to state
  that persistence and delivery failures may be silent and that provider
  guidance depends on successful state recovery and Telegram delivery.

**Expected failure:**

- [x] Run `uv run pytest tests/test_plan_chat_handler.py tests/test_readme.py`
  and confirm the README assertions fail on the unconditional retry-guidance
  claims while the runtime branch tests establish the intended source of truth.

**Implementation change:**

- [x] Correct the README's workflow and privacy/failure sections to distinguish
  provider failure recovery from persistence and Telegram delivery failures.
- [x] State that provider failures yield bounded retry guidance only if state
  recovery and message delivery succeed; persistence or delivery failures can
  be silent to the user and are logged with bounded metadata.
- [x] Do not change worker recovery, persistence, logging, or delivery behavior.

**Verification:**

- [x] Run `uv run pytest tests/test_plan_chat_handler.py tests/test_readme.py`
  and confirm the behavioral and documentation contracts pass together.

**Regression tests:**

- [x] Run `uv run pytest tests/test_llm_client.py
  tests/test_plan_chat_handler.py tests/test_readme.py`.

**Acceptance criteria:**

- [x] README no longer promises universal retry guidance and accurately covers
  all three reviewed failure branches at both affected locations.
- [x] Runtime behavior remains unchanged and focused tests pass before Task 4.

### Task 4: Document the inclusive submitted-meal date window

**Finding:** P3 — README calls an eight-date inclusive range a seven-day
window.

**Files:**

- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_readme.py`
- Modify: `README.md`

**Failing or missing test first:**

- [x] Add or strengthen boundary tests proving `today - 7 days` is accepted and
  `today - 8 days` is rejected for submitted meals.
- [x] Add a README assertion requiring the range to be described as UTC today
  through the previous seven dates, inclusive, totaling eight calendar dates.

**Expected failure:**

- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_readme.py` and
  confirm the documentation assertion fails because README currently says
  "seven-day UTC window" while the runtime boundary assertions pass.

**Implementation change:**

- [x] Replace the inaccurate date-window wording in README with the explicit
  inclusive bounds and eight-calendar-date count.
- [x] Do not change validation logic, accepted dates, prompts, or persisted
  submitted-meal data.

**Verification:**

- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_readme.py` and
  confirm both boundary dates and README wording pass.

**Regression tests:**

- [x] Run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py
  tests/test_readme.py`.

**Acceptance criteria:**

- [x] README and executable boundaries agree that eight UTC calendar dates are
  accepted, from today minus seven days through today inclusive.
- [x] No submitted-meal behavior changes and tests pass before Task 5.

### Task 5: Document prompt delimiter normalization

**Finding:** P3 — prompt documentation says raw dietary wording is included
exactly even though delimiter sequences are normalized.

**Files:**

- Modify: `tests/test_prompts.py`
- Modify: `tests/test_readme.py`
- Modify: `docs/prompt.md`

**Failing or missing test first:**

- [x] Retain or strengthen prompt tests proving ordinary dietary text is not
  semantically interpreted while `---` and `===` are replaced by their safe
  delimiter-normalized forms.
- [x] Add a documentation assertion requiring `docs/prompt.md` to describe raw
  dietary content as uninterpreted but delimiter-normalized, and to name both
  protected delimiter sequences.

**Expected failure:**

- [x] Run `uv run pytest tests/test_prompts.py tests/test_readme.py` and confirm
  the documentation assertion fails on the current "exactly as raw text"
  wording while the rendering test demonstrates normalization.

**Implementation change:**

- [x] Update the raw dietary constraints and preferences descriptions in
  `docs/prompt.md` to state that content is semantically uninterpreted but
  delimiter-normalized for section safety.
- [x] Do not change `_escape_text()`, prompt markers, or rendered prompt
  behavior.

**Verification:**

- [x] Run `uv run pytest tests/test_prompts.py tests/test_readme.py` and confirm
  prompt behavior and documentation agree.

**Regression tests:**

- [x] Run `uv run pytest tests/test_prompts.py tests/test_llm_client.py
  tests/test_plan_chat_handler.py tests/test_readme.py`.

**Acceptance criteria:**

- [x] Documentation no longer promises byte-exact rendering and accurately
  describes both uninterpreted content and delimiter normalization.
- [x] Prompt output remains unchanged and tests pass before Task 6.

### Task 6: Restore the original plan until external gates are complete

**Finding:** P3 — the original plan was archived despite its rule that
deployment and issue-comment work must be complete first.

**Files:**

- Modify: `tests/test_readme.py`
- Move from directory: `docs/plans/completed/`
- Move to directory: `docs/plans/`
- File: `2026-08-28-simplify-meal-planning-to-conversational-drafts.md`

**Failing or missing test first:**

- [x] Replace the archive-only assertion with a lifecycle test requiring the
  original plan at the active path, absent from `completed/`, while its stated
  deployment and issue-follow-up gates remain outstanding.
- [x] Make the test preserve the distinction between implementation checkboxes
  and external Post-Completion work; do not rewrite historical plan content to
  make the assertion pass.

**Expected failure:**

- [x] Run `uv run pytest tests/test_readme.py` and confirm the lifecycle test
  fails because the original plan is currently under `docs/plans/completed/`.

**Implementation change:**

- [x] Move the original plan back to the active `docs/plans/` path without
  editing its contents.
- [x] Leave it active until deployment/live verification and the associated
  issue comment with commit or PR linkage are actually complete and evidenced.

**Verification:**

- [x] Run `uv run pytest tests/test_readme.py` and confirm the active-plan
  lifecycle assertion passes.
- [x] Verify the move is byte-preserving by comparing the moved file with its
  task-start content or object hash.

**Regression tests:**

- [x] Run `uv run pytest` and confirm the full suite passes.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run ruff format --check .`.
- [x] Run `uv run mypy`.
- [x] Run `git diff --check`.

**Acceptance criteria:**

- [x] The original plan exists only at the active path and its content is
  unchanged.
- [x] All six review findings are covered by executable tests and the resulting
  implementation or documentation change.
- [x] Full Pytest, Ruff, formatting, Mypy, and diff checks pass.
- [x] This remediation plan is moved to `docs/plans/completed/` only after all
  of its implementation tasks and checks pass; the original plan remains active
  until its separate external gates are complete.

## Post-Completion

These items require external systems and are not part of remediation
implementation:

- Deploy the retained Bot and Plan Chat resources through the repository's
  approved branch and pull-request workflow.
- Verify the live AWS migration, Telegram delivery paths, and provider-backed
  Plan Chat behavior.
- Comment on the associated GitHub issue with the completed commit or pull
  request link, following repository instructions.
- Archive the original simplification plan only after those external gates are
  complete and evidenced.

No GitHub issue is created by this planning task.
