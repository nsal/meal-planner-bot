# Simplify and Repair Deployment Runner

> Planned for GitHub issue
> [#42](https://github.com/nsal/meal-planner-bot/issues/42).

## Overview

Replace the oversized deployment orchestrator with a lean, typed Python runner
that reads `.env` and executes the documented AWS, SAM, and Telegram stages in
one obvious sequence. The runner will announce each stage on stdout, stop at
the first failure, preserve useful secret-safe diagnostics, and distinguish an
AWS deployment failure from a post-deployment Telegram failure.

Remove DynamoDB transaction-policy simulation from routine and recovery
deployments. Keep the read-only verifier as a standalone troubleshooting tool
for initial deployments, IAM/template changes, or suspected authorization
problems. Refresh the README so it accurately describes all recent deployment
behavior and serves as the operational contract for the script.

## Context (from discovery)

- `scripts/deploy.py` is an approximately 900-line typed orchestrator that
  already loads `.env`, but its layers obscure the requested linear workflow.
- `scripts/verify_transaction_permission.py` catches `ClientError` and
  `BotoCoreError` broadly, discarding the AWS operation, error code, and useful
  message behind a generic verification failure.
- `scripts/deploy.py` currently runs IAM simulation after SAM deployment and
  Telegram configuration, then reports any verification error as an overall
  deployment failure even though the stack may already be deployed.
- `template.yaml` explicitly grants the Bot Lambda
  `dynamodb:TransactWriteItems` on the exact table ARN, while
  `tests/test_template.py` protects that policy contract.
- `README.md`, `tests/test_deploy.py`, and
  `tests/test_verify_transaction_permission.py` are the primary behavior and
  documentation surfaces affected by this refactor.
- The project uses Python 3.14, `uv`, Ruff at 80 columns, strict mypy, and
  pytest as configured in `pyproject.toml`.

## Development Approach

- **Testing approach:** TDD. Add or change expectations before implementing
  each behavior slice.
- Complete each task fully and make its focused tests pass before starting the
  next task.
- Keep `scripts/deploy.py` as the stable deployment entry point and preserve
  the existing `.env` fields and supported CLI options: `--guided`,
  `--sync-secrets`, and `--post-deploy-only`.
- Favor direct sequential code and small stage functions over general-purpose
  orchestration types. Retain an abstraction only when it materially protects
  secrets, interactive subprocess behavior, or testability.
- Every task that changes code must add or update success and failure tests for
  that behavior.
- Update this plan immediately if implementation scope or sequencing changes.
- Never expose `.env` secret values in commands, stdout, stderr, exceptions,
  or summaries.
- Use `uv`/`uvx` for all execution and Ruff for formatting and linting.

## Testing Strategy

- **Deployment unit tests:** assert exact stage ordering and announcements,
  `.env`-derived arguments and child environment, CLI mode boundaries,
  fail-fast behavior, interactive command handling, and secret redaction.
- **Outcome tests:** distinguish failures before `sam deploy` completes from
  failures during CloudFormation output resolution or Telegram setup.
- **Recovery tests:** prove `--post-deploy-only` skips secret checks, SAM
  validation, build, deployment, and IAM simulation while rerunning all
  idempotent Telegram stages.
- **Verifier tests:** cover safe AWS operation/code/message diagnostics for
  `ClientError`, safe botocore failures, denied simulation, malformed AWS
  responses, and successful explicit permission.
- **Documentation contract tests:** assert that README commands, supported
  modes, stdout behavior, recovery guidance, and standalone IAM troubleshooting
  match the implementation.
- **Project gates:** run focused tests after every task, then Ruff formatting,
  Ruff linting, strict mypy (including deployment scripts explicitly if the
  configured target omits them), and the full pytest suite.

## Progress Tracking

- Mark completed checklist items with `[x]` immediately when done.
- Add newly discovered work with an `➕` prefix.
- Record blockers or failed assumptions with a `⚠️` prefix.
- Keep this plan synchronized with actual implementation and scope.
- Move the plan to `docs/plans/completed/` only after all acceptance checks
  pass.

## Solution Overview

`scripts/deploy.py` remains the one operator command. It loads and validates
deployment settings from `.env`, constructs subprocess argument lists without
shell interpolation, and executes explicit stages in source order. Each stage
prints a numbered heading and each safe external command is shown before it
runs. Interactive AWS login and guided SAM deployment retain terminal access;
other failures include bounded, redacted stdout and stderr.

Routine mode performs prerequisite/configuration checks, AWS remote login and
identity confirmation, secret existence or explicitly authorized secret sync,
SAM validation, SAM build, SAM deployment, stack-output resolution, Telegram
command registration, webhook setup, and webhook verification. Telegram
substeps are individually announced even though they call the Python API
client rather than external commands.

Once `sam deploy` succeeds, the runner records a clear phase boundary and
announces that AWS deployment completed. Any subsequent failure states that
deployment succeeded but post-deployment configuration failed and points to
`--post-deploy-only`. Recovery mode announces the skipped deployment stages,
resolves the existing stack, and repeats Telegram configuration.

The IAM transaction verifier is never invoked automatically. It remains a
standalone, documented diagnostic with useful sanitized AWS errors. The source
template and its tests remain the normal release-time IAM contract.

## Technical Details

- Preserve the `.env` schema, fixed `meal-planner` profile, fixed `eu-west-1`
  region, secret double opt-in, and existing SAM parameter overrides.
- Print stable numbered stage headings to stdout and named Telegram substeps.
- Render commands with `shlex.join` only for display; execute them as argument
  sequences with `shell=False` behavior.
- Redact all configured secret values before emitting diagnostic text and
  bound verbose command output to the existing safe maximum.
- Track whether SAM deployment completed so the top-level error message can
  identify deployment-phase versus post-deployment failure accurately.
- Do not invoke `scripts/verify_transaction_permission.py` from routine,
  guided, or post-deploy-only mode.
- For standalone verifier `ClientError`, report the AWS operation name, error
  code, and sanitized message. For other botocore errors, report a safe error
  type/message without credentials.
- Keep the README command `uv run python scripts/deploy.py`, document the
  announced stage sequence and recovery behavior, and move IAM simulation into
  a troubleshooting section with its caller-permission requirement.

## What Goes Where

- **Implementation Steps:** deployment-runner refactor, tests, standalone IAM
  diagnostic repair, and README updates in this repository.
- **Post-Completion:** run the script from the operator machine that owns the
  `meal-planner` AWS profile, perform the Telegram smoke test, publish through
  a dedicated branch and pull request, and update the associated GitHub issue.

## Implementation Steps

### Task 1: Establish the lean runner foundation

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] replace architecture-specific assertions with the exact public CLI,
  `.env`, stage-order, and stdout contract selected in the brainstorm
