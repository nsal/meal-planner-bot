# Remediate Numbered Profile Removal Review Findings

## Overview

Correct three defects found in the independent review of numbered profile
removal:

- render each refreshed removal keyboard from the profile snapshot actually
  committed by DynamoDB;
- prevent a numbered constraint removal from deleting unrelated dietary
  preferences; and
- reject conversational text while a numbered removal operation is active.

The remediation keeps revision-guarded callbacks and the existing atomic
profile/state transaction. It narrows the removal path so that one selected
item is the only profile content removed, and it makes buttons the sole input
mechanism while removal mode is active.

## Context (from review)

- `DynamoRepository.remove_profile_item_and_transition_state` currently
  delegates to `save_profile_and_transition_state`, which applies generic
  amendment preparation before writing.
- Generic preparation canonicalizes and deduplicates dietary preferences. The
  handler currently ignores that committed representation and constructs its
  refreshed view from the pre-commit `updated` profile.
- Generic constraint preparation also removes preferences that conflict with
  any remaining constraint. That behavior is appropriate for constraint-add
  amendments but is not appropriate when removing one constraint.
- `BotHandler._handle_profile_edit_input` still routes text entered during a
  `REMOVE` operation through `_apply_profile_amendment`, preserving the retired
  exact-text removal path.
- Existing handler tests commonly mock the repository transaction. The missing
  regression coverage must exercise the real moto-backed `DynamoRepository`
  where repository canonicalization and transaction behavior are observable.
- The executor's final gates passed before review: Ruff formatting and linting,
  mypy, and 1,871 pytest tests. Two pre-existing Pydantic serializer warnings
  remain outside this remediation.

## Development Approach

- **Testing approach:** TDD. Add each regression test first and record the
  expected failure against the reviewed implementation.
- Complete tasks in order. Do not begin a later task until the focused tests
  for the current task pass.
- Keep the existing callback wire format, deterministic presentation
  projection, conversation state shape, and DynamoDB revision guards.
- Treat numbered removal as a distinct persistence operation. Do not weaken or
  change the generic add/amendment conflict guards.
- Preserve all profile fields other than the selected item and the required
  `profile_revision` increment.
- Keep the plan synchronized during implementation and mark an item `[x]` only
  after its behavior and tests are complete.

## Testing Strategy

- **Repository transaction tests:** use moto-backed DynamoDB to assert the
  returned committed profile, persisted profile, state transition, revision
  increments, stale no-op behavior, and exact preservation of unrelated data.
- **Handler/repository integration tests:** use the existing
  `real_profile_handler` fixture to exercise two consecutive callbacks through
  the real repository. Include duplicate valid preferences and multiple batch
  rules so any rendered/persisted projection drift is visible.
- **Handler input tests:** parameterize family, dietary-constraint, and combined
  dietary-preference/batch-rule removal categories. Assert typed input produces
  guidance and no profile or conversation-state mutation.
- **Regression gates:** run focused handler and repository tests after each
  task, then run Ruff, mypy, and the complete pytest suite.
- The project has no separate browser/UI end-to-end suite. Live Telegram and
  AWS checks remain post-completion work.

## Progress Tracking

- Mark completed items with `[x]` immediately.
- Add newly discovered work with a `➕` prefix.
- Record blockers or deviations with a `⚠️` prefix.
- Keep this plan active under `docs/plans/` until all remediation and final
  verification are complete.

## Solution Overview

Make the numbered-removal transaction return `UserProfile | None`, where a
profile value is the exact revision-incremented snapshot written atomically and
`None` means the guarded transaction did not commit. The handler will render
that returned snapshot instead of synthesizing a revision on its pre-commit
candidate. This creates one authoritative source for persisted ordering and
callback indices.

Separate removal persistence from `_prepare_guarded_profile`. The generic
amendment path will retain its canonicalization and constraint-conflict
semantics, while the removal transaction will validate its removal-specific
preconditions, increment the revision, and serialize the already validated
immutable profile candidate without deleting or rewriting unrelated profile
collections.

