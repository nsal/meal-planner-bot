# Merge AWS Secrets

## Overview

Replace the three independently billed AWS Secrets Manager secrets for the
Telegram bot token, Telegram webhook secret, and LLM API key with one JSON
secret. This reduces the steady-state secret count while keeping each Lambda
environment variable and application configuration interface unchanged.

The SAM template will select individual JSON fields through versionless
CloudFormation dynamic references. The existing non-secret
`SecretRefreshToken` remains the explicit deployment trigger that causes Lambda
configuration updates after a secret rotation.

## Context (from discovery)

- The project is a Python 3.14 SAM application with Bot and Planner Lambda
  functions; deployment is orchestrated by `scripts/deploy.py`.
- `template.yaml` currently accepts `TelegramBotTokenSecretName`,
  `TelegramWebhookSecretName`, and `LlmApiKeySecretName`, then resolves each
  secret directly into existing Lambda environment variables.
- `DeploymentSettings` reads the corresponding three secret-name settings and
  builds three `(name, value)` entries for `synchronize_secrets`.
- `SecretRefreshToken` is generated for each deployment and applied to both
  functions, forcing CloudFormation to re-resolve the versionless references.
- `tests/test_deploy.py`, `tests/test_template.py`, and `tests/test_readme.py`
  codify the deployment, template, and operator-documentation contracts.

## Development Approach

- **Testing approach:** TDD. Add or adapt focused tests first, observe their
  expected failure against the current three-secret behavior, then implement
  the smallest change that makes them pass.
- Store one JSON `SecretString` with exactly these stable fields:
  `telegram_bot_token`, `telegram_webhook_secret`, and `llm_api_key`.
- Keep `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `LLM_API_KEY` as
  local plaintext operator inputs and Lambda environment variable names. Do
  not add application-side JSON parsing or runtime Secrets Manager API calls.
- Keep secret material out of command arguments, logs, diagnostics, and plan
  output. Continue using a mode-600 temporary file for AWS CLI writes.
- Complete each task, including its tests and required command, before starting
  the next one. Update this plan immediately if the implementation scope
  changes.

## Testing Strategy

- Unit-test `DeploymentSettings` and `synchronize_secrets` for the single-name
  configuration, one JSON payload, missing-secret behavior, opt-in writes, and
  redaction of all constituent values.
- Template-test one `AppSecretsSecretName` parameter and references using
  `SecretString:<json-key>` while confirming the existing environment variable
  names and refresh marker remain intact.
- Documentation-test the revised `.env`, deployment, rotation, and legacy
  cleanup guidance.
- Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  and `uv run mypy`. Also run the repository's SAM validation/build preflight
  through `uvx --from aws-sam-cli` as documented.

## Progress Tracking

- Mark completed items with `[x]` immediately during implementation.
- Add newly discovered work with a `➕` prefix and document blockers with a
  `⚠️` prefix.
- Keep this plan synchronized with any implementation deviation.

## Solution Overview

`APP_SECRETS_SECRET_NAME` will replace the three individual secret-name
variables and SAM parameters. The deployment script serializes the three local
secret values into one JSON object and performs one Secrets Manager
describe/create/update action. The template uses field-level dynamic references
to populate the existing environment variable values, avoiding all handler,
settings, and runtime IAM changes.

After any JSON secret update, a SAM deployment passes a new random
`SecretRefreshToken`. Because it changes a Lambda environment setting,
CloudFormation updates both function configurations and resolves the current
JSON fields. Changing only the Secrets Manager value does not otherwise update
the previously resolved Lambda environment.

## Technical Details

- Add one deployment setting and template parameter:
  `APP_SECRETS_SECRET_NAME` / `AppSecretsSecretName`.
- Serialize the secret with `json.dumps` from a typed mapping. Preserve the
  stable field names exactly; AWS `put-secret-value` replaces the entire JSON
  object rather than merging one field.
- Use versionless references of the form
  `{{resolve:secretsmanager:${SecretName}:SecretString:llm_api_key}}`, with no
  `version-stage` or `version-id`, so the next resource update reads
  `AWSCURRENT`.
- Retain the existing double opt-in for secret writes:
  `SYNC_SECRETS=true` plus `--sync-secrets`.
- Preserve the webhook rotation order: publish the complete JSON object,
  deploy with a new refresh token, then register and verify the Telegram
  webhook with the new webhook-secret field.

## What Goes Where

- **Implementation Steps:** repository changes, tests, and documentation that
  can be delivered together in this codebase.
- **Post-Completion:** AWS Secret Manager migration and verification, which
  require the deploying account and must not be automated destructively.

## Implementation Steps

### Task 1: Convert deployment settings and secret synchronization to one JSON secret

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy.py`

- [x] Add failing tests that require `APP_SECRETS_SECRET_NAME`, reject missing
  or blank values safely, and retire the three individual secret-name inputs.
- [x] Add failing tests that require one stable JSON payload, one
  `describe-secret` request, and at most one `create-secret` or
  `put-secret-value` request during synchronization.
