# Meal Planner Bot Post-Review Correctness Remediation

## Overview

- Address all eight actionable findings from the review of commit `0f28539`.
- Prevent confirmed plans, meal outcomes, and edits from being overwritten by
  asynchronous plan or grocery work.
- Make grocery failure recovery truthful and reachable while separating
  persisted workflow state from Telegram notification delivery.
- Keep external-call budgets within the deployed function and API integration
  deadlines, and make secret rotation and SAM artifact verification reliable.
- Preserve the existing Telegram -> HTTP API -> Bot Lambda architecture and
  the asynchronous Planner Lambda rather than expanding this remediation into
  a broader queue or webhook redesign.

## Context (from discovery)

- **Primary stack:** Python 3.14, Pydantic, boto3/DynamoDB, LiteLLM, Telegram
  Bot API, AWS SAM, pytest, Ruff, and mypy.
- **Plan lifecycle:** `src/meal_planner/bot_handler.py`,
  `src/meal_planner/planner_handler.py`, and
  `src/meal_planner/db/dynamo.py` currently mix targeted outcome writes with
  whole-plan saves.
- **Profile onboarding:** `PROFILE_DRAFT` exists, but complete-looking
  candidates with inconsistent member counts are rejected without retaining
  the newly collected fields.
- **Runtime budgets:** AWS Lambda supports a maximum function timeout of 900
  seconds, but this repository deploys the Bot Lambda at 30 seconds and the
  Planner Lambda at 120 seconds. The bot is behind an API Gateway HTTP API,
  whose maximum integration timeout is 30 seconds and cannot be increased.
- **Deployment:** Secrets Manager dynamic references are resolved when the
  containing resource is created or modified; changing only a secret value
  does not refresh the existing Lambda environment.
- **Release checks:** `tests/test_template.py` validates selected built-template
  properties but does not compare every deploy-relevant property.

Official AWS references:

- The AWS Lambda Developer Guide's *Lambda quotas* page documents the
  900-second maximum Lambda function timeout.
- The API Gateway Developer Guide's *HTTP API quotas* page documents the
  non-increasable 30-second maximum integration timeout.
- The CloudFormation User Guide's *Secrets Manager dynamic references* page
  documents that a secret-value-only change does not refresh a resource.

## Development Approach

- **Testing approach:** TDD; add a failing regression test for each finding
  before changing production code.
- Complete each task fully before moving to the next.
- Make small, focused changes and prefer targeted DynamoDB updates over
  whole-document rewrites.
- Every task that changes code must add or update success and failure tests.
- All tests for a task must pass before the next task begins.
- Update this plan immediately when scope or implementation details change.
- Preserve backward compatibility within the clean pre-release schema where
  practical; no legacy DynamoDB migration is required.
- Use `uv run` for project tools, Ruff for formatting/linting at 80 columns,
  and strict mypy typing for all Python changes.

## Testing Strategy

- **Repository integration tests:** use moto DynamoDB tests for conditional
  writes, revision checks, targeted updates, and stale-worker behavior.
- **Handler unit tests:** cover same-week generation, confirmation/retry state
  transitions, notification failures, and multi-turn profile accumulation.
- **Concurrency tests:** deterministically interleave an edit or outcome update
  between grocery generation's read and completion write.
- **Configuration tests:** calculate worst-case retry, backoff, Telegram, and
  safety-margin budgets for both functions and reject unsafe combinations.
- **SAM tests:** compare normalized source and built templates, while allowing
  only expected build-time `CodeUri` differences.
- **Final verification:** run pytest, Ruff lint, Ruff format check, strict mypy,
  SAM validate/build, and required built-artifact smoke tests.
- This project has no browser UI or UI-based end-to-end test suite.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a ➕ prefix.
- Document issues or blockers with a ⚠️ prefix.
- Update this plan if implementation deviates from the design below.
- Keep this plan synchronized with the code and test state.

## Solution Overview

- Add an integer plan `revision` that changes only when meal-plan content is
  edited, not when outcomes or grocery state change.
- Replace unconditional lifecycle saves with repository operations that use
  DynamoDB condition expressions and targeted updates.
- Allow generated drafts to replace drafts for the same week, but never a
  confirmed plan. This also prevents a slow generation event from reverting a
  plan confirmed while that event was running.
- Capture the plan revision before grocery LLM work. Complete or fail grocery
  generation only when the revision and pending state still match; update only
  grocery fields so concurrent meal outcomes survive.
- Persist successful grocery data before sending Telegram notifications, and
  never convert `ready` to `error` because notification delivery failed.
- Treat confirmation of an active `confirmed/error` plan as an explicit
  grocery retry, transitioning it atomically to `pending` before invocation.
