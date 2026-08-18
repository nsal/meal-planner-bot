# Automate AWS and Telegram Deployment

## Overview

Create one typed Python deployment orchestrator that reads deployment settings
from the ignored `.env` file, authenticates interactively to AWS, prepares and
deploys the SAM stack, and completes every required Telegram and AWS
post-deployment operation. This closes the gap where application code can be
deployed successfully while Telegram's native command menu remains stale.

The command will use only the `meal-planner` AWS profile and `eu-west-1`
region. It will make AWS secret mutation an explicit double opt-in, fail at the
first unsuccessful stage, avoid exposing credentials, and provide a safe
post-deployment-only recovery path.

## Context (from discovery)

- `README.md` currently documents separate commands for AWS authentication,
  secret creation, SAM validation/build/deploy, Telegram command registration,
  webhook configuration, and IAM transaction-permission verification.
- `scripts/configure_telegram_commands.py` registers the canonical catalogue
  through `TelegramAPI.set_my_commands`, but `sam deploy` does not invoke it.
- `scripts/verify_transaction_permission.py` already performs the required
  read-only IAM check through boto3, but does not currently accept an explicit
  AWS profile.
- `src/meal_planner/config.py` uses typed Pydantic settings and reads `.env`;
  deployment settings need a separate model because stack names, AWS secret
  names, and mutation intent are operator configuration rather than Lambda
  runtime configuration.
- `template.yaml` requires secret names, Telegram allowed user IDs, model
  choices, and a changing `SecretRefreshToken`; it outputs the webhook URL,
  table name, and Lambda function names needed after deployment.
- The repository has no GitHub Actions deployment workflow or shared release
  script, so a local Python orchestrator is the reusable automation boundary.
- AWS CLI `aws login --remote` requires AWS CLI 2.32.0 or newer and owns the
  interactive URL and authorization-code exchange.

## Development Approach

- **Testing approach:** TDD. Write failing tests for each behavior slice before
  implementing that slice.
- Complete each task and make its focused tests pass before starting the next
  task.
- Keep deployment orchestration in `scripts/deploy.py` initially. Use small,
  typed collaborators inside that module without creating a new package until
  reuse warrants it.
- Reuse the canonical Telegram command catalogue and existing verification
  behavior instead of copying them.
- Treat all tokens, keys, webhook secrets, login codes, and secret values as
  sensitive. Never print them or include them in diagnostic command previews.
- Every code task includes success, failure, and edge-case tests.
- Update this plan immediately if implementation scope or sequencing changes.
- Follow `pyproject.toml`: Ruff formatting and linting at 80 columns, strict
  mypy, Python 3.14, and all execution through `uv` or `uvx`.
- Run `uv run pytest` and require a green suite before completion.

## Testing Strategy

- Unit-test deployment settings, CLI parsing, stage selection, subprocess
  arguments, environment propagation, secret synchronization decisions,
  CloudFormation output parsing, and secret-safe failures.
- Mock subprocess and Telegram HTTP boundaries. Tests must not authenticate,
  mutate AWS, deploy a real stack, or call Telegram.
- Assert exact ordering for routine, guided, secret-sync, and
  post-deployment-only workflows.
- Retain focused tests for the existing command-registration and IAM verifier
  helpers when their interfaces change.
- Test that every AWS and SAM operation selects profile `meal-planner` and
  region `eu-west-1`, including boto3-based verification through its session.
- Run focused test files at the end of each task, then Ruff, mypy, and the full
  test suite during acceptance verification.

## Progress Tracking

- Mark completed items with `[x]` immediately when finished.
- Add newly discovered tasks with an `➕` prefix.
- Document blockers with a `⚠️` prefix.
- Keep this plan aligned with the delivered behavior and verification results.

## Solution Overview

Add `scripts/deploy.py` as the sole release entry point. A typed
`DeploymentSettings` model loads `.env` and requires the fixed AWS profile and
region plus stack, Telegram, model, allowlist, and Secrets Manager settings. A
typed command runner executes external tools without shell interpolation,
supports attached terminal I/O for AWS login, and reports sanitized failures.

