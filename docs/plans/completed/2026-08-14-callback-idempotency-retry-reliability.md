# Callback, Idempotency, and Retry Reliability

> Planned for GitHub issue
> [#23](https://github.com/nsal/meal-planner-bot/issues/23).

## Overview

Address three post-review reliability findings in the callback, meal logging,
and LiteLLM retry paths. A committed meal check-in must never be reported as a
failed update merely because its Telegram success message could not be
delivered. Reprocessing one Telegram update must remain idempotent even when a
second LLM pass extracts a different meal date or type. Rate-limit retries must
honor numeric `Retry-After` guidance exposed by real LiteLLM exception headers.

The remediation preserves public Telegram event formats, existing persisted
meal records, the date-range meal-history access pattern, bounded retry delays,
and current deployment configuration. It adds no dependency, table, index, or
environment variable.

## Context (from discovery)

- Files/components involved: `src/meal_planner/bot_handler.py`,
  `src/meal_planner/db/dynamo.py`, `src/meal_planner/llm/client.py`, and their
  corresponding tests in `tests/`.
- `BotHandler.handle_callback` currently handles validation, persistence, and
  success-message delivery inside one broad exception boundary. A delivery
  error after `update_meal_outcome` succeeds therefore enters the mutation
  failure path.
- `DynamoRepository.log_meal` includes `entry.date_key` and
  `entry.meal_type.value` in the sort key even when `source_update_id` is
  available. Reclassification of either field can create a second item for the
  same Telegram update.
- `get_meal_history` efficiently queries date-prefixed meal keys. A stable meal
  sort key based only on the update ID would require a broader query or a new
  index, so an atomic idempotency marker is the smaller compatible change.
- `LLMClient._retry_delay` reads only `exc.retry_after`; LiteLLM commonly
  exposes provider response headers through `exc.headers` or
  `exc.response.headers`.
- The repository uses Python 3.14, `uv`, Ruff at 80 columns, strict mypy,
  pytest, and generated SAM artifact freshness/import checks.

## Development Approach

- **Testing approach**: TDD; add a failing regression test before each fix.
- Complete each task fully before moving to the next.
- Make small, focused changes and retain current public and persisted-data
  contracts.
- Every code task must add or update tests for its success and failure paths.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation scope changes.
- Prefer localized control-flow and parsing helpers over broad abstractions.
- Avoid dependencies, schema/index changes, and deployment settings unless a
  failing test proves one is required.

## Testing Strategy

- **Callback unit tests**: prove a committed update remains successful when its
  Telegram success message fails, while genuine repository failures retain the
  controlled failure message and callback acknowledgement.
- **DynamoDB unit tests**: prove one source update cannot create two meals when
  retries disagree on date, meal type, or content; verify distinct updates,
  timestamp fallback, legacy query behavior, and unexpected transaction errors.
- **LLM unit tests**: cover direct exception headers, response headers, numeric
  strings, the five-second cap, invalid guidance fallback, and existing
  exponential backoff.
- **Deployment tests**: rebuild `.aws-sam/build/` after source changes and run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`.
- **Project gates**: run full pytest, Ruff lint and format checks, strict mypy,
  and `git diff --check`.
- There is no UI test suite; Telegram behavior is covered at the API boundary.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered work with a ➕ prefix.
- Record blockers or failed assumptions with a ⚠️ prefix.
- Keep this plan synchronized with implementation and verification results.
- Move the plan to `docs/plans/completed/` only after every required gate
  passes.

## Solution Overview

Narrow the callback mutation failure boundary so it ends once
`update_meal_outcome` returns successfully. Deliver the post-commit success
message in a separate best-effort block that logs Telegram failures and leaves
the acknowledgement as `Meal updated`.

For conversational meal logs with a `source_update_id`, atomically write the
existing date-indexed meal item and a stable marker keyed only by user and
source update. Condition the marker on nonexistence. If the marker condition
fails on a repeated update, treat the transaction as an idempotent success and
leave the first committed meal unchanged. This preserves efficient date-range
history queries and legacy meal keys without adding an index. Calls without a
source update keep the current timestamp-based single-item write.

Extend retry-delay extraction to inspect case-insensitive `Retry-After` values
from `exc.headers` and then `exc.response.headers`, with the existing attribute
as a compatibility fallback. Accept nonnegative numeric seconds, cap guidance
at five seconds, and use bounded exponential backoff for absent or malformed
values.

## Technical Details

- The callback success message is post-commit delivery. Its failure must not
  call the mutation-failure message, change the successful acknowledgement, or
  invoke `update_meal_outcome` again.
- Repository and validation failures before commit retain the current generic
  failure message and `Unable to update meal` acknowledgement.
- Use a stable marker sort key such as
  `MEAL_UPDATE#<source_update_id>`. Do not include LLM-derived date, meal type,
  description, or generated timestamps in that marker key.
- The source-update transaction contains the date-indexed meal `Put` and a
  conditional marker `Put`. Marker conflicts are expected duplicates only when
  cancellation reasons contain no unexpected failure code; other client errors
  must propagate.
- The first successfully processed update remains authoritative. A retry with
  different extracted fields is ignored rather than replacing or duplicating
  the original meal.
- Marker items are internal metadata and must not validate as `MealLogEntry` or
  appear in `get_meal_history` results.
- Retry headers may be mapping-like objects and values may be strings, integers,
  or floats. Header names must be read case-insensitively.
- Header precedence is direct exception headers, response headers, the legacy
  `retry_after` attribute, then exponential backoff.
- Preserve the current five-second maximum delay and configured retry-count and
  timeout budgets.

## What Goes Where

- **Implementation Steps**: callback failure isolation, transactional meal-log
  deduplication, Retry-After header parsing, regression tests, SAM rebuild, and
  project verification.
- **Post-Completion**: manual Telegram/AWS timing checks, deployment, pull
  request publication, and issue follow-up.

## Implementation Steps

### Task 1: Isolate committed callback delivery

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a failing regression test where `update_meal_outcome` succeeds and
  the subsequent success `send_message` raises `TelegramAPIError`
- [x] assert the committed callback sends no mutation-failure message, does not
  repeat the repository write, and acknowledges `Meal updated`
- [x] separate post-commit success delivery from callback mutation failure
  handling and log delivery failure without exposing internal details
- [x] retain success coverage for normal delivery and failure coverage for a
  repository exception before commit
- [x] run `uv run pytest tests/test_bot_handler.py`; all tests must pass before
  Task 2

### Task 2: Make meal-log retries independent of extracted fields

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] add a failing regression test that logs the same `source_update_id` with
  different dates, meal types, descriptions, and timestamps and observes only
  the first committed meal
