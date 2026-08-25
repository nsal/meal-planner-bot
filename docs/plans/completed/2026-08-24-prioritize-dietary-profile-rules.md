# Prioritize and Enforce Dietary Profile Rules

Tracking issue: [#61](https://github.com/nsal/meal-planner-bot/issues/61)

## Overview

Simplify the profile from three overlapping planning fields to two clearly
prioritized concepts: non-negotiable dietary constraints and persistent
dietary preferences whose strictness follows the user's wording. Remove and
discard goals, allow a current plan preference to override only conflicting
stored preferences, and prevent every newly generated plan from being saved
or displayed until it passes deterministic constraint and strict-rule
validation.

The change extends the existing preference interpretation, evidence matching,
and single automatic repair path. It does not revalidate or alter plans made
before a profile constraint changes. Users remain responsible for regenerating
those plans.

## Context (from discovery)

- `src/meal_planner/models/schemas.py` persists profile constraints,
  preferences, and goals as separate string lists. It also defines the current
  exact-count-only `PreferenceRequirement` used in asynchronous planner events.
- `src/meal_planner/bot_handler.py` has a deterministic `/profile` amendment
  workflow, while request-specific plan text is interpreted by the LLM before
  planner dispatch and retained for retry.
- `src/meal_planner/preferences.py` already provides normalized evidence
  matching, exact-count validation, completeness checks, and bounded terminal
  messaging.
- `src/meal_planner/planner_handler.py` runs application validation before
  persistence or display and schedules one repair in a fresh asynchronous
  invocation when attempt one fails.
- `src/meal_planner/llm/prompts.py`, `src/meal_planner/llm/parser.py`,
  `src/meal_planner/telegram/api.py`, `src/meal_planner/router.py`, DynamoDB
  serialization tests, README text, and profile/planner tests all reference the
  current three-field or exact-count contracts.
- The project uses Python 3.14, Pydantic, Aiogram, DynamoDB, Ruff with an
  80-column limit, strict Mypy, Pytest, and `uv` commands from
  `pyproject.toml`.

## Development Approach

- **Testing approach:** TDD. For every numbered task, write the focused failing
  tests first, implement only enough production behavior to pass them, add the
  listed edge cases, and run the focused test set before continuing.
- Complete one task fully and update its checkboxes immediately before starting
  the next task.
- Reuse the existing interpretation, evidence, planner-event, and repair
  architecture. Add one shared structured rule vocabulary instead of separate
  profile-rule and plan-rule implementations.
- Keep raw user wording beside its structured interpretation so profile display,
  clarification, removal, retries, and failure messages remain understandable.
- Treat constraints as a separate, highest-priority validation input. Do not
  encode constraints as ordinary preferences that a resolver can replace.
- Fail closed when a new constraint cannot be interpreted or when a generated
  candidate violates an active constraint. Never persist or display such a
  candidate.
- Keep the scope lean: do not scan, invalidate, migrate, or otherwise modify
  existing weekly plans after a profile change. Do not add nutrient-compliance
  calculation or manufacturing cross-contamination claims.
- Preserve compatibility for legacy DynamoDB profile records while discarding
  `goals`. Normalize legacy raw constraints and preferences at the profile
  boundary without attempting network calls during model deserialization.
- Update this plan immediately if implementation discoveries change scope or
  architecture.
- Follow repository standards throughout: full type hints, Ruff only, strict
  Mypy, Pytest, and `uv` for Python tool execution.

## Testing Strategy

- Use schema tests for bounded rule contracts, compatibility normalization,
  goal removal, enum values, and asynchronous event round trips.
- Use prompt/parser tests for supported count operators, weekdays, meal scope,
  strict versus best-effort tone, exclusions, ambiguity, unparsed clauses, and
  safe clarification.
- Use pure unit tests for constraint conflicts, priority resolution, partial
  lower-priority preservation, alias expansion, evidence matching, and complete
  validation outcomes.
- Use bot-handler, router, Telegram API, and Dynamo tests for the profile edit
  workflow, confirmation, atomic conflict cleanup, retry state, legacy records,
  and removal of Goals from the UI.
- Use planner-handler tests for initial success, safety rejection, automatic
  repair, repaired success, terminal failure, stale requests, duplicate repair
  delivery, and the no-save/no-display contract.
- Update existing assertions that intentionally encode the removed `goals` or
  exact-count-only behavior; do not broadly rewrite unrelated tests.
- The repository has no browser-based E2E harness. Telegram flows are covered
  through router, handler, and API payload tests.
- At final verification run `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `uv run pytest`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered work with a `➕` prefix.
- Record blockers or deviations with a `⚠️` prefix.
- Do not begin the next task while the current task's focused tests fail.
- Keep this document synchronized with the implementation and verification
  state.

## Solution Overview

The canonical profile exposes only `dietary_constraints` and
`dietary_preferences`. Each entry retains bounded source text and a structured
interpretation. Constraints contain normalized forbidden food terms, including
deterministic expansions from a small reviewed alias/category registry.
Preferences and current plan instructions share a typed rule contract covering
food alternatives, optional meal scope, optional weekdays, count operator,
count, and strict or best-effort strength.

Profile input is interpreted before persistence. Ambiguous or unsupported
wording is clarified instead of guessed. A new preference that conflicts with
an active constraint is rejected. A new constraint is accepted as the highest
priority and atomically removes conflicting stored preference rules, with a
user-facing explanation. Historical weekly plans are not inspected.

At `/plan`, the application resolves current plan rules over stored dietary
preference rules while preserving all compatible lower-priority intent.
Constraints are then applied independently and cannot be weakened. A current
upper bound can cap a stored exact or minimum request while preserving the
largest compatible obligation; unrelated stored rules remain unchanged.

The planner receives raw wording and the effective structured rules. After the
LLM returns a candidate, application code checks declared meal names and
ingredient items for constraint exclusions, strict counts, weekdays, meal
scope, and the existing completeness contract. Attempt one failure schedules
the existing bounded repair. Attempt two failure saves and displays nothing,
keeps the previous draft unchanged, and produces actionable safe feedback.

## Technical Details

### Canonical structured rules

Introduce bounded Pydantic contracts with names finalized during Task 1 but
with these semantics:

```json
{
  "id": "r1",
  "source_text": "eggs for breakfast twice",
  "foods_any_of": ["egg"],
  "meal_type": "breakfast",
  "weekdays": [],
  "operator": "exactly",
  "count": 2,
  "strength": "strict"
}
```

- Operators are `exactly`, `at_least`, and `at_most`; zero is allowed where it
  represents an explicit exclusion such as "no eggs this week."
- Weekdays use a stable enum or ISO weekday values and cannot contain
  duplicates. A weekday-specific strict rule must match every named day unless
  a higher-priority current instruction narrows it.
- `strict` rules participate in rejection and repair. `best_effort` is emitted
  only when wording explicitly permits omission, such as "if convenient."
- "I'd like eggs for breakfast" becomes strict `at_least 1`; it is not silently
  reduced to prompt-only guidance.
- Constraint entries have source text plus a nonempty, de-duplicated collection
  of forbidden normalized terms. Constraint rules are not assigned a
  replaceable priority.
- Existing profile strings require a deterministic compatibility representation
  during deserialization. Known constraint phrases use the reviewed registry;
  unknown legacy constraint text remains active and blocks new generation with
  a focused remediation message rather than being ignored. Legacy preference
  strings remain visible and must be interpreted through the normal planning
  preparation path before they can become enforceable rules.

### Interpretation and profile confirmation

- Extend the existing interpreter response rather than introduce a second LLM
  wire format. Give it an explicit mode for constraint, stored preference, or
  current plan preference.
- Require every meaningful clause to become a rule or exclusion. Nonempty
  unparsed text, contradictions, unsupported semantics, or invalid counts
  produce one focused clarification and no mutation.
- Add a bounded pending interpretation to the durable profile-edit state and a
  confirmation step. Display the source wording and concise interpreted
  meaning before saving.
- At confirmation, reload the current profile and re-run deterministic conflict
  checks so concurrent amendments cannot bypass constraints.
- Reject a preference conflicting with a constraint. When confirming a new
  constraint, remove only conflicting preference entries in the same guarded
  profile/workflow transaction and report their source text.
- Removing a constraint or preference continues to use deterministic selection
  from the canonical displayed entries.

### Priority resolution

Resolve only stored dietary preferences against current plan preferences:

```text
dietary constraints (independent, never replaceable)
    > current plan preferences
    > stored dietary preferences
    > nutrition targets and general guidance
```

- Compare rules by overlapping food evidence, meal scope, and weekday scope.
- A higher-priority exact rule replaces the overlapping lower obligation.
- A higher-priority maximum caps a lower exact/minimum rule but preserves the
  largest satisfiable lower request. Three stored egg breakfasts plus a current
  maximum of two therefore resolves to exactly two, preferably on stored days.
- A higher-priority minimum raises an incompatible lower maximum within the
  higher rule's scope.
- An explicit higher-priority zero rule removes the overlapping positive stored
  obligation.
- Preserve non-conflicting weekday preferences and all unrelated food rules.
- Detect contradictions inside one priority tier and clarify before planner
  dispatch rather than choosing arbitrarily.
- Produce stable IDs and ordering so retries reuse the same resolved rules
  without reinterpretation.

### Constraint evidence and aliases

- Match constraints against every generated meal name and declared ingredient
  item with the existing normalized whole-word/phrase matcher.
- Maintain a deliberately small reviewed mapping for supported category terms,
  for example dairy to milk, cheese, butter, cream, whey, and casein. Keep the
  mapping application-owned, deterministic, typed, and directly unit-tested.
- Do not infer safety from calories, descriptions not present in the plan, or
  an LLM self-assessment.
- If an active constraint cannot be represented by validated terms, do not
  start generation. Ask the user to edit or clarify that constraint.
- Document that validation covers meal names and declared ingredients, not
  undeclared product cross-contamination or medical certification.

### Generation and repair

- Build the effective rule set before dispatch and persist it in the existing
  conversation/generation context so retries do not reinterpret wording.
- Include stored raw preference wording, current raw wording, effective strict
  rules, best-effort rules, and constraints in clearly separated prompt
  sections reflecting their priority.
- Validate constraints first, followed by strict effective rules and plan
  completeness. Best-effort misses may be summarized but never invalidate a
  candidate.
- Add stable safety issue codes and bounded locations to existing validation
  feedback. Keep raw profile text, meal content, user IDs, and chat IDs out of
  logs and repair metadata.
- Attempt one schedules exactly one repair in the existing asynchronous path.
  Attempt two repeats the complete safety and compliance gate.
- A terminal failure never writes the candidate and never calls `send_plan`.
  It leaves the previous draft unchanged, preserves retryable request context,
  and reports the unresolved source clauses safely.

### Goals removal and compatibility

- Remove Goals from profile categories, operations, Telegram keyboards,
  displays, onboarding/profile prompts, conversational fields, and public model
  exports.
- Remove `goals` from canonical profile and profile-update models. Pydantic's
  compatibility boundary accepts legacy records containing the key but drops
  it from canonical dumps and all later DynamoDB writes.
- Do not translate or retain existing goal values in dietary preferences.
- Keep legacy allergies/restrictions normalization into dietary constraints.
- Do not scan or mutate historical weekly plans after any profile update.

## Implementation Steps

### Task 1: Define the shared dietary rule contracts

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/models/__init__.py`
- Modify: `tests/test_schemas.py`

- [x] write failing schema tests for rule operators, strictness, weekdays,
  zero-count exclusions, bounds, duplicates, and JSON round trips
- [x] write failing compatibility tests proving legacy `goals` input is
  discarded and absent from canonical profile/update dumps
- [x] add the typed shared rule and constraint-entry contracts with complete
  validation and exports
- [x] remove `goals` from canonical profile/update fields and normalize legacy
  raw constraint/preference entries without network-dependent migration
- [x] update generation and conversation context schemas to carry stored,
  current, effective, and constraint rules with stable retry serialization
- [x] run `uv run pytest tests/test_schemas.py` and require it to pass before
  Task 2

### Task 2: Add deterministic constraint aliases and conflict checks

**Files:**
- Create: `src/meal_planner/dietary_rules.py`
- Create: `tests/test_dietary_rules.py`

- [x] write failing tests for normalized exact and alias-expanded exclusions,
  including whole-word matches and false-positive substring rejection
- [x] write failing tests for preference-versus-constraint conflicts across
  food alternatives, meal scopes, weekdays, and zero rules
- [x] implement the small typed alias/category registry and deterministic
  constraint expansion
- [x] implement conflict results that retain stable rule IDs and bounded source
  references without leaking raw content into logs
- [x] cover unknown/uninterpretable constraint behavior as an explicit
  fail-closed result
- [x] run `uv run pytest tests/test_dietary_rules.py` and require it to pass
  before Task 3

### Task 3: Extend interpretation prompts and parsing

**Files:**
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `src/meal_planner/llm/parser.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_parser.py`

- [x] write failing prompt/parser tests for `exactly`, `at_least`, `at_most`,
  weekday scopes, explicit best effort, exclusions, and mode-specific output
- [x] write failing tests proving "I'd like" yields strict minimum-one behavior
  while "if convenient" yields best effort
- [x] write failing tests for ambiguity, contradictions, unknown constraint
  terms, incomplete clause coverage, malformed responses, and bounded
  clarification
- [x] extend the existing interpretation prompt and parser around the shared
  rule contract without adding a duplicate domain model
- [x] keep every meaningful clause accounted for and reject silent partial
  interpretations
- [x] run `uv run pytest tests/test_prompts.py tests/test_parser.py` and require
  it to pass before Task 4

### Task 4: Implement deterministic priority resolution

**Files:**
- Modify: `src/meal_planner/dietary_rules.py`
- Modify: `tests/test_dietary_rules.py`

- [x] write failing table-driven tests for exact replacement, maximum capping,
  minimum raising, explicit zero, partial weekday overlap, and unrelated rules
- [x] write the regression case where stored Monday/Wednesday/Friday egg
  breakfasts plus a current maximum of two resolves to exactly two preferred
  days rather than permitting zero
- [x] implement stable scope-overlap and precedence resolution with constraints
  outside the replaceable preference tiers
- [x] preserve compatible fragments and stable ordering/IDs across repeated
  resolution and retry serialization
- [x] reject same-tier contradictions with a typed clarification outcome
- [x] run `uv run pytest tests/test_dietary_rules.py` and require it to pass
  before Task 5

### Task 5: Persist interpreted profile entries and discard goals

**Files:**
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/factories.py`

- [x] write failing Dynamo tests for canonical structured profile round trips,
  legacy raw entries, discarded goals, and absence of goals on later writes
- [x] write failing transaction tests for guarded preference rejection and
  atomic constraint save plus conflicting-preference removal
- [x] adapt profile serialization and state-guarded profile writes to the new
  canonical entries without changing historical plan records
- [x] preserve existing allergy/restriction compatibility and deterministic
  de-duplication behavior
- [x] update test factories to produce canonical profiles and add explicit
  legacy fixtures where compatibility is under test
- [x] run `uv run pytest tests/test_dynamo.py` and require it to pass before
  Task 6

### Task 6: Add profile interpretation and confirmation workflow

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/router.py`
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_telegram_api.py`

- [x] write failing workflow tests for constraint/preference interpretation,
  clarification, confirmation, cancellation, stale callbacks, and replay
- [x] write failing tests that reject a conflicting preference and atomically
  remove/report conflicting preferences when a constraint is confirmed
- [x] write failing router/API tests proving Goals is absent from profile
  display, keyboards, callback values, and supported operations
- [x] add bounded pending-rule state and confirmation callbacks to the existing
  durable profile workflow
- [x] recheck conflicts against the latest profile at confirmation and use the
  existing guarded profile/workflow transaction
- [x] preserve deterministic remove/back/done/close behavior for the two
  remaining dietary categories
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_router.py
  tests/test_telegram_api.py` and require it to pass before Task 7

### Task 7: Prepare and dispatch effective plan rules

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py`

- [x] write failing tests for combining stored preferences with a current plan
  preference and preserving unrelated lower-priority rules
- [x] write failing tests that reject current plan preferences conflicting with
  constraints before planner invocation
- [x] write failing retry tests proving effective rules are retained and not
  reinterpreted after dispatch or provider failure
- [x] resolve profile and current rules before invoking the planner and carry
  the resolved snapshot through conversation and Lambda event contexts
- [x] render separate constraint, strict, and best-effort prompt sections with
  the agreed priority and remove every Goals prompt reference
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_prompts.py` and
  require it to pass before Task 8

### Task 8: Validate exclusions and generalized preference rules

**Files:**
- Modify: `src/meal_planner/preferences.py`
- Modify: `tests/test_preferences.py`

- [x] write failing tests for constraint matches in meal names and ingredients,
  including aliases, alternatives, normalization, and false positives
- [x] write failing tests for exact, minimum, maximum, zero, weekday, and meal
  scope validation with distinct-meal evidence
- [x] write failing tests proving strict misses invalidate while best-effort
  misses do not
- [x] extend the existing validation result with typed safety and generalized
  rule evidence while retaining completeness checks
- [x] add bounded user-facing summaries for satisfied strict rules, unmet
  clauses, and best-effort outcomes without exposing raw data in logs
- [x] run `uv run pytest tests/test_preferences.py` and require it to pass
  before Task 9

### Task 9: Enforce safety through automatic repair and publication

**Files:**
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] write failing tests proving a constraint-violating first candidate is not
  saved or displayed and schedules one bounded repair
