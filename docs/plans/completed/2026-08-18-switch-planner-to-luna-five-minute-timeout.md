# Switch Planner to Luna with a Five-Minute Timeout

## Overview

Change only the asynchronous meal-planner workflow to use
`gpt-5.6-luna` with `high` reasoning effort. Extend the planner's
application-level deadline from 180 to 300 seconds and raise its Lambda
timeout to 310 seconds, leaving time for a controlled deadline response.

The conversational bot remains on `gpt-5.6-luna` with `medium` reasoning
effort. Planner retries, per-attempt request timeout, and 512 MB allocation
remain unchanged.

## Context (from discovery)

- `template.yaml` supplies the planner model and reasoning effort through SAM
  parameters, configures the Lambda timeout, and sets
  `PLANNER_FUNCTION_TIMEOUT_SECONDS`.
- `src/meal_planner/config.py` provides local defaults and checks that the
  bounded external-call budget fits the planner deadline.
- `scripts/deploy.py` independently defines the deploy-command defaults and
  passes them as SAM parameter overrides.
- `tests/test_config.py`, `tests/test_template.py`, `tests/conftest.py`, and
  `tests/test_deploy.py` cover the affected defaults and generated template or
  deployment behaviour.
- `README.md` documents the local configuration defaults and timing budget.

## Development Approach

- **Testing approach:** Regular — implement each focused change, then add or
  update its tests before beginning the next task.
- Keep the model, reasoning effort, SAM timeout, and application deadline in
  agreement across runtime configuration, deployment configuration, and
  documentation.
- Do not alter retry counts, request timeouts, memory allocation, or
  conversational-model settings.
- Run the relevant tests after every task. Before completion, run Ruff format
  checks, Ruff linting, strict Mypy, and the full test suite with `uv`.
- Update this plan if implementation scope changes.

## Testing Strategy

- Unit-test local `Settings` defaults and explicit environment overrides for
  the planner model, reasoning effort, and 300-second deadline.
- Verify SAM template defaults and `PlannerFunction` properties expose the
  intended model, `high` effort, `Timeout: 300`, and
  `PLANNER_FUNCTION_TIMEOUT_SECONDS: '300'`.
- Verify the deployment settings retain the same defaults and emit the matching
  SAM parameter overrides.
- Run `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`.

## Solution Overview

Use a configuration-only migration, with tests guarding every configuration
surface. The planner handler already reads `planner_llm_model`,
`planner_llm_reasoning_effort`, and `planner_function_timeout_seconds`; no
handler control-flow change is needed. Raising both deadline settings together
keeps the in-process timeout logic aligned with AWS Lambda's execution limit.

## Technical Details

| Setting | Current | Target |
| --- | --- | --- |
| Planner model | `gpt-5.6-terra` | `gpt-5.6-luna` |
| Planner reasoning effort | `medium` | `high` |
| Lambda `Timeout` | 180 seconds | 310 seconds |
| `PLANNER_FUNCTION_TIMEOUT_SECONDS` | 180 seconds | 300 seconds |
| LLM request timeout / retries | 45 seconds / 2 | unchanged |
| Planner memory | 512 MB | unchanged |

The 300-second application deadline remains inside Lambda's 900-second maximum
and runs ten seconds before the 310-second Lambda cutoff. It continues to
satisfy the existing configuration budget validation.

## Implementation Steps

### Task 1: Align planner runtime and deploy defaults

**Files:**

- Modify: `src/meal_planner/config.py`
- Modify: `scripts/deploy.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_deploy.py`

- [x] Change planner-only defaults to `gpt-5.6-luna` and `high` in runtime and
  deployment settings.
- [x] Change the planner runtime default deadline to 300 seconds, leaving its
  maximum validation bound and all retry/request settings unchanged.
- [x] Update shared test environment defaults without changing conversational
  defaults.
- [x] Add or update configuration tests for default and explicit planner
  model/effort/deadline values, including the configuration budget validation.
- [x] Add or update deploy tests to assert the generated SAM parameter
  overrides use the planner's Luna/high defaults.
- [x] Run focused configuration and deployment tests; they must pass before
  Task 2.

### Task 2: Raise the deployed Planner Lambda deadline

**Files:**

- Modify: `template.yaml`
- Modify: `tests/test_template.py`

- [x] Set the `PlannerLlmModel` and `PlannerLlmReasoningEffort` SAM parameter
  defaults to Luna and `high`.
- [x] Set `PlannerFunction.Properties.Timeout` to 310 seconds, reserving ten
  seconds for a controlled exit after the application deadline.
- [x] Set `PLANNER_FUNCTION_TIMEOUT_SECONDS` to the matching string value
  `300`.
- [x] Preserve the existing 512 MB memory, request timeout, retry count, and
  bot configuration.
- [x] Update template tests to assert all changed planner defaults and timeout
  values, plus unchanged planner retry settings.
- [x] Run focused template tests; they must pass before Task 3.

### Task 3: Document the new planner defaults and deadline

**Files:**

- Modify: `README.md`

- [x] Update the planner model and reasoning-effort defaults in the
  configuration table.
- [x] Document the 300-second application deadline and 310-second Lambda
  timeout.
- [x] Amend the timing-budget explanation so it accurately describes the
  five-minute planner allowance and unchanged request/retry budget.
- [x] Verify all documented names and values exactly match `template.yaml` and
  `src/meal_planner/config.py`.
- [x] Run the focused configuration, deployment, and template tests after the
  documentation cross-check; they must pass before Task 4.

### Task 4: Verify acceptance criteria

- [x] Confirm only planner settings changed: Luna/high and a five-minute
  execution/application deadline.
- [x] Confirm the conversational model remains Luna/medium and planner memory,
  retry counts, and request timeouts are unchanged.
- [x] Run `uv run ruff format --check .` and `uv run ruff check .`.
- [x] Run `uv run mypy`.
- [x] Run the full test suite with `uv run pytest`.

### Task 5: Update documentation and plan status

**Files:**

- Modify: `README.md` (only if final verification exposes a documentation gap)
- Move: `docs/plans/2026-08-18-switch-planner-to-luna-five-minute-timeout.md`
  to `docs/plans/completed/`

- [x] Record any implementation-scope deviation or verification result in this
  plan.
- [x] Update README documentation if required by final verification.
- [x] Move the completed plan to `docs/plans/completed/` after implementation
  and verification succeed.

## Implementation and Verification Results

- Updated planner configuration surfaces to Luna/high and a 300-second
  application deadline. The Lambda timeout is 310 seconds, which leaves a
  ten-second buffer for the handler to return a controlled timeout response.
  Conversational Luna/medium settings, planner memory, request timeouts,
  retry counts, and backoff remain unchanged.
- Follow-up review found that the original timeout setting was only a budget
  validation value. The Lambda handler now uses a POSIX interval timer to
  enforce that deadline and returns HTTP 504 when it expires.
- Added coverage for planner defaults, explicit overrides, budget validation,
  deployment parameter overrides, SAM template properties, and the runtime
  application deadline.
- Focused tests passed: configuration/deployment (36 tests) and template (22
  tests). The existing SAM artifact was rebuilt before template tests.
- Final verification passed: `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `uv run pytest` (372 tests).

## Post-Completion

Deploy the dedicated feature branch through the normal pull-request workflow;
do not push or merge directly to `master`. After deployment, invoke `/plan`
with a representative profile and confirm the Planner Lambda has a 310-second
AWS timeout, enforces its 300-second application deadline, and completes
without the former three-minute deadline message. Check CloudWatch `REPORT`
lines for duration and maximum memory usage before considering any future
memory adjustment.
