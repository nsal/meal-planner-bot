# Remediate Dietary Profile Follow-Up Save Concurrency Findings

## Overview

Remediate the three actionable findings from the independent review of the
completed dietary-profile follow-up. The changes must make ordinary profile
saves honor the caller's observed create/update state, carry an existing
profile revision through ordinary bot updates, and retain the latest merged
profile draft after a controlled save conflict.

This work is limited to the ordinary profile-save and retry-draft path. The
review found the constraint, effective-rule identity, weekday-capacity, and
repair-location changes consistent with the preceding plan, so those areas
must remain unchanged.

## Context

- **Completed original plan:**
  `docs/plans/completed/2026-08-24-prioritize-dietary-profile-rules.md`.
- **First remediation plan:** the `docs/plans` plan ending in
  `review-remediation.md` for this feature.
- **Completed follow-up plan:** the `docs/plans` plan ending in
  `review-remediation-follow-up.md` for this feature.
- `DynamoRepository.save_profile` currently performs a strongly consistent
  read inside the repository and chooses create versus update from that late
  read instead of from the caller's observation.
- `BotHandler._update_profile` merges updates through
  `ProfileUpdateEntities`. That model intentionally contains editable profile
  fields but not `UserProfile.profile_revision`, so model reconstruction
  resets the revision unless the handler carries it separately.
- The controlled conflict branch saves a retry draft only when no persisted
  draft existed. When the request merged new input into an existing draft,
  this condition leaves the older stored draft in place.
- The executor ledger reports that Ruff format and lint, Mypy, the full
  1,209-test Pytest suite, and `git diff --check` passed after the six follow-up
  tasks. The suite retained two known Pydantic serializer warnings.
- The follow-up plan's final verification checkboxes remain unchecked. This
  remediation must not edit that plan merely to reconcile its checkboxes with
  the executor ledger.
- The worktree has overlapping pre-existing changes. Preserve all unrelated
  work and both preceding plans exactly.

## Review Findings Covered

1. **P1: Preserve create intent across the conditional save.**
   `src/meal_planner/db/dynamo.py:158` chooses creation or update from its own
   late read. If two onboarding requests both observed no profile and the
   second enters `save_profile` after the first commits revision zero, the
   second takes the update branch and overwrites the winner at revision one.
   Pass the caller's expected absence or revision into the repository and
   derive the write condition from that token. Cover the staggered creation
   race where both callers first observe absence but the losing save starts
   after the winning commit.
2. **P1: Carry the observed revision through ordinary profile updates.**
   `src/meal_planner/bot_handler.py:2725` rebuilds a `UserProfile` through
   `ProfileUpdateEntities`, which has no `profile_revision`. Existing-profile
   and persisted-draft completion therefore default to revision zero. After a
   profile reaches revision one, an otherwise valid ordinary update is always
   stale. Carry the observed revision separately and pass it explicitly to
   `save_profile`. Cover a real-repository bot update from revision one that
   succeeds and advances exactly once.
3. **P2: Persist the newly merged draft after a save conflict.**
   `src/meal_planner/bot_handler.py:2727` skips `save_profile_draft` when a
   persisted partial draft already exists. If the latest message completes or
   changes that draft and the profile save conflicts, the stored retry draft
   loses the newest input. Save the newly merged draft on every controlled
   conflict and verify that an existing partial draft is replaced by the
   latest merged fields.

The review otherwise accepted the constraint, rule-identity,
weekday-capacity, and repair-location changes. It was read-only and relied on
the executor's final-gate evidence. Its material limitation was that no
isolated follow-up baseline exists for overlapping dirty files, so attribution
outside the six original task areas is unreliable. The findings above are
directly tied to the ordinary-save work from follow-up Task 4.

## Scope and Constraints

- **In scope:** the ordinary profile-save expectation contract, propagation of
  an observed profile revision through bot draft completion, retry-draft
  persistence after a controlled conflict, and focused regressions for all
  three findings.
- **Out of scope:** dietary semantics, rule priority, weekday validation,
  planner repair feedback, profile-confirmation transactions, unrelated draft
  redesign, data backfills, deployment, and unrelated refactoring.
