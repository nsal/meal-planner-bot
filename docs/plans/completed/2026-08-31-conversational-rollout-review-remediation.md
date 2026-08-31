# Conversational Rollout Independent Review Remediation

## Overview

Remediate all six actionable findings from the independent review of the
conversational simplification work. The retained architecture is coherent,
but rollout remains blocked until persisted meals containing retired
`batch_link` metadata remain readable.

This is a deletion-led remediation. It must preserve strict current models,
remove dead workflow code, and must not restore retired batch models,
field-by-field meal logging, or any other removed architecture.

The supplied pre-review baseline passed with `481 passed, 1 skipped`. The
optional SAM built-handler import check was skipped because no built handler
artifact existed. The reviewer relied on those supplied gates and did not
rerun tests. Attribution is limited by the overlapping dirty baseline.

## Context

- Legacy persisted meal items can contain `batch_link`, including an explicit
  `null`, while `MealLogEntry` now rejects all extra fields.
- Profile setup writes draft, profile, and conversation state in separate
  operations, allowing stale or losing requests to persist data.
- Plan Chat bounds profile and conversation input but not the rendered meal
  history section.
- Planning cancellation can report failure after its state deletion has
  already succeeded.
- `BotHandler._handle_meal_workflow()` and
  `DynamoRepository.log_meal_and_transition()` are unreachable remnants of
  the retired field-by-field workflow.
- Prior completed plans under `docs/plans/completed/` are immutable inputs and
  must not be moved or modified by this remediation.

## Development Approach

- Use strict TDD: add the smallest failing regression test before each
  implementation change and confirm that it demonstrates the finding.
- Complete tasks sequentially. All focused tests for a task must pass before
  starting the next task.
- Keep the existing `MealLogEntry(extra="forbid")` posture. Compatibility
  may remove only explicitly known retired keys before strict validation.
- Condition every profile-setup transaction on the complete observed setup
  state identity, including revision and step.
- Preserve current conflict and operational-error distinctions.
- Use `uv run` for Python tools and Ruff for Python formatting.
- Keep Python fully typed and formatted to the project 80-column limit.
- Update this plan when scope or verification results change.

## Review-Finding Coverage

Each finding is assigned exactly once. Tasks 2 and 3 share state-condition
construction, but cover distinct write paths.

1. P1 legacy `batch_link` compatibility: Task 1.
2. P2 final profile setup must own setup state: Task 3.
3. P2 profile draft and state progress must be atomic: Task 2.
4. P2 bounded Plan Chat meal history: Task 4.
5. P2 successful planning cancellation delivery failure: Task 5.
6. P3 unreachable field-by-field meal workflow: Task 6.

## Solution Overview

Normalize known retired meal metadata at the `MealLogEntry` validation
boundary while continuing to reject unknown fields. Add DynamoDB transactions
for intermediate and final profile-setup writes so only the owner of the
observed state can alter the draft or profile. Bound rendered history by both
record count and characters, retaining the newest records deterministically
and marking truncation explicitly. Separate successful session mutation from
post-delete Telegram delivery. Finally, delete the unreachable meal workflow
and its repository-only transaction without removing compatibility parsing
that is still used to clear legacy states.

## Technical Decisions

- `MealLogEntry` will strip only `batch_link` in a `mode="before"`
  compatibility validator. Unknown extra keys must still fail validation.
- Task 2 will introduce reusable observed-state condition construction for
  profile setup. Task 3 will reuse that condition in the completion
  transaction rather than creating weaker ownership checks.
- Intermediate setup commits will atomically put `PROFILE_DRAFT` and advance
  `CONVERSATION_STATE`.
- Final setup commits will atomically put `PROFILE`, delete
  `PROFILE_DRAFT`, and conditionally delete `CONVERSATION_STATE`.
- Meal history will retain at most 50 newest records and at most 12,000
  rendered characters. Retained records will display chronologically, and a
  stable marker will state how many older records were omitted.
- Telegram delivery after a successful planning-state deletion is a delivery
  failure, not a persistence failure. The callback acknowledgement remains
  successful and no retry button for the deleted session is sent.
- Legacy state shapes needed by `_restart_legacy_meal_workflow()` remain
  readable only so old state can be cleared. They must not become routable
  workflows again.

## Testing Strategy

- Schema tests prove narrow compatibility and continued rejection of unknown
  fields.
- Repository tests use actual legacy DynamoDB item shapes and exercise both
  successful and conflicting profile-setup transactions.
- Handler tests deterministically interleave state replacement with setup
  progress and completion.
- Plan Chat worker and prompt tests cover retained legacy meals and both
  history limits.
- Retained-surface tests prevent dead workflow symbols and repository methods
  from returning.
- The final task runs the complete repository quality gates.