The orchestrator will authenticate with `aws login --remote`, confirm the
resolved AWS identity, check prerequisites and configuration, optionally
synchronize secrets, run local quality gates, validate and build SAM, deploy,
then register Telegram commands and the webhook. Finally it verifies webhook
state and the deployed DynamoDB transaction permission and prints a non-secret
summary. `--post-deploy-only` will safely repeat only the idempotent external
configuration and verification stages after a partial release failure.

The selected Python approach is preferred over shell because it provides
typed validation, deterministic tests, structured error handling, and safer
secret handling. A hybrid shell/Python boundary is rejected because it adds a
second orchestration surface without a current need.

## Technical Details

The `.env` deployment fields will include:

```dotenv
AWS_PROFILE=meal-planner
AWS_REGION=eu-west-1
STACK_NAME=meal-planner-dev
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
LLM_API_KEY=...
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
TELEGRAM_BOT_TOKEN_SECRET_NAME=meal-planner/bot-token
TELEGRAM_WEBHOOK_SECRET_NAME=meal-planner/webhook-secret
LLM_API_KEY_SECRET_NAME=meal-planner/llm-key
SYNC_SECRETS=false
```

`AWS_PROFILE` and `AWS_REGION` will reject values other than `meal-planner`
and `eu-west-1`. Secret writes require both `SYNC_SECRETS=true` and the
`--sync-secrets` CLI flag. With either control absent, the orchestrator only
checks that all named secrets exist. Secret values sent to AWS CLI will travel
through standard input or another non-command-line channel and must never
appear in process arguments or logs.

Supported modes:

- routine: authenticate, confirm identity, prepare, validate, test, build,
  deploy non-interactively, and run all post-deployment tasks;
- `--guided`: use SAM guided deployment for the first stack deployment while
  retaining the same validation and post-deployment stages;
- `--sync-secrets`: enable secret creation/update only when the `.env` intent
  also opts in;
- `--post-deploy-only`: authenticate, confirm identity, load existing stack
  outputs, and rerun idempotent Telegram and AWS verification tasks without a
  build or deploy.

The login subprocess will be attached directly to the terminal:

```bash
aws login --remote --profile meal-planner --region eu-west-1
```

The AWS CLI will print the URL and read the authorization code. After login,
`sts get-caller-identity` will resolve the account and principal, which the
operator must confirm before any mutation. All later AWS and SAM commands will
receive explicit profile and region arguments; child environments will also
set `AWS_PROFILE=meal-planner` and `AWS_REGION=eu-west-1` for internal SDK use.

Post-deployment requires `WebhookUrl`, `MealPlannerTableName`,
`BotFunctionName`, and `PlannerFunctionName` outputs. Telegram registration
will replace the default command catalogue, set the expected webhook URL and
secret token, then require `getWebhookInfo` to report that URL without an
error. An AWS success followed by a Telegram failure is reported as a partial
deployment with a non-zero exit; it is not rolled back automatically.

## What Goes Where

- **Implementation Steps:** typed deployment code, Telegram API additions,
  verifier profile support, tests, and operator documentation in this
  repository.
- **Post-Completion:** a live interactive login, deployment to the intended AWS
  account, real Telegram verification, and GitHub branch/PR publication.

## Implementation Steps

### Task 1: Define and validate deployment configuration

**Files:**
- Create: `scripts/deploy.py`
- Create: `tests/test_deploy.py`

- [x] write failing tests for loading required deployment values from `.env`
  and environment overrides
- [x] write failing tests that require AWS profile `meal-planner`, region
  `eu-west-1`, non-empty stack/secret names, valid Telegram user IDs, and
  secret-safe validation errors
- [x] write failing tests for `--guided`, `--sync-secrets`, mutually compatible
  modes, and `--post-deploy-only` argument parsing