- Keep `ProfileUpdateEntities` limited to user-editable fields. Do not add the
  canonical profile revision to LLM-extracted or persisted user input merely
  to transport concurrency state.
- Preserve legacy profile items with no `profile_revision` as observed
  revision zero, without weakening stale-write rejection for canonical items.
- Map only DynamoDB conditional-check failure to the controlled `False`
  result. Re-raise all other DynamoDB errors.
- No new dependency is expected. Use `uv run`, full type hints, Ruff's
  configured 80-column format, strict Mypy, and the repository's Pytest suite.
- Do not modify either preceding remediation plan or the completed original
  plan. Record implementation-time scope changes only in this plan.

## Development Approach

- **Testing approach: TDD.** Add the smallest regression for a finding, run it
  against the current implementation, and record the expected failure before
  changing production code.
- Complete Task 1 and all of its focused regressions before Task 2.
- Keep the caller's observation token explicit. Do not use a repository read,
  the candidate document, or a default argument to infer whether a write is a
  creation or update.
- Keep each change focused on the reviewed profile path and preserve unrelated
  dirty work.
- If implementation uncovers an in-scope deviation, add a `⚠️` note here.
  Add newly required in-scope work with a `➕` checklist item.
- Mark an item `[x]` only after its implementation and tests pass.

## Testing Strategy

- **Repository concurrency:** use Moto-backed DynamoDB tests. Cover a
  staggered create race, ordinary update from an exact revision, a stale
  update, a legacy revisionless item, and a non-conditional client error.
- **Bot revision propagation:** use the existing real-repository bot fixture.
  Exercise both a direct existing-profile update and completion through a
  persisted draft after the profile has reached revision one.
- **Conflict retry state:** start with a persisted partial draft, merge fields
  from the latest message, force `save_profile` to return `False`, and inspect
  the exact draft passed to `save_profile_draft`.
- Tests must not use skips, xfails, live AWS, live Telegram, network calls, or
  live LLM requests.

## Solution Overview

Make the ordinary profile-save API require an explicit expectation supplied by
the caller: `None` means the caller observed no profile, while an integer means
the caller observed that exact canonical revision. `save_profile` will no
longer read the profile to decide which condition to use. It will write
revision zero only for expected absence and revision `N + 1` only for expected
revision `N`.

At the bot boundary, capture the existing profile's revision before merging
through `ProfileUpdateEntities`. Keep this concurrency value outside the draft
model, restore it when constructing the complete `UserProfile`, and pass the
same value to `save_profile`. A new onboarding profile passes expected absence.

On a controlled profile conflict, always persist the newly computed `draft`,
regardless of whether the request started from an existing partial draft. The
canonical profile remains untouched, the draft is not deleted, and the user
receives the existing bounded stale-state message.

## Technical Details

### Caller-observed save expectation

- Change `DynamoRepository.save_profile` to accept a required keyword such as
  `expected_revision: int | None`. The lack of a default is intentional:
  every caller must state whether it observed absence or a specific revision.
- For `expected_revision is None`, issue one conditional `put_item` requiring
  the profile key not to exist and write `profile_revision=0`.
- For integer revision `N`, require the profile key to exist and the stored
  revision to equal `N`, then write `profile_revision=N + 1`.
- For observed revision zero only, accept either a missing revision attribute
  on a legacy profile item or an explicit zero. Do not let a missing revision
  satisfy expectations for any later revision.
- Derive both the condition and next revision exclusively from the supplied
  expectation. Remove the late `get_item` from `save_profile`.
- Continue returning `False` only for a conditional-check failure and `True`
  after a successful write. Preserve propagation of all other client errors.

### Revision transport through bot draft completion

- In `BotHandler._update_profile`, capture
  `existing.profile_revision if existing is not None else None` before
  selecting or merging draft data.
- Continue validating and persisting only editable fields through
  `ProfileUpdateEntities`; do not serialize profile concurrency metadata into
  an LLM-facing draft.
