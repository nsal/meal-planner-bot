# Enforce Meal-Plan Preferences with Evidence-Based Validation

Tracking issue: [#46](https://github.com/nsal/meal-planner-bot/issues/46)

## Overview

Make request-specific meal-plan preferences reliably enforceable instead of
prompt-only suggestions. Interpret supported natural-language frequency rules,
validate generated meal names and ingredients against those rules, and never
persist or display a plan that violates an exact measurable requirement.

This change also replaces the generic terminal treatment of a first invalid
planner result with one bounded asynchronous repair attempt and actionable
failure feedback. It retains the existing `/plan` preference workflow, raw
preference persistence, `WeeklyPlan` storage shape, long-running Planner model,
and manual `/plan` retry behavior.

## Context (from discovery)

- `src/meal_planner/bot_handler.py` currently stores one raw preference and
  dispatches it directly to the asynchronous Planner Lambda.
- `src/meal_planner/llm/prompts.py` labels that preference high priority, but
  neither the prompt nor application code provides a compliance contract.
- `src/meal_planner/planner_handler.py` validates JSON, Pydantic structure, and
  the requested week. It saves a schema-valid plan without checking whether
  the meals satisfy the preference.
- `src/meal_planner/models/schemas.py` requires seven uniquely numbered days
  and unique meal types per day, but generated plans may still contain empty
  days, missing ingredients, or zero-calorie meals.
- `src/meal_planner/telegram/api.py` displays meal names and calories but not
  ingredients. Egg-based dishes such as shakshuka and frittata therefore do
  not visibly demonstrate compliance with an egg-frequency request.
- Planner generation intentionally uses one provider call with a 240-second
  timeout inside a 300-second application deadline. A second repair call must
  run in a separate asynchronous invocation.
- The raw invalid response and Pydantic feedback are not retained. Existing
  privacy-safe logs cannot identify the exact structural cause of the reported
  first failure after the fact.

## Development Approach

- **Testing approach:** TDD. Write focused failing tests before each behavior
  change, implement the smallest production change, add edge-case regressions,
  and run the focused tests before proceeding.
- Complete each numbered task fully and mark its checkboxes immediately before
  beginning the next task.
- Keep the design lean: add one new domain model,
  `PreferenceRequirement`; continue using the existing `WeeklyPlan` response
  and derive evidence in application code.
- Do not add a food ontology or alias database. Culinary interpretation belongs
  to the LLM; generated meal names and ingredient items provide verifiable
  evidence.
- Preserve backward compatibility for already persisted plans. Enforce stricter
  completeness at the generated-plan boundary instead of making old DynamoDB
  records impossible to deserialize.
- Keep one long provider request per Planner Lambda invocation. Do not shorten
  its timeout or reintroduce multiple provider calls within one invocation.
- Update this plan immediately if implementation discoveries change its scope
  or architecture.
- Follow `pyproject.toml`: Python 3.14, strict type hints, Ruff at 80 columns,
  Mypy strict mode, Pytest, and `uv` for every Python tool invocation.

## Testing Strategy

- Unit-test the requirement contract, interpretation parser, evidence matcher,
  exact-count validator, and generated-plan completeness checks.
- Test supported alternatives, meal-type scopes, distinct-meal counting,
  singular/plural normalization, repeated evidence, and substring rejection.
- Exercise clarification, first-attempt success, repair success, terminal
  repair failure, provider failure, duplicate events, and stale events through
  handler tests with deterministic mocked LLM responses.
- Verify Telegram output includes a compact preference-satisfaction summary
  only for an accepted plan.
- Verify logs contain safe failure categories and bounded schema locations but
  exclude preferences, meal content, ingredients, credentials, user IDs, and
  chat IDs.
- Verify the SAM template grants only the Planner function the permission it
  needs to invoke its bounded repair asynchronously.
- Run the entire existing suite to protect grocery generation, plan revisions,
  confirmation, concurrency, and retry-state behavior.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Record blockers or deviations with a `⚠️` prefix.
- Do not proceed to a new task while the current task's focused tests fail.
- Keep this document synchronized with implementation and verification status.

## Solution Overview

When the user answers the `/plan` preference question, the existing Bot LLM
interprets supported measurable clauses into `PreferenceRequirement` values.
Each rule contains a stable ID, the source clause, one or more alternative food
terms, an optional meal type, and an exact weekly count. The interpreter must
return either complete requirements or a focused clarification; it may not
silently discard ambiguous or unsupported text.

The bot passes the raw preference and validated requirements through the
existing asynchronous generation context. The planner receives both so it can
use culinary knowledge when constructing meal names and ingredient lists. The
LLM still returns an ordinary `WeeklyPlan`.

After structural parsing, application code derives matches from normalized meal
names and ingredient item names. It checks meal-type scope, counts distinct
meals, combines alternatives under one rule, and requires exact equality. It
also enforces useful generated-plan completeness: breakfast, lunch, and dinner
on every day, nonempty ingredient lists, and positive calories.

A compliant result is persisted and followed by an evidence-based Telegram
summary. An invalid first result schedules exactly one logical repair event in
a fresh Planner invocation with bounded validation feedback. A terminal repair
failure preserves the raw preference in retry-ready state and reports the
unmet rules. Existing request-ID, revision, and draft compare-and-swap checks
prevent stale results from being published.

## Technical Details

### Preference requirement contract

`PreferenceRequirement` is the only new domain model. Its intended shape is:

```json
{
  "id": "r1",
  "source_text": "crepes or pancakes on a breakfast once",
  "foods_any_of": ["crepes", "pancakes"],
  "meal_type": "breakfast",
  "exact_count": 1
}
```

- IDs are unique within one request and bounded for safe event transport.
- `source_text` is a bounded clause from the saved preference.
- `foods_any_of` is nonempty, bounded, and duplicate-free after normalization.
- `meal_type` is optional; omission means any meal during the seven-day plan.
- `exact_count` is positive and must fit the selected scope.
- The interpreter wire response may use a small object containing
  `requirements`, `clarification`, and `unparsed_text`; parse it into the one
  named model rather than adding an unnecessary envelope domain type.

### Interpretation and clarification

- No-preference phrases continue to bypass interpretation and generate normally.
- A complete interpretation covers every meaningful source clause.
- Ambiguity, conflicting rules, impossible counts, unsupported subjective
  wording, or nonempty `unparsed_text` keeps the workflow at
  `AWAITING_PREFERENCE` and sends one focused question.
- The existing `ConversationState.preference` temporarily retains the raw
  wording while clarification is pending. The next reply is combined with it
  and interpreted again; no new workflow kind or step is introduced.
- Interpreter transport or response-format failures leave state recoverable
  and ask the user to retry without invoking the planner.

### Evidence matching

- A meal is uniquely identified by `(day, meal_type)` and counts at most once
  per requirement, even when several fields contain matching evidence.
- Match against the meal name and each ingredient item's name after Unicode
  case folding, punctuation and whitespace normalization, and conservative
  singular/plural normalization.
- Use whole words or phrases. Do not accept arbitrary substring matches.
- `foods_any_of` forms one union, so salmon and trout together count toward one
  exact total.
- One meal may satisfy multiple compatible requirements.
- An omelette, shakshuka, or frittata satisfies an egg rule only when its
  generated name or ingredients contain normalized egg evidence.
- The matcher returns typed internal results for control flow and display, but
  those results are not a new LLM or persistence contract.

### Generation and repair flow

- Extend `PlanGenerationContext` and planner events with validated requirements,
  an attempt number limited to one or two, and bounded repair feedback.
- Initial-generation prompts render the raw preference and exact interpreted
  rules. Repair prompts add only safe, bounded structural and compliance
  feedback.
- Attempt one never saves or displays an invalid candidate. It asynchronously
  invokes attempt two and leaves the matching conversation state generating.
- Keep repair payloads small: regenerate from the original context and bounded
  violations instead of carrying the entire rejected plan.
- Attempt two cannot schedule another repair. Terminal failure transitions the
  matching state to retry-ready and reports structural or requirement-specific
  reasons.
- The Planner Lambda uses its invocation ARN/name for self-invocation. Add a
  narrowly scoped IAM statement without creating a CloudFormation self-reference
  cycle.
- AWS asynchronous delivery is at least once. Duplicate workers may consume
  compute, but state rechecks and conditional persistence must prevent duplicate
  publication, state clearing, or contradictory Telegram results.
- Plan revisions remain outside preference interpretation in this change and
  retain their existing planning-instruction behavior.

### User-facing behavior

A successful response follows the existing plan with a compact summary:

```text
Preferences satisfied:
• Crepes or pancakes: 1 breakfast
• Eggs: 3 breakfasts
• Salmon or trout: 1 dinner
```

Terminal messages distinguish malformed plan structure from unmet preference
rules. They explain that no draft was saved and that `/plan` will retry the
saved preference. User-facing feedback may quote the user's own bounded clauses;
operational logs may not.

## What Goes Where

- **Implementation Steps:** code, tests, SAM permissions, documentation, and
  plan tracking that can be completed in this repository.
- **Post-Completion:** deployment, live Telegram verification, CloudWatch
  inspection, and the required GitHub issue comment linking the final commit or
  pull request.

## Implementation Steps

### Task 1: Define the preference requirement model

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/test_schemas.py`

- [x] write failing tests for valid exact-count requirements and optional meal
  scopes
- [x] write failing tests for empty alternatives, duplicate normalized foods,
  invalid IDs, nonpositive counts, oversized values, and impossible field
  combinations
- [x] implement the bounded, typed `PreferenceRequirement` model and export it
- [x] add regression tests proving existing plan and conversation models remain
  backward compatible
- [x] run `uv run pytest tests/test_schemas.py`; it must pass before Task 2

### Task 2: Interpret preferences into complete measurable rules

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_parser.py`

- [x] write failing prompt tests for exact counts, alternative foods, optional
  meal scopes, clause coverage, ambiguity, conflicts, and unsupported wording
- [x] write failing parser tests for complete requirements, clarification,
  unparsed text, malformed objects, and duplicate IDs
- [x] add the focused preference-interpretation prompt without changing the
  general conversational intent prompt
- [x] parse the small wire response into `PreferenceRequirement` values and a
  clarification outcome without introducing a second domain envelope model
- [x] add edge-case tests proving omitted clauses cannot be treated as a
  successful interpretation
- [x] run `uv run pytest tests/test_prompts.py tests/test_parser.py`; it must
  pass before Task 3

### Task 3: Add recoverable preference clarification to `/plan`

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing workflow tests for complete interpretation, focused
  clarification, combined clarification replies, and interpreter failure
- [x] update the existing plan-request state invariant so raw preference text
  may be retained while awaiting clarification without adding a workflow step
- [x] call the interpreter before transitioning to `GENERATING`, while
  preserving no-preference behavior and update idempotency
- [x] keep ambiguous or unsupported requests recoverable and prevent premature
  Planner Lambda invocation
- [x] add race, repeated-update, length-boundary, and retry regressions for all
  modified state transitions
- [x] run `uv run pytest tests/test_schemas.py tests/test_bot_handler.py`; it
  must pass before Task 4

### Task 4: Derive and validate meal evidence

**Files:**

- Create: `src/meal_planner/preferences.py`
- Create: `tests/test_preferences.py`

- [x] write failing matcher tests for names, ingredients, case, punctuation,
  whitespace, singular/plural forms, whole phrases, and false substrings
- [x] write failing count tests for alternatives, meal scopes, distinct meals,
  repeated evidence, compatible overlapping rules, and exact-count excesses
- [x] implement small typed matching and validation functions with no food
  aliases or external dependencies
- [x] implement generated-plan completeness checks for three required daily
  meals, nonempty ingredients, and positive calories without tightening stored
  `WeeklyPlan` deserialization
- [x] add edge-case tests for empty days, missing meal types, zero calories,
  empty ingredients, impossible counts, and stable validation feedback
- [x] run `uv run pytest tests/test_preferences.py`; it passes before Task 5

### Task 5: Carry interpreted rules through planner events and prompts

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_prompts.py`

- [x] write failing context and event tests for requirements, attempt bounds,
  bounded repair feedback, and invalid nested values
- [x] write failing prompt tests proving raw wording and every interpreted rule
  appear without weakening permanent profile constraints
- [x] extend the existing `PlanGenerationContext`, bot dispatch, and Planner
  event boundary with requirements and repair metadata
- [x] render exact rules in initial and repair generation prompts while keeping
  the existing `WeeklyPlan` output schema
- [x] add compatibility and error tests for no-preference requests, legacy
  direct handler calls, malformed events, and plan revisions
- [x] run the four focused test modules; they must pass before Task 6

### Task 6: Publish only compliant plans with visible evidence

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/preferences.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_preferences.py`

- [x] write failing handler tests proving compliant plans save once and include
  a requirement-satisfaction summary
- [x] write failing tests proving structurally invalid, incomplete, under-count,
  and over-count plans are neither saved nor displayed
- [x] integrate generated-plan completeness and preference validation after
  Pydantic parsing and before lifecycle normalization or persistence
- [x] format the accepted evidence summary from application-derived matches and
  send it only after successful draft delivery
- [x] add delivery-failure, stale-state, persistence-conflict, and no-preference
  regressions for the modified success path
- [x] run `uv run pytest tests/test_preferences.py tests/test_planner_handler.py`;
  it must pass before Task 7

### Task 7: Run one bounded repair in a fresh Planner invocation

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `template.yaml`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_template.py`

- [x] write failing tests proving an invalid first attempt schedules attempt two
  without saving, displaying, or transitioning retry-ready
- [x] write failing tests proving attempt-two success publishes once and
  attempt-two failure becomes retry-ready without scheduling attempt three
- [x] add the Planner self-invocation boundary with bounded requirements and
  validation feedback, retaining one provider call per invocation
- [x] add narrowly scoped SAM invocation permission without a self-reference
  cycle or changes to Planner timeouts, memory, or provider attempt count
- [x] add duplicate, stale, cancellation, invocation-failure, and direct-call
  regressions that prevent contradictory publication or notification
- [x] run `uv run pytest tests/test_planner_handler.py tests/test_template.py`;
  it must pass before Task 8

### Task 8: Make terminal failures specific and privacy-safe

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_planner_handler.py`
- Modify: `tests/test_parser.py`

- [x] write failing tests for distinct structural, completeness, compliance,
  response-format, timeout, transient, and permanent failure outcomes
- [x] retain bounded machine-readable validation codes and safe schema
  locations through the repair flow
- [x] report unmet requirement clauses to the requesting user and state clearly
  that no draft was saved and `/plan` retains the preference
- [x] log only attempt, elapsed time, model, failure category, and bounded safe
  validation metadata
- [x] add privacy regressions excluding raw preferences, plans, meals,
  ingredients, credentials, raw events, user IDs, and chat IDs from logs
- [x] run `uv run pytest tests/test_parser.py tests/test_planner_handler.py`; it
  must pass before Task 9

### Task 9: Verify acceptance criteria

**Files:**

- Modify if needed: tests and implementation files listed above
- Modify: this plan file when verification discovers a deviation

- [x] verify the reported example produces exactly one pancake-or-crepe
  breakfast, three egg-evidenced breakfasts, and one salmon-or-trout dinner
- [x] verify an accepted plan displays the evidence summary and a violating
  plan is never persisted or displayed
- [x] verify first structural or compliance failure receives one fresh repair
  invocation and terminal failure remains manually retryable
- [x] verify no food alias database, new workflow kind, new workflow step,
  persisted meal tags, or extra in-invocation provider attempt was introduced
- [x] run `uv run ruff format --check .` and fix formatting failures
- [x] run `uv run ruff check .` and fix lint failures
- [x] run `uv run mypy` and fix all type errors
- [x] run `uv run pytest` until the full suite passes

Verification record (2026-08-19): The reported three-rule example is covered
by `test_reported_preference_example_has_exact_evidence_counts`. Accepted
plans derive and send the evidence summary only after persistence and delivery;
invalid plans are covered by planner rejection and bounded repair tests. The
repair lifecycle is covered for both structural and compliance failures,
including terminal retry-ready state. Repository inspection and template tests
confirmed no alias database, workflow enum/step, meal tags, or additional
provider call was introduced. The first verification run exposed only an
incorrect expected parser code in the new structural-repair test and a Ruff
formatting issue; both were corrected without production changes. Final checks:
`uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy` passed;
`uv run pytest` passed with 493 tests and 2 skips. Task 10 remains untouched.

### Task 10: Update documentation and archive the completed plan

**Files:**

- Modify: `README.md`
- Modify: tests covering documented runtime and template behavior
- Move after completion:
  `docs/plans/2026-08-19-enforce-meal-plan-preferences-with-evidence-based-validation.md`
  to `docs/plans/completed/`

- [x] document supported exact-count preference syntax, clarification behavior,
  evidence matching, automatic repair, and terminal manual retry behavior
- [x] document that semantic interpretation is LLM-assisted while counts and
  meal evidence are application-validated
- [x] update documentation assertions or configuration tests affected by the
  new repair invocation behavior
- [x] rerun documentation-adjacent tests and the full required Ruff, Mypy, and
  Pytest checks
- [x] mark every checkbox complete, record scope changes and final verification,
  and move this plan into `docs/plans/completed/`

Final verification record (2026-08-19): Task 10 changed only `README.md`,
`tests/test_template.py`, and this plan. No production implementation,
configuration, dependency, or lockfile changes were made. The README now
documents exact-count syntax, clarification, application-derived evidence,
the LLM/application validation boundary, one fresh-invocation repair, and
manual `/plan` retry after terminal failure. Documentation-adjacent tests
(`uv run pytest tests/test_template.py tests/test_deploy.py`) passed with 42
tests and 2 skips. `uv run ruff format --check .`, `uv run ruff check .`, and
`uv run mypy` passed. The full `uv run pytest` suite passed with 493 tests and
2 skips. No unresolved issues remain for this task.

## Post-Completion

**Manual verification**

- Deploy through a dedicated feature branch and pull request; never push or
  merge directly to `master`.
- Run `/plan` with the reported preference and verify the plan plus satisfaction
  summary in Telegram.
- Try an ambiguous request and verify the clarification reply resumes the same
  plan workflow.
- Force a first-attempt violation and confirm exactly one repair completes in a
  separate Planner invocation.
- Inspect CloudWatch records to confirm useful failure categories without raw
  preference or meal content.

**External system updates**

- After implementation, use a Conventional Commit message containing the issue
  number.
- Add a comment to the associated GitHub issue summarizing the completed work
  and linking the commit or pull request, as required by `AGENTS.md`.
- Deploy the SAM IAM-policy change through the repository's normal reviewed
  release process.