At the start of `_handle_profile_edit_input`, after validating the active state
fields and before pending-rule decoding, profile loading, interpretation, or
amendment logic, detect `ProfileEditOperation.REMOVE`. Return concise guidance
to use the numbered buttons and leave the removal state untouched.

## Technical Details

- Change `DynamoRepository.remove_profile_item_and_transition_state` from a
  boolean result to `UserProfile | None`. Return the committed snapshot only
  after `transact_write_items` succeeds; return `None` for invalid operation
  preconditions and conditional conflicts; continue raising unrelated AWS
  failures.
- Keep both DynamoDB writes in one transaction. Preserve the existing profile
  revision, conversation revision, workflow kind, step, category, operation,
  and creation-time conditions.
- Build the removal transaction's profile item from the validated removal
  candidate plus exactly one `profile_revision` increment. Do not invoke
  `_prepare_guarded_profile` or a canonicalization step that deduplicates or
  filters unrelated collections.
- In `BotHandler._handle_profile_removal_callback`, treat `None` as stale/no
  commit and pass the returned committed `UserProfile` directly to
  `send_profile_operation`.
- The combined preference category remains ordered as stored dietary
  preferences followed by batch rules. Consecutive callbacks must be generated
  from that same committed ordering and revision.
- Typed removal input must not call `_load_profile`, `_apply_profile_amendment`,
  rule interpretation, any repository write/transition method, or profile menu
  rendering. The existing removal keyboard and state remain available.

## What Goes Where

- `src/meal_planner/db/dynamo.py`: removal-specific transaction preparation and
  committed-profile return contract.
- `src/meal_planner/bot_handler.py`: consume the committed snapshot and reject
  conversational removal input.
- `tests/test_dynamo.py`: transaction return, preservation, conflict, and
  no-op coverage.
- `tests/test_bot_handler.py`: real-repository consecutive removal and typed
  input no-mutation coverage.
- This plan: implementation progress and final verification record only. No
  README or `AGENTS.md` change is expected because the documented numbered
  button behavior and repository conventions do not change.

## Implementation Steps

### Task 1: Render consecutive removals from the committed profile

**Review finding:** P1 — Refresh from the committed canonical profile before
issuing new buttons.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a failing repository test for
  `remove_profile_item_and_transition_state` proving a successful transaction
  returns the exact revision-incremented `UserProfile` stored in DynamoDB, while
  a stale/conditional failure returns `None` and leaves both records unchanged
- [x] add a failing moto-backed handler regression using
  `real_profile_handler`, duplicate valid dietary preferences, and at least two
  batch rules; remove the first batch rule, then invoke a second callback from
  the refreshed keyboard and assert the displayed second selection removes the
  displayed item
- [x] record the expected pre-fix failure: the first transaction may persist a
  canonicalized/deduplicated ordering while
  `_handle_profile_removal_callback` renders its local `updated` ordering at the
  same new revision, so the second callback removes a different item from the
  one displayed
- [x] change `remove_profile_item_and_transition_state` to return the committed
  `UserProfile` on success and `None` on validation or conditional failure,
  without changing its transaction guards or unrelated-error propagation
- [x] update `_handle_profile_removal_callback` to use the returned committed
  profile as the sole source for the success message's refreshed list,
  callback indices, and profile revision; remove the locally synthesized
  `refreshed` profile
- [x] update existing mocked transaction assertions and fixtures for the new
  `UserProfile | None` contract, including persistence-error and stale no-op
  paths