- [x] write failing tests for repaired success and repaired safety failure,
  including no save, no display, unchanged previous draft, and retry-ready state
- [x] write failing tests for stable safety error classification, bounded repair
  feedback, stale requests, and duplicate repair events
- [x] run constraint validation before strict preference and completeness
  validation on both attempts
- [x] integrate safety issues with the existing asynchronous repair lifecycle
  and terminal messaging without adding another repair mechanism
- [x] ensure best-effort misses never trigger repair and successful plans retain
  only the relevant raw planning instructions
- [x] run `uv run pytest tests/test_planner_handler.py` and require it to pass
  before Task 10

⚠️ Full `uv run pytest` verification is 1,111 passed and 2 failed because the
pre-existing `.aws-sam/build` artifacts are stale compared with source files;
the focused Task 9 suite and all Task 9 static checks pass.

### Task 10: Remove remaining Goals behavior and update user documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/prompt.md`
- Modify: `tests/test_readme.py`
- Modify: relevant existing tests returned by `rg -n "goals|Goals" tests src`

- [x] write or update documentation tests for the two-field profile, priority
  order, interpretation confirmation, validation, one repair, and terminal
  fail-closed behavior
- [x] remove remaining functional Goals references and update intentional test
  fixtures/assertions to the canonical two-field profile
