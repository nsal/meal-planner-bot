# Simplify Deployment Preflight

## Overview

Refocus the deployment script on automating manual AWS deployment steps. Remove
all Python quality gates from the routine deployment path so an operator's
`.env` values cannot affect unit-test outcomes during deployment. Retain the
SAM checks that directly validate and build the deployable infrastructure.

## Context

- `scripts/deploy.py` runs Ruff formatting and lint, mypy, the full pytest
  suite, SAM validation/build, and an artifact-specific pytest target before a
  routine deployment.
- `tests/test_deploy.py` uses `FakeRunner` to assert orchestration commands and
  their boundaries without invoking AWS or SAM.
- `README.md` describes the current routine workflow and its recovery-mode
  boundaries.
- The failed deployment occurred because pytest ran with deployment
  configuration available through `.env` and the child environment; some
  configuration tests intentionally rely on absent values.

## Development Approach

- **Testing approach:** Regular (implementation first, then targeted tests).
- Keep the change narrow: the script remains responsible for its AWS/SAM
  workflow, not code quality enforcement.
- Rename the preflight function to describe its remaining SAM-only purpose.
- Do not add CI, flags, environment isolation layers, or configuration changes.
- Update tests for each changed behavior and run the required validation before
  moving to the next task.

## Testing Strategy

- Unit-test the successful SAM-only preflight command sequence and ensure it
  contains neither `uv` nor `pytest` commands.
- Unit-test a failed SAM preflight in the routine workflow to confirm deployment
  does not start after the failure.
- Run `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`,
  and `uv run mypy` after the implementation, per project standards.

## Solution Overview

Replace the mixed local-quality/deployment gate with a deployment preflight
that runs `sam validate --lint` followed by `sam build --beta-features`. Both
commands continue to receive the deployment child environment and secret
redaction values. The routine workflow invokes the renamed preflight before
`sam deploy`; guided and post-deploy-only behavior remains unchanged.

## Technical Details

- Remove the Ruff, mypy, full-suite pytest, and artifact pytest commands from
  the command tuple.
- Remove `REQUIRE_SAM_ARTIFACTS` injection because no subprocess runs the
  artifact pytest target.
- Keep command-stage labels for diagnostic clarity, including the beta-features
  requirement for the SAM build.
- Update README language so routine and recovery workflows accurately describe
  SAM preflight rather than quality gates.

## Implementation Steps

### Task 1: Make deployment preflight SAM-only

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] Rename `run_quality_gates` to a SAM-preflight name and update its routine
  deployment call site.
- [x] Retain only SAM validation and beta-features build commands, preserving
  the configured child environment and secret-safe diagnostics.
- [x] Remove the artifact-test environment override and all Python quality-gate
  command definitions.
- [x] Update the successful orchestration test to assert the exact SAM-only
  preflight sequence and the absence of `uv`/pytest commands.
- [x] Add a failing-SAM-preflight test that proves `sam deploy` is not invoked.
- [x] Run `uv run pytest tests/test_deploy.py` and fix failures before Task 2.

### Task 2: Document the lean deployment workflow

**Files:**
- Modify: `README.md`

- [x] Replace the routine-workflow description of Ruff, mypy, pytest, and
  artifact tests with SAM validation and build.
- [x] Clarify that recovery mode skips SAM preflight, building, and deployment.
- [x] Review the documented command sequence against `run_deployment`.
- [x] Verify documentation-only changes do not introduce line-formatting issues
  with `uv run ruff format --check .`.

### Task 3: Verify acceptance criteria

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`
- Modify: `README.md`

- [x] Confirm a routine deployment performs no Ruff, mypy, or pytest command.
- [x] Confirm SAM validation and `sam build --beta-features` run before deploy.
- [x] Confirm post-deploy-only mode still bypasses the SAM preflight.
- [x] Run `uv run pytest`.
- [x] Run `uv run ruff format --check .`, `uv run ruff check .`, and
  `uv run mypy`.

### Task 4: Finalize documentation and plan tracking

**Files:**
- Modify: `docs/plans/2026-08-18-simplify-deployment-preflight.md`

- [x] Mark completed implementation and verification items immediately as work
  finishes.
- [x] Record any scope changes or blockers in this plan.
- [x] Move this plan to `docs/plans/completed/` after all verification passes.

## Completion Record

- Focused verification: `uv run pytest tests/test_deploy.py` — 17 passed.
- Full verification: `uv run pytest` — 373 passed, 2 skipped.
- Static verification: `uv run ruff format --check .`, `uv run ruff check .`,
  and `uv run mypy` all passed.
- Scope changes: none. Task 4 updated this plan only; the implementation,
  tests, and README changes were already present in the shared workspace and
  were not modified here.
- Blockers: none for repository verification.
- Unresolved follow-up: a live routine deployment with the operator `.env` was
  not performed because it requires interactive AWS access and external state.

## Post-Completion

- Manually rerun the routine deployment with the normal operator `.env` to
  confirm it reaches the SAM preflight instead of invoking pytest.
- Create a dedicated branch and pull request before publishing the implementation;
  do not push or merge directly to `master`.
