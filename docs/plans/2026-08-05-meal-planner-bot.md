# Meal Planner Bot

## Overview
- A Telegram-based family meal planning assistant powered by LLM
- Solves the daily "what to cook" problem by generating weekly meal plans tailored to user preferences, allergies, restrictions, and per-person calorie targets
- Generates consolidated grocery lists from confirmed plans
- Conversational interface for logging meals, editing plans, and asking questions
- Uses vendor-agnostic LLM (via LiteLLM) as the reasoning engine — the app manages state, the LLM does the creative/analytical work
- AWS serverless deployment: API Gateway + Lambda + DynamoDB

## Context (from discovery)
- **New project** — no existing codebase
- **Stack:** Python 3.12, aiogram (Telegram), LiteLLM (LLM abstraction), boto3 (DynamoDB), Pydantic (schemas)
- **Infrastructure:** AWS API Gateway (HTTP API) → Bot Lambda (fast path) → Planner Lambda (async) → DynamoDB (single-table)
- **Architecture:** LLM-as-brain with structured state — app assembles context from DB, sends to LLM with prompt templates, parses structured JSON output, persists results
- **Single DynamoDB table** with 3 entity types: PROFILE, MEAL, PLAN

## Development Approach
- **Testing approach**: Regular (code first, then tests)
- Complete each task fully before moving to the next
- Make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional — they are a required part of the checklist
  - write unit tests for new functions/methods
  - write unit tests for modified functions/methods
  - add new test cases for new code paths
  - update existing test cases if behavior changes
  - tests cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- Run tests after each change
- Maintain backward compatibility

## Testing Strategy
- **Unit tests**: required for every task (see Development Approach above)
- **Integration tests**: test Lambda handler with mocked DynamoDB and LLM responses
- **No e2e tests in v1** — manual testing via Telegram is sufficient for a personal tool
- Use `pytest` as test runner, `moto` for DynamoDB mocking, `unittest.mock` for LLM calls

## Progress Tracking
- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope
- Keep plan in sync with actual work done

## Solution Overview

### Architecture
```
┌──────────┐   webhook   ┌──────────────┐  async invoke  ┌──────────────────┐
│ Telegram │────────────▶│ Bot Lambda   │──────────────▶│ Planner Lambda   │
│          │◀────────────│ (fast path)  │               │ (generates plan, │
└──────────┘             └──────┬───────┘               │  grocery list,   │
                                │                       │  sends to user)  │
                                ▼                       └────────┬─────────┘
                         ┌─────────────┐                        │
                         │  DynamoDB   │◀───────────────────────┘
                         └─────────────┘
```

### Key Design Decisions
- **LLM-as-brain**: No custom recommendation engine. LLM handles intent classification, meal suggestions, plan generation, grocery aggregation. App is a context manager.
- **Single LLM call for conversation**: System prompt includes user context; LLM returns natural language + structured JSON metadata in one call (intent, entities).
- **Vendor-agnostic LLM**: LiteLLM abstracts provider — swap OpenAI/Anthropic/Gemini/local with config change.
- **Two Lambdas**: Bot Lambda stays fast (webhook response <5s). Planner Lambda runs async for heavy plan generation (up to 120s).
- **Family support (v1)**: Shared preferences/restrictions, per-person calorie targets. Grocery quantities multiplied by family size.
- **Conversational plan edits**: No separate edit UI — user says "swap Thursday dinner" and LLM handles it.

### DynamoDB Single-Table Design
```
PK                │ SK                       │ Attributes
──────────────────┼──────────────────────────┼──────────────────────────
USER#<uid>        │ PROFILE                  │ name, family_members[{name, calorie_target}],
                  │                          │ allergies[], dietary_preferences{},
                  │                          │ restrictions[], goals, people_count
USER#<uid>        │ MEAL#<date>#<meal_type>  │ description, created_at
USER#<uid>        │ PLAN#<week_start>        │ status (draft/confirmed), days[{day,
                  │                          │ meals[{meal_type, name, ingredients[],
                  │                          │ est_calories, was_cooked}]}],
                  │                          │ grocery_list{sections[{name, items[]}]}
```

### Telegram Commands
| Command | Action |
|---|---|
| `/start` | Onboarding — collect profile via conversation |
| `/profile` | View/update profile + family members |
| `/plan` | Generate weekly meal plan (async) |
| `/grocery` | Show grocery list from current plan |
| `/today` | Show today's planned meals |
| `/submit_meals` | Daily check-in — mark meals as cooked/skipped/swapped |

