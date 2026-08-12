# Align Lambda Build and Tests on Python 3.14 ARM64

## Overview

- Standardize the project, AWS SAM functions, dependency lock, and static
  analysis tools on Python 3.14.
- Move both greenfield Lambda functions from x86_64 to ARM64 so deployed
  binaries match the selected production architecture.
- Rebuild all generated SAM dependencies as Linux ARM64 artifacts and verify
  both Lambda handlers through the artifact root without Docker or host
  dependency fallback.
- Make ordinary test runs portable while providing a strict release mode that
  fails when required SAM artifacts are missing or unusable.

## Context (from discovery)

- Files and components involved:
  - `template.yaml` defines two Python 3.14 Lambda functions that currently
    target x86_64 and use SAM's `python-uv` build method.
  - `pyproject.toml` still advertises Python 3.12+, and Ruff and mypy still
    target Python 3.12.
  - `uv.lock` reflects the current broad Python compatibility contract.
  - `tests/test_template.py` validates the template and imports built
    handlers, but its compatibility guard checks only CPU architecture.
  - `meal_planner/__init__.py` maps the repository-root SAM artifact to the
    `src/meal_planner` package and should remain unchanged.
  - The current release-readiness plan records the SAM build remediation and
    must remain consistent with the final ARM64 design.
- Related patterns:
  - Python tooling must run through `uv` and use Ruff, mypy, and pytest.
  - SAM artifacts are generated under ignored `.aws-sam/` paths.
  - Runtime dependencies come from `pyproject.toml` plus `uv.lock`; duplicate
    requirements files are being removed.
- Dependencies:
  - Python 3.14 on Linux ARM64.
  - AWS SAM CLI with the `python-uv` workflow.
  - ARM64-compatible wheels for all locked runtime dependencies.
  - No Docker requirement.

## Development Approach

- **Testing approach**: TDD. Add or update failing contract tests before each
  corresponding configuration or helper change, then make that task pass.
- Complete each task fully before moving to the next.
- Make small, focused changes and preserve unrelated uncommitted work.
- Every task that changes behavior must add or update tests for success and
  error or mismatch paths.
- All tests for a task must pass before the next task begins.
- Update this plan immediately if implementation scope changes.
- Mark completed checklist items as soon as they are finished.

## Testing Strategy

- Static contract tests will assert that SAM, packaging metadata, Ruff, and
  mypy all target Python 3.14 and that SAM targets ARM64.
- Unit tests will exercise host compatibility decisions for matching and
  mismatching operating system, architecture, and Python runtime values.
- Unit tests will exercise optional and required behavior when SAM artifacts
  are missing.
- Artifact smoke tests will run under matching Linux ARM64 Python 3.14 and
  import both handlers from the generated artifact root in isolated Python
  mode.
- Artifact subprocesses must not use the project virtual environment or user
  site packages as dependency fallbacks.
- Release verification will run Ruff, mypy, the full pytest suite, SAM
  validation, a clean SAM build, and required-mode artifact tests.
- This backend-only change has no UI end-to-end test surface.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document blockers with a `⚠️` prefix.
- Keep this plan synchronized with any implementation deviation.

## Solution Overview

- Use one deployment contract: CPython 3.14 on Linux ARM64.
- Change SAM's global architecture to `arm64` while retaining the Python 3.14
  runtime, root build context, handler paths, and `python-uv` build method.
- Narrow project Python metadata to `>=3.14,<3.15`, configure Ruff for
  Python 3.14, configure mypy for Python 3.14, and refresh `uv.lock`.
- Determine artifact import compatibility using operating system,
  architecture, and Python major/minor version rather than architecture alone.
- Skip artifact imports on incompatible developer hosts with a precise reason.
  In required release mode, fail when a compatible host lacks built artifacts.
- On compatible hosts, start an isolated Python subprocess, explicitly add the
  artifact root to `sys.path`, and import each configured Lambda module.
- Remove stale generated x86_64 build output and rebuild dependencies for
  ARM64 before final verification.

## Technical Details

- SAM contract:
  - Runtime: `python3.14`.
  - Architecture: `arm64`.
  - Handlers: `meal_planner.bot_handler.lambda_handler` and
    `meal_planner.planner_handler.lambda_handler`.
  - Build method: `python-uv` with repository-root `CodeUri`.
- Python project contract:
  - `requires-python = ">=3.14,<3.15"`.
  - Ruff `target-version = "py314"`.
  - mypy `python_version = "3.14"`.
- Compatible artifact-import host:
  - `platform.system() == "Linux"`.
  - `platform.machine().lower()` is `aarch64` or `arm64`.
  - `sys.version_info[:2] == (3, 14)`.
- Required artifact mode:
  - Use `REQUIRE_SAM_ARTIFACTS=1` for release and CI verification.
  - Without the flag, a missing artifact is an explicit pytest skip.
  - With the flag, a missing artifact is a pytest failure.
- Isolated import subprocess:
  - Use the active Python 3.14 executable with `-I -S`.
  - Pass the artifact path and module name as subprocess arguments.
  - Insert the artifact root into `sys.path` inside the subprocess.
  - Capture stdout and stderr and report them on failure.
- Fresh rebuild:
  - Confirm `.aws-sam/build` is ignored generated output.
  - Remove only that exact generated build directory.
  - Run SAM validation and build through `uvx --from aws-sam-cli sam`.

