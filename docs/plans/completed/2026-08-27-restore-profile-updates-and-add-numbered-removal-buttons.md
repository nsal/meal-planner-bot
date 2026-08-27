# Restore Profile Updates and Add Numbered Removal Buttons

## Overview

Restore bot availability for the single development user whose persisted
dietary preferences contain legacy entries with no structured `DietaryRule`,
then replace exact-text profile removal with revision-safe numbered buttons.
The change must preserve valid structured preferences, dietary constraints,
family data, plans, meal history, and unrelated DynamoDB attributes.

The application will tolerate and discard only legacy unstructured preference
shapes when reading a complete saved profile. New profile updates and writes
remain strict. A targeted, exact-user repair command will permanently remove
the malformed entries after the compatibility code is deployed. Profile views
and removal menus will render deterministic numbered lists, while the removal
callback will use the displayed index together with the profile revision to
prevent stale buttons from deleting a different item.

## Context (from discovery)

- `src/meal_planner/models/schemas.py` converts legacy preference strings to
  entries whose `rule` is `None`, while `DietaryPreferenceEntry.rule` is
  mandatory. `UserProfile.model_validate` therefore raises before commands can
  respond.
- `src/meal_planner/db/dynamo.py` canonicalizes profile rule IDs and already
  performs profile/state writes with profile- and conversation-revision guards.
- `src/meal_planner/bot_handler.py` currently asks users to type exact text for
  removal and contains several defensive `rule is None` branches left over
  from the incompatible migration behavior.
- `src/meal_planner/router.py` validates profile callback shapes and enforces
  Telegram's 64-byte callback-data limit. `src/meal_planner/telegram/api.py`
  owns profile text and inline-keyboard rendering.
- `scripts/reset_profile_dietary_fields.py` already targets one explicit user
  with a consistent read and conditional update, but it rejects the malformed
  profile and would clear both constraints and preferences.
- The project is Python 3.14 with Pydantic 2, boto3/DynamoDB, Telegram inline
  callbacks, pytest/moto, Ruff, and strict mypy. Repository rules require
  80-column Ruff formatting, `uv run pytest`, and `uv run mypy`.

## Development Approach

- **Testing approach:** TDD. Add or update regression tests before each
  implementation change, then make the focused test set pass before proceeding.
- Complete each task fully before moving to the next and mark its checkboxes as
  soon as the work is complete.
- Keep changes small and focused. Reuse the existing typed models, Telegram
  renderers, and guarded profile/state transaction rather than introducing a
  second amendment system.
- Every task that changes code includes success and error-path tests for the
  modified behavior.
- All focused tests must pass before starting the next task. Update this plan
  if implementation scope or sequencing changes.
- Maintain strict validation for new inputs and writes. Backward compatibility
  applies only to reading and deleting the known legacy unstructured
  preference shapes.

## Testing Strategy

- **Schema tests:** raw strings, missing `rule`, and `rule: null` are discarded
  from complete persisted profiles; valid entries in mixed lists survive;
  constraints remain unchanged; profile-update inputs continue rejecting raw
  or null-rule preferences.
- **Repository and repair tests:** consistent exact-key reads, valid-entry
  retention, conditional revision updates, idempotency, concurrent-change
  rejection, preservation of unrelated attributes/items, no scans, and
  redacted errors.
- **Router tests:** valid bounded selection callbacks and rejection of malformed,
  oversized, negative, out-of-range-shaped, and unsupported payloads.
- **Telegram API tests:** deterministic numbering, combined dietary and batch
  preference labels, compact wrapped number buttons, empty states, and callback
  data below 64 bytes.
- **Handler tests:** direct numbered removal for family members, constraints,
  dietary preferences, and batch rules; repeated removals; stale revision and
  invalid-index no-ops; duplicate display text; load-failure replies; and
  preservation of add/change workflows.
- **Full verification:** `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy`, and `uv run pytest`.
- The project has no separate browser/UI end-to-end suite. Telegram behavior is
  exercised through handler and API contract tests plus a manual dev smoke test.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered work with a `➕` prefix.
- Record blockers or deviations with a `⚠️` prefix.
- Keep this plan synchronized with the implementation and verification results.

## Solution Overview

Use a narrow read-compatibility adapter in `UserProfile`, not an optional
canonical rule model. Complete saved profiles drop only preference entries that
cannot contain a valid structured rule. `ProfileUpdateEntities` and canonical
write paths keep rejecting those shapes, preventing the compatibility behavior
from becoming a new input contract. Repository reads should log only a bounded
repair category/count when cleanup occurs and must not mutate DynamoDB.