- When the draft is complete, construct `UserProfile` with the captured
  revision for model consistency and pass that same revision as
  `expected_revision` to `save_profile`.
- Apply the same rule whether data begins from the existing profile, a
  persisted partial draft, or empty onboarding state.

### Latest retry draft on conflict

- Treat the local `draft` after applying the current message as the retry
  source of truth for this request.
- When `save_profile` returns `False`, call `save_profile_draft(user_id, draft)`
  even if `persisted_draft` was non-null.
- Do not delete the draft or send a success response after a controlled
  conflict. Preserve the latest canonical profile and the existing bounded
  stale-state feedback.
- Keep the current profile-draft write contract in this focused remediation.
  If a regression demonstrates a separate concurrent-draft overwrite, record
  that evidence as newly discovered scope before introducing a draft revision
  protocol; do not silently broaden `ProfileUpdateEntities`.

## Implementation Steps

### Task 1: Use caller-observed state for ordinary profile saves

**Severity:** P1

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `DynamoRepository.save_profile`, `BotHandler._update_profile`,
`UserProfile.profile_revision`, ordinary profile-save callers

**TDD sequence:**

- [x] **Failing test first:** Replace or extend the creation-race regression
  so two callers both observe absence before either save. Commit the winner,
  then start the loser's save with the same expected-absence token. Assert the
  loser returns `False`, cannot overwrite the winner, and cannot advance the
  winner to revision one.
- [x] **Failing test first:** Add a Moto-backed bot regression that creates a
  profile, advances it to revision one, then performs an ordinary profile
  update through `BotHandler._update_profile`. Assert the update succeeds,
  preserves unrelated fields, and writes revision two exactly once.
- [x] Add the same revision-one bot control when the latest message completes
  a persisted partial `ProfileUpdateEntities` draft. Assert draft merging does
  not reset the expected profile revision.
- [x] Add repository controls for expected absence, exact-revision success,
  stale-revision conflict, competing ordinary updates, and a revisionless
  legacy item observed as revision zero.
- [x] **Expected pre-fix failure:** Run the new tests and record that the
  staggered second creator sees the winner in the repository's late read and
  overwrites it as an update, while a revision-one bot update reconstructs a
  revision-zero profile and is rejected as stale.
- [x] **Implementation change:** Require the caller's expected absence or
  revision in `save_profile`, remove its decision-making `get_item`, and build
  the conditional expression and next revision only from the supplied token.
- [x] Capture the observed revision separately in `_update_profile`, restore
  it when constructing the complete `UserProfile`, and pass it explicitly to
  `save_profile` for existing-profile and persisted-draft paths. Pass expected
  absence for new onboarding.
- [x] Update every ordinary `save_profile` caller and affected test fixture to
  supply an explicit expectation. Do not change the guarded confirmation
  transaction.
- [x] Preserve the controlled `False` result for conditional failures and add
  or retain a test proving non-conditional DynamoDB errors are re-raised.
- [x] **Passing-test verification:** Run the new staggered-create and
  revision-one bot tests and require the winning document, fields, and
  revision to match exactly.
- [x] **Regression tests:** Run `uv run pytest tests/test_dynamo.py
  tests/test_bot_handler.py` and resolve every failure before Task 2.

**Acceptance criteria:**

- [x] A caller that observed absence cannot overwrite a profile created before
  its save begins.
- [x] The repository never infers create/update intent from a late profile
  read or from a defaulted candidate revision.
- [x] An ordinary bot update from revision one succeeds and advances to
  revision two exactly once through both direct and persisted-draft paths.
- [x] A stale ordinary update cannot overwrite a later canonical profile.
- [x] Creation, exact updates, legacy revision-zero records, conditional
  conflicts, and unexpected DynamoDB errors retain deterministic behavior.

### Task 2: Save the latest merged draft after a profile conflict

**Severity:** P2; depends on Task 1

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

**Symbols:** `BotHandler._update_profile`, `persisted_draft`, `draft`,
`DynamoRepository.save_profile_draft`

**TDD sequence:**

