# Plan Chat prompt

Plan Chat uses one plain-text provider request for each user turn. The system
instruction identifies the assistant as a family meal-planning helper; the
user message is the prompt assembled by
[`build_plan_chat_prompt()`](../src/meal_planner/llm/prompts.py).

The response is returned as Telegram text. It is an editable draft, not an
official meal plan. The application does not parse the response, calculate
nutrition, or guarantee that dietary constraints, preferences, calorie
targets, or other meal semantics were followed.

## Five prompt sections

The prompt has five bounded context sections. Each section is delimited so
raw user text remains content rather than becoming a new instruction.

1. **Household members and targets** — household name, size, every member,
   required calorie target, and optional protein and fibre targets. Missing
   optional targets are omitted rather than invented.
2. **Raw dietary constraints** — the saved constraint wording is semantically
   uninterpreted but delimiter-normalized for section safety. Ordinary
   wording is preserved, while `---` and `===` sequences are replaced with
   safe lookalike characters. Empty constraints render as `No dietary
   constraints provided.`
3. **Raw dietary preferences** — the saved preference wording is semantically
   uninterpreted but delimiter-normalized for section safety. Ordinary
   wording is preserved, while `---` and `===` sequences are replaced with
   safe lookalike characters. Empty preferences render as `No dietary
   preferences provided.` The prompt does not convert either dietary field
   into a rule contract.
4. **Submitted meals** — submitted meal descriptions grouped by ISO date and
   meal type.
5. **Planning conversation** — the original request, the latest generated
   response when one exists, and the current instruction.

The prompt also contains short draft instructions around these sections. They
tell the model to use plain headings and bullets, keep meal details short,
include approximate calorie estimates where relevant, avoid Markdown tables,
and treat the result as a draft.

## Meal-history window

For a request with UTC context date `D`, the history section includes submitted
meals from `D - 20 days` through `D`, inclusive: exactly 21 calendar dates.
Entries outside that range are omitted. The worker retrieves this context
freshly for every turn with `days=21`; drafts and conversation responses are
not meal-history evidence.

History is preference evidence, not an obligation. It helps the model avoid
unhelpful repetition, but it does not impose quotas, required foods, or a
schedule. Empty history is rendered as:

```text
No submitted meals in the supplied 21-day window.
```

## Follow-ups

The first turn has no previous response and includes:

```text
Original request:
  ...
Previous draft response:
  No previous draft response; this is the first request.
Current instruction:
  ...
```

After a successful turn, the state keeps only the latest response. A follow-up
therefore carries the original request, latest response, and new instruction;
it does not send an unlimited transcript. The worker reloads the current
profile and the current 21-day history window for every follow-up, so context
can change as the user records meals or edits the profile.

The initial request, current instruction, and latest response have bounded
sizes. Delimiter-like text is normalized before rendering. The worker event
contains only the user, chat, session, request, and observed revision IDs; it
does not contain prompt contents or generated text.

## Clarification behavior

If essential information is missing, the model may ask at most one focused
clarification question instead of writing a menu. The user can answer in the
same session. The raw original request remains attached, while the answer is
the current instruction for the next request.

The application does not interpret the answer, merge it into a structured
preference, or assert that the resulting draft satisfies it. This keeps
clarification conversational and leaves the response subject to the same
draft-only disclaimer.

## Transport and safety boundaries

The request is a single text call through `LLMClient`; it does not request a
JSON object or a structured meal-plan schema. Empty, oversized, provider, and
transport failures are technical failures, not semantic review results. A
failure restores a retryable session state. A stale session, replaced request,
changed revision, cancelled session, or expired state cannot publish a late
response.

Telegram receives plain text with headings and bullets. The final message
chunk carries the session-scoped `End planning` button. The button ends only
the matching Plan Chat session.