- [x] document strict-versus-best-effort examples and the plan preference
  override example using egg breakfasts
- [x] document that constraints apply to newly generated plans only and that
  declared-ingredient validation is not medical cross-contamination
  certification
- [x] run `uv run pytest tests/test_readme.py` plus every focused test file
  modified in this task and require them to pass before Task 11

### Task 11: Verify acceptance criteria

**Files:**
- Modify if needed: files implicated by verification failures
- Modify: `docs/plans/2026-08-24-prioritize-dietary-profile-rules.md`

- [x] verify Goals is absent from canonical models, persistence, prompts,
  Telegram UI, callbacks, and planning behavior
- [x] verify constraints cannot be overridden by stored or current preferences
  and that no failing candidate is saved or displayed
- [x] verify current plan rules override only conflicting stored preferences and
  flexible wording follows the interpreted strictness contract
- [x] verify profile changes do not scan or mutate previously generated plans
- [x] run `uv run ruff format .`, then `uv run ruff format --check .`
- [x] run `uv run ruff check .` and fix all findings
- [x] run `uv run mypy` and fix all findings
- [x] run `uv run pytest` and require the complete suite to pass
- [x] inspect `git diff --check` and the final diff for unrelated changes,
  privacy leaks, and accidental plan/schema expansion

⚠️ Verification deviation: the ignored `.aws-sam/build/BotFunction-Shared`
artifact was stale and the SAM CLI was unavailable. Its existing generated
application source tree was refreshed in place without deletion or cleanup;
the final full suite then passed. No generated artifact is tracked.