### Prompt Templates
1. **Conversational** — every message: system prompt with user context → LLM returns response + JSON metadata (intent, entities)
2. **Plan generation** — `/plan`: user profile + meal history + previous plan feedback → LLM returns structured 7-day plan as JSON
3. **Grocery list** — after plan confirmed: plan JSON → LLM returns consolidated, section-grouped grocery list

## Technical Details

### Project Structure
```
meal-planner-bot/
├── src/
│   └── meal_planner/
│       ├── __init__.py
│       ├── config.py               # Env vars, settings (Pydantic BaseSettings)
│       ├── bot_handler.py          # Bot Lambda entry point
│       ├── planner_handler.py      # Planner Lambda entry point
│       ├── router.py               # Message → intent routing
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py           # LiteLLM wrapper, retry logic
│       │   ├── prompts.py          # Prompt templates + context assembly
│       │   └── parser.py           # Extract JSON from LLM responses
│       ├── db/
│       │   ├── __init__.py
│       │   └── dynamo.py           # DynamoDB CRUD operations
│       ├── telegram/
│       │   ├── __init__.py
│       │   └── api.py              # Send messages, format replies
│       └── models/
│           ├── __init__.py
│           └── schemas.py          # Pydantic models for all entities
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # Shared fixtures (DynamoDB mock, LLM mock)
│   ├── test_router.py
│   ├── test_bot_handler.py
│   ├── test_planner_handler.py
│   ├── test_dynamo.py
│   ├── test_llm_client.py
│   ├── test_prompts.py
│   ├── test_parser.py
│   └── test_schemas.py
├── template.yaml                   # SAM template (API GW + Lambdas + DynamoDB)
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
└── docs/
    └── plans/
        └── 2026-08-05-meal-planner-bot.md
```

### Processing Flows

**Conversational message:**
1. API GW receives Telegram webhook POST
2. Bot Lambda parses update, loads user profile + current plan + recent meals from DynamoDB
3. Builds conversational prompt with context
4. Calls LLM → gets response + JSON metadata
5. Parses intent from metadata (log_meal, edit_plan, update_profile, suggestion, chitchat)
6. Persists state changes to DynamoDB based on intent
7. Sends natural language response to Telegram
8. Returns 200 to API GW

**Plan generation (`/plan`):**
1. Bot Lambda receives `/plan` command
2. Sends "Working on your plan..." to Telegram
3. Invokes Planner Lambda asynchronously (Lambda invoke with InvocationType='Event')
4. Returns 200 to API GW
5. Planner Lambda loads full user context from DynamoDB
6. Builds plan generation prompt
7. Calls LLM → gets structured 7-day plan JSON
8. Stores plan in DynamoDB with status=draft
9. Sends plan to user via Telegram API, asks for confirmation/edits

**Submit meals (`/submit_meals`):**
1. Bot Lambda receives `/submit_meals` command
2. Loads today's planned meals from DynamoDB
3. Sends meal list with inline keyboard buttons (✅ Cooked / ❌ Skipped / 🔄 Swapped)
4. User taps buttons → callback queries handled by Bot Lambda
5. Updates `was_cooked` flags on plan in DynamoDB
6. If swapped → asks "What did you have instead?" → logs to MEAL entity

## What Goes Where
- **Implementation Steps** (`[ ]` checkboxes): all code, tests, SAM template — everything in this repo
- **Post-Completion** (no checkboxes): Telegram bot registration (BotFather), AWS deployment (`sam deploy`), LLM API key setup, first-run testing via Telegram

---

## Implementation Steps