- [x] **Failing test first:** Start with an existing partial profile draft,
  apply a latest message that completes or changes multiple fields, force
  `save_profile` to return `False`, and assert `save_profile_draft` receives
  the fully merged local `draft` rather than no call or the older object.
- [x] Assert the stored retry draft contains the latest scalar, member, and
  dietary fields, preserves earlier fields omitted by the latest message, and
  contains no profile concurrency metadata.
- [x] Add controls for a conflict with no prior draft, successful save with a
  prior draft, and an incomplete merged draft. Assert conflicts preserve the
  appropriate latest draft, success deletes it, and incomplete input retains
  the existing accumulation behavior.
- [x] **Expected pre-fix failure:** Run the focused tests and record that the
  `persisted_draft is None` guard skips draft persistence after a controlled
  conflict, leaving the previous partial draft and losing the latest input.
- [x] **Implementation change:** On every controlled `save_profile` conflict,
  persist the newly merged `draft` regardless of whether a prior draft was
  loaded. Keep the canonical profile unchanged and do not delete retry state.
- [x] Preserve bounded stale-state feedback and ensure no false success
  message is returned. Keep unexpected draft-storage failures visible through
  the existing application error boundary.
- [x] **Passing-test verification:** Run the new conflict tests and inspect the
  exact saved draft fields and repository calls.
- [x] **Regression tests:** Run `uv run pytest tests/test_bot_handler.py
  tests/test_dynamo.py` and resolve every failure before final verification.

**Acceptance criteria:**

- [x] Every controlled ordinary profile conflict leaves the newest merged
  draft available for retry.
- [x] A pre-existing partial draft never causes the current message's fields
  to be discarded.
- [x] The latest canonical profile remains untouched and the user receives no
  success response after a conflict.
- [x] Successful saves still delete the draft, while incomplete updates retain
  existing accumulation behavior.
- [x] Retry drafts contain editable profile data only, not canonical profile
  revision state.

## Verification Requirements

After both tasks are complete:

- [x] Confirm each of the three review findings has a regression that failed
  before its implementation change and passes afterward.
- [x] Run `uv run ruff format .`, then `uv run ruff format --check .`, and
  require success with the configured 80-column limit.
- [x] Run `uv run ruff check .` and require no lint findings.
- [x] Run `uv run mypy` and require no type errors.
- [x] Run `uv run pytest` and require the full suite to pass. Record warning
  output and distinguish the two known Pydantic serializer warnings from any
  newly introduced warning.
- [x] Run `git diff --check` and require success.
- [x] Review the accumulated implementation diff for correctness, stale-write
  safety, legacy compatibility, error handling, privacy, and test coverage.
- [x] Confirm the original completed plan retains SHA-256
  `70e9908c6f1ade37b4c0a2276ff85f22cdf0d7b2679a56510cefc2c29a356fa8`.
- [x] Confirm the first remediation plan retains SHA-256
  `30716b398d8a3cb9be97e23f240c7998a21cdc0379826fdaf242bc79bb667adf`.
- [x] Confirm the completed follow-up plan retains SHA-256
  `50b0cfe944d6cf40b1182c19488d7591592e65522856f0c85fd6a12e946e6ad3`.
- [x] Confirm no unrelated pre-existing work was discarded or attributed to
  this remediation.
- [x] Confirm that no approved deviations remain before moving this plan to
  `docs/plans/completed/`.

## Post-Completion

**Manual verification**

- In a non-production Telegram environment, update a profile already beyond
  revision zero and verify the update succeeds once without stale feedback.
- Simulate two new-profile requests that both observed absence and verify only
  the first save wins.
- Force an ordinary save conflict after completing an existing partial draft
  and verify the next retry begins with the latest merged values.

**External system updates**

- No GitHub issue, commit, push, pull request, deployment, or external action
  is part of this planning task.
- A separately authorized implementation must use a dedicated branch, a
  Conventional Commit containing issue `#61`, and a pull request rather than
  pushing or merging directly to `master`.
- After implementation, comment on issue `#61` with the commit or pull request
  link and final verification results.