- [x] add failing tests for numbered routine-stage announcements and safe
  command previews
- [x] simplify configuration, CLI parsing, stage announcements, and command
  execution around direct typed helpers
- [x] retain secret redaction, bounded diagnostics, interactive subprocess
  support, and test injection without general orchestration machinery
- [x] add success and failure tests for executable absence, command failure,
  safe previews, interactive execution, and secret redaction
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  Task 2

### Task 2: Replace orchestration machinery with a sequential deployment flow

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] simplify `scripts/deploy.py` around direct typed stage functions and a
  linear routine/recovery flow while preserving the existing CLI and `.env`
  schema
- [x] remove enums, result wrappers, and general orchestration abstractions
  that no longer provide safety or testability
- [x] add failing tests for the exact routine and guided command order,
  identity confirmation, secret double opt-in, and fail-fast boundaries
- [x] implement fail-fast routine execution through SAM validation, build, and
  deployment with unchanged profile, region, and parameter targeting
- [x] add success tests for routine and guided execution through successful SAM
  deployment
- [x] add failure tests for malformed identity, rejected confirmation, secret
  mismatch, SAM validation/build failure, and deployment failure
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  Task 3

### Task 3: Make automatic Telegram setup explicit and recoverable

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] announce AWS deployment completion immediately after successful
  `sam deploy`
- [x] announce CloudFormation output resolution and each Telegram substep:
  register commands, set webhook, and verify webhook
- [x] report post-deployment failures without claiming the AWS deployment
  failed, and include the `--post-deploy-only` recovery command
- [x] make recovery mode announce skipped stages and rerun only output
  resolution plus automatic Telegram configuration
- [x] add or update success tests for automatic Telegram setup in routine,
  guided, and recovery modes
- [x] add or update failure tests for malformed outputs, Telegram API errors,
  webhook mismatch, and webhook-reported errors
- [x] run `uv run pytest tests/test_deploy.py tests/test_telegram_api.py` and
  require them to pass before Task 4

### Task 4: Keep IAM verification standalone and make failures actionable

**Files:**
- Modify: `scripts/verify_transaction_permission.py`
- Modify: `tests/test_verify_transaction_permission.py`
- Modify: `tests/test_deploy.py`