- Persist incomplete profile candidates and prefer an existing draft over the
  active profile on later turns.
- Define per-function call budgets. The 900-second Lambda service maximum is
  acknowledged, but the Bot Lambda budget must remain below the HTTP API's
  30-second integration deadline and its configured 30-second timeout.
- Add a non-secret CloudFormation refresh parameter to force both Lambda
  resources to update and re-resolve dynamic secret references during rotation.
- Normalize only generated `CodeUri` values before comparing complete source
  and built SAM templates.

## Technical Details

### Plan revision and conditional writes

- Add `revision: int` to `WeeklyPlan`, defaulting to `0` and constrained to a
  non-negative value.
- Add a generated-draft repository write with this condition:
  `attribute_not_exists(status) OR status = draft`.
- Confirm drafts with a targeted update conditioned on `status = draft` and
  the expected revision.
- Replace meal edits with a targeted nested-meal update that increments
  `revision`, clears stale groceries for confirmed plans, and uses the loaded
  revision as an optimistic-lock condition.
- Grocery completion updates only `grocery_list` and `grocery_status`, with
  conditions for `status = confirmed`, `grocery_status = pending`, and the
  captured revision.
- Grocery failure applies the same conditions. A stale worker must not replace
  a newer `pending` or `ready` state with `error`.
- Outcome updates remain targeted and do not increment plan revision.

### Grocery retry and notification flow

```text
draft --confirm--> confirmed / pending --worker--> confirmed / ready
                                      | failure
                                      v
                              confirmed / error
                                      |
                                confirm again
                                      v
                              confirmed / pending
```

- A notification failure after `ready` is logged but does not change persisted
  grocery state or content.
- An invocation failure after confirmation or retry transitions the exact plan
  from `pending` to `error` conditionally.
- If a worker detects a stale revision, it exits without notification or state
  mutation because a newer edit/finalization owns the plan.

### External-call budget

- Add the configured function timeout to settings, supplied separately for the
  Bot and Planner Lambdas.
- Validate the conservative worst-case budget:

```text
LLM attempts * LLM request timeout
+ retry waits at the maximum bounded delay
+ Telegram request allowance
+ handler/DynamoDB safety margin
<= configured function timeout
```

- Use Bot-specific defaults that remain below both its 30-second Lambda timeout
  and the HTTP API's 30-second integration timeout.
- Keep Planner-specific retries within its configured 120-second timeout.
- Do not raise the Bot Lambda to 900 seconds: Lambda permits it, but the HTTP
  API integration would still stop waiting after 30 seconds.

### Secret refresh

- Add a non-secret `SecretRefreshToken` SAM parameter and expose it as a Lambda
  environment value for both functions.
- Require operators to change the token whenever a referenced secret value is
  rotated, forcing CloudFormation to modify the Lambda resources and re-resolve
  all dynamic references.
- Document that webhook-secret rotation with a single accepted secret requires
  a tightly coordinated Telegram/Lambda update and may have a brief transition
  window; zero-downtime dual-secret acceptance is outside this remediation.

### Finding traceability

| Review finding | Planned task |
|---|---:|
| Preserve confirmed plans during same-week generation | Task 2 |
| Preserve concurrent mutations during grocery finalization | Tasks 1 and 3 |
| Fit cumulative retries inside the effective deadline | Task 6 |
| Force a Lambda resource update during secret rotation | Task 7 |
| Keep ready groceries when notification delivery fails | Task 4 |
| Make error-state grocery generation retryable | Task 4 |
| Save incomplete profile candidates | Task 5 |
| Compare every deploy-relevant template field | Task 8 |

## What Goes Where

- **Implementation Steps:** repository, handler, schema, configuration, SAM,
  documentation, and automated test changes within this repository.
- **Post-Completion:** deployment to an AWS test stack, actual secret rotation,
  Telegram webhook coordination, and manual concurrency/latency observation.

## Implementation Steps

### Task 1: Add optimistic plan revisions and targeted lifecycle operations

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`

- [x] write failing schema tests for a default revision, JSON round trip, and
  rejection of negative revisions
- [x] write failing repository tests for conditional draft creation,
  conditional confirmation, targeted meal replacement, revision increments,
  and revision conflicts
- [x] add the typed non-negative `revision` field to `WeeklyPlan`
- [x] implement a generated-draft write that may replace a draft but rejects a
  confirmed plan for the same week
- [x] implement an atomic draft-to-confirmed/pending transition conditioned on
  status and expected revision
- [x] implement a targeted meal edit that increments revision and invalidates
  groceries only for confirmed plans
- [x] ensure conditional-check failures return controlled false/conflict
  results while unrelated DynamoDB errors still propagate
- [x] update shared factories for revision-aware plans
- [x] run `uv run pytest tests/test_schemas.py tests/test_dynamo.py` and fix all
  failures before Task 2

### Task 2: Prevent stale plan generation from replacing confirmed plans

**Files:**
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] write a failing test proving a same-week confirmed plan survives a later
  generation result
- [x] write a failing interleaving test where one generation returns after
  another draft has already been confirmed
- [x] save generated plans through the conditional generated-draft repository
  operation from Task 1
- [x] send a truthful controlled message when a generated result is discarded
  because that week is already confirmed
- [x] retain current successful draft generation and malformed-response behavior
- [x] run `uv run pytest tests/test_planner_handler.py tests/test_dynamo.py` and
  fix all failures before Task 3

### Task 3: Make grocery completion concurrency-safe

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_planner_handler.py`

