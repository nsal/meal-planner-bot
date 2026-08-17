# Harden Plan Revision Concurrency and Idempotency

## Overview

Harden asynchronous whole-draft revisions against DynamoDB consistency delay,
unexpected worker failures, plan snapshot races, duplicate Lambda delivery, and
duplicate Telegram updates. A revision request must never leave a dead
`GENERATING` lock, confirm an obsolete draft, publish more than one replacement,
or append the same amendment twice.

The change keeps the current revision workflow and user experience, while
making ownership decisions use strongly consistent state reads and preserving a
durable idempotency marker for every Telegram update that starts a revision.

- GitHub issue:
  [#33](https://github.com/nsal/meal-planner-bot/issues/33).
- Selected approach: atomically acquire the revision workflow and write a
  durable source-update marker.
- Testing approach: TDD; add each race or failure regression test before its
  implementation.

## Context (from discovery)

- **Files/components involved:** `src/meal_planner/planner_handler.py`,
  `src/meal_planner/bot_handler.py`, `src/meal_planner/db/dynamo.py`,
  `tests/test_planner_handler.py`, `tests/test_bot_handler.py`, and
  `tests/test_dynamo.py`.
- **Current worker flow:** `PlannerHandler.revise_plan` reads an exact plan
  snapshot, checks a `PLAN_REVISION` conversation lock, generates a replacement,
  and atomically replaces the draft while deleting the lock.
- **Current consistency gap:** plans can request a consistent read, but
  `get_conversation_state` cannot. Revision workers and the bot therefore make
  lock decisions from eventually consistent state.
- **Current recovery gap:** expected LLM failures retain `RETRY_READY`, while an
  unexpected exception only notifies the user and can leave the matching state
  in `GENERATING`. Some stale-plan exits also return without releasing it.
- **Current idempotency pattern:** meal writes use durable source-update marker
  items in DynamoDB transactions. Revision state already has `last_update_id`,
  but `_start_plan_revision` incorrectly reads the ID from LLM entities and no
  marker survives successful state deletion.
- **Project standards:** Python 3.14, type hints with strict Mypy, Ruff at 80
  columns, `uv` for commands, and a passing full Pytest suite before completion.

## Development Approach

- **Testing approach:** TDD. Start each implementation task with failing tests
  for its success, conflict, duplicate, and error behavior.
- Complete each task fully before moving to the next task.
- Make small, focused changes and preserve backward-compatible repository
  defaults for callers that do not require a strongly consistent read.
- Every task that changes Python behavior includes new or updated tests for all
  changed paths.
- Run the focused tests and then `uv run pytest`; both must pass before starting
  the next task.
- Update this plan immediately if scope or architecture changes.
- Use Ruff only for formatting and lint fixes, with the configured 80-column
  limit.
- Add no dependency unless essential. If one is added, use `uv add`, update
  `uv.lock`, and verify it with `uv lock --check`.
- Keep implementation work off `master` and publish it through a dedicated
  bug-fix branch and pull request.

## Testing Strategy

- **Repository tests:** verify opt-in consistent reads, atomic workflow and
  update-marker creation, marker durability after state deletion, duplicate
  rejection, and propagation of unexpected transaction failures.
- **Planner tests:** simulate missed/stale state, unexpected exceptions,
  pre-generation plan conflicts, conflicts after generation, duplicate workers,
  cancellation, and replacement by a newer request.
- **Bot tests:** prove the normalized Telegram update ID is passed explicitly,
  workflow reads are strong, duplicate updates do not invoke another planner,
  and distinct update IDs can start later revisions.
- **Schema tests:** no schema change is currently required because
  `ConversationState.last_update_id` already stores the normalized source ID.
  Add schema coverage only if implementation reveals a contract change.
- **End-to-end UI tests:** not applicable; this repository has no browser UI.
  Handler-level Telegram update tests cover the external workflow boundary.
- **Required final gates:** `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, and `uv lock --check`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with a `➕` prefix.
- Document issues or blockers with a `⚠️` prefix.
- Update the plan if implementation deviates from the selected design.
- Keep this document synchronized with tests and implementation.
- Move it to `docs/plans/completed/` only after every required gate passes.

## Solution Overview

Add an opt-in `consistent_read` argument to conversation-state retrieval. The
bot will use it when deciding whether confirmation or another amendment is
allowed, and the planner will use it whenever it validates or transitions a
revision lock. This prevents a newly written lock from being missed and avoids
acting on an obsolete draft during DynamoDB replication delay.

Centralize stale revision resolution in the planner. When the plan no longer
matches the request snapshot, or the final replacement transaction loses a
race, re-read the request state consistently. If the exact request still owns
the lock, clear it conditionally and report the plan conflict. If the state is
already absent, cancelled, replaced, or advanced, suppress the stale-worker
notification. Unexpected failures will transition the exact matching request
to `RETRY_READY` before telling the user to retry.

For Telegram idempotency, pass `source_update_id` explicitly into revision
startup. A dedicated repository operation will atomically create the
conversation lock and a `PLAN_REVISION_UPDATE#{source_update_id}` marker. The
marker remains after the worker deletes the conversation state, so redelivery
of the same Telegram update is treated as an idempotent success without another
Lambda invocation. Updates without a valid source ID retain the existing
conditional state-write behavior.

Rejected alternatives:

- Keeping completed conversation-state tombstones complicates all future
  workflow routing and blocks clean reuse of the single state key.
- Storing processed update IDs on `WeeklyPlan` mixes Telegram transport details
  into the plan domain and handles cancellation or pre-publication failure less
  reliably.

## Technical Details

### Strong revision-lock reads

- Extend `DynamoRepository.get_conversation_state` with a keyword-only
  `consistent_read: bool = False` parameter.
- Set DynamoDB `ConsistentRead=True` only when requested, preserving the current
  default for non-lock callers.
- Make `BotHandler._get_conversation_state` request a consistent read because
  its result gates confirmation, amendment, retry, and cancellation routing.
- Use consistent state reads in revision worker ownership checks, retry-state
  retention, and conflict resolution.

### Retry and terminal-state handling

- On an unexpected exception in `revise_plan`, attempt the conditional
  `GENERATING` to `RETRY_READY` transition before sending the retry message.
- The transition must match request ID and state revision so an old worker
  cannot overwrite cancellation, a retry, or a newer workflow.
- Audit early exits after ownership is established. Terminal missing-profile,
  missing-plan, or ineligible-plan paths must conditionally clear the exact lock
  or deliberately retain a retry-ready state consistent with their message.
- Recovery failure must be logged without replacing the original worker error
  or preventing the best-effort Telegram notification.

### Stale-plan and duplicate-worker conflicts

```text
replacement or snapshot conflict
  -> strongly re-read conversation state
  -> exact GENERATING request still owns it?
       yes -> conditionally clear it -> notify only after successful clear
       no  -> stale/duplicate/cancelled/replaced worker -> log and stay silent
```

- Apply this resolution both before generation when the plan snapshot is
  already stale and after a failed replacement transaction.
- Never clear state by user alone; require the exact request ID and expected
  state revision.
- A duplicate worker that loses after the winner publishes sees no matching
  state and sends no misleading failure message.

### Durable Telegram update marker

- Pass the normalized `source_update_id` from `_apply_intent_metadata` to
  `_start_plan_revision`; never read it from LLM-produced entities.
- Add a repository operation that writes the revision conversation state and
  source marker in one DynamoDB transaction when an update ID is available.
- Use the marker key
  `PK=USER#{user_id}, SK=PLAN_REVISION_UPDATE#{source_update_id}` and require
  both marker and conversation-state keys to be absent.
- Retain the marker without the conversation-state TTL so successful cleanup,
  cancellation, retry, and delayed redelivery cannot reapply the update.
- After a conditional start failure, strongly check the marker. Treat a present
  marker as an idempotent duplicate and do not invoke the Planner Lambda; treat
  an absent marker as a competing workflow.
- Preserve the existing single conditional conversation-state put for updates
  that lack a valid Telegram source ID.

## What Goes Where

- **Implementation Steps:** repository consistency and marker operations,
  planner recovery and conflict handling, bot source-ID propagation, focused
  regression tests, and documentation are implemented in this repository.
- **Post-Completion:** cloud deployment, live duplicate-delivery exercises, and
  GitHub issue/PR updates require external systems and are listed separately.

## Implementation Steps

### Task 1: Make revision lock reads strongly consistent

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing repository tests for default and strongly consistent
  conversation-state reads.
- [x] Write failing bot and planner tests asserting strong reads at all revision
  ownership decisions.
- [x] Add the backward-compatible `consistent_read` repository parameter and
  request it from the bot and revision worker paths.
- [x] Write edge-case tests showing a missing or non-matching strongly read lock
  prevents confirmation or revision publication.
- [x] Run the focused Dynamo, bot, and planner tests.
- [x] Run `uv run pytest`; it must pass before Task 2.

### Task 2: Recover matching locks after revision failures

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing tests for unexpected profile, prompt-building, generation,
  and repository exceptions while a matching request is `GENERATING`.
- [x] Write failing tests proving cancellation or a newer state is never
  overwritten by delayed recovery.
- [x] Transition the exact matching request to `RETRY_READY` before sending the
  existing unexpected-failure retry message.
- [x] Audit terminal precondition exits and conditionally resolve any matching
  lock whose message does not promise an active retry.
- [x] Write tests for recovery-write failure so it is logged and does not mask
  the original failure notification.
- [x] Run `uv run pytest tests/test_planner_handler.py`.
- [x] Run `uv run pytest`; it must pass before Task 3.

### Task 3: Resolve stale snapshots without leaking locks or noisy duplicates

**Files:**

- Modify: `src/meal_planner/planner_handler.py`
- Modify: `tests/test_planner_handler.py`

- [x] Write failing tests for a plan revision mismatch detected before LLM
  generation and for a plan change during final publication.
- [x] Write failing duplicate-delivery tests where one worker publishes and the
  losing worker observes absent, cancelled, or replaced request state.
- [x] Add one strongly consistent, conditional conflict-resolution path used by
  both pre-generation and post-transaction plan conflicts.
- [x] Clear only the exact matching request and notify only when that cleanup
  proves this worker still owned the abandoned lock.
- [x] Suppress conflict messages for duplicate, cancelled, replaced, and already
  completed requests while retaining diagnostic logs.
- [x] Run `uv run pytest tests/test_planner_handler.py`.
- [x] Run `uv run pytest`; it must pass before Task 4.

### Task 4: Make revision startup idempotent per Telegram update

**Files:**

- Modify: `src/meal_planner/db/dynamo.py`
- Modify: `src/meal_planner/bot_handler.py`
- Modify: `tests/test_dynamo.py`
- Modify: `tests/test_bot_handler.py`

- [x] Write failing repository tests for atomic lock-and-marker creation,
  rollback on either condition failure, durable markers after state deletion,
  and unexpected transaction errors.
- [x] Write failing bot tests proving `source_update_id` is passed explicitly,
  a redelivered update does not invoke another revision, and a distinct update
  can start a later revision.
- [x] Implement the typed repository operation for atomic revision-state and
  `PLAN_REVISION_UPDATE` marker creation, with a no-ID fallback.
- [x] Add a strongly consistent marker lookup that distinguishes an idempotent
  duplicate from a competing active workflow after conditional failure.
- [x] Pass `source_update_id` into `_start_plan_revision`, persist it as
  `last_update_id`, and suppress duplicate planner invocation.
- [x] Run `uv run pytest tests/test_dynamo.py tests/test_bot_handler.py`.
- [x] Run `uv run pytest`; it must pass before Task 5.

### Task 5: Verify all acceptance criteria and project quality gates

**Files:**

- Modify if needed: files changed in Tasks 1-4
- Modify if needed: `uv.lock`

- [x] Verify all five review findings have direct regression tests.
- [x] Verify missing-lock, unexpected-failure, stale-plan, duplicate-worker,
  cancelled/replaced request, duplicate-update, and no-update-ID paths.
- [x] Run `uv run ruff format .` and review the resulting changes.
- [x] Run `uv run ruff check .`; fix all findings.
- [x] Run `uv run ruff format --check .`; it must pass.
- [x] Run `uv run mypy`; it must pass with strict project settings.
- [x] Run `uv lock --check`; it must pass.
- [x] Run `uv run pytest`; the full suite must pass.

### Task 6: [Final] Update documentation and close implementation tracking

**Files:**

- Modify: `docs/plans/2026-08-17-harden-plan-revision-concurrency-idempotency.md`
- Modify if needed: `README.md`
- Modify if needed: `AGENTS.md`
- Move on completion:
  `docs/plans/2026-08-17-harden-plan-revision-concurrency-idempotency.md`
  to `docs/plans/completed/`

- [x] Record any architectural deviations and newly discovered work in this
  plan.
- [x] Update `README.md` only if user-visible workflow behavior changes.
- [x] Update `AGENTS.md` only if a reusable project convention is introduced.
- [x] Confirm every implementation and verification checkbox is complete.
- [x] Move this plan to `docs/plans/completed/`.
- [ ] Comment on the associated GitHub issue with the Conventional Commit or PR
  link and a concise summary of the completed work.

## Post-Completion

Items below require external systems and are informational rather than
implementation checkboxes.

### Manual verification

- Send one amendment through a test Telegram bot and confirm the revised draft
  can be reviewed and confirmed normally.
- Redeliver the same Telegram update after successful revision and verify no new
  Planner Lambda invocation or appended instruction appears.
- Deliver the same Planner Lambda event twice and verify only one draft is
  published and no stale-result failure message follows it.
- Force an unexpected worker failure and verify the user can immediately reply
  `retry` without `/cancel` or TTL expiry.

### External system updates

- Deploy through the existing non-`master` branch and pull-request workflow.
- Verify DynamoDB IAM already permits strongly consistent `GetItem` and
  transactional writes for the new marker item; no new API action is expected.
- Monitor Planner Lambda logs for suppressed duplicates and conditional
  conflicts after deployment.