After deploying compatibility, run the targeted repair for the only user. The
repair consistently reads the exact `USER#<id>` / `PROFILE` item, classifies
each raw preference independently, retains valid structured entries, validates
the cleaned complete profile, and conditionally updates only
`dietary_preferences` plus `profile_revision`. It must not clear constraints or
touch other partition items.

For removal, selecting a Remove operation transitions the existing profile
workflow into `AWAITING_PROFILE_INPUT` and renders a revision-stamped numbered
selection instead of a text prompt. Selection callbacks identify the category,
one-based displayed index, and rendered profile revision. The handler performs
a consistent read, rejects stale revisions or invalid indices, resolves the
selected entry using the same deterministic ordering as the renderer, and uses
the existing atomic profile/state transaction. On success it retains the
remove-selection state and renders the refreshed list, allowing consecutive
removals. Add and numeric family-change operations remain text-based.

## Technical Details

- Keep `DietaryPreferenceEntry.rule: DietaryRule` required. Add a
  complete-profile-only normalization step that filters raw strings and mapping
  entries with an absent or null `rule` before Pydantic validates the list.
  Structurally present but invalid non-null rules must still raise, so corruption
  is not silently broadened beyond the known migration shape.
- Repository diagnostics must not include preference source text or raw profile
  payloads. User-facing validation failures should produce a bounded recovery
  message instead of escaping the Lambda request and leaving Telegram silent.
- Extend `ProfileCallback` with a removal-selection action and bounded integer
  fields for one-based index and non-negative profile revision. Use a compact
  callback wire format whose worst case stays within 64 UTF-8 bytes.
- Define one deterministic selection projection shared by handler and renderer:
  family members in stored order; constraints in stored order; and the
  preference category as dietary preference entries followed by batch rules,
  each with a visible type label. The callback index is never trusted without
  verifying the rendered profile revision against a consistent current read.
- Keep the conversation state in the existing profile-edit workflow. A
  successful removal increments both profile and state revisions atomically;
  stale callbacks, missing profiles, empty lists, and invalid indices do not
  mutate either record.
- Render `/profile` constraints, dietary preferences, and batch rules as
  numbered dot-separated lines. Removal keyboards use number-only buttons,
  wrap buttons into bounded rows, and retain Back, Done, and Close navigation.
- Change the one-time script from a full dietary reset into malformed-preference
  repair. Its CLI continues requiring explicit table, AWS profile, region, and
  user ID and rejects wildcard/broad targets.

## What Goes Where

- **Implementation Steps:** schema compatibility, repository diagnostics,
  targeted repair, callback contract, Telegram rendering, handler workflow,
  tests, and repository documentation.
- **Post-Completion:** deploy the compatibility code, invoke the exact-user
  repair, inspect bounded CloudWatch results, and manually verify the Telegram
  workflow in the development bot.

## Implementation Steps

### Task 1: Add narrow saved-profile compatibility for null rules

**Files:**
- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`

- [x] write failing schema tests for raw-string, missing-rule, and null-rule
  legacy preferences in complete saved profiles
- [x] write failing mixed-list tests proving valid structured preferences and
  all constraints survive while only known legacy entries are discarded
- [x] write failing strict-input tests proving `ProfileUpdateEntities` and
  malformed non-null structured rules remain rejected
- [x] implement complete-profile-only filtering without making
  `DietaryPreferenceEntry.rule` optional
- [x] add bounded repository diagnostics for a profile read that discarded
  legacy preferences, without logging user content
- [x] write repository tests for successful compatible reads and bounded logs
- [x] run `uv run pytest tests/test_schemas.py tests/test_dynamo.py`; it must pass
  before Task 2

### Task 2: Narrow the exact-user repair to malformed preferences

**Files:**
- Modify: `scripts/reset_profile_dietary_fields.py`
- Modify: `tests/test_reset_profile_dietary_fields.py`
- Modify: `README.md`

- [x] write failing repair tests for profiles containing only legacy entries
  and profiles mixing valid structured and malformed preferences
- [x] write failing preservation tests for dietary constraints, valid
  preferences, family/nutrition fields, unknown profile attributes, and all
  related user partition items
- [x] write failing safety tests for exact-key access, idempotency, conditional
  races, missing profiles, unrecoverable profile corruption, broad targets, and
  redacted AWS failures
- [x] change the repair classifier to drop only raw strings and mapping entries
  with missing/null rules, then validate the cleaned complete profile
- [x] conditionally update only `dietary_preferences` and `profile_revision` and
  report retained/removed counts without profile contents
- [x] update the CLI naming, help, and README example so the command's narrowed
  semantics and deploy-before-repair ordering are explicit
- [x] run `uv run pytest tests/test_reset_profile_dietary_fields.py`; it must
  pass before Task 3

### Task 3: Define bounded revision-stamped removal callbacks

**Files:**
- Modify: `src/meal_planner/router.py`
- Modify: `tests/test_router.py`

- [x] write failing parser/model tests for valid family, constraint, and
  preference removal-selection callbacks
- [x] write failing tests for missing fields, extra fields, invalid categories,
  zero/negative/oversized indices, negative revisions, malformed integers, and
  payloads over 64 bytes
- [x] add the removal-selection action and bounded index/profile-revision fields
  to the typed callback contract
- [x] implement one compact exact wire format and preserve every existing
  profile callback format
- [x] assert generated worst-case supported callbacks remain below Telegram's
  byte limit
- [x] run `uv run pytest tests/test_router.py`; it must pass before Task 4

### Task 4: Render deterministic numbered profiles and selection keyboards

**Files:**
- Modify: `src/meal_planner/telegram/api.py`
- Modify: `tests/test_telegram_api.py`

- [x] write failing renderer tests for numbered constraints, dietary
  preferences, batch rules, and family removal entries in stored order
- [x] write failing tests for combined preference/batch type labels, empty
  categories, duplicate display text, button wrapping, navigation controls, and
  bounded callback payloads
- [x] introduce one deterministic presentation shape used to render labels and
  resolve category-relative selection order without exposing persisted IDs
- [x] update `/profile` to use numbered dot-separated rule lines
- [x] update remove-operation rendering to show the numbered list and matching
  number-only buttons stamped with the profile revision
- [x] retain current text prompts for add and family target-change operations
- [x] run `uv run pytest tests/test_telegram_api.py`; it must pass before Task 5

### Task 5: Apply selected removals atomically and refresh the list

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_dynamo.py`