- [x] remove automatic IAM-verifier integration code from `scripts/deploy.py`
- [x] add failing verifier tests for AWS operation name, error code, and
  sanitized message on `ClientError`
- [x] add failing verifier tests for safe, useful `BotoCoreError` diagnostics
  without credential leakage
- [x] implement bounded and sanitized standalone verifier diagnostics
- [x] retain tests for allowed, denied, malformed, exact-resource, and
  profile-aware verification paths
- [x] assert again that no deployer mode invokes the standalone verifier
- [x] run `uv run pytest tests/test_deploy.py
  tests/test_verify_transaction_permission.py` and require it to pass before
  Task 5

### Task 5: Refresh deployment documentation as the operational contract

**Files:**
- Modify: `README.md`
- Modify: `tests/test_deploy.py`

- [x] update the README deployment sequence to match the lean runner exactly,
  including configuration loading, AWS authentication, secret handling, SAM
  stages, and automatic Telegram configuration
- [x] document stdout stage announcements and the boundary between AWS
  deployment success and post-deployment failure
- [x] update routine, guided, secret-sync, and post-deploy-only examples and
  recovery guidance against current behavior
- [x] move IAM transaction simulation to troubleshooting and explain when it
  is useful, what it verifies, and which caller permissions it requires
- [x] review and update other recently changed deployment details, removing
  stale claims and ensuring profile, region, model, timeout, stack-output, and
  webhook descriptions match current source
- [x] add or update documentation contract tests for all changed operational
  claims
- [x] run documentation-focused tests and `uv run ruff format --check .`
  before Task 6

### Task 6: Verify acceptance criteria

**Files:**
- Modify: `scripts/deploy.py` if verification reveals defects
- Modify: `scripts/verify_transaction_permission.py` if verification reveals
  defects
- Modify: `tests/test_deploy.py` if coverage gaps are found
- Modify: `tests/test_verify_transaction_permission.py` if coverage gaps are
  found
- Modify: `README.md` if implementation details differ

- [x] verify the normal runner reads `.env` and performs the documented stages
  in exact order with useful stdout announcements
- [x] verify all execution modes stop at the first failing stage and never
  expose configured secrets
- [x] verify successful SAM deployment cannot subsequently be mislabeled as a
  deployment failure
- [x] verify automatic Telegram configuration and post-deploy-only recovery
  behavior
- [x] verify IAM simulation is standalone and emits actionable safe errors
- [x] run `uv run ruff format --check .` and `uv run ruff check .`
- [x] run `uv run mypy` and explicitly type-check changed scripts if they are
  outside the configured mypy target
- [x] run `uv run pytest` and require the full suite to pass
- [x] run `git diff --check`

### Task 7: Finalize plan and documentation status

**Files:**
- Modify: `README.md` if acceptance verification finds drift
- Modify: `docs/plans/2026-08-18-simplify-and-repair-deployment-runner.md`

- [x] record focused and full verification outcomes in this plan
- [x] document any scope changes, new tasks, or blockers discovered during
  implementation
- [x] update `AGENTS.md` only if implementation establishes a genuinely new
  reusable project convention
- [x] ensure every completed checklist item is marked immediately
- [x] move this plan to `docs/plans/completed/` only after implementation and
  every required check pass

## Verification Results

- Focused deployment and verifier tests: `28 passed`.
- Full test suite: `376 passed, 2 skipped`.
- `uv run ruff format --check .`: passed.
- `uv run ruff check .`: passed.
- `uv run mypy`: passed.
- Explicit script mypy (`scripts/deploy.py` and
  `scripts/verify_transaction_permission.py`): passed.
- `git diff --check`: passed.

No scope blockers or new reusable project conventions were found. The
orchestration-only mode and summary wrappers were removed; the command result
boundary remains because it protects captured diagnostics and test injection.

## Post-Completion

**Manual verification:** Run `uv run python scripts/deploy.py` from the trusted
operator environment containing the `meal-planner` AWS profile and `.env`.
Confirm the printed AWS identity, observe every announced stage, verify the
final success message, and exercise the Telegram command menu and webhook with
a non-production smoke test. Run the standalone IAM diagnostic only for first
deployment, an IAM/template change, or suspected authorization trouble.

**External system updates:** Implement and publish on a dedicated branch via a
pull request; never push or merge directly to `master`. Use a Conventional
Commit containing the associated issue number. After publication, comment on
the GitHub issue with the commit or PR link and a concise implementation and
verification summary.