### Task 12: Finalize documentation and implementation record

**Files:**
- Modify: `README.md` if verification exposes missing behavior
- Modify: `docs/prompt.md` if verification exposes missing prompt guidance
- Move: `docs/plans/2026-08-24-prioritize-dietary-profile-rules.md` to
  `docs/plans/completed/2026-08-24-prioritize-dietary-profile-rules.md`

- [x] update README or prompt documentation only for verified implementation
  behavior not already covered in Task 10
- [x] record any approved scope changes, deviations, and final verification
  commands/results in this plan
- [x] ensure every task checkbox and acceptance criterion accurately reflects
  completed work
- [x] move the completed plan into `docs/plans/completed/`
- [x] run `uv run pytest tests/test_readme.py` after final documentation changes

**Task 12 implementation record:**

- Documentation review: no README or prompt changes were required. The
  documentation and `tests/test_readme.py` already cover the verified
  two-field profile, priority order, interpretation confirmation,
  strict-versus-best-effort rules, validation evidence and limits, one repair,
  terminal fail-closed behavior, and newly generated plans only.
- Scope changes: none approved. No implementation code, tests, dependencies,
  lockfiles, or generated artifacts were changed for Task 12.
- Verification deviation: the ignored `.aws-sam/build/BotFunction-Shared`
  artifact was stale and the SAM CLI was unavailable. Its existing generated
  application source tree was refreshed in place during Task 11 without
  deletion or cleanup; no generated artifact is tracked.
