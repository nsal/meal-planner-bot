# Add Telegram Command Menu and Help

## Overview

Add a native Telegram command menu so entering `/` presents every supported
bot command with a concise description. Add `/help` as the in-chat reference
for the same commands, keeping user guidance and Telegram's menu consistent.

The command menu will be registered explicitly as a deployment operation,
rather than during webhook handling. This keeps normal updates fast and makes
the deployed command definitions reviewable in the repository.

## Context (from discovery)

- The application is a Python 3.14 AWS Lambda Telegram bot. `BotHandler` in
  `src/meal_planner/bot_handler.py` dispatches the eight existing commands.
- `src/meal_planner/telegram/api.py` owns synchronous, typed wrappers around
  Telegram Bot API endpoints and surfaces controlled `TelegramAPIError`s.
- `template.yaml` deploys the webhook Lambda, while `README.md` currently
  directs operators to register the webhook manually after SAM deployment.
- `tests/test_bot_handler.py` and `tests/test_telegram_api.py` provide focused
  behavior and HTTP-payload coverage. `pyproject.toml` requires Ruff (80
  columns), strict mypy, and pytest via `uv`.

## Development Approach

- **Testing approach:** Regular (implement first, then tests).
- Complete one task and its tests before starting the next; update this plan
  if scope changes.
- Keep one canonical command catalogue that produces both `/help` content and
  the payload sent to Telegram, preventing description drift.
- Add type hints to every new Python definition and retain safe plain-text
  Telegram messages.
- Run `uv run pytest` after each task; do not continue with failures.
- Before completion, run `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`.

## Testing Strategy

- Unit-test the command catalogue and help rendering for every supported
  command, correct order, and concise descriptions.
- Unit-test `setMyCommands` payload formation, configured HTTP timeout, and
  the existing controlled transport/API failure behavior.
- Unit-test `/help` routing and the unknown-command hint without requiring
  DynamoDB or a live Telegram bot.
- Unit-test the deployment helper's argument validation and API invocation by
  mocking its Telegram client; live Telegram menu verification remains a
  post-deployment check.

## Progress Tracking

- Mark completed items with `[x]` as work is finished.
- Add newly discovered work with an `➕` prefix and blockers with a `⚠️`
  prefix.
- Keep this document aligned with the implemented scope and test results.

## Solution Overview

Introduce a small, immutable command catalogue in the Telegram package. It
will list `/help` plus every existing command with Telegram-compatible
descriptions. `BotHandler` will use the same catalogue to render an in-chat
plain-text help response.

Extend `TelegramAPI` with `set_my_commands`, then provide a one-purpose
deployment helper that sends the canonical catalogue after SAM deployment.
The README will replace the hand-written command-registration step with that
helper while retaining the webhook registration and verification steps.

## Technical Details

- Define a typed immutable command value (command name and description) and a
  stable `BOT_COMMANDS` sequence in `meal_planner.telegram.commands`.
- Include: `start`, `help`, `profile`, `plan`, `grocery`, `today`,
  `submit_meals`, `checkin`, and `cancel`. Descriptions remain one concise
  line, user-neutral, and no longer than Telegram's command-description
  limit.
- Have the catalogue provide or support a deterministic, plain-text help
  message (`/command — description` per line). Do not use Markdown parse mode.
- Add `TelegramAPI.set_my_commands(commands)` to POST a JSON `commands` array
  to Telegram's `setMyCommands` endpoint through the existing `_post` path.
- Add a `scripts/configure_telegram_commands.py` command that reads the bot
  token from an explicit environment variable, invokes `set_my_commands`, and
  returns a non-zero status with a safe error message on configuration or
  Telegram failure. The deployment guide will run it after `sam deploy`.
- Add `help` to the existing command-handler mapping and implement `_cmd_help`
  without profile, workflow, or database writes. Change the unknown-command
  response to direct users to `/help`.

## What Goes Where

- **Implementation Steps:** repository changes, tests, and operator
  documentation that can be delivered in this codebase.
- **Post-Completion:** deployment and live Telegram checks requiring real
  credentials and a target environment.

## Implementation Steps

### Task 1: Create the canonical Telegram command catalogue

**Files:**
- Create: `src/meal_planner/telegram/commands.py`
- Create or Modify: `tests/test_telegram_commands.py`

