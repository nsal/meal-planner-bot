# Remediate Profile Amendment Review Findings

## Overview

Remediate the release-blocking and follow-up findings from the review of the
profile amendment workflow. The work restores canonical onboarding, preserves
the distinction between unanswered and explicitly empty dietary constraints,
and makes each deterministic amendment atomic with its workflow transition.

The remediation also rejects ambiguous family-member names, acknowledges
Telegram profile callbacks before synchronous work, and aligns the documented
`/profile` command with the bot command catalogue. The completed workflow must
retain legacy data while passing the full repository verification gate.

## Context

- **Original plan:**
  `docs/plans/2026-08-20-profile-amendment-workflow.md`
- **Remediation issue:** GitHub issue #50, following original issue #49.
- **Current baseline:** Ruff and mypy pass; the full suite reports 747 passed,
  5 failed, and 2 skipped.
- **Release blocker:** `_update_profile()` still reads removed `allergies` and
  `restrictions` attributes from `ProfileUpdateEntities`.
- **Persistence model:** `UserProfile` and `ConversationState` share one
  DynamoDB table under separate sort keys. Existing repository transaction
  patterns can provide all-or-nothing profile/state writes.
- **Workflow model:** profile callbacks select a deterministic operation, then
  one text message mutates the profile and returns the state to its category
  menu.

## Review Findings Covered

- **P1:** Use canonical `dietary_constraints` throughout profile orchestration.
- **P1:** Preserve unanswered and explicit-empty semantics for legacy drafts.
- **Design simplification:** Remove profile revision/CAS machinery; the bot is
  the only supported writer and conversation-state CAS owns amendment races.
- **P2:** Commit profile mutations and workflow transitions atomically.
- **P2:** Reject ambiguous case-insensitive family-member names.
- **P2:** Acknowledge profile callbacks before database and Telegram rendering.
- **P3:** Keep README command text consistent with the command catalogue.

## Development Approach

- **Testing approach:** TDD. For every task, add or update the specified test
  first and observe the expected failure before changing implementation.
- Complete tasks in order. Tasks 1 and 2 restore canonical draft behavior;
  Task 3 removes unnecessary profile-revision machinery; Tasks 4 and 5 enforce
  deterministic member identity and atomic profile amendment writes.
- Keep canonical persisted output free of `allergies` and `restrictions` while
  retaining explicit compatibility tests for supported legacy input.
- Treat the active conversation state as the concurrency authority for profile
  amendments. The bot repository is the only supported profile writer; there
  are no independent administrative or external profile-write paths, and
  concurrent profile mutations outside the active amendment workflow are out
  of scope.
- Update this plan immediately if implementation scope changes. Mark each
  checklist item complete only after its required focused tests pass.
- Use `uv run` for all Python tools. Format only with Ruff at the configured
  80-column line length.
- Before delivery, run `uv run ruff format .`, `uv run ruff check .`,
  `uv run mypy`, and `uv run pytest`. Do not move either plan to `completed/`
  while any required check fails.

## Testing Strategy

- **Schema tests:** legacy normalization matrices, no-value phrases, and
  canonical output.
- **Repository tests:** unchanged legacy re-save retention and transactional
  profile/state writes guarded by the exact observed conversation state.
- **Handler tests:** canonical onboarding, case-insensitive member identity,
  ambiguous amendments, callback acknowledgement order, and the complete
  `/profile` workflow.
- **Documentation tests:** compare the `/profile` description in the README,
  `BOT_COMMANDS`, and `render_help()`.
- **Manual verification:** exercise Telegram callback timing and each profile
  edit category after all automated checks pass.

## Solution Overview

Use separate compatibility adapters for complete `UserProfile` records and
partial `ProfileUpdateEntities` drafts. A complete profile may default absent
constraints to an empty list, while a draft must retain `None` until the user
has answered. Legacy no-value phrases count as an explicit empty answer, not as
literal constraints.

Keep profile persistence simple: profiles do not carry a revision, and normal
profile saves replace the one profile owned by the user. Deterministic
amendment text uses a new repository transaction that writes the profile and
next conversation state together. Its state condition identifies the exact
active workflow instance through revision, `created_at`, workflow kind, step,
category, and operation, so duplicate input and cancelled or replaced states
cannot authorize an old edit. Expiry remains a state-load concern: input
accepted immediately before expiry may finish immediately after expiry.