- [x] run `uv run pytest tests/test_dynamo.py::test_numbered_profile_removal_transaction_retains_remove_mode tests/test_dynamo.py::test_numbered_profile_removal_transaction_rejects_stale_profile_revision`; both must pass
- [x] run the new consecutive-removal integration test by exact node ID; it
  must pass before Task 2
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py`; it
  must pass before Task 2

**Acceptance criteria:**

- A successful removal transaction returns the same profile snapshot that a
  consistent repository read observes, including its incremented revision and
  persisted collection ordering.
- Every refreshed number button is generated from that returned snapshot.
- Two consecutive preference/batch callbacks cannot remove an item different
  from the label and number shown after the first callback.
- Conditional conflicts remain no-ops, and unrelated DynamoDB errors remain
  distinguishable.

### Task 2: Preserve unrelated profile data during constraint removal

**Review finding:** P1 — Prevent constraint removal from deleting unrelated
preferences.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a failing DynamoDB test with multiple constraints and a structurally
  valid preference that conflicts with a constraint not selected for removal;
  assert the transaction removes only the selected constraint
- [x] include duplicate valid preferences, batch rules, family members,
  nutrition targets, goals or other supported fields in the preservation
  fixture and assert their values and ordering remain unchanged
- [x] add or extend a real-repository handler test proving the numbered
  constraint callback persists exactly the selected removal, increments both
  revisions, retains remove mode, and renders the returned committed snapshot
- [x] record the expected pre-fix failure: delegation to
  `save_profile_and_transition_state` invokes `_prepare_guarded_profile`, whose
  dietary-constraint branch filters the unrelated conflicting preference
- [x] implement removal-specific profile preparation inside
  `remove_profile_item_and_transition_state`: validate the removal preconditions,
  increment `profile_revision`, and serialize the supplied validated immutable
  candidate without invoking generic amendment conflict cleanup or
  collection-deduplicating canonicalization
- [x] keep `save_profile_and_transition_state` and `_prepare_guarded_profile`
  unchanged for add and ordinary amendment workflows so their existing
  conflict handling remains intact
- [x] verify the new focused repository and real-handler tests pass by exact
  node ID
- [x] run `uv run pytest tests/test_dynamo.py -k 'profile and (transaction or removal or constraint)'`; it must pass
- [x] run `uv run pytest tests/test_bot_handler.py tests/test_dynamo.py`; it
  must pass before Task 3

**Acceptance criteria:**

- Removing one constraint changes only that constraint collection entry plus
  the required profile and state revisions.
- Preferences remain byte-for-byte equivalent at the model-dump level,
  including a valid preference that conflicts with a different remaining
  constraint and duplicate source text.
- Family data, batch rules, nutrition fields, and other unrelated profile data
  remain unchanged and in the same order.
- Generic add/amendment conflict guards keep their current behavior.
- Transaction conflicts do not partially modify either the profile or the
  conversation state.

### Task 3: Make numbered buttons the only removal input

**Review finding:** P2 — Reject conversational text while numbered removal
mode is active.

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] add a parameterized failing handler test for
  `ProfileEditCategory.FAMILY`, `DIETARY_CONSTRAINTS`, and
  `DIETARY_PREFERENCES` with an active `REMOVE` operation; use text that would
  previously match a family member, constraint, dietary preference, or batch
  rule
- [x] assert each case returns concise guidance to use the numbered buttons,
  leaves the existing `AWAITING_PROFILE_INPUT` removal state unchanged, and
  does not mutate the profile
- [x] assert the blocked path does not load the profile, call
  `_apply_profile_amendment`, invoke rule interpretation, write/transition
  repository state, send a profile category menu, or claim an item was removed
- [x] record the expected pre-fix failure: `_handle_profile_edit_input` reaches
  `_apply_profile_amendment`, performs exact-text removal, commits the profile,
  and exits removal mode
- [x] add an early `ProfileEditOperation.REMOVE` branch in
  `_handle_profile_edit_input` after state/category/operation validation and
  before pending-rule, profile-load, interpretation, or amendment logic
- [x] preserve all text-based `ADD` behavior and numeric family `CHANGE`
  behavior; add explicit regression assertions that those operations still
  reach their existing paths
- [x] run the new parameterized typed-removal test by exact node ID; it must
  pass
- [x] run `uv run pytest tests/test_bot_handler.py -k 'profile and (input or amendment or removal or change)'`; it must pass
- [x] run `uv run pytest tests/test_bot_handler.py`; it must pass before Task 4

**Acceptance criteria:**

- Conversational text cannot remove family members, constraints, dietary
  preferences, or batch rules while `REMOVE` is active.
- The user receives button-specific guidance and can continue using the same
  revision-stamped removal keyboard.
- Rejected text causes no profile write, state transition, LLM call, success
  message, or category-menu navigation.
- Add and family target-change text workflows remain unchanged.

### Task 4: Verify remediation acceptance criteria and quality gates

**Files:**

- Modify: `docs/plans/2026-08-27-restore-profile-updates-and-add-numbered-removal-buttons-review-remediation.md`

- [x] verify the new Dynamo-backed consecutive-removal test covers duplicate
  preferences, multiple batch rules, committed ordering, and both callbacks
- [x] verify the constraint-removal preservation test covers a preference that
  conflicts with a non-selected remaining constraint and all unrelated fields
- [x] verify typed removal input is a no-op for every removal category and that
  add/change regressions pass
- [x] run `uv run ruff format --check .`
- [x] run `uv run ruff check .`
- [x] run `uv run mypy`
- [x] run `uv run pytest`
- [x] record exact focused and full verification results, including any
  demonstrably pre-existing warnings or external limitations, in this plan
- [x] after all checks pass, move this plan to `docs/plans/completed/`

**Acceptance criteria:**

- All three independent review findings have a regression test that failed for
  the reviewed defect and passes with the remediation.
- Ruff formatting, Ruff linting, strict mypy, and the complete pytest suite all
  pass.
- No callback format, profile display ordering contract, add/change workflow,
  or generic amendment guard regresses.

**Final permitted Task 4 verification attempt (2026-08-27):**

- [x] Focused regressions: `uv run pytest -q tests/test_dynamo.py::test_numbered_profile_removal_transaction_retains_remove_mode tests/test_dynamo.py::test_numbered_profile_removal_transaction_rejects_stale_profile_revision tests/test_dynamo.py::test_numbered_constraint_removal_preserves_unrelated_profile_data tests/test_bot_handler.py::test_real_numbered_preference_removal_refreshes_from_committed_profile tests/test_bot_handler.py::test_real_numbered_constraint_removal_preserves_unrelated_profile_data tests/test_bot_handler.py::test_numbered_removal_rejects_conversational_text_without_mutation tests/test_bot_handler.py::test_profile_edit_text_paths_remain_enabled_outside_removal` — **11 passed in 1.99s**. The inspected fixtures cover duplicate preferences, multiple batch rules, committed ordering, both consecutive callbacks, the conflicting non-selected constraint, unrelated fields, all four typed removal categories, and ADD/CHANGE text paths.
- [x] Formatting: `uv run ruff format --check .` — **passed** on the
  follow-up check: all 111 files were already formatted.
- [x] Lint: `uv run ruff check .` — **passed** (`All checks passed!`).
- [x] Types: `uv run mypy` — **passed** (`Success: no issues found in 20 source files`).
- [x] Full suite: `uv run pytest` — **1,875 passed in 17.72s**. No warnings were reported in this run. No live AWS, Telegram, deployment, or CloudWatch verification was run; the plan explicitly identifies those as external post-completion checks and not prerequisites for this plan.

Task 4 completed after the follow-up formatting check; this plan was moved to
`docs/plans/completed/`.

## Post-Completion

**Manual and external verification:**

1. Deploy the remediated code to the development bot.
2. Exercise consecutive numbered preference/batch removals in Telegram and
   confirm each refreshed label matches the next selected item.
3. Exercise constraint removal on a repaired development profile and confirm
   unrelated preferences remain present.
4. Send text while each removal category is active and confirm the bot keeps
   the numbered keyboard available without changing the profile.
5. Inspect bounded CloudWatch diagnostics for transaction conflicts or profile
   validation failures without exposing stored profile content.

Live AWS repair, deployment, CloudWatch inspection, and Telegram smoke testing
remain external actions and are not prerequisites for creating this plan.