## What Goes Where

- **Implementation Steps** contain repository configuration, tests, lockfile
  refresh, generated artifact rebuild, verification, and documentation.
- **Post-Completion** lists deployment and CI actions that require external AWS
  or GitHub infrastructure.

## Implementation Steps

### Task 1: Encode the Python 3.14 ARM64 stack contract

**Files:**
- Modify: `tests/test_template.py`
- Modify: `template.yaml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [x] add failing tests that assert SAM uses Python 3.14 and ARM64
- [x] add failing tests that assert project metadata, Ruff, and mypy all target
  Python 3.14
- [x] change SAM's global architecture from x86_64 to ARM64
- [x] narrow the project Python requirement and update Ruff and mypy targets
- [x] refresh `uv.lock` with `uv lock` and verify it is synchronized
- [x] run `uv run pytest tests/test_template.py` and make the new contract tests
  pass before Task 2

### Task 2: Make artifact imports platform-complete and isolated

**Files:**
- Modify: `tests/test_template.py`

- [x] add failing tests for matching Linux ARM64 Python 3.14 hosts
- [x] add failing tests for operating-system, architecture, and Python-version
  mismatch paths
- [x] replace the architecture-only guard with a typed compatibility helper
  that derives expectations from the deployment contract
- [x] replace `PYTHONPATH`-based imports with an isolated `-I -S` subprocess
  that inserts the artifact root explicitly
- [x] add assertions that preserve captured subprocess diagnostics on import
  failure
- [x] run `uv run pytest tests/test_template.py` and make all Task 2 tests pass
  before Task 3

### Task 3: Enforce optional and required SAM artifact modes

**Files:**
- Modify: `tests/test_template.py`
- Modify:
  `docs/plans/2026-08-10-meal-planner-bot-release-readiness-remediation.md`

- [x] add failing tests for missing artifacts in ordinary optional mode
- [x] add failing tests for missing artifacts when
  `REQUIRE_SAM_ARTIFACTS=1`
- [x] implement explicit skip behavior for ordinary mode and failure behavior
  for required mode
- [x] document the required-mode verification command in the release-readiness
  plan
- [x] run `uv run pytest tests/test_template.py` and make all Task 3 tests pass
  before Task 4

### Task 4: Rebuild and smoke-test Linux ARM64 artifacts

**Files:**
- Regenerate: `.aws-sam/build/` (ignored build output)
- Verify: `meal_planner/__init__.py`
- Verify: `src/meal_planner/bot_handler.py`
- Verify: `src/meal_planner/planner_handler.py`

- [x] confirm `.aws-sam/build` is ignored and resolve its exact generated path
- [x] remove only the stale generated `.aws-sam/build` directory
- [x] run `uvx --from aws-sam-cli sam validate --template-file template.yaml
  --region us-east-1`
- [x] run `uvx --from aws-sam-cli sam build --beta-features`
- [x] verify generated native extensions target ARM64 and both artifacts contain
  locked runtime dependencies
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py` and confirm
  both Lambda handlers import successfully before Task 5

### Task 5: Verify all acceptance criteria

**Files:**
- Verify: `template.yaml`
- Verify: `pyproject.toml`
- Verify: `uv.lock`
- Verify: `tests/test_template.py`
- Verify: generated `.aws-sam/build/`

- [x] verify all Overview requirements are implemented
- [x] verify incompatible hosts skip artifact imports with a precise reason
- [x] verify required mode fails for a deliberately missing artifact path
  through unit-test isolation rather than deleting a real artifact
- [x] run `uv run ruff check .`
- [x] run `uv run ruff format --check .`
- [x] run `uv run mypy`
- [x] run `uv run pytest` and confirm the full suite passes
- [x] run required-mode artifact tests and confirm no artifact test is skipped
- [x] run `git diff --check`

### Task 6: Finalize documentation and plan tracking

**Files:**
- Modify:
  `docs/plans/2026-08-10-meal-planner-bot-release-readiness-remediation.md`
- Move:
  `docs/plans/2026-08-10-align-lambda-build-and-tests-python-3-14-arm64.md`
  to `docs/plans/completed/` after implementation

- [x] update the release-readiness plan so its runtime, architecture, commands,
  and Task 1 completion state match the verified implementation
- [x] update README or AGENTS guidance only if the implementation introduces a
  new required developer workflow
- [x] rerun `uv run pytest tests/test_template.py` after documentation updates
- [x] confirm every implementation checkbox in this plan is complete
- [x] move this completed plan to `docs/plans/completed/`

## Post-Completion

**Manual verification:**

- Deploy the ARM64 stack to a non-production AWS environment.
- Invoke both Lambda functions and confirm CloudWatch logs show successful
  Python 3.14 startup without native-extension import errors.
- Exercise the Telegram webhook and asynchronous planner flow end to end.

**External system updates:**

- Configure CI release verification to use a Linux ARM64 Python 3.14 runner and
  set `REQUIRE_SAM_ARTIFACTS=1` after SAM build.
- Update any deployment dashboards or infrastructure documentation that still
  describe the functions as x86_64.
- After implementation is committed or published in a pull request, comment on
  the associated GitHub issue with the commit or PR link.