- [x] implement the typed `DeploymentSettings`, deployment mode, and CLI parser
  with all new Python definitions fully typed
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  task 2

### Task 2: Add safe command execution and AWS authentication

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] write failing tests that AWS CLI 2.32.0+, `uv`, and `uvx` prerequisites
  are checked with controlled errors
- [x] write failing tests that `aws login --remote` runs first with attached
  terminal I/O, profile `meal-planner`, and region `eu-west-1`
- [x] write failing tests for STS identity parsing, malformed responses,
  explicit operator confirmation, and cancellation before mutation
- [x] write failing tests that captured subprocess failures are sanitized and
  never echo secret values or authorization input
- [x] implement the typed command runner, prerequisite checks, interactive
  authentication, and identity confirmation
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  task 3

### Task 3: Check and explicitly synchronize AWS secrets

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] write failing tests that the default path verifies all three named
  Secrets Manager secrets without retrieving or changing their values
- [x] write failing tests that secret writes require both `SYNC_SECRETS=true`
  and `--sync-secrets`, with a controlled error for mismatched intent
- [x] write failing tests for create-missing and update-existing behavior,
  partial AWS failures, fixed profile/region arguments, and stable ordering
- [x] write failing tests proving secret values do not appear in subprocess
  arguments, captured output, exceptions, or summaries
- [x] implement read-only secret checks and double-opt-in synchronization using
  a non-command-line channel for secret values
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  task 4

### Task 4: Automate quality gates, SAM build, and deployment

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] write failing tests for the exact pre-deployment order: Ruff format
  check, Ruff lint, mypy, full pytest, SAM validate, SAM build, and fresh
  artifact template tests
- [x] write failing tests for routine and guided SAM deployment arguments,
  including fixed profile/region, capabilities, stack name, secret names,
  allowed user IDs, model settings, and a generated refresh token
- [x] write failing tests that a failed quality gate, validation, build, or
  deployment stops all subsequent deployment and Telegram stages
- [x] write failing tests that routine deployment accepts an empty changeset
  while guided deployment retains interactive terminal behavior
- [x] implement quality-gate, build, and deploy stages without shell command
  interpolation
- [x] run `uv run pytest tests/test_deploy.py` and require it to pass before
  task 5

### Task 5: Add Telegram webhook configuration and verification

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `scripts/deploy.py`
- Modify: `tests/test_telegram_api.py`
- Modify: `tests/test_deploy.py`

- [x] write failing Telegram API tests for exact `setWebhook` and
  `getWebhookInfo` requests, timeout behavior, malformed results, and
  controlled API/transport failures
- [x] write failing deployer tests for required CloudFormation outputs,
  canonical command registration, webhook setup, URL verification, Telegram
  error reporting, and post-deployment stage ordering
- [x] implement typed Telegram API methods for setting and reading webhook
  state through the existing controlled HTTP path
- [x] implement stack-output resolution plus command and webhook registration,
  requiring the deployed URL and an error-free webhook state
- [x] run `uv run pytest tests/test_telegram_api.py tests/test_deploy.py` and
  require it to pass before task 6

### Task 6: Make AWS verification profile-aware and complete recovery mode

**Files:**
- Modify: `scripts/verify_transaction_permission.py`
- Modify: `scripts/deploy.py`
- Modify: `tests/test_verify_transaction_permission.py`
- Modify: `tests/test_deploy.py`

- [x] write failing verifier tests for required `--profile`, boto3 session
  creation with `meal-planner`, and region `eu-west-1`
- [x] write failing deployer tests that invoke permission verification with the
  fixed profile/region and stop on denial or malformed AWS output
- [x] write failing tests proving `--post-deploy-only` skips secret checks,
  quality gates, build, and deploy while retaining login, identity
  confirmation, stack resolution, Telegram tasks, IAM verification, and a
  non-secret summary
- [x] modify the permission verifier to construct all clients from an explicit
  profile-aware boto3 session