- [x] define an immutable, fully typed command descriptor and the ordered
  catalogue for `/help` and all eight existing commands
- [x] add a deterministic plain-text help renderer sourced only from the
  command catalogue
- [x] validate descriptions against Telegram command name/description limits
  before an API call can be made
- [x] write tests for the full ordered catalogue and rendered help text
- [x] write edge-case tests for invalid catalogue entries or descriptions
- [x] run `uv run pytest tests/test_telegram_commands.py` — must pass before
  task 2

### Task 2: Add command registration to the Telegram client and deployment helper

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Create: `scripts/configure_telegram_commands.py`
- Modify: `tests/test_telegram_api.py`
- Create: `tests/test_configure_telegram_commands.py`

- [x] add the typed `TelegramAPI.set_my_commands` method that serializes the
  canonical command descriptors to Telegram's `setMyCommands` payload
- [x] retain the existing timeout, JSON parsing, and `TelegramAPIError`
  guarantees by reusing `_post`
- [x] implement a typed CLI entry point that requires the bot-token environment
  variable, registers the catalogue once, and returns a safe failure status
- [x] write Telegram API tests for the exact endpoint, JSON payload, and
  timeout, plus controlled failure propagation
- [x] write CLI tests for missing configuration, successful registration, and
  Telegram API failure
- [x] run `uv run pytest tests/test_telegram_api.py
  tests/test_configure_telegram_commands.py` — must pass before task 3

### Task 3: Serve in-chat help through the command router

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add `/help` to the command dispatch map and implement a stateless
  `_cmd_help` response using the canonical renderer
- [x] ensure `/help` neither reads nor mutates profile, plan, or conversation
  state
- [x] replace the unknown-command recommendation from `/start` with `/help`
- [x] write handler tests for complete help output, no repository interaction,
  and unknown-command guidance
- [x] write routing coverage proving the menu's existing commands still reach
  their current handlers
- [x] run `uv run pytest tests/test_bot_handler.py` — must pass before task 4

### Task 4: Document and verify command-menu deployment

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-08-18-add-telegram-command-menu-and-help.md`

- [x] document `/help` and the short descriptions of all native menu commands
  in the user-facing command reference
- [x] add the post-SAM-deploy command-registration invocation using the
  existing `TELEGRAM_BOT_TOKEN` operational convention
- [x] document how to rerun command registration after a command-catalogue or
  bot-token change, without requiring a webhook update
- [x] add or update documentation-focused tests if this repository's README
  assertions cover the revised workflow
- [x] run relevant documentation/template tests — must pass before task 5

### Task 5: Verify acceptance criteria

- [x] verify every supported command, including `/help`, appears once in both
  the catalogue and rendered help response
- [x] verify command registration is deployment-operated and never invoked in
  the webhook request path
- [x] verify descriptions are concise, plain text, and Telegram-compatible
- [x] run `uv run ruff format --check .` and `uv run ruff check .`
- [x] run `uv run mypy` and `uv run pytest`

### Task 6: [Final] Update documentation and plan status

- [x] update `README.md` if any implementation detail differs from this plan
- [x] update this plan with completed checkboxes, verification outcomes, and
  any scope changes
- [x] move this plan to `docs/plans/completed/` after implementation is fully
  verified

## Verification Outcomes

- `uv run pytest tests/test_telegram_commands.py`: 11 passed.
- `uv run pytest tests/test_telegram_api.py tests/test_configure_telegram_commands.py`: 14 passed.
- `uv run pytest tests/test_bot_handler.py`: 68 passed.
- `uv run pytest tests/test_router.py tests/test_template.py`: 41 passed after
  rebuilding the SAM artifact.
- `uv run ruff format --check .`: passed.
- `uv run ruff check .`: passed.
- `uv run mypy`: passed with no issues in 17 source files.
- `uv run pytest`: 350 passed.

No scope changes were required.

## Post-Completion

**Manual verification:** Deploy to a non-production stack, run the command
registration helper with the real bot token, open the bot chat, enter `/`, and
confirm Telegram displays all nine commands with their intended descriptions.
Send `/help`, `/start`, and an unsupported command to confirm the text and
fallback guidance. Repeat after a bot-token rotation.

**External system updates:** Register the command menu separately for each bot
token/environment used by the project. Open a feature branch and pull request;
do not push or merge directly to `master`.