- [x] write failing handler tests for removal of a family member, constraint,
  dietary preference, and batch rule by numbered callback
- [x] write failing workflow tests for refreshed lists, consecutive removals,
  final empty states, duplicate display text, and unchanged add/change flows
- [x] write failing no-mutation tests for stale profile revisions, stale
  conversation state, invalid/out-of-range indices, missing profiles, and the
  last remaining family member
- [x] render a consistent profile snapshot when a Remove operation is selected
  while retaining the existing awaiting-input conversation state contract
- [x] resolve selections against the current revision-matched deterministic
  projection and build the immutable updated profile
- [x] extend the guarded profile/state transaction usage so successful removals
  retain removal mode, increment both revisions, and allow a refreshed list
- [x] acknowledge callbacks and return clear stale/empty/success responses
  without exposing stored values in errors
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py`; it must
  pass before Task 6

### Task 6: Contain profile validation failures at the bot boundary

**Files:**
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] write failing command, callback, and conversational-update tests proving a
  residual profile `ValidationError` still produces a Telegram reply
- [x] write failing tests proving diagnostics are bounded and do not contain raw
  profile data or validation input values
- [x] add one request-boundary profile-load failure path that returns a concise
  recovery message while preserving normal routing and callback acknowledgement
- [x] keep unexpected infrastructure and programming failures distinguishable
  in logs without broad exception-driven data repair
- [x] run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 7
- ⚠️ Full-suite verification still reports two stale `.aws-sam` artifact
  mismatches for `src/meal_planner/bot_handler.py`; rebuilding those artifacts
  is outside Task 6's permitted file scope. The focused handler suite passes.

### Task 7: Verify acceptance criteria and repository quality gates

**Files:**
- Modify: `docs/plans/2026-08-27-restore-profile-updates-and-add-numbered-removal-buttons.md`

- [x] verify valid preferences, all constraints, unrelated profile attributes,
  and related DynamoDB items are preserved by compatibility and repair paths
- [x] verify new writes cannot persist a preference without a valid structured
  rule
- [x] verify every numbered removal category handles success, empty, stale,
  duplicate-label, and invalid-selection cases without deleting the wrong item
- [x] verify all callback payloads fit Telegram's 64-byte contract
- [x] run `uv run ruff format --check .`
- [x] run `uv run ruff check .`
- [x] run `uv run mypy`
- [x] run the full suite with `uv run pytest`

Verification results (2026-08-27):

- Compatibility and repair preservation criteria passed through the schema,
  repository, and targeted repair regression tests. Valid preferences,
  constraints, unrelated profile attributes, and related partition items were
  retained; only known malformed preferences were removed.
- Strict-write criteria passed: raw, missing-rule, null-rule, and malformed
  non-null preference inputs remain rejected.
- Numbered-removal criteria passed through handler, repository, and Telegram
  API tests for family members, constraints, dietary preferences, and batch
  rules, including successful refreshes, empty states, stale revisions,
  duplicate labels, invalid indices, and no-mutation protections.
- Callback-size criteria passed through router and Telegram API assertions;
  generated callbacks remain below Telegram's 64-byte limit.
- `uv run ruff format --check .` — passed; 110 files already formatted.
- `uv run ruff check .` — passed; all checks passed.
- `uv run mypy` — passed; no issues in 20 source files.
- `uv run pytest` — passed; 1,871 passed, 2 warnings in 18.37s.
- The first full-suite run exposed only stale ignored `.aws-sam` copies of
  `bot_handler.py`. Refreshed ignored artifacts with
  `uvx --from aws-sam-cli sam build --beta-features`, then reran every gate
  successfully.
- ⚠️ Unresolved risks: deployment, live AWS repair, CloudWatch inspection,
  and manual Telegram smoke testing remain intentionally unverified external
  steps. The suite retains two existing Pydantic serializer warnings.

### Task 8: Finalize documentation and archive the plan

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-08-27-restore-profile-updates-and-add-numbered-removal-buttons.md`
- Move to: `docs/plans/completed/2026-08-27-restore-profile-updates-and-add-numbered-removal-buttons.md`