### Task 1: Project scaffolding and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `src/meal_planner/__init__.py`
- Create: `src/meal_planner/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [x] Initialize `pyproject.toml` with project metadata, Python 3.14, pytest config
- [x] Create `requirements.txt` with runtime deps: aiogram, litellm, boto3, pydantic, pydantic-settings
- [x] Create `requirements-dev.txt` with dev deps: pytest, pytest-asyncio, moto[dynamodb], pytest-mock
- [x] Create `src/meal_planner/__init__.py`
- [x] Implement `src/meal_planner/config.py` with Pydantic `BaseSettings`: TELEGRAM_BOT_TOKEN, LLM_MODEL, LLM_API_KEY, DYNAMODB_TABLE_NAME, AWS_REGION
- [x] Create `tests/conftest.py` with shared fixtures (env vars, basic config)
- [x] Write tests for config loading (valid env, missing required vars)
- [x] Run tests — must pass before task 2


### Task 2: Pydantic data models

**Files:**
- Create: `src/meal_planner/models/__init__.py`
- Create: `src/meal_planner/models/schemas.py`
- Create: `tests/test_schemas.py`

- [x] Define `FamilyMember` model (name, calorie_target)
- [x] Define `UserProfile` model (name, family_members list, allergies, dietary_preferences, restrictions, goals, people_count)
- [x] Define `Ingredient` model (item, amount)
- [x] Define `PlannedMeal` model (meal_type, name, ingredients list, est_calories, was_cooked)
- [x] Define `PlanDay` model (day number, meals list)
- [x] Define `GrocerySection` model (name, items list)
- [x] Define `WeeklyPlan` model (week_start_date, status, days list, grocery_list)
- [x] Define `MealLogEntry` model (date, meal_type, description, created_at)
- [x] Define `ConversationIntent` enum and `LLMResponseMetadata` model (intent, entities dict)
- [x] Write tests for model validation (valid data, missing fields, type coercion)
- [x] Write tests for edge cases (empty lists, optional fields, invalid enum values)
- [x] Run tests — must pass before task 3


### Task 3: DynamoDB data access layer

**Files:**
- Create: `src/meal_planner/db/__init__.py`
- Create: `src/meal_planner/db/dynamo.py`
- Create: `tests/test_dynamo.py`

- [x] Implement `DynamoRepository` class initialized with boto3 resource and table name
- [x] Implement `get_profile(user_id)` — returns `UserProfile` or None
- [x] Implement `save_profile(user_id, profile)` — upserts PROFILE entity
- [x] Implement `log_meal(user_id, entry)` — writes MEAL#date#meal_type entity
- [x] Implement `get_meal_history(user_id, days=14)` — queries MEAL# entities with SK begins_with, returns list sorted by date
- [x] Implement `save_plan(user_id, plan)` — writes PLAN#week_start entity (full plan document including grocery list)
- [x] Implement `get_current_plan(user_id)` — gets most recent PLAN entity
- [x] Implement `update_meal_status(user_id, week_start, day, meal_type, was_cooked)` — updates was_cooked flag on a specific meal within the plan document
- [x] Write tests using moto mock DynamoDB for all CRUD operations (success cases)
- [x] Write tests for error/edge cases (profile not found, empty history, no current plan)
- [x] Run tests — must pass before task 4


### Task 4: LLM client wrapper

**Files:**
- Create: `src/meal_planner/llm/__init__.py`
- Create: `src/meal_planner/llm/client.py`
- Create: `tests/test_llm_client.py`

- [x] Implement `LLMClient` class wrapping LiteLLM `acompletion` call
- [x] Accept model name and API key from config
- [x] Implement `chat(system_prompt, user_message)` — returns raw LLM response text
- [x] Implement `chat_json(system_prompt, user_message)` — calls LLM with JSON mode enabled, returns parsed dict
- [x] Add retry logic with exponential backoff for transient errors (rate limits, timeouts)
- [x] Add error handling for API errors, returning a user-friendly fallback message
- [x] Write tests with mocked LiteLLM calls (success, retry on transient error, failure)
- [x] Write tests for JSON mode (valid JSON returned, malformed JSON handling)
- [x] Run tests — must pass before task 5


### Task 5: Prompt templates and context assembly

**Files:**
- Create: `src/meal_planner/llm/prompts.py`
- Create: `tests/test_prompts.py`

- [x] Implement `build_conversational_prompt(profile, current_plan, recent_meals)` — assembles system prompt with user context, instructions for intent classification + JSON metadata output
- [x] Implement `build_plan_prompt(profile, meal_history, previous_plan)` — assembles plan generation prompt with calorie targets, preferences, constraints, output JSON schema
- [x] Implement `build_grocery_prompt(plan)` — assembles grocery list prompt from confirmed plan, with people_count for quantity multiplication
- [x] Ensure all prompt builders handle missing/empty context gracefully (no plan yet, no meal history, new user)
- [x] Write tests verifying prompt content includes correct context (profile data present, meal history included, calorie targets per family member)
- [x] Write tests for edge cases (new user with no history, empty plan)
- [x] Run tests — must pass before task 6


### Task 6: LLM response parser

**Files:**
- Create: `src/meal_planner/llm/parser.py`
- Create: `tests/test_parser.py`

- [x] Implement `parse_conversational_response(raw_text)` — extracts natural language reply and JSON metadata block from LLM response
- [x] Implement `parse_plan_response(raw_text_or_dict)` — validates and converts LLM plan output into `WeeklyPlan` model
- [x] Implement `parse_grocery_response(raw_text_or_dict)` — validates and converts LLM grocery output into list of `GrocerySection` models
- [x] Handle malformed LLM output gracefully — return the raw text as reply with a logged warning if JSON parsing fails
- [x] Write tests with realistic LLM response samples (well-formed JSON, missing fields, no JSON block, malformed JSON)
- [x] Write tests for edge cases (empty response, partial JSON)
- [x] Run tests — must pass before task 7


### Task 7: Telegram API helper

**Files:**
- Create: `src/meal_planner/telegram/__init__.py`
- Create: `src/meal_planner/telegram/api.py`
- Create: `tests/test_telegram_api.py`

- [x] Implement `TelegramAPI` class initialized with bot token
- [x] Implement `send_message(chat_id, text, parse_mode="Markdown")` — sends text message via Telegram Bot API HTTP call
- [x] Implement `send_plan(chat_id, plan: WeeklyPlan)` — formats plan into readable Telegram message (day-by-day, meal names + calories)
- [x] Implement `send_grocery_list(chat_id, sections)` — formats grocery list grouped by section
- [x] Implement `send_meal_checkin(chat_id, meals_today)` — sends today's meals with inline keyboard buttons (✅/❌/🔄)
- [x] Handle Telegram message length limit (4096 chars) — split long messages
- [x] Write tests with mocked HTTP calls (success, API error, message splitting)
- [x] Write tests for formatting functions (plan rendering, grocery list rendering)
- [x] Run tests — must pass before task 8

### Task 8: Message router

**Files:**
- Create: `src/meal_planner/router.py`
- Create: `tests/test_router.py`

- [x] Implement `route_update(update_dict)` — parses Telegram update, returns routing decision
- [x] Handle command messages: `/start`, `/profile`, `/plan`, `/grocery`, `/today`, `/submit_meals` → return command name + args
- [x] Handle callback queries (inline keyboard button presses from `/submit_meals`) → return callback data
- [x] Handle free text messages → return "conversational" route
- [x] Extract `chat_id`, `user_id`, `message_text` from update
- [x] Write tests for all command types, callback queries, and free text routing
- [x] Write tests for malformed updates (missing fields, unsupported update types)
- [x] Run tests — must pass before task 9

### Task 9: Bot Lambda — command handlers

**Files:**
- Create: `src/meal_planner/bot_handler.py`
- Create: `tests/test_bot_handler.py`

- [x] Implement Lambda `handler(event, context)` — parses API GW event, delegates to router
- [x] Implement `/start` handler — begins onboarding conversation, asks for profile details via LLM conversational prompt
- [x] Implement `/profile` handler — loads profile from DynamoDB, sends formatted profile to user
- [x] Implement `/plan` handler — sends "Working on your plan..." message, invokes Planner Lambda asynchronously via boto3 `lambda_client.invoke(InvocationType='Event')`
- [x] Implement `/grocery` handler — loads current plan from DynamoDB, sends grocery list (or "No plan yet" if none)
- [x] Implement `/today` handler — loads current plan, extracts today's meals, sends formatted response
- [x] Implement `/submit_meals` handler — loads today's planned meals, sends inline keyboard via `send_meal_checkin`
- [x] Implement callback query handler — processes ✅/❌/🔄 button presses, updates meal status in DynamoDB
- [x] Write tests for each command handler with mocked DynamoDB and Telegram API (success cases)
- [x] Write tests for error cases (no profile, no plan, DynamoDB errors)
- [x] Run tests — must pass before task 10

### Task 10: Bot Lambda — conversational handler

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Implement conversational message handler — loads user context from DynamoDB, builds conversational prompt, calls LLM
- [x] Parse LLM response metadata for intent: `log_meal` → save to meal log, `edit_plan` → update plan in DynamoDB, `update_profile` → update profile, `suggestion` / `chitchat` → reply only
- [x] Handle `log_meal` intent — extract date, meal_type, description from LLM metadata, call `log_meal()` on DynamoDB
- [x] Handle `edit_plan` intent — extract day, meal_type, new meal details from LLM metadata, update plan in DynamoDB
- [x] Handle `update_profile` intent — extract changed fields from LLM metadata, update profile in DynamoDB
- [x] Send natural language portion of LLM response back to user via Telegram
- [x] Handle LLM errors gracefully — send "Sorry, I had trouble understanding that" fallback
- [x] Write tests for each intent flow with mocked LLM responses and DynamoDB
- [x] Write tests for error handling (LLM failure, parse failure, DB failure)
- [x] Run tests — must pass before task 11

### Task 11: Planner Lambda — plan generation

**Files:**
- Create: `src/meal_planner/planner_handler.py`
- Create: `tests/test_planner_handler.py`

- [x] Implement Lambda `handler(event, context)` — receives user_id and chat_id from invocation payload
- [x] Load full user context from DynamoDB: profile, meal history (last 14 days), previous plan with was_cooked flags
- [x] Build plan generation prompt with all context
- [x] Call LLM with JSON mode for structured plan output
- [x] Parse and validate LLM response into `WeeklyPlan` model
- [x] Build grocery list prompt from generated plan
- [x] Call LLM for grocery list generation
- [x] Parse grocery response, attach to plan document
- [x] Save complete plan (meals + grocery list) to DynamoDB with status=draft
- [x] Send formatted plan to user via Telegram API, ask "Anything you'd like to change?"
- [x] Handle LLM/parsing errors — send error message to user via Telegram
- [x] Write tests for full plan generation flow with mocked LLM and DynamoDB
- [x] Write tests for error cases (LLM failure, invalid plan structure, missing profile)
- [x] Run tests — must pass before task 12

### Task 12: SAM template — infrastructure as code

**Files:**
- Create: `template.yaml`

- [x] Define SAM template with `AWS::Serverless::Function` for Bot Lambda (Python 3.12, 256MB, 30s timeout, API GW HTTP API event)
- [x] Define `AWS::Serverless::Function` for Planner Lambda (Python 3.12, 512MB, 120s timeout, no API event)
- [x] Define `AWS::DynamoDB::Table` with PK (string) + SK (string), on-demand billing
- [x] Grant Bot Lambda read/write on DynamoDB table + invoke permission on Planner Lambda
- [x] Grant Planner Lambda read/write on DynamoDB table
- [x] Define environment variables for both Lambdas: TABLE_NAME, BOT_TOKEN (from SSM), LLM_API_KEY (from SSM), LLM_MODEL, PLANNER_FUNCTION_NAME
- [x] Define SSM parameters or reference existing ones for secrets (bot token, LLM API key)
- [x] Add API GW output (webhook URL) to stack outputs
- [x] Validate template with `sam validate`
- [x] Run tests — must pass before task 13

### Task 13: Verify acceptance criteria

- [ ] Verify all commands work: `/start`, `/profile`, `/plan`, `/grocery`, `/today`, `/submit_meals`
- [ ] Verify conversational flows: meal logging, plan editing, profile updates, general questions
- [ ] Verify plan generation produces valid structured output with correct calorie targets
- [ ] Verify grocery list consolidates ingredients and multiplies by family size
- [ ] Verify `/submit_meals` inline keyboard and callback handling
- [ ] Verify error handling: missing profile, no plan, LLM failures
- [ ] Run full test suite: `pytest tests/ -v`
- [ ] Verify test coverage meets reasonable standard

### Task 14: Update documentation

- [ ] Create README.md with: project description, architecture diagram, setup instructions (BotFather, AWS credentials, env vars), deployment steps (`sam build && sam deploy`), usage guide for all commands
- [ ] Document environment variables and secrets required
- [ ] Document prompt templates and how to customize rules
- [ ] Move this plan to `docs/plans/completed/`

---

## Post-Completion
- Register Telegram bot with BotFather, obtain bot token
- Store bot token and LLM API key in AWS SSM Parameter Store
- Deploy with `sam build && sam deploy --guided`
- Set Telegram webhook URL to API GW endpoint: `https://api.telegram.org/bot<token>/setWebhook?url=<api-gw-url>`
- Send `/start` to the bot and complete onboarding
- Test full flow: onboarding → `/plan` → review plan → `/grocery` → `/submit_meals`
- Iterate on prompt rules based on actual plan quality
