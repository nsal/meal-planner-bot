# Meal Planner Bot

A Telegram-based family meal planning assistant powered by LLM.

## LLM configuration

The Bot Lambda uses `gpt-5.6-luna` with medium reasoning effort for
conversational Telegram replies. The Planner Lambda uses `gpt-5.6-terra` with
medium reasoning effort for weekly plans and grocery generation.

These settings are configured through environment variables:

- `CONVERSATIONAL_LLM_MODEL`
- `CONVERSATIONAL_LLM_REASONING_EFFORT`
- `PLANNER_LLM_MODEL`
- `PLANNER_LLM_REASONING_EFFORT`

Start planner generation at medium effort. Compare medium with high on
representative plans before increasing the production default; high may improve
quality but increases latency and token usage.