## Implementation Steps

### Task 1: Preserve legacy meals without restoring batch models

**Finding:** P1 legacy `batch_link` compatibility and rollout blocker.

**Files:**

- Modify: `src/meal_planner/models/schemas.py`
- Modify: `tests/test_schemas.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_plan_chat_handler.py`

- [x] Add failing `MealLogEntry` tests using actual legacy shapes with
  `batch_link=None` and a populated historical value; retain a control case
  proving an unrelated extra key is rejected.
- [x] Add a failing `DynamoRepository.get_meal_history()` test that inserts
  the legacy DynamoDB shape and expects the core meal to be returned rather
  than logged and skipped.
- [x] Add a failing Plan Chat worker test proving a legacy-shaped stored meal
  reaches `build_plan_chat_prompt()` as meal-history evidence.
- [x] Confirm the expected failures demonstrate that strict validation drops
  otherwise valid legacy meals.
- [x] Add a narrow `mode="before"` validator on `MealLogEntry` that removes
  only `batch_link` before current-field validation; do not add a batch field
  to the model or relax `extra="forbid"`.
- [x] Run `uv run pytest tests/test_schemas.py tests/test_dynamo.py
  tests/test_plan_chat_handler.py` and confirm the new tests pass.
- [x] Run relevant `/submit_meals` and Plan Chat regression tests in
  `tests/test_bot_handler.py` and `tests/test_plan_chat_handler.py`.

**Acceptance criteria:**

- Valid persisted meals containing legacy `batch_link` metadata remain in
  meal history and Plan Chat evidence.
- Current writes do not emit `batch_link`.
- Unknown extra fields remain invalid and malformed records are still skipped.
- No retired batch model, relation, or workflow is reintroduced.

### Task 2: Atomically commit profile draft progress and state

**Finding:** P2 intermediate profile setup race.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] Add a failing repository test for two distinct answers based on the
  same observed profile-setup state; assert exactly one transaction advances
  state and the losing answer never alters `PROFILE_DRAFT`.
- [x] Add a failing handler test that replaces the setup state between read
  and commit; assert the stale request changes neither draft nor replacement
  state and receives the existing conflict response.
- [x] Confirm the expected failures show `save_profile_draft()` can persist a
  losing answer before `transition_conversation_state()` rejects it.
- [x] Add a typed
  `DynamoRepository.save_profile_draft_and_transition_state()` transaction
  that puts the draft and next state only when revision, step, and the full
  observed setup identity still match.
- [x] Factor the observed profile-setup state condition only as needed for
  reuse by Task 3, preserving conditional-conflict versus operational-error
  behavior.
- [x] Update `BotHandler._handle_profile_setup_input()` to use the atomic
  repository operation for every non-final setup answer.
- [x] Run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and
  confirm the new concurrency tests pass.
- [x] Run existing profile setup, duplicate-update, stale-state, and
  persistence-failure regressions.

**Acceptance criteria:**

- Draft and setup-state progress commit together or not at all.
- A stale or losing request cannot mutate the stored draft.
- Only the winner's answer is paired with the winner's next step.
- Operational DynamoDB failures remain visible and produce the existing safe
  retry response.

### Task 3: Condition final profile completion on setup ownership

**Finding:** P2 final profile setup race.

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] Add a failing repository test that replaces `CONVERSATION_STATE` after
  final setup input is read; assert the stale transaction cannot write
  `PROFILE`, delete the replacement state, or delete its draft.
- [x] Add a failing handler test for `/profile` or `/submit_meals` replacing
  setup state before final commit; assert the existing profile remains
  unchanged and no success message is sent.
- [x] Add failing success and operational-error tests for one atomic final
  commit covering new-profile and existing-profile revision conditions.
- [x] Confirm the expected failures show `save_profile()` currently succeeds
  before setup-state ownership is checked.
- [x] Add a typed `DynamoRepository.complete_profile_setup()` transaction
  that conditionally puts `PROFILE`, deletes `PROFILE_DRAFT`, and deletes the
  observed setup state as one commit.
- [x] Reuse Task 2's full observed-state condition, including revision and
  step, and preserve optimistic profile revision checks.
- [x] Update the final branch of
  `BotHandler._handle_profile_setup_input()` to use the transaction and to
  distinguish stale work from operational failure.
