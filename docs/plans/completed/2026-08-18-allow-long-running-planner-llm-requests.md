# Allow Long-Running Planner LLM Requests

Tracking issue: [#44](https://github.com/nsal/meal-planner-bot/issues/44)

## Overview

Replace the Planner Lambda's two short 45-second LLM attempts with one
240-second whole-plan attempt. The configured Luna planner normally takes two
to three minutes to generate a weekly plan, so the current policy guarantees
timeouts before a successful response can arrive. The revised policy keeps the
existing asynchronous Bot-to-Planner flow and LiteLLM provider abstraction,
while leaving enough of the 300-second application deadline for persistence,
Telegram delivery, and retry-state recovery.

Add one privacy-safe failure log for each failed planner LLM attempt. This will
make CloudWatch reveal the timeout duration and failure category without
recording prompts, preferences, plans, credentials, or Telegram data.

## Context

- **Project:** Python 3.14 AWS SAM Telegram bot with separate Bot and Planner
  Lambda functions, DynamoDB state, and LiteLLM as the provider boundary.
- **Relevant implementation:** `src/meal_planner/config.py` validates
  function-call budgets; `src/meal_planner/planner_handler.py` runs and
  recovers planner requests; `src/meal_planner/llm/client.py` forwards the
  configured timeout to `litellm.acompletion`.
- **Deployment configuration:** `template.yaml` currently supplies a
  300-second Planner application deadline inside a 310-second Lambda, with two
  45-second LLM attempts.
- **Observed failure:** CloudWatch recorded about 94 seconds, which matches
  two consecutive 45-second LiteLLM request timeouts rather than an AWS Lambda
  timeout.
- **Provider decision:** Retain LiteLLM to preserve future support for
  open-code and non-OpenAI model providers. Do not replace it with the OpenAI
  SDK in this change.

## Development Approach

- **Testing approach:** TDD. Write focused failing tests before each behavior
  change, then implement only enough code to pass them.
- Complete each task, including its tests and required checks, before beginning
  the next task.
- Keep the change narrowly scoped to Planner LLM timing, observability, and
  matching deployment/documentation configuration.
- Preserve the Bot Lambda timeout, Planner Lambda timeout, asynchronous event
  contract, saved-preference retry flow, and LiteLLM abstraction.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
  `uv run pytest` before completion. Use `uv run` for every Python tool.

## Testing Strategy

- Add configuration tests proving a 240-second one-attempt Planner budget is
  valid, while incompatible long-timeout/multiple-attempt combinations are
  rejected.
- Update template tests to assert matching 240-second and one-attempt Planner
  environment values.
- Add Planner handler tests that prove a timeout makes exactly one provider
  call, retains retry-ready conversation state, and reports the existing
  user-facing retry message.
- Add logging tests that assert a failure record carries operational context
  (attempt, elapsed time, model, category) and excludes prompt/preference
  content.
- Retain all existing success, invalid-response, persistence, and revision
  regressions. This change does not alter revision semantics.

## Solution Overview

The Planner handler will keep its bounded generation loop, but configuration
will set its maximum attempt count to one. It will therefore make one LiteLLM
structured completion request with a 240-second transport timeout. The
existing 300-second in-process deadline still interrupts a stuck invocation
before AWS's 310-second function timeout. A terminal LLM timeout or failure
will continue through the existing retry-state retention and Telegram message
path, so the user can re-run `/plan` with the saved preference.

The failure log belongs at the Planner orchestration boundary, where the
attempt number and elapsed duration are known. It will use structured values
or stable key/value fields and sanitize error detail. It must not include the
system prompt, preference, generated output, raw event, token, or chat/user
identifiers.

## Technical Details

- Set `PLANNER_LLM_REQUEST_TIMEOUT_SECONDS` to `240` in `template.yaml` and
  change its `PlannerSettings` default and maximum validation bound to support
  the selected policy.
- Set `PLANNER_LLM_MAX_RETRIES` to `1` in `template.yaml` and retain its
  minimum bound of one. Its current name represents total whole-plan attempts;
  clarify that wording in user-facing documentation.
- Budget calculation: one 240-second LLM request + two 10-second Telegram
  allowances + 20-second safety margin = 280 seconds, within the 300-second
  application deadline. There is no retry-backoff wait with one attempt.
- Measure each strict JSON call using a monotonic clock. On a typed LLM failure
  log attempt number, elapsed milliseconds, configured model, and a normalized
  failure category; keep terminal behavior unchanged.
- Remove/update tests that expect automatic initial-plan repair on a second
  provider attempt. Manual `/plan` retry remains the recovery path.

## Implementation Steps

### Task 1: Specify the long-running single-attempt policy with tests

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_template.py`
- Modify: `tests/test_llm_client.py`

- [x] Write failing configuration tests for a valid 240-second, one-attempt
  Planner budget and an invalid budget that exceeds the application deadline.
- [x] Write failing template tests that require Planner environment values of
  `240` seconds and `1` total attempt.
- [x] Write/update LLM client construction tests that require the configured
  Planner timeout to be forwarded as the LiteLLM `timeout` argument.
- [x] Run the focused configuration, template, and LLM client tests; confirm
  they fail only for the intended unimplemented policy.

### Task 2: Implement and deploy the single long Planner request

**Files:**
- Modify: `src/meal_planner/config.py`
- Modify: `template.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_template.py`
- Modify: `tests/test_planner_handler.py`

- [x] Change Planner settings and SAM defaults to one 240-second attempt while
  preserving the 300-second application deadline and 310-second Lambda limit.
- [x] Keep budget validation accurate for the one-attempt policy and reject
  incompatible configurations before Lambda startup.
- [x] Update Planner handler tests to prove a timeout makes exactly one LLM
  request, retains the retry-ready state, and sends the existing `/plan` retry
  message.
- [x] Update invalid-response tests to prove initial generation no longer
  issues an automatic repair call.
- [x] Run focused Planner/configuration/template tests and fix failures before
  Task 3.

### Task 3: Add privacy-safe failed-attempt diagnostics

**Files:**
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing tests for a warning record on each typed Planner LLM
  failure, including attempt number, elapsed duration, model, and category.
- [x] Implement monotonic-duration measurement around the strict JSON call and
  emit the sanitized failure log before existing recovery behavior.
- [x] Ensure the log never contains prompts, preferences, generated plans,
  credentials, chat IDs, or user IDs.
- [x] Add success-path coverage proving no failure log is emitted after a valid
  generated plan.
- [x] Run focused Planner handler tests and fix failures before Task 4.

### Task 4: Synchronize operational documentation

**Files:**
- Modify: `README.md`
- Modify: `tests/test_template.py`

- [x] Update the environment-variable table and timeout explanation to state
  that Planner generation uses one 240-second provider attempt.
- [x] Document the CloudWatch diagnostic fields and the distinction between a
  LiteLLM request timeout and the Planner Lambda application deadline.
- [x] Confirm documentation does not imply a second automatic Planner attempt
  or expose sensitive logging content.
- [x] Re-run documentation-adjacent template tests and correct any mismatches.

### Task 5: Verify acceptance criteria

**Files:**
- Modify: `docs/plans/2026-08-18-allow-long-running-planner-llm-requests.md`

- [x] Verify the checked-in deployment configuration represents exactly one
  240-second
  Planner LLM attempt, 300-second application deadline, and 310-second Lambda
  timeout.
- [x] Verify terminal initial-plan failures preserve the preference and retain
  the current `/plan` retry behavior.
- [x] Verify a Planner LLM failure is diagnosable from one sanitized
  CloudWatch log record.
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  and `uv run pytest`; fix all failures.

### Task 6: Update plan status and archive it

**Files:**
- Move: `docs/plans/2026-08-18-allow-long-running-planner-llm-requests.md`
  to `docs/plans/completed/2026-08-18-allow-long-running-planner-llm-requests.md`

- [x] Mark implementation tasks complete as their tests and checks pass.
- [x] Record any scope changes, discovered risks, or validation results in this
  plan before archiving it.
- [x] Move the plan to `docs/plans/completed/` only after every required check
  passes.

## Completion Record

- Tasks 1–3 implemented the one-attempt Planner policy, retry-state recovery,
  and sanitized typed-failure diagnostics.
- Task 4 updated the README with the 240-second provider timeout, 280-second
  Planner budget, 300-second application deadline, 310-second Lambda timeout,
  and CloudWatch fields.
- Acceptance checks verified the checked-in SAM values, retained preference
  recovery, and privacy-safe warning behavior. Live AWS deployment and manual
  Telegram verification were not performed in this workspace.
- No provider, credential, asynchronous event-contract, Bot timeout, or
  revision-semantics changes were made.
- Final validation: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, and `uv run pytest` passed; pytest reported 385 passed and 2
  skipped.

## Post-Completion

**Manual verification:** Deploy through the existing feature branch/PR flow,
run `/plan` with a realistic profile, and confirm a normal two-to-three-minute
generation arrives as a draft. If it fails, inspect the sanitized Planner
failure record and verify `/plan` reuses the saved preference.

**External system updates:** No provider or credential change is required.
Do not deploy directly to `master`; use a dedicated branch and pull request.