- [x] add success coverage proving distinct source update IDs still create
  distinct date-indexed meal records returned by `get_meal_history`
- [x] atomically write the meal and a source-update-only conditional marker when
  `source_update_id` is present
- [x] treat marker-only conditional cancellation as an idempotent duplicate and
  propagate transaction conflicts, service failures, and ambiguous
  cancellations
- [x] retain and test the non-source timestamp key path, date boundaries,
  pagination, malformed-item filtering, and legacy meal-key compatibility
- [x] run `uv run pytest tests/test_dynamo.py`; all tests must pass before
  Task 3

### Task 3: Honor LiteLLM Retry-After headers

**Files:**

- Modify: `src/meal_planner/llm/client.py`
- Modify: `tests/test_llm_client.py`

- [x] replace attribute-only test doubles with realistic exceptions carrying
  numeric-string `Retry-After` values in direct headers
- [x] add failing coverage for `exc.response.headers`, case-insensitive header
  names, capped guidance, and malformed or negative values
- [x] add a small typed helper that safely reads mapping-like header containers
  without depending on a concrete HTTP client type
- [x] update `_retry_delay` to apply the documented precedence, numeric parsing,
  nonnegative validation, and five-second cap
- [x] retain tests proving absent or invalid guidance uses bounded exponential
  fallback and permanent errors are not retried
- [x] run `uv run pytest tests/test_llm_client.py`; all tests must pass before
  Task 4

### Task 4: Rebuild and verify deployment artifacts

**Files:**

- Verify: `template.yaml`
- Verify: `tests/test_template.py`
- Rebuild: `.aws-sam/build/`

- [x] verify the fixes require no dependency, environment variable, IAM
  permission, DynamoDB index, or SAM parameter change
- [x] update `template.yaml` and deployment tests only if implementation
  disproves that assumption
- [x] rebuild with `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`; all tests must
  pass before Task 5

### Task 5: Verify acceptance criteria and project standards

**Files:**

- Verify: `src/meal_planner/bot_handler.py`
- Verify: `src/meal_planner/db/dynamo.py`
- Verify: `src/meal_planner/llm/client.py`
- Verify: `tests/`

- [x] verify post-commit callback delivery failures cannot enter the mutation
  failure path or produce contradictory user messages
- [x] verify one Telegram update can commit at most one meal regardless of LLM
  reclassification while distinct updates remain independent
- [x] verify real LiteLLM header shapes control bounded retry delay and
  malformed guidance falls back safely
- [x] run `uv run pytest` and fix failures until the full suite passes
- [x] run `uv run ruff check .` and fix every finding
- [x] run `uv run ruff format --check .` and fix every formatting difference
- [x] run `uv run mypy` and fix every type error
- [x] run `git diff --check` and fix every whitespace error

### Task 6: Finalize plan tracking and documentation

**Files:**

- Modify: `README.md` only if verification reveals missing operator guidance
- Modify: `AGENTS.md` only if implementation discovers a reusable project rule
- Move:
  `docs/plans/2026-08-14-callback-idempotency-retry-reliability.md` to
  `docs/plans/completed/`

- [x] record implementation deviations and final verification results in this
  plan
- [x] update README or AGENTS only when the implementation creates reusable
  guidance not already documented
- [x] confirm every implementation and verification checkbox is complete
- [x] rerun `uv run pytest` after final documentation changes
- [x] move this plan to `docs/plans/completed/` only after all checks pass

## Verification Results

- No implementation deviation: no dependency, schema, index, environment,
  IAM, or SAM template change was required.
- `uv run pytest`: 174 passed.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 20 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and
  `git diff --check`: passed.

## Post-Completion

**Manual verification:**

- In a Telegram test chat, submit a meal check-in while forcing the success
  message request to fail; verify the meal changes once and the callback still
  acknowledges success.
- Replay one conversational meal-log update with different extracted fields and
  verify DynamoDB contains one meal plus one internal source-update marker.
- Exercise a provider 429 with `Retry-After` and confirm logs/timing reflect the
  bounded provider-guided delay.

**External system updates:**

- Implement on the existing dedicated branch; never push or merge directly to
  `master`.
- Open a pull request after local verification.
- Comment on the associated GitHub issue with the Conventional Commit or pull
  request link and concise verification results after implementation.