- [x] implement the complete routine, guided, and recovery orchestration plus
  the final deployment summary
- [x] run `uv run pytest tests/test_verify_transaction_permission.py
  tests/test_deploy.py` and require it to pass before task 7

### Task 7: Document the single deployment workflow

**Files:**
- Modify: `README.md`
- Modify: `tests/test_deploy.py`

- [x] write or update documentation assertions for the fixed profile, fixed
  region, required `.env` fields, routine deployment, guided deployment,
  double-opt-in secret synchronization, and recovery command
- [x] replace the fragmented deployment commands with the orchestrator as the
  primary workflow while retaining relevant AWS permission prerequisites and
  live smoke-test guidance
- [x] document AWS CLI 2.32.0+, remote authorization-code handling, identity
  confirmation, safe partial-failure recovery, and the fact that `.env` must
  never be committed
- [x] document that secret synchronization and live deployment are external
  mutations and show non-secret example configuration only
- [x] run documentation-focused and deployer tests and require them to pass
  before task 8

### Task 8: Verify acceptance criteria

- [x] verify a normal successful run performs login, preparation, deployment,
  command registration, webhook verification, permission verification, and a
  safe summary in the designed order
- [x] verify every AWS/SAM path is pinned to profile `meal-planner` and region
  `eu-west-1`, including subprocess and boto3 paths
- [x] verify secrets cannot be changed with only one opt-in control and cannot
  leak through arguments, output, errors, or summaries
- [x] verify post-deployment-only recovery is idempotent and does not build,
  deploy, or synchronize secrets
- [x] run `uv run ruff format --check .` and `uv run ruff check .`
- [x] run `uv run mypy`
- [x] run `uv run pytest` and confirm the full suite passes

### Task 9: [Final] Update documentation and plan status

**Files:**
- Modify: `README.md` if implementation details changed
- Modify: `docs/plans/2026-08-18-automate-aws-telegram-deployment.md`

- [x] update `README.md` for any verified implementation detail that differs
  from the planned interface
- [x] record focused and full verification outcomes in this plan
- [x] add any newly established reusable project convention to `AGENTS.md` only
  if one is discovered during implementation
- [x] ensure every completed item is marked and every deviation is explained
- [x] move this plan to `docs/plans/completed/` only after implementation and
  all verification are complete

## Post-Completion

**Manual verification:** Run the deployer from a trusted terminal, complete
the remote AWS login for the intended account, inspect and confirm the printed
identity, and deploy a non-production stack. Type `/` in the bot chat and
confirm all canonical commands appear. Send `/help`, inspect Telegram webhook
status, and complete a transactional meal-log smoke test.

**External system updates:** Ensure the authenticating AWS identity has
`SignInLocalDevelopmentAccess` plus the deployment and read-only verification
permissions documented in the README. If secret synchronization is requested,
review the target secret names before enabling both opt-in controls. Implement
on a dedicated branch, commit with the associated issue number using
Conventional Commits, open a pull request, and never push or merge directly to
`master`. After completion, comment on the associated GitHub issue with the
commit or PR link and a summary of the delivered work.

## Verification outcomes

- Focused deployment, Telegram, and verifier tests: 32 passed.
- Full suite: `uv run pytest` — 364 passed.
- Formatting and linting: `uv run ruff format --check .` and `uv run ruff
  check .` passed; all 62 files were formatted.
- Static typing: `uv run mypy` passed for 17 source files, and an additional
  mypy run passed for all three scripts.
- `uvx --from aws-sam-cli sam build --beta-features` refreshed the ignored SAM
  artifact; the full template/artifact tests then passed.

## Implementation deviations

- The standalone transaction verifier keeps `--profile` optional for backwards
  compatibility with its existing direct invocation. The deployment
  orchestrator always supplies `--profile meal-planner`, which uses the
  explicit profile-aware boto3 session required by the deployment workflow.
- No new reusable project convention was discovered, so `AGENTS.md` was left
  unchanged.