Member identity is the stripped, casefolded name, so `Nick`, `NICK`, and
` nick ` identify the same person. New onboarding, family replacement, and
member addition reject duplicate identities. Persisted legacy duplicates
remain readable and can receive unrelated edits, but name-based amendment
actions fail with a controlled message instead of selecting the first match.

## Technical Details

### Constraint migration semantics

- If a canonical field is present, it remains authoritative.
- For `ProfileUpdateEntities`, no legacy keys or legacy keys containing only
  `None` mean unanswered and preserve `dietary_constraints=None`.
- An explicit empty list or recognized no-value phrase means answered with no
  constraints and normalizes to `[]`.
- Non-empty legacy values merge allergies first, then restrictions, remove
  recognized no-value phrases, and de-duplicate case-insensitively while
  preserving first spelling and order.
- `UserProfile` retains complete-record semantics: absent, `None`, empty, and
  no-value legacy inputs produce a valid canonical list.
- Canonical model dumps and re-saves omit both legacy attribute names.

### Transaction contract

Add a repository operation such as `save_profile_and_transition_state()` that
accepts the updated `UserProfile`, next `ConversationState`, and observed
profile-edit state. It must:

1. write the updated profile;
2. write the next state only if the exact observed profile-edit state still
   owns the operation;
3. commit both writes or neither write;
4. return a controlled conflict result for conditional transaction failures;
5. re-raise unrelated DynamoDB failures for handler-level error reporting.

The state condition must include revision, `created_at`, `workflow_kind`,
`step`, selected category, and selected operation. This prevents duplicate or
stale input from committing after `/cancel`, `/plan`, `/submit_meals`,
`/profile`, or a replacement workflow with a reused numeric revision. No
profile condition is required because all supported profile mutations are
owned by the bot flow and an active amendment is serialized by its state.

## Implementation Steps

### Task 1: Preserve legacy constraint semantics for partial drafts

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`

**TDD sequence:**

- [x] **Test first:** Add a table-driven schema matrix for
  `ProfileUpdateEntities.normalize_legacy_constraints` covering absent legacy
  keys, both legacy fields set to `None`, mixed `None` and explicit empty,
  explicit no-value scalar phrases, non-empty scalar/list values, ordering,
  and case-insensitive de-duplication.
- [x] **Expected failure:** Confirm current validation turns both legacy
  `None` values into `[]` and retains phrases such as `"no allergies"` as
  literal constraints.
- [x] Add complete-profile cases proving `UserProfile` accepts equivalent
  legacy records with canonical complete-record semantics and emits only
  `dietary_constraints` from `model_dump()`.
- [x] Add DynamoDB round-trip tests for legacy `PROFILE_DRAFT` items and an
  unchanged legacy `PROFILE` re-save that retains every real constraint while
  removing legacy attributes.
- [x] Split `_normalize_legacy_constraints` into shared merge primitives and
  model-specific adapters so `ProfileUpdateEntities` preserves unanswered
  `None` while `UserProfile` produces a canonical list.
- [x] Normalize generic and field-specific legacy no-value phrases before
  merging, without treating real scalar constraints as no-value answers.
- [x] **Verify new tests pass:** Run
  `uv run pytest tests/test_schemas.py tests/test_dynamo.py`.
- [x] **Run regressions:** Run
  `uv run pytest tests/test_prompts.py tests/test_bot_handler.py` and confirm
  only the already-known canonical orchestration failures remain before
  Task 2.

**Acceptance criteria:**

- [x] A legacy partial draft with no known constraint answer remains
  `dietary_constraints=None`.
- [x] Explicit empty/no-value answers become `[]`; real legacy values retain
  order and first spelling without duplicates.
- [x] Complete legacy profiles remain readable and unchanged canonical re-save
  retains all real dietary-safety information.
- [x] Canonical dumps and persisted writes contain neither `allergies` nor
  `restrictions`.

### Task 2: Restore canonical onboarding and conversational profile updates

**Severity:** P1

**Depends on:** Task 1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_prompts.py` if `/start` wording is asserted there