- [x] Replace the three secret-name settings and `secret_specs` with one typed
  secret-name setting and a deterministic JSON serialization helper; retain
  individual plaintext-value redaction and file-based secret writes.
- [x] Update synchronization and SAM parameter override construction to use
  only the single secret name, retaining double opt-in semantics.
- [x] Add/finish edge-case tests for update/create failures and confirm no JSON
  payload or constituent secret appears in command arguments or diagnostics.
- [x] Run `uv run pytest tests/test_deploy.py` and require it to pass before
  task 2.

### Task 2: Update SAM dynamic references for JSON fields

**Files:**
- Modify: `template.yaml`
- Modify: `tests/test_template.py`

- [x] Add failing template tests for exactly one `AppSecretsSecretName`
  parameter and no legacy individual secret-name parameters.
- [x] Add failing template tests for the three JSON-key references while
  preserving the existing Lambda environment variable names and
  `SECRET_REFRESH_TOKEN` references.
- [x] Replace the three parameters and direct `SecretString` references in
  `template.yaml` with field-level references to the single secret.
- [x] Confirm both Lambda resources still receive a configuration change when
  `SecretRefreshToken` changes and that no runtime `GetSecretValue` policy or
  application JSON parsing is introduced.
- [x] Add/finish structural assertions covering both generated Lambda
  configurations and the absence of a whole-secret environment value.
- [x] Run `uv run pytest tests/test_template.py` and
  `uvx --from aws-sam-cli sam validate --lint --region eu-west-1`; both pass.

### Task 3: Document the one-secret workflow and safe rotation sequence

**Files:**
- Modify: `README.md`
- Modify: `tests/test_readme.py`

- [x] Add failing documentation tests for `APP_SECRETS_SECRET_NAME`, the
  removal of obsolete secret-name variables, and single-secret terminology.
- [x] Update the `.env` example, routine deployment description, and secret
  synchronization section to describe one JSON Secrets Manager secret.
- [x] Replace the manual rotation example with a file-based complete JSON
  object update followed by a deployment containing a new
  `SecretRefreshToken`; state that partial field updates are not merged.
- [x] Document the webhook rotation ordering and the deliberate manual
  post-cutover cleanup of the three legacy secrets only after successful live
  verification.
- [x] Add/finish tests covering rotation, refresh, and cleanup wording without
  embedding real secret values.
- [x] Run `uv run pytest tests/test_readme.py`; 8 tests pass.
  task 4.

### Task 4: Verify the complete migration contract

**Files:**
- Modify: `scripts/deploy.py` (only if checks reveal a defect)
- Modify: `template.yaml` (only if checks reveal a defect)
- Modify: `tests/test_deploy.py` (only if checks reveal a defect)
- Modify: `tests/test_template.py` (only if checks reveal a defect)
- Modify: `README.md` (only if checks reveal a defect)

- [x] Review the final diff against this plan: one JSON secret name, three
  JSON keys, unchanged application environment interface, and no runtime
  secret retrieval.
- [x] Run `uv run pytest`; 1,058 passed and 2 skipped.
- [x] Run `uv run ruff check .` and `uv run ruff format --check .`; both pass.
  findings before proceeding.
- [x] Run `uv run mypy`; no issues found.
- [x] Run `uvx --from aws-sam-cli sam build --beta-features --region eu-west-1`
  and verify the generated template retains
  the intended dynamic references.
- [x] Record all completed checks in this plan before task 5.

### Task 5: Finalize implementation documentation and plan tracking

**Files:**
- Modify: `docs/plans/2026-08-20-merge-aws-secrets.md`
- Move: `docs/plans/2026-08-20-merge-aws-secrets.md` to
  `docs/plans/completed/2026-08-20-merge-aws-secrets.md`

- [x] Verify every acceptance criterion in the Overview and Technical Details
  is implemented and checked.
- [x] Confirm each preceding task includes completed tests for success and
  error behavior.
- [x] Run the final full test suite: `uv run pytest` (1,058 passed, 2 skipped).
- [x] Update this plan with validation results and the credential-free SAM
  command adjustment. Live deployment and smoke verification were completed
  successfully by the operator.
- [x] Move the completed plan to `docs/plans/completed/` now that all
  implementation checkboxes are complete.

## Post-Completion

**AWS migration and verification**

- Set `APP_SECRETS_SECRET_NAME` in the ignored operator `.env`, retaining the
  three local plaintext values used to construct the JSON payload.
- Run `uv run python scripts/deploy.py --sync-secrets`, inspect the confirmed
  AWS identity and the target secret name, and verify the deployment plus
  Telegram webhook end to end.
- For an individual credential rotation, update the complete JSON object using
  a restricted local file, then deploy with a new `SecretRefreshToken` before
  relying on the new value.
- After successful production verification, schedule deletion or remove the
  three legacy Secrets Manager secrets through the approved AWS process. This
  action is intentionally not automated by the repository.
- Confirm the deployer remains authorized to read the new secret name during
  CloudFormation deployment; no new Lambda runtime permission is required
  because values are resolved into function configuration by CloudFormation.
