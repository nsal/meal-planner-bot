# Code Review — meal-planner-bot
**Date:** 2026-08-05 | **Reviewer:** Antigravity

## Executive Summary

The codebase is clean, well-structured, and fully passes all automated checks
(`83/83 tests passed`, `ruff` clean, `mypy --strict` clean). The plan is
followed faithfully through Task 12. Below are findings ranked by severity.

---

## 🔴 Critical Issues

### 1. `asyncio.run()` inside a Lambda handler is risky
**Files:** [`bot_handler.py:270`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/bot_handler.py#L270), [`planner_handler.py:56`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/planner_handler.py#L56)

`asyncio.run()` creates a new event loop every invocation and raises
`RuntimeError` if called inside an already-running loop. In Lambda, the
runtime can reuse execution contexts where an event loop may already exist
(especially when using LiteLLM which itself uses an async loop internally).

```python
# bot_handler.py:270 — problematic
raw_response = asyncio.run(client.chat(system_prompt, route.text))
```

**Fix:** Make the entry points fully async, or use a dedicated runner pattern:
```python
import asyncio

def _run_async(coro):  # type: ignore[type-arg]
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
```
Or refactor `handle_conversational` / `generate_plan` to be `async` and
propagate that up to an async Lambda runner.

---

### 2. `_cmd_today` always shows Day 1, not the actual current day
**File:** [`bot_handler.py:176`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/bot_handler.py#L176)

```python
today_plan = plan.days[0]  # ← always index 0, i.e. always Day 1
```

`plan.days` is stored in insertion order, not keyed by current weekday. If the
user runs `/today` on Wednesday, they still see Monday's meals.

**Fix:** Calculate the offset from `plan.week_start` to today's date and
select the matching `PlanDay`:
```python
from datetime import date
week_start = date.fromisoformat(plan.week_start)
day_offset = (date.today() - week_start).days + 1  # 1-based
today_plan = next(
    (d for d in plan.days if d.day == day_offset),
    plan.days[0],  # fallback
)
```
Same bug exists in `_cmd_submit_meals` at line 194.

---

### 3. Callback handler parses `day` with `int()` — no bounds/error guard
**File:** [`bot_handler.py:206`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/bot_handler.py#L206)

```python
data_parts = route.callback_data.split(":")
if len(data_parts) == 4 and data_parts[0] == "checkin":
    day = int(data_parts[1])  # raises ValueError if not a digit
```

A malformed or tampered `callback_data` string will raise an unhandled
`ValueError` that propagates out of `handle_callback`. The outer
`handle_update` has no try/except.

**Fix:** wrap in try/except or validate with `str.isdigit()` before casting.

---

## 🟡 Medium Issues

### 4. Secrets passed as plain CloudFormation parameters (not SSM references)
**File:** [`template.yaml:13–20`](file:///Users/nikolaysalionov/code/meal-planner-bot/template.yaml#L13)

The plan states *(Task 12)*: *"Define SSM parameters or reference existing ones
for secrets"*, but the template accepts `TelegramBotToken` and `LlmApiKey` as
plain `String` parameters (with `NoEcho: true` only). This means the secrets
are stored in the CloudFormation stack and visible in `sam deploy` output.

**Fix:** Use `AWS::SSM::Parameter::Value<String>` type or `resolve:ssm:`
syntax so secrets are fetched from SSM at deploy time and never stored in the
stack:
```yaml
TelegramBotToken:
  Type: AWS::SSM::Parameter::Value<String>
  Default: /meal-planner/bot-token
```

---

### 5. `get_meal_history` fetches all items, then slices — scalability concern
**File:** [`db/dynamo.py:48–59`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/db/dynamo.py#L48)

```python
response = self.table.query(
    KeyConditionExpression=...,
    ScanIndexForward=False,
)
items = response.get("Items", [])
...
return entries[:days]  # slices after fetching all
```

DynamoDB `query` without a `Limit` fetches ALL meal items for the user in one
page, even if the user has years of history. At minimum add `Limit=days` to
the query or add a date-range condition using a `KeyConditionExpression` with a
`between` or `begins_with` on the SK date prefix.

---

### 6. `WeeklyPlan` alias vs attribute naming inconsistency
**File:** [`models/schemas.py:79`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/models/schemas.py#L79)

```python
week_start: str = Field(alias="week_start_date")
```

The field is accessed via `.week_start` in code (e.g. `plan.week_start`), but
stored/serialised as `week_start_date`. The `@property week_start_date`
duplicates this. The dual naming (`week_start` vs `week_start_date`) creates
confusion: `save_plan` uses `plan.week_start_date` (the property) while most
code uses `plan.week_start`.

**Fix:** Rename the attribute to `week_start_date` without an alias (or keep
the alias but update all callers to one consistent name).

---

### 7. No input validation on LLM `entities` dict before writing to DB
**File:** [`bot_handler.py:299–306`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/bot_handler.py#L299)

The `date` and `meal_type` fields from LLM metadata are written directly into
`MealLogEntry` using `str(entities.get(...))` with no format validation.
A hallucinated date like `"tomorrow"` or `"August 5th"` would be stored as-is.

**Fix:** Validate/normalize `date` with `datetime.strptime` before constructing
`MealLogEntry`, falling back to `today_str` on parse failure.

---

### 8. Missing test for `test_lambda_handler_invalid_json` settings call
**File:** [`tests/test_bot_handler.py:404–407`](file:///Users/nikolaysalionov/code/meal-planner-bot/tests/test_bot_handler.py#L404)

```python
def test_lambda_handler_invalid_json(mocker: Any) -> None:
    event = {"body": "invalid json{"}
    res = lambda_handler(event, None)
```

This test calls `lambda_handler` which calls `get_settings()` which **reads
real env vars**. Without `mock_env`, this test passes only because the env
doesn't have the required vars and `get_settings()` raises a `ValidationError`
— but the test still passes because the JSON parse fails first. Add `mock_env`
fixture or mock `get_settings` to be explicit.

---

## 🟢 Minor / Style Issues

### 9. Redundant UPPERCASE properties in `config.py`
**File:** [`config.py:34–52`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/config.py#L34)

```python
@property
def TELEGRAM_BOT_TOKEN(self) -> str:
    return self.telegram_bot_token
```

These five `@property` methods are redundant — they just return the lowercase
attributes. Callers can already access `settings.telegram_bot_token` directly.
The properties only add confusion about whether to use the uppercase or
lowercase name. Remove them or pick one convention.

---

### 10. `LLMClient.initial_backoff` defaults to `0.01s` — acceptable only in tests
**File:** [`llm/client.py:26`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/llm/client.py#L26)

```python
initial_backoff: float = 0.01,
```

This is a test-friendly default but not a real production default. For
production, 1–2 seconds initial backoff is typical for LLM rate-limits. Keep
`0.01` for tests, but set the production default higher (e.g. `1.0`).

---

### 11. `build_plan_prompt` has a hardcoded default `week_start`
**File:** [`llm/prompts.py:72`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/llm/prompts.py#L72)

```python
def build_plan_prompt(
    ...
    week_start: str = "2026-08-10",  # hardcoded date
) -> str:
```

This will silently produce plans dated `2026-08-10` if the caller omits the
argument. `planner_handler.py` correctly passes `week_start`, but a future
caller might forget. Use `""` or compute a sensible default dynamically.

---

### 12. `_cmd_today` output uses `.capitalize()` on `meal_type` but raw string stored
**File:** [`bot_handler.py:181`](file:///Users/nikolaysalionov/code/meal-planner-bot/src/meal_planner/bot_handler.py#L181)

Minor UX: `meal_type` is stored as lowercase free-text (e.g. `"breakfast"`).
The display is `meal_type.capitalize()`. If the LLM returns `"Breakfast"` or
`"BREAKFAST"`, the display becomes `"Breakfast"` correctly, but the DB key
stores the original casing — potential mismatch in `update_meal_status` which
does `.lower()` comparison. Already handled in `update_meal_status` but not in
`handle_callback`'s `meal_type = data_parts[2]` (no `.lower()` call before
passing to `update_meal_status`).

---

## 📋 What's Missing vs. Plan

| Plan Item | Status |
|---|---|
| Task 13: Acceptance criteria verification | ❌ Not started (expected) |
| Task 14: README + docs | ❌ Not started (expected) |
| `test_router.py`: no test for unsupported update types (photo, sticker) | ⚠️ Partial |
| `test_dynamo.py`: no pagination test for `get_meal_history` | ⚠️ Missing |

---

## ✅ What's Done Well

- **Architecture clarity**: clean two-lambda design, clear separation of
  concerns across modules
- **Type safety**: `mypy --strict` passes on all 15 source files with no
  suppressions except the necessary `boto3` stubs
- **Test coverage**: 83 tests with meaningful assertions, good intent-flow
  coverage in `test_bot_handler.py`
- **Error resilience**: LLM fallback messages, graceful JSON parse failures,
  DynamoDB not-found handling
- **Single-table DynamoDB design**: correctly uses PK/SK composite key pattern
- **Prompt engineering**: all three prompt builders correctly handle
  empty/missing context
- **Ruff**: zero lint issues, 80-column line length enforced

---

## Priority Fix Order

1. 🔴 Fix `asyncio.run()` async pattern (correctness risk in Lambda)
2. 🔴 Fix `/today` and `/submit_meals` to use the actual current day
3. 🔴 Add try/except around `int(data_parts[1])` in callback handler
4. 🟡 Move secrets to SSM references in `template.yaml`
5. 🟡 Add `Limit=` to `get_meal_history` DynamoDB query
6. 🟡 Validate LLM-supplied `date` field before DB write