**Symbols:** `BotHandler._update_profile`, start/onboarding prompt rendering,
`ProfileUpdateEntities`

**TDD sequence:**

- [x] **Test first:** Convert the five failing onboarding/existing-profile
  regression cases to canonical `dietary_constraints` input and assertions;
  add a canonical end-to-end onboarding case from empty draft to saved profile.
- [x] **Expected failure:** Confirm each valid update reaches the removed
  `draft.allergies`/`draft.restrictions` access and raises `AttributeError`.
- [x] Add explicit handler compatibility tests proving supported legacy
  `allergies`/`restrictions` entity input is normalized and saved only as
  `dietary_constraints`, including legacy no-value answers.
- [x] Replace the legacy required-field tuple and missing-label map in
  `_update_profile()` with one `dietary_constraints` requirement.
- [x] Update `/start`, onboarding follow-up text, and related test assertions
  to use canonical dietary-constraint terminology.
- [x] Ensure incomplete drafts remain persisted and complete canonical drafts
  save successfully without reading removed attributes.
- [x] **Verify new tests pass:** Run the five previously failing tests plus all
  newly added canonical and legacy compatibility cases.
- [x] **Run regressions:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_schemas.py \
  tests/test_prompts.py`.

**Acceptance criteria:**

- [x] The five baseline onboarding/profile-update failures are green.
- [x] New onboarding requires exactly one canonical constraint answer and does
  not ask separately for allergies and restrictions.
- [x] Supported legacy entity input remains backward compatible but cannot
  create legacy persisted fields or legacy prompt labels.
- [x] Incomplete and complete draft behavior remains deterministic.

### Task 3: Remove profile revision machinery

**Severity:** P2 simplification

**Depends on:** Tasks 1 and 2

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `UserProfile`, `DynamoRepository.save_profile`, profile save call
sites

**TDD sequence:**

- [x] **Test first:** Update schema and repository tests to define profiles as
  single documents without a revision and normal saves as direct replacements.
- [x] Add a compatibility test proving an old persisted `revision` attribute is
  ignored on read and omitted by the next canonical profile save.
- [x] Remove `UserProfile.revision`, the optional `expected_revision` parameter,
  conditional profile writes, and handler branches for profile CAS conflicts.
- [x] Update factories and handler/repository assertions to use the simplified
  profile persistence contract.
- [x] **Verify new tests pass:** Run
  `uv run pytest tests/test_schemas.py tests/test_dynamo.py \
  tests/test_bot_handler.py`.

**Acceptance criteria:**

- [x] Profiles contain no application revision field after canonical save.
- [x] Legacy items carrying a revision remain readable during migration.
- [x] The repository exposes one straightforward profile replacement path.
- [x] Conversation-state revisions remain unchanged and continue protecting
  workflow transitions.

### Task 4: Enforce unique case-insensitive member identity

**Severity:** P2

**Depends on:** Task 2

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._apply_profile_amendment`,
`BotHandler._update_profile`

**TDD sequence:**

- [x] **Test first:** Add onboarding and family-replacement tests with `Nick`,
  `NICK`, and whitespace variants, proving they identify the same person.
- [x] **Expected failure:** Confirm duplicate names can currently be supplied,
  while remove/change-calorie amendments select the first legacy match.
- [x] Add amendment tests for a legacy loaded profile containing ambiguous
  names; assert remove and calorie-change actions return a controlled migration
  message and perform no profile/state write.
- [x] Add positive regressions proving exact case-insensitive matching remains
  available when exactly one member matches and additions preserve display
  spelling.
- [x] Introduce one shared stripped, casefolded member-identity helper; reject
  duplicates during onboarding, family replacement, and member addition.
- [x] Reject name-based amendments when more than one persisted member matches;
  do not silently select a member or auto-rename legacy data.
- [x] Permit unrelated constraint, preference, and goal amendments on readable
  legacy profiles that contain duplicate member names.
- [x] **Verify new tests pass:** Run focused onboarding, family replacement,
  and amendment duplicate-name tests.
- [x] **Run regressions:** Run `uv run pytest tests/test_bot_handler.py`.

**Acceptance criteria:**