- [x] update `/profile` documentation with numbered removal behavior and stale
  button recovery
- [x] document the one-user repair command, its exact preservation/deletion
  semantics, deploy-before-repair ordering, and safe retry behavior
- [x] record focused and full verification results in this plan
- [x] confirm no new engineering convention requires an `AGENTS.md` update
- [x] move the completed plan into `docs/plans/completed/`

Task 8 completion record (2026-08-27):

- README now documents one-based, number-only removal buttons for every
  removable profile category, the combined dietary-preference/batch-rule
  ordering and labels, revision-stamped stale-button rejection, and recovery
  by reopening `/profile`. Add and change operations remain text-based.
- README documents the explicit one-user repair command, exact-key consistent
  read, known malformed-shape deletion rules, complete-profile validation,
  preservation guarantees, deploy-before-repair ordering, no-scan behavior,
  idempotent retry, and conditional-conflict/AWS-failure safety.
- Focused verification passed:
  - `uv run pytest tests/test_schemas.py tests/test_dynamo.py` — 427 passed.
  - `uv run pytest tests/test_reset_profile_dietary_fields.py` — 10 passed.
  - `uv run pytest tests/test_router.py` — 125 passed.
  - `uv run pytest tests/test_telegram_api.py` — 44 passed.
  - `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py` — 551
    passed, with 2 existing warnings.
  - `uv run pytest tests/test_bot_handler.py` — 393 passed, with 2 existing
    warnings.
- Final quality verification passed:
  - `uv run ruff format --check .` — passed; 110 files already formatted.
  - `uv run ruff check .` — passed.
  - `uv run mypy` — passed; no issues in 20 source files.
  - `uv run pytest` — 1,871 passed, 2 warnings in 18.37s.
  - The initial full-suite stale `.aws-sam` artifact mismatches were resolved
    with `uvx --from aws-sam-cli sam build --beta-features` before the final
    successful run. The two remaining Pydantic serializer warnings are
    pre-existing.
- The only applicable repository instruction file remains `AGENTS.md`; this
  documentation/archive work introduced no new engineering convention, so no
  AGENTS.md update is required.
- Deployment, live AWS repair, CloudWatch inspection, and manual Telegram
  smoke testing remain external post-completion steps and were not run.

## Post-Completion

**Deployment and targeted repair:**

1. Deploy the compatibility and numbered-removal code to the development bot.
2. Invoke the repair command for the one explicit user and development table;
   do not use a scan or broad selector.
3. Confirm the command reports the expected removed/retained counts and advanced
   profile revision. A second invocation should report an unchanged no-op.
4. Check CloudWatch for bounded repair/read diagnostics and confirm no further
   `dietary_preferences.*.rule` validation failures occur.

**Manual Telegram verification:**

- Send `/profile` and confirm the bot replies and shows numbered constraints,
  preferences, and batch rules.
- Remove a constraint, dietary preference, and batch rule using number buttons;
  confirm the correct item disappears and the refreshed list remains active.
- Reuse an old removal button after another amendment and confirm it is rejected
  as stale without deleting anything.
- Remove entries until a category is empty and confirm Back/Done/Close still
  work.
- Add a new constraint and preference, complete their confirmation flows, and
  verify new structured entries survive a fresh `/profile` load.

**External systems:**

- Deployment, the exact-user DynamoDB repair, CloudWatch inspection, and live
  Telegram smoke testing require the development AWS/Telegram environment and
  are intentionally not automated by repository tests.
