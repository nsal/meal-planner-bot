# Tolerate Historical Telegram Webhook Errors During Deployment Verification

**GitHub issue:** [#52](https://github.com/nsal/meal-planner-bot/issues/52)

## Overview

Change the deployment runner so that a correctly configured Telegram webhook
is not treated as failed merely because Telegram retains a historic delivery
error. The runner will continue to configure the command menu and webhook,
then require `getWebhookInfo` to return the exact CloudFormation webhook URL.
It will report retained error metadata as a sanitized warning instead of
failing the deployment.

This fixes the observed false failure after a successful AWS deployment: the
bot served `/help`, while Telegram still reported an older `503 Service
Unavailable` error. It keeps malformed responses, URL mismatches, and Telegram
API failures as deployment failures.

## Context

- **Files/components involved:** `scripts/deploy.py`,
  `tests/test_deploy.py`, and possibly `README.md`.
- **Current flow:** `configure_telegram()` registers commands, calls
  `setWebhook`, obtains `getWebhookInfo`, validates `result`, validates `url`,
  then fails when `last_error_message` is truthy.
- **Failure evidence:** Telegram's last error was from 2026-08-20T10:06:52Z;
  later Bot Lambda invocations completed successfully and the bot replied to
  `/help`.
- **Existing safety patterns:** post-deployment errors redact configured
  secrets through `_safe_error`; `--post-deploy-only` is intentionally
  idempotent and avoids AWS redeployment.
- **Scope boundary:** do not add webhook deletion, polling, active health
  checks, or changes to the Telegram API client.

## Development Approach

- **Testing approach:** TDD. Change the existing failing webhook-error test
  before implementation, then add focused regression cases and run them before
  moving to documentation.
- Keep the change local to deployment orchestration. Use an existing logger or
  a small, injected-safe warning mechanism; do not expose bot tokens, webhook
  secrets, or LLM keys.
- Treat the matching webhook URL as the configuration success criterion.
- Preserve failures for malformed Telegram results, URL mismatch, and
  `TelegramAPIError` failures from command registration, setting, or reading
  webhook configuration.
- Complete each task, its tests, and its required checks before the next task.
  Use `uv run`, Ruff as the sole formatter, and 80-column text.
- Update this plan if implementation materially changes the selected
  configuration-only verification contract.

## Testing Strategy

- Update the historical-error regression to prove `last_error_message` does
  not raise when the Telegram URL matches the deployed `WebhookUrl`.
- Capture the warning and prove it includes diagnostic error metadata without
  configured secret values.
- Retain or add tests that malformed `result` and a different URL still raise
  `DeploymentError`.
- Retain the post-deployment failure test proving a genuine Telegram API
  failure is wrapped as `PostDeploymentError` after AWS deployment.
- Run the focused deployment tests during TDD, then Ruff, mypy, and the full
  `uv run pytest` suite. This repository has no UI end-to-end test framework;
  external verification is manual.

## Progress Tracking

- Mark items complete only after the stated test or verification evidence.
- Add newly discovered work with a `➕` prefix and record blockers with a
  `⚠️` prefix.
- Keep this plan synchronized with the implementation if scope changes.

## Solution Overview

The deployment flow remains:

`setMyCommands` -> `setWebhook` -> `getWebhookInfo` -> validate response and
URL -> optionally warn about retained Telegram delivery metadata.

`last_error_message` and `last_error_date` describe delivery history, not a
reliable health assertion for the newly configured URL. The runner will log
them only after successful response and URL validation. This makes routine and
recovery deployments correctly reflect the active configuration while keeping
all direct configuration and transport failures blocking.

## Technical Details

- Update `configure_telegram()` in `scripts/deploy.py` after the URL equality
  check.
- Read `result["last_error_message"]` defensively. When it is truthy, emit one
  redacted warning; include `last_error_date` only as optional diagnostic data.
- Do not change the `TelegramAPI` request contract, stack-output parsing,
  deployment stages, or exception types.
- Update deployment tests using the existing fake Telegram client and logging
  capture fixture/pattern. Assert successful completion rather than an error
  for retained metadata.
- If README wording promises an error-free Telegram webhook state, revise it
  to state that URL configuration is verified and historic delivery errors are
  reported for operator follow-up.

## What Goes Where

- **Implementation Steps:** local code, test, and documentation changes that
  can be completed in this repository.
- **Post-Completion:** a manual Telegram command check after deployment; no
  third-party configuration change is expected.

## Implementation Steps

### Task 1: Add warning-only handling for retained Telegram errors

**Files:**
- Modify: `tests/test_deploy.py`
- Modify: `scripts/deploy.py`

- [x] Change the existing webhook-error test first to require a successful
  `configure_telegram()` call when the returned URL matches and
  `last_error_message` is present.
- [x] Add a failing regression that captures one sanitized warning, including
  optional error-date handling and excluding all configured secret values.
- [x] Implement warning-only handling after malformed-result and URL checks,
  without changing the existing Telegram API call sequence.
- [x] Keep failures for malformed results, URL mismatch, and `TelegramAPIError`
  covered by focused success and error tests.
- [x] Run `uv run pytest tests/test_deploy.py` and fix all failures before
  Task 2.

### Task 2: Document the configuration-only verification contract

**Files:**
- Modify: `README.md` (only if its existing wording promises no webhook error)
- Modify: `tests/test_deploy.py` (only if README contract assertions change)

- [x] Inspect the deployment and recovery documentation for an inaccurate
  "error-free webhook state" promise.
- [x] Update only the affected operator guidance to explain that the deployed
  webhook URL is validated and retained Telegram delivery errors are warnings.
- [x] Keep troubleshooting guidance directing operators to inspect current
  webhook status and Lambda logs for actual delivery problems.
- [x] Update or add README contract tests for any changed wording.
- [x] Run the affected tests with `uv run pytest tests/test_deploy.py` before
  Task 3.

### Task 3: Verify acceptance criteria and repository quality gates

**Files:**
- Modify: `scripts/deploy.py` (verification only)
- Modify: `tests/test_deploy.py` (verification only)
- Modify: `README.md` (verification only, if changed)

- [x] Verify that a matching webhook URL with historical error metadata exits
  successfully and produces a sanitized warning.
- [x] Verify that malformed webhook responses, URL mismatch, and real Telegram
  API failures still block deployment with the existing safe error boundary.
- [x] Run `uv run ruff check .` and `uv run ruff format --check .`.
- [x] Run `uv run mypy src scripts` using the project's configured Python
  environment.
- [x] Run the full suite with `uv run pytest` and resolve every failure before
  completion.

### Task 4: Finalize documentation and plan tracking

**Files:**
- Modify: `docs/plans/2026-08-21-tolerate-historical-telegram-webhook-errors.md`
- Move: `docs/plans/2026-08-21-tolerate-historical-telegram-webhook-errors.md`
  to `docs/plans/completed/` after implementation is accepted

- [x] Mark every completed implementation item immediately after its evidence
  exists, recording any scope change or blocker in this plan.
- [x] Confirm `README.md` needs no further changes beyond Task 2.
- [x] Confirm all acceptance criteria and quality gates from Task 3 passed.
- [x] Move this plan to `docs/plans/completed/` only when the implementation is
  complete.

## Post-Completion

After merging and deploying from a dedicated branch through a pull request,
run `uv run python scripts/deploy.py --post-deploy-only`. Confirm it succeeds
when Telegram retains historic error metadata, then send `/help` and confirm a
reply. If a new delivery problem occurs, inspect `getWebhookInfo` and the Bot
Lambda CloudWatch log group; the warning is diagnostic, not a replacement for
runtime monitoring.