- [x] New onboarding, family replacement, and addition reject duplicate member
  identities before persistence.
- [x] Legacy duplicate profiles remain readable for controlled remediation.
- [x] Remove and calorie-change operations never choose arbitrarily among
  ambiguous matches.
- [x] Valid unique member edits and `people_count` invariants still pass.

### Task 5: Commit profile amendments and state transitions atomically

**Severity:** P2

**Depends on:** Tasks 3 and 4

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** new `DynamoRepository.save_profile_and_transition_state`,
`BotHandler._handle_profile_edit_input`

**TDD sequence:**

- [x] **Test first:** Add real DynamoDB transaction tests for a matching
  profile/state pair, cancelled/deleted state, replacement workflow, changed
  operation, duplicate input, and unrelated DynamoDB failure.
- [x] **Expected failure:** Demonstrate the current two-write sequence persists
  the profile before a failed state transition, so a cancelled or replaced
  workflow can still change user data.
- [x] Assert every state conflict leaves both the profile and the replacement
  or cancelled workflow exactly unchanged; include a replacement state with a
  reused numeric revision to exercise `created_at` workflow identity.
- [x] Add a handler-level full repository sequence for
  `/profile -> Amend -> category -> operation -> text -> category -> Done`,
  asserting one profile write, correct state transitions, and no LLM call.
- [x] Add handler/repository race tests for `/cancel`, `/plan`,
  `/submit_meals`, `/profile`, and stale or duplicate input before commit;
  assert no profile mutation and no replacement-state mutation. Keep expiry
  validation covered at state load rather than inside the transaction.
- [x] Implement `save_profile_and_transition_state()` with one DynamoDB
  transaction and an exact observed profile-edit state condition containing
  revision, `created_at`, workflow kind, step, category, and operation.
- [x] Replace the separate `save_profile()` and
  `transition_conversation_state()` calls in
  `_handle_profile_edit_input()` with the transaction and remove partial-commit
  success/error messages.
- [x] Map conditional cancellation to a controlled stale-menu response and
  preserve explicit handling for unexpected persistence failures.
- [x] **Verify new tests pass:** Run focused transaction, race, and full-flow
  tests.
- [x] **Run regressions:** Run
  `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py \
  tests/test_router.py tests/test_telegram_api.py`.

**Acceptance criteria:**

- [x] A successful amendment writes the profile and transitions the exact
  owning workflow in one transaction.
- [x] Any cancellation, replacement, duplicate input, or workflow conflict
  commits neither write.
- [x] A reused state revision cannot authorize input from an older workflow
  instance.
- [x] The real repository-backed profile flow completes through Done without
  invoking the LLM.
- [x] No response claims the profile changed when its menu transition failed.

### Task 6: Acknowledge profile callbacks before synchronous work

**Severity:** P2