- [x] Run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py` and
  confirm all new finalization tests pass.
- [x] Run complete profile setup, restart, concurrent edit, cleanup, and
  duplicate-delivery regressions.

**Acceptance criteria:**

- Profile save, setup-state consumption, and draft deletion are atomic.
- A request that no longer owns the observed setup state cannot overwrite a
  profile.
- Both first profile creation and revision-checked replacement remain valid.
- Conflict and operational-failure messages accurately describe the outcome.

### Task 4: Bound rendered meal history in Plan Chat prompts

**Finding:** P2 unbounded provider prompt history.

**Files:**

- Modify: `src/meal_planner/llm/prompts.py`
- Modify: `tests/test_prompts.py`
- Modify: `tests/test_plan_chat_handler.py`

- [x] Add a failing `_render_history()` or `build_plan_chat_prompt()` test
  with more than 50 maximum-length meals and assert deterministic item and
  12,000-character history-section bounds.
- [x] Add failing boundary tests proving the newest records are retained,
  retained records have stable chronological rendering, and the exact omitted
  count appears in an explicit truncation marker.
- [x] Add a worker regression test proving an oversized 21-day repository
  result still produces a bounded provider prompt.
- [x] Confirm the expected failure demonstrates the current prompt includes
  the complete unbounded history.
- [x] Add named constants and deterministic selection/rendering in
  `_render_history()` for the 50-record and 12,000-character limits.
- [x] Ensure escaping and truncation cannot create a partial structural
  delimiter or omit the truncation marker itself.
- [x] Run `uv run pytest tests/test_prompts.py
  tests/test_plan_chat_handler.py` and confirm all new tests pass.
- [x] Run existing prompt escaping, date-window, and Plan Chat request
  regressions.

**Acceptance criteria:**

- Meal history has deterministic record-count and rendered-character bounds.
- The newest evidence is retained and older omissions are disclosed.
- The generated prompt remains structurally valid at both boundaries.
- Normal-sized histories preserve their existing output.

### Task 5: Preserve successful planning cancellation outcome

**Finding:** P2 contradictory failure after successful deletion.

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_bot_handler.py`

- [x] Add a failing `_handle_plan_chat_callback()` test where state deletion
  succeeds and the first Telegram success delivery raises
  `TelegramAPIError`.
- [x] Assert the failing test expects a successful callback
  acknowledgement, no contradictory “couldn't end planning” message, and no
  end button for the deleted session.
- [x] Add a control test proving a persistence failure before deletion still
  reports failure and preserves the retry path.
- [x] Confirm the expected failure demonstrates that the broad exception
  handler currently treats post-delete delivery as failed persistence.
- [x] Separate post-delete success-message delivery handling from mutation
  handling in `BotHandler._handle_plan_chat_callback()`; log delivery failure
  without changing the successful outcome.
- [x] Run `uv run pytest tests/test_bot_handler.py` and confirm the new tests
  pass.
- [x] Run existing stale-button, changed-state, duplicate callback,
  acknowledgement-failure, and cancellation regressions.

**Acceptance criteria:**

- Successful state deletion always retains the “Planning ended” callback
  outcome.
- A failed success-message delivery never claims that planning remains active.
- Failures before a confirmed delete continue to report mutation failure.
- Callback acknowledgement errors remain isolated and logged.

### Task 6: Delete the unreachable field-by-field meal workflow

**Finding:** P3 dead meal workflow surface.

**Files:**

- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_retained_surface.py`

- [x] Add failing retained-surface assertions that
  `BotHandler._handle_meal_workflow` and
  `DynamoRepository.log_meal_and_transition` do not exist.
- [x] Confirm the expected failure proves both unreachable symbols remain in
  the retained architecture.
- [x] Remove `BotHandler._handle_meal_workflow()` and imports used only by
  that method.
- [x] Remove `DynamoRepository.log_meal_and_transition()` and its tests after
  confirming it has no live call site outside the dead handler.
- [x] Keep `_restart_legacy_meal_workflow()` and only the schema/router state
  needed to recognize and clear persisted legacy workflows; do not route
  input back into those workflows.
- [x] Add or refine a retained-surface audit proving structured one-shot meal
  submission remains the only writable meal-log path.
- [x] Run `uv run pytest tests/test_retained_surface.py
  tests/test_dynamo.py tests/test_bot_handler.py tests/test_router.py` and
  confirm the new assertions and retained behavior pass.
- [x] Run `rg -n "_handle_meal_workflow|log_meal_and_transition" src` and
  confirm there are no definitions or call sites.

**Acceptance criteria:**

- The unreachable handler and repository transaction are absent.
- No production call site or obsolete repository test remains.
- Structured `/submit_meals` submission and legacy-state cleanup still work.
- No retired field-by-field route or batch architecture is restored.

### Task 7: Run final repository verification and close the plan

**Files:**

- Modify: `docs/plans/2026-08-31-conversational-rollout-review-remediation.md`
- Move after all gates pass:
  `docs/plans/2026-08-31-conversational-rollout-review-remediation.md` to
  `docs/plans/completed/2026-08-31-conversational-rollout-review-remediation.md`

- [x] Verify every finding is covered exactly once and every task acceptance
  criterion is satisfied.
- [x] Run the deletion-led retained-surface and import audits; confirm retired
  batch models and field-by-field workflow symbols remain absent.
- [x] Run `uv run pytest` and require the complete suite to pass.
- [x] Run `uv run ruff check .` and require no lint findings.
- [x] Run `uv run ruff format --check .` and require no formatting changes.
- [x] Run `uv run mypy` and require strict type checking to pass.
- [x] Run `git diff --check` and require no whitespace errors.
- [x] If SAM built Python handlers exist, run the optional built-handler
  import check; otherwise record the artifact absence as an explicit skip.
- [x] Record exact command results, resolved failures, residual limitations,
  and any scope changes in this plan.
- [x] Confirm prior files under `docs/plans/completed/` are unchanged, then
  move only this completed remediation plan to `docs/plans/completed/`.

**Acceptance criteria:**

- All focused and full repository gates pass.
- The P1 rollout blocker and all P2/P3 findings are resolved.
- The final record truthfully distinguishes tested behavior from optional or
  external verification.
- No prior completed plan is modified or moved.

**Task 7 verification record (2026-08-31):**

- Coverage audit: `uv run python - <<'PY' ... PY` reported six finding
  assignments, exactly `P1->Task 1`, `P2->Task 3`, `P2->Task 2`,
  `P2->Task 4`, `P2->Task 5`, and `P3->Task 6`; Tasks 1–6 had no unchecked
  items. All six acceptance-criteria sections were reviewed against their
  focused tests and task reports and are satisfied.
- Retained-surface audit: `uv run pytest tests/test_retained_surface.py
  tests/test_readme.py tests/test_router.py` — 153 passed. Source scans with
  `rg -n '_handle_meal_workflow|log_meal_and_transition' src --glob
  '*.py'` and `rg -n '^class (Batch|MealBatch|PlannedBatch|SubmittedMealBatch)|^(from|import) .*batch' src --glob '*.py'`
  returned no matches. The only retained batch-named source is the explicit
  `batch_link` compatibility validator and legacy-state cleanup.
- Focused remediation suite: `uv run pytest tests/test_schemas.py
  tests/test_dynamo.py tests/test_bot_handler.py tests/test_prompts.py
  tests/test_plan_chat_handler.py tests/test_retained_surface.py
  tests/test_router.py tests/test_template.py` — 351 passed, 1 skipped.
- Full suite: `uv run pytest` — 499 passed, 1 skipped in 6.34s.
- Lint: `uv run ruff check .` — `All checks passed!`
- Formatting: `uv run ruff format --check .` — `114 files already formatted`.
- Strict typing: `uv run mypy` — `Success: no issues found in 16 source
  files`.
- Whitespace: `git diff --check` — passed with no output.
- Optional SAM verification: the explicit artifact audit reported `SKIP: no
  SAM built Python handler artifacts found under .aws-sam/build; built-handler
  import check not applicable.` No built-handler import was run. The one
  skipped test in the focused and full suites is the normal missing-artifact
  skip from `tests/test_template.py`.
- Resolved verification issue: an initial local coverage-audit attempt used
  `python - <<'PY' ... PY` and failed with `zsh:3: command not found: python`.
  It was immediately rerun as `uv run python - <<'PY' ... PY` and passed; no
  repository files were changed by either command.
- Resolved implementation failures reported during Tasks 1–6 were fixed
  before this final run: strict legacy-meal validation initially rejected
  `batch_link`; setup races initially used separate writes; finalization
  initially exposed an empty DynamoDB expression-value map; history was
  initially unbounded; cancellation initially conflated post-delete delivery
  failure with persistence failure; and the two retired workflow symbols were
  initially present. Their focused regressions now pass.
- Limitations: no live AWS, Telegram, or provider verification was performed;
  SAM built-handler import verification remains pending an environment that
  produces compatible artifacts. Attribution remains limited by the extensive
  pre-existing dirty baseline described in the overview.
- Scope: no implementation or prior completed plan was changed during Task 7.
  This plan alone was updated with the checklist and this record, then moved
  to `docs/plans/completed/` after the gates passed.

## Post-Completion

The following actions require external systems and are intentionally outside
implementation of this plan:

- Review and merge the changes through a pull request on a dedicated branch;
  never push or merge directly to `master`.
- Deploy through the normal AWS pipeline and run live Telegram smoke tests for
  legacy meal history, profile setup, Plan Chat, and planning cancellation.
- Run the SAM built-handler import check in an environment that produces the
  deployment artifacts if it remained skipped locally.
- Comment on the associated GitHub issue with the Conventional Commit or pull
  request link after those external artifacts exist.

No GitHub issue is created by this planning task.