- [x] write failing repository tests for targeted grocery-ready and
  grocery-error transitions with matching and stale revisions
- [x] write a failing deterministic race test that records a meal outcome while
  grocery LLM generation is in progress and proves the outcome survives
- [x] write a failing deterministic race test that edits a meal during grocery
  generation and proves the stale worker cannot publish groceries or overwrite
  the edit
- [x] remove whole-plan pending/ready/error saves from grocery finalization
- [x] capture the plan revision before the LLM call and complete or fail only
  with a matching revision and pending status
- [x] preserve targeted meal outcomes without treating them as grocery-content
  revisions
- [x] suppress stale-worker state changes and user notifications
- [x] run `uv run pytest tests/test_dynamo.py tests/test_planner_handler.py` and
  fix all failures before Task 4

### Task 4: Separate grocery recovery from Telegram notification delivery

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_dynamo.py`

- [x] write a failing test proving Telegram notification failure leaves a
  successfully generated grocery list in `ready`
- [x] write failing tests for retrying an active `confirmed/error` plan and for
  rejecting retry from `pending`, `ready`, draft, expired, or missing plans
- [x] add an atomic `error -> pending` grocery retry transition for the exact
  active plan and expected revision
- [x] make `confirm_plan` confirm a draft or retry an active confirmed/error
  plan with truthful, distinct responses
- [x] conditionally restore `error` if the asynchronous invocation itself fails
- [x] move the ready notification outside the generation/persistence error
  boundary and log delivery failure without changing plan state
- [x] retain the existing successful confirmation and grocery-display flows
- [x] run `uv run pytest tests/test_{bot,planner}_handler.py`
- [x] run `uv run pytest tests/test_dynamo.py` and fix all failures before
  Task 5

### Task 5: Preserve incomplete profile candidates across turns

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] write a failing onboarding test where all fields are present but
  `family_members` is empty or shorter than `people_count`
- [x] write a failing existing-profile test that changes household size and
  supplies replacement member targets on a later turn
- [x] distinguish a missing profile draft from an empty draft in the repository
  API
- [x] prefer a persisted draft over the active profile when accumulating a
  later update turn
- [x] persist structurally valid but incomplete candidates before requesting
  the missing member names or calorie targets
- [x] save the active profile and delete the draft only after full validation
  succeeds
- [x] retain truthful handling for invalid calorie values and repository errors
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py` and fix
  all failures before Task 6

### Task 6: Enforce per-function external-call budgets