**Depends on:** Task 5

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._handle_profile_callback`, profile callback dispatch

**TDD sequence:**

- [x] **Test first:** Add ordered-call tests proving a valid profile callback
  is acknowledged after basic route/payload validation but before state reads,
  state writes, and all menu/message rendering.
- [x] **Expected failure:** Confirm the current `finally` block acknowledges
  only after DynamoDB and `send_message`/menu calls complete.
- [x] Add ordering tests for successful rendering, stale/expired state,
  repository exceptions, Telegram delivery failures, and acknowledgement
  failure; assert exactly one acknowledgement attempt per callback.
- [x] Move acknowledgement to the start of validated profile callback handling
  and use `Processing profile action`, which does not depend on later work.
- [x] Remove the late duplicate acknowledgement path. Continue processing when
  acknowledgement itself fails, and report later operational failures through
  ordinary messages when possible.
- [x] Retain parser/route behavior for malformed callbacks and missing callback
  IDs without attempting unsafe persistence.
- [x] **Verify new tests pass:** Run the focused callback ordering and failure
  tests.
- [x] **Run regressions:** Run
  `uv run pytest tests/test_bot_handler.py tests/test_router.py \
  tests/test_telegram_api.py`.

**Acceptance criteria:**

- [x] Every valid profile callback is acknowledged once before synchronous
  database or Telegram rendering work.
- [x] Slow or failed rendering cannot delay a callback acknowledgement.
- [x] Acknowledgement failure does not cause duplicate profile writes or
  suppress controlled menu error messages.
- [x] Expired, stale, and malformed callback behavior remains covered.

### Task 7: Align `/profile` documentation and command descriptions

**Severity:** P3

**Depends on:** Tasks 2 and 6

**Files:**

- Modify: `README.md`
- Modify: `src/meal_planner/telegram/commands.py`
- Modify: `tests/test_readme.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BOT_COMMANDS`, `render_help`

**TDD sequence:**

- [x] **Test first:** Add a consistency test that extracts the documented
  `/profile` command description and compares it with `BOT_COMMANDS` and the
  line rendered by `render_help()`.
- [x] **Expected failure:** Confirm README says `View and amend` while the bot
  command catalogue/help says only `View the household profile`.
- [x] Choose one user-facing description, preferably `View and amend the
  household profile`, and apply it consistently to `BOT_COMMANDS`, `/help`, and
  the README command reference.
- [x] Retain the detailed README amendment examples and canonical
  dietary-constraint terminology.
- [x] **Verify new tests pass:** Run
  `uv run pytest tests/test_readme.py tests/test_bot_handler.py`.
- [x] **Run regressions:** Run all command/help/README tests and confirm every
  command remains present exactly once.

**Acceptance criteria:**

- [x] README, Telegram command menu, and `/help` describe `/profile`
  consistently.
- [x] The wording accurately communicates both viewing and amendment.
- [x] Existing command ordering and all non-profile descriptions are unchanged.

### Task 8: Verify release acceptance criteria

**Files:**

- Modify: `docs/plans/2026-08-20-remediate-profile-amendment-review-findings.md`
- Modify: `docs/plans/2026-08-20-profile-amendment-workflow.md`

**TDD sequence:**

- [x] **Test first:** Before remediation begins, preserve the failing baseline
  and named tests demonstrating each P1/P2/P3 finding in the task notes.
- [x] **Expected failure:** Record the initial full-suite result of 747 passed,
  5 failed, and 2 skipped, including the five canonical onboarding failures.
- [x] Verify every review finding has a corresponding regression test and all
  new tests pass without xfail, skip, or order dependence.
- [x] Run `uv run ruff format .` and confirm no non-Ruff formatter was used.
- [x] Run `uv run ruff check .` and resolve all diagnostics.
- [x] Run `uv run mypy` and resolve all strict typing errors.
- [x] Run `uv run pytest` and require the complete suite to pass.
- [x] Run `git diff --check` and inspect the complete accumulated diff for
  accidental implementation or documentation regressions.
- [x] **Verify new tests pass:** Re-run all newly added remediation tests as one
  focused command after the full suite.
- [x] **Run regressions:** Re-run `uv run pytest` after any formatting or final
  documentation adjustment.

**Task 8 verification notes (2026-08-21):**

- The preserved pre-remediation baseline was 747 passed, 5 failed, and 2
  skipped; the five failures were the canonical onboarding/profile update
  regressions caused by removed legacy entity attributes.
- Finding coverage is provided by schema and DynamoDB legacy-draft,
  canonical-output, revisionless-save, and transaction-conflict tests;
  handler onboarding, identity-ambiguity, atomic-flow, stale/replacement,
  callback-ordering/failure, and no-partial-success tests; and the README,
  command-catalogue, and help consistency test.
- `uv run ruff format .`: 79 files left unchanged. No non-Ruff formatter was
  used.
- `uv run ruff check .`: passed. `uv run mypy`: passed. `git diff --check`:
  passed.
- `uv run pytest`: 800 passed, 2 skipped. The two skips are pre-existing
  unrelated template checks; no remediation test is skipped or xfailed.
- Focused remediation regressions, run together after the full suite, passed:
  `uv run pytest tests/test_schemas.py tests/test_dynamo.py
  tests/test_bot_handler.py tests/test_readme.py tests/test_prompts.py
  tests/test_telegram_commands.py tests/test_router.py tests/test_telegram_api.py
  -q` — 467 passed.
- The complete accumulated diff was inspected. No accidental implementation
  or documentation regressions were found; Tasks 1–7 and all pre-existing
  files were preserved.

**Acceptance criteria:**

- [x] All retained review findings are remediated and covered by tests; profile
  revision findings are closed by the documented single-writer design decision.
- [x] The five baseline failures are fixed, and the full suite is green.
- [x] Ruff format, Ruff lint, mypy, pytest, and `git diff --check` all pass.
- [x] No legacy dietary field is emitted by canonical prompts or persistence.
- [x] No cancelled, stale, or replaced profile workflow can mutate a profile.

### Task 9: Complete documentation and delivery tracking

**Files:**

- Modify: `README.md` if final verification finds a user-facing gap
- Modify: `docs/plans/2026-08-20-profile-amendment-workflow.md`
- Move: `docs/plans/2026-08-20-remediate-profile-amendment-review-findings.md`
  to `docs/plans/completed/`
- Move: `docs/plans/2026-08-20-profile-amendment-workflow.md` to
  `docs/plans/completed/`

**TDD sequence:**

- [x] **Test first:** Run README and command consistency tests before any final
  documentation edit. `uv run pytest tests/test_readme.py
  tests/test_telegram_commands.py -q` passed with 16 tests.
- [x] **Expected failure:** The consistency tests were already green, so there
  was no stale assertion or mismatch to record and no unnecessary README edit
  was made.
- [x] Update both plans with actual task outcomes, verification commands,
  deviations, blockers, and retained manual-only checks. The original plan's
  historical blockers are replaced with the remediation outcomes and the
  manual Telegram checks remain explicitly outstanding.
- [x] Update README or `AGENTS.md` only if implementation introduced a genuine
  user-facing behavior or reusable repository convention not already covered.
  No final README or `AGENTS.md` change was needed; the Task 7 README change
  already documents the delivered `/profile` behavior.
- [x] **Verify new tests pass:** Re-run
  `uv run pytest tests/test_readme.py tests/test_bot_handler.py` after the
  documentation-only tracking update.
- [x] **Run regressions:** Re-run the complete verification gate from Task 8:
  Ruff format, Ruff lint, mypy, the full pytest suite, and `git diff --check`.
- [x] Move both plans to `docs/plans/completed/` only after every authorized
  documentation and repository check is green.
- [x] Create a Conventional Commit referencing issue #50 on the existing
  feature branch; no push or merge to `master` was performed.
- [x] Comment on issue #50 with the completed work and commit link.

**Task 9 verification notes (2026-08-21):**

- README and command consistency tests passed before this tracking update;
  no README mismatch was found.
- The post-update focused README/handler regression command and the complete
  verification gate are recorded in the execution report. The final gate was
  green: 79 files unchanged by Ruff format, Ruff lint passed, mypy passed for
  19 source files, `uv run pytest` passed with 800 tests and 2 unrelated
  template skips, and `git diff --check` passed.
- No manual Telegram verification was performed in this execution. The
  callback timing, every profile category/operation, stale-workflow races,
  invalid input, and legacy unchanged re-save checks remain manual-only
  follow-ups listed below.
- Commit `fix(profile): remediate amendment review findings (#50)` was created
  on the existing feature branch; no push, pull request, or merge was
  performed.

**Acceptance criteria:**

- [x] Documentation describes the behavior that passed automated verification.
- [x] Both plans accurately reflect completed work and are moved only after a
  green authorized delivery gate.
- [x] Version-control and issue updates follow `AGENTS.md` requirements.

## Post-Completion

### Manual Telegram verification

- Open `/profile` and confirm the callback spinner clears immediately for root,
  category, operation, Back, Done, and Close actions.
- Complete one successful edit in every category and one controlled invalid
  input without invoking conversational LLM parsing.
- Start an amendment, replace it with `/plan`, `/submit_meals`, and `/profile`,
  and confirm old text/callbacks cannot mutate the profile.
- Cancel an amendment immediately before sending its text and confirm neither
  the profile nor the replacement workflow changes.
- Verify a legacy account retains merged dietary constraints after an unchanged
  canonical re-save.

### External delivery

- Deploy only after the pull request is approved and all CI checks pass.
- Do not implement data auto-renaming for legacy duplicate family members;
  resolve those records explicitly with the user if encountered.