- Final verification from Task 11:
  - `uv run ruff format .` — passed.
  - `uv run ruff format --check .` — passed; 89 files formatted.
  - `uv run ruff check .` — passed.
  - `uv run mypy` — passed; no issues in 20 source files.
  - `uv run pytest` — passed; 1,112 passed, 2 warnings.
  - `git diff --check` — passed.
- Task 12 verification:
  - `uv run pytest tests/test_readme.py` — passed; 11 passed.

## Post-Completion

**Manual verification**

- In Telegram, add and confirm a dietary constraint, attempt to add a
  conflicting dietary preference, and verify the preference is rejected.
- Confirm a constraint that conflicts with an existing preference and verify
  the preference removal is clearly reported.
- Generate a plan with stored weekday preferences and a stricter current count
  limit; verify the displayed plan reflects the resolved rule.
- Force a constraint violation in a test/staging provider response and verify
  the candidate is never displayed before or after terminal repair failure.
- Add a new constraint while an older plan exists and verify that older plan is
  left untouched, as explicitly required.

**External system updates**

- Implement on a dedicated feature branch and open a pull request; never push
  or merge directly to `master`.
- After implementation, use a Conventional Commit message containing the
  tracking issue number.
- Comment on the tracking GitHub issue with the completed commit or pull
  request link and verification results.