**Files:**
- Modify: `src/meal_planner/config.py`
- Modify: `src/meal_planner/llm/client.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `template.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_llm_client.py`
- Modify: `tests/test_template.py`

- [x] write failing configuration tests for safe Bot and Planner budgets and
  for combinations that exceed their configured function timeout
- [x] write a failing test that includes maximum provider-guided retry waits,
  Telegram allowance, and a handler safety margin in the budget calculation
- [x] add a validated configured-function-timeout setting and a conservative
  cumulative external-call budget validator
- [x] define explicit Bot and Planner timeout/retry environment values instead
  of relying on one global budget for both functions
- [x] choose Bot defaults below the 30-second HTTP API and Lambda deadline and
  Planner defaults below its 120-second configured deadline
- [x] keep the documented AWS Lambda platform maximum at 900 seconds while
  explaining why it is not the effective Bot webhook deadline
- [x] ensure exhausted requests still have enough reserved time to send one
  controlled Telegram response
- [x] run `uv run pytest tests/test_{config,llm_client,template}.py`
  and fix all failures before Task 7

### Task 7: Force secret re-resolution during documented rotations

**Files:**
- Modify: `template.yaml`
- Modify: `README.md`
- Modify: `tests/test_template.py`

- [x] write a failing template test for a non-secret `SecretRefreshToken`
  parameter consumed by both Lambda resources
- [x] add the refresh parameter and environment marker so a changed token
  modifies both functions and re-resolves their dynamic secret references
- [x] replace the no-op rotation example with a deploy command that supplies a
  new unique refresh token and preserves required stack parameters
- [x] document separate LLM key, bot token, and webhook secret rotation order,
  verification, rollback, and the single-secret transition limitation
- [x] retain versionless Secrets Manager dynamic references and ensure no
  secret value is accepted as a CloudFormation parameter
- [x] run `uv run pytest tests/test_template.py` and SAM validation, fixing all
  failures before Task 8

### Task 8: Compare the complete normalized SAM build template

**Files:**
- Modify: `tests/test_template.py`

- [x] write failing helper tests proving changes to function timeouts, memory,
  environment variables, policies, events, and outputs are detected as stale
- [x] write a success test proving generated `CodeUri` differences are the only
  normalized build-specific differences
- [x] replace selected-field comparisons with a deep normalized source-versus-
  built template comparison
- [x] keep source-file byte comparisons and dependency/import smoke checks
- [x] make mismatch diagnostics identify the first differing template path
- [x] run `uv run pytest tests/test_template.py` and fix all failures before
  Task 9

### Task 9: Verify acceptance criteria

**Files:**
- Modify: `tests/` only if final integration coverage is missing
- Modify: `docs/plans/2026-08-11-meal-planner-bot-post-review-remediation.md`

- [x] verify every row in the finding traceability table has passing regression
  coverage
- [x] verify repeated and interleaved generation cannot replace a confirmed
  plan
- [x] verify edits and outcomes survive grocery finalization races
- [x] verify grocery retry and notification-failure state transitions
- [x] verify incomplete onboarding survives across turns
- [x] verify Bot and Planner call budgets fit their deployed deadlines, while
  documentation accurately states Lambda's 900-second maximum
- [x] run `uv run pytest` and confirm all tests pass
- [x] run `uv run ruff check .` and confirm it passes
- [x] run `uv run ruff format --check .` and confirm it passes
- [x] run `uv run mypy` and confirm strict typing passes
- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] run `uvx --from aws-sam-cli sam build --beta-features`
- [x] run `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`
  against the fresh build
- [x] fix every failure before Task 10

### Task 10: [Final] Update documentation and close plan tracking

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md` only if a reusable engineering convention was introduced
- Modify: `docs/plans/2026-08-11-meal-planner-bot-post-review-remediation.md`
- Move: `docs/plans/2026-08-11-meal-planner-bot-post-review-remediation.md`
  to `docs/plans/completed/`

- [x] update runtime-budget, lifecycle, retry, concurrency, secret-rotation,
  deployment, and troubleshooting documentation to match implementation
- [x] cite the official 900-second Lambda maximum and 30-second HTTP API
  integration maximum without conflating them with configured timeouts
- [x] update AGENTS.md only if implementation creates a reusable project rule
- [x] mark every completed plan item `[x]` and record exact final verification
  evidence
- [x] rerun `uv run ruff check .` and `uv run ruff format --check .` after all
  documentation changes
- [x] move the completed plan to `docs/plans/completed/`
- [ ] comment on the associated GitHub issue with the implementation commit or
  draft PR link

### Final verification evidence

- `uv run pytest`: 128 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`:
  passed.
- `uvx --from aws-sam-cli sam validate --lint --region us-east-1`: passed.
- `uvx --from aws-sam-cli sam build --beta-features`: passed.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: passed after
  the final build.
- The GitHub issue number and implementation commit or PR were not supplied,
  so no external issue comment was made.

## Post-Completion

*Items requiring manual intervention or external systems; these are not
implementation checkboxes.*

### Manual verification

- Deploy to a non-production AWS stack and confirm the Bot Lambda remains
  responsive within the HTTP API's 30-second integration timeout.
- Trigger transient LLM timeouts and verify one controlled fallback is delivered
  before the webhook deadline.
- Interleave confirmation, plan editing, grocery finalization, and meal check-in
  actions while observing DynamoDB revisions and final state.
- Disconnect Telegram notification delivery after grocery persistence and
  verify `/grocery` still returns the ready list after connectivity recovers.
- Exercise an error-state grocery retry from a real conversational confirmation.
- Complete onboarding across multiple turns, including changing the household
  size of an existing profile.

### External system updates

- Rotate a test LLM secret, deploy with a new `SecretRefreshToken`, and verify
  both Lambda configurations use the new value.
- Coordinate a test webhook-secret rotation with Telegram, verify expected 403
  behavior only during the bounded transition window, and confirm recovery.
- Inspect the CloudFormation change set and Lambda configuration update before
  rotating production secrets.
- Monitor Lambda duration, timeout, error, and throttling metrics after release;
  consider an asynchronous conversational worker as a separate design project
  if normal LLM latency cannot reliably fit the HTTP API deadline.
