"""DynamoDB repository integration tests."""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Generator

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    GrocerySection,
    GroceryStatus,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    PlanStatus,
    ProfileUpdateEntities,
)
from tests.factories import make_plan, make_profile


@pytest.fixture
def dynamodb_table() -> Generator[Any, None, None]:
    with mock_aws():
        table = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).create_table(
            TableName="test-meal-planner",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


@pytest.fixture
def repo(dynamodb_table: Any) -> DynamoRepository:
    return DynamoRepository(dynamodb_table)


def test_profile_and_onboarding_draft_round_trip(
    repo: DynamoRepository,
) -> None:
    assert repo.get_profile("user") is None
    draft = ProfileUpdateEntities(name="Alex", people_count=2)
    repo.save_profile_draft("user", draft)
    assert repo.get_profile_draft("user").name == "Alex"
    profile = make_profile()
    repo.save_profile("user", profile)
    assert repo.get_profile("user") == profile
    repo.delete_profile_draft("user")
    assert repo.get_profile_draft("user") is None


def test_conversation_state_round_trip_and_revision_guard(
    repo: DynamoRepository,
) -> None:
    """Conversation state is isolated and stale revisions cannot replace it."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    assert repo.get_conversation_state("user") == state
    newer = state.model_copy(
        update={"revision": 1, "updated_at": now + timedelta(seconds=1)}
    )
    assert repo.transition_conversation_state(
        "user", newer, expected_revision=state.revision
    )
    assert not repo.transition_conversation_state(
        "user", state, expected_revision=state.revision
    )
    assert repo.delete_conversation_state("user", expected_revision=1)
    assert repo.get_conversation_state("user") is None


def test_revision_replacement_and_state_cleanup_are_atomic(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(revision=3, planning_instructions=["Egg breakfasts"])
    repo.save_plan("user", plan)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=plan.week_start,
        expected_plan_revision=plan.revision,
        request_id="revision-1",
        revision=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(
        week_start=plan.week_start,
        revision=4,
        planning_instructions=["Egg breakfasts", "Avoid cauliflower"],
    )

    assert repo.replace_draft_and_clear_revision_state(
        "user",
        replacement,
        expected_plan_revision=3,
        request_id="revision-1",
        expected_state_revision=0,
    )
    assert repo.get_plan("user", plan.week_start) == replacement
    assert repo.get_conversation_state("user") is None


def test_revision_replacement_rejects_stale_plan_or_state(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(revision=1)
    repo.save_plan("user", plan)
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=plan.week_start,
        expected_plan_revision=1,
        request_id="revision-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", state)
    replacement = make_plan(week_start=plan.week_start, revision=2)
    assert not repo.replace_draft_and_clear_revision_state(
        "user",
        replacement,
        expected_plan_revision=0,
        request_id="wrong-request",
        expected_state_revision=0,
    )
    assert repo.get_plan("user", plan.week_start) == plan
    assert repo.get_conversation_state("user") == state


def test_replacements_from_one_snapshot_have_one_winner(
    repo: DynamoRepository,
) -> None:
    """A delayed transition cannot overwrite the winning replacement."""
    now = datetime.now(timezone.utc)
    initial = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert repo.save_conversation_state("user", initial)
    first = initial.model_copy(
        update={
            "revision": initial.revision + 1,
            "updated_at": now + timedelta(seconds=1),
        }
    )
    second = first.model_copy(update={"updated_at": now + timedelta(seconds=2)})

    assert repo.save_conversation_state(
        "user", first, expected_revision=initial.revision
    )
    assert not repo.save_conversation_state(
        "user", second, expected_revision=initial.revision
    )
    assert not repo.transition_conversation_state(
        "user", initial, expected_revision=initial.revision
    )
    assert repo.get_conversation_state("user") == first


def _meal(day: int, hour: int, description: str) -> MealLogEntry:
    return MealLogEntry(
        date=date(2026, 8, day),
        meal_type="lunch",
        description=description,
        created_at=datetime(2026, 8, day, hour, tzinfo=timezone.utc),
    )


def _completed_meal_state(
    *, revision: int = 0, now: datetime | None = None
) -> ConversationState:
    current = now or datetime.now(timezone.utc)
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
        meal_draft=MealLogDraft(
            date=date(2026, 8, 8),
            meal_type="lunch",
            description="Soup",
        ),
        revision=revision,
        created_at=current,
        updated_at=current,
        expires_at=current + timedelta(hours=24),
    )


def test_atomic_meal_and_state_transition_writes_one_meal_and_marker(
    repo: DynamoRepository,
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    entry = _meal(8, 12, "Soup")
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )

    assert repo.log_meal_and_transition(
        "user",
        entry,
        next_state,
        expected_revision=state.revision,
        source_update_id="100",
    )

    assert repo.get_conversation_state("user") == next_state
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == [
        entry
    ]
    marker = repo.table.get_item(
        Key={"PK": "USER#user", "SK": "MEAL_UPDATE#100"}
    )
    assert marker["Item"]["SK"] == "MEAL_UPDATE#100"


def test_atomic_meal_and_state_transition_rejects_stale_revision(
    repo: DynamoRepository,
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    competing = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )
    assert repo.transition_conversation_state(
        "user", competing, expected_revision=state.revision
    )
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=2),
        }
    )

    assert not repo.log_meal_and_transition(
        "user",
        _meal(8, 12, "Stale meal"),
        next_state,
        expected_revision=state.revision,
        source_update_id="101",
    )
    assert repo.get_conversation_state("user") == competing
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []
    assert (
        repo.table.get_item(
            Key={"PK": "USER#user", "SK": "MEAL_UPDATE#101"}
        ).get("Item")
        is None
    )


def test_atomic_meal_and_state_transition_propagates_transaction_error(
    repo: DynamoRepository, mocker: Any
) -> None:
    state = _completed_meal_state()
    assert repo.save_conversation_state("user", state)
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "TransactWriteItems",
    )
    mocker.patch.object(
        repo.table.meta.client, "transact_write_items", side_effect=error
    )
    next_state = state.model_copy(
        update={
            "revision": 1,
            "updated_at": state.updated_at + timedelta(seconds=1),
        }
    )

    with pytest.raises(ClientError) as raised:
        repo.log_meal_and_transition(
            "user",
            _meal(8, 12, "Failed meal"),
            next_state,
            expected_revision=state.revision,
            source_update_id="102",
        )

    assert raised.value is error
    assert repo.get_conversation_state("user") == state
    assert repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8)) == []


def test_meal_history_includes_multiple_meals_and_date_boundaries(
    repo: DynamoRepository,
) -> None:
    for entry in (
        _meal(1, 8, "too old"),
        _meal(2, 8, "start boundary"),
        _meal(2, 12, "same day second meal"),
        _meal(8, 8, "end boundary"),
    ):
        repo.log_meal("user", entry)
    history = repo.get_meal_history("user", days=7, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "end boundary",
        "same day second meal",
        "start boundary",
    ]


def test_meal_log_retries_are_idempotent_per_source_update(
    repo: DynamoRepository,
) -> None:
    first = _meal(8, 12, "first description")
    retry = MealLogEntry(
        date=date(2026, 8, 9),
        meal_type="dinner",
        description="retry description",
        created_at=datetime(2026, 8, 8, 13, tzinfo=timezone.utc),
    )
    distinct = MealLogEntry(
        date=date(2026, 8, 8),
        meal_type="dinner",
        description="distinct update",
        created_at=datetime(2026, 8, 8, 14, tzinfo=timezone.utc),
    )

    repo.log_meal("user", first, source_update_id="100")
    repo.log_meal("user", retry, source_update_id="100")
    repo.log_meal("user", distinct, source_update_id="101")

    saved = repo.table.get_item(
        Key={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#UPDATE#100#lunch",
        }
    )["Item"]
    assert saved["description"] == "first description"
    assert (
        repo.table.get_item(Key={"PK": "USER#user", "SK": "MEAL_UPDATE#100"})[
            "Item"
        ]["SK"]
        == "MEAL_UPDATE#100"
    )
    history = repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "distinct update",
        "first description",
    ]


def test_source_meal_transaction_contains_stable_marker_after_meal(
    mocker: Any,
) -> None:
    """Greenfield writes make markerless source updates unreachable."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "meal"), source_update_id="100"
    )

    request = table.meta.client.transact_write_items.call_args.kwargs
    meal_put, marker_put = request["TransactItems"]
    assert list(meal_put) == ["Put"]
    assert list(marker_put) == ["Put"]
    assert meal_put["Put"]["Item"]["SK"].startswith(
        "MEAL#2026-08-08#UPDATE#100#"
    )
    assert marker_put["Put"] == {
        "TableName": "test-meal-planner",
        "Item": {"PK": "USER#user", "SK": "MEAL_UPDATE#100"},
        "ConditionExpression": "attribute_not_exists(PK)",
    }


def test_marker_only_transaction_cancellation_is_distinguishable(
    mocker: Any,
) -> None:
    """A marker conflict is distinct from an unexpected transaction failure."""
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "retry"), source_update_id="100"
    )


def test_meal_marker_conflict_is_an_idempotent_success(mocker: Any) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    table.meta.client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )

    DynamoRepository(table).log_meal(
        "user", _meal(8, 12, "duplicate"), source_update_id="100"
    )


@pytest.mark.parametrize(
    "error",
    [
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "TransactWriteItems",
        ),
        ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [
                    {"Code": "None"},
                    {"Code": "TransactionConflict"},
                ],
            },
            "TransactWriteItems",
        ),
    ],
)
def test_meal_transaction_unexpected_failures_propagate(
    mocker: Any, error: ClientError
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    table.meta.client.transact_write_items.side_effect = error

    with pytest.raises(ClientError) as raised:
        DynamoRepository(table).log_meal(
            "user", _meal(8, 12, "meal"), source_update_id="100"
        )

    assert raised.value is error


def test_meal_log_without_source_id_and_legacy_keys_are_queryable(
    repo: DynamoRepository,
) -> None:
    timestamped = _meal(8, 12, "timestamped")
    legacy = _meal(8, 13, "legacy")

    repo.log_meal("user", timestamped)
    repo.table.put_item(
        Item={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#2026-08-08T13:00:00+00:00#lunch",
            **legacy.model_dump(mode="json"),
        }
    )

    saved = repo.table.get_item(
        Key={
            "PK": "USER#user",
            "SK": "MEAL#2026-08-08#TIME#2026-08-08T12:00:00+00:00#lunch",
        }
    )["Item"]
    assert saved["description"] == "timestamped"
    history = repo.get_meal_history("user", days=1, on_date=date(2026, 8, 8))
    assert [entry.description for entry in history] == [
        "legacy",
        "timestamped",
    ]


def test_meal_history_paginates_and_skips_malformed_items(mocker: Any) -> None:
    table = mocker.MagicMock()
    valid = {
        "PK": "USER#user",
        "SK": "MEAL#2026-08-08#x",
        **_meal(8, 8, "valid").model_dump(mode="json"),
    }
    table.query.side_effect = [
        {
            "Items": [{"PK": "USER#user", "SK": "bad"}],
            "LastEvaluatedKey": {"PK": "x"},
        },
        {"Items": [valid]},
    ]
    history = DynamoRepository(table).get_meal_history(
        "user", days=1, on_date=date(2026, 8, 8)
    )
    assert [entry.description for entry in history] == ["valid"]
    assert table.query.call_count == 2


def test_plan_selection_distinguishes_latest_exact_and_active(
    repo: DynamoRepository,
) -> None:
    active = make_plan(week_start=date(2026, 8, 4), status=PlanStatus.CONFIRMED)
    future = make_plan(week_start=date(2026, 8, 18))
    repo.save_plan("user", active)
    repo.save_plan("user", future)
    assert repo.get_latest_plan("user") == future
    assert repo.get_plan("user", "2026-08-04") == active
    assert repo.get_active_plan("user", date(2026, 8, 10)) == active
    assert repo.get_active_plan("user", date(2026, 8, 11)) is None


def test_active_plan_snapshot_returns_legacy_absent_epoch(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(week_start=date(2026, 8, 10), status=PlanStatus.CONFIRMED)
    repo.save_plan("user", plan)

    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))

    assert snapshot is not None
    assert snapshot.plan == plan
    assert snapshot.active_epoch is None


def test_active_callback_write_rejects_older_overlapping_plan(
    repo: DynamoRepository,
) -> None:
    older = make_plan(week_start=date(2026, 8, 10), status=PlanStatus.CONFIRMED)
    repo.save_plan("user", older)
    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))
    assert snapshot is not None
    assert snapshot.active_epoch is None

    newer = make_plan(week_start=date(2026, 8, 12))
    repo.save_plan("user", newer)
    assert repo.confirm_plan("user", newer.week_start_date, 0)

    assert not repo.update_meal_outcome(
        "user",
        older.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=snapshot.active_epoch,
    )
    saved_older = repo.get_plan("user", older.week_start_date)
    assert saved_older is not None
    assert saved_older.days[0].meals[0].outcome is MealOutcome.UNREPORTED


def test_active_callback_write_accepts_present_epoch(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(week_start=date(2026, 8, 10))
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, 0)

    snapshot = repo.get_active_plan_snapshot("user", on_date=date(2026, 8, 13))
    assert snapshot is not None
    assert snapshot.active_epoch == 1
    assert repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=snapshot.active_epoch,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.days[0].meals[0].outcome is MealOutcome.COOKED


def test_present_epoch_values_are_scoped_to_condition_check(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)

    assert repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=7,
    )

    request = table.meta.client.transact_write_items.call_args.kwargs
    update, condition_check = request["TransactItems"]
    update_values = update["Update"]["ExpressionAttributeValues"]
    condition_values = condition_check["ConditionCheck"][
        "ExpressionAttributeValues"
    ]
    assert ":expected_epoch" not in update_values
    assert condition_values == {":expected_epoch": 7}


def test_get_plan_consistency_is_opt_in(mocker: Any) -> None:
    table = mocker.MagicMock()
    plan = make_plan()
    table.get_item.return_value = {
        "Item": {
            "PK": "USER#user",
            "SK": f"PLAN#{plan.week_start_date}",
            **plan.model_dump(by_alias=True, mode="json"),
        }
    }
    repo = DynamoRepository(table)

    assert repo.get_plan("user", plan.week_start_date) == plan
    assert "ConsistentRead" not in table.get_item.call_args.kwargs

    assert (
        repo.get_plan("user", plan.week_start_date, consistent_read=True)
        == plan
    )
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True


def test_transaction_conditional_conflict_returns_false(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)
    table.meta.client.transact_write_items.side_effect = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "conditional conflict",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )

    assert not repo.update_meal_outcome(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=1,
    )


def test_transaction_service_failure_is_reraised(mocker: Any) -> None:
    table = mocker.MagicMock()
    table.name = "test-meal-planner"
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo = DynamoRepository(table)
    mocker.patch.object(repo, "get_plan", return_value=plan)
    error = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "transaction conflict",
            },
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        },
        "TransactWriteItems",
    )
    table.meta.client.transact_write_items.side_effect = error

    with pytest.raises(ClientError) as raised:
        repo.update_meal_outcome(
            "user",
            plan.week_start_date,
            1,
            "lunch",
            MealOutcome.COOKED,
            expected_epoch=1,
        )

    assert raised.value is error


def test_atomic_outcome_updates_preserve_independent_meals(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    repo.save_plan("user", plan)
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 2, "lunch", MealOutcome.SWAPPED
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.days[0].meals[0].outcome is MealOutcome.COOKED
    assert saved.days[1].meals[0].outcome is MealOutcome.SWAPPED
    assert not repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "dinner", MealOutcome.SKIPPED
    )


def test_atomic_outcome_rejects_draft(repo: DynamoRepository) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    assert not repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )


def test_generated_draft_cannot_replace_confirmed_plan(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    draft = make_plan(week_start=week)
    assert repo.save_generated_draft("user", draft, expected_revision=None)
    replacement = make_plan(week_start=week, revision=1)
    replacement.days[0].meals[0].name = "Replacement"
    assert repo.save_generated_draft("user", replacement, expected_revision=0)
    assert repo.confirm_plan("user", draft.week_start_date, 1)
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=1
    )
    saved = repo.get_plan("user", week)
    assert saved is not None
    assert saved.status is PlanStatus.CONFIRMED


def test_generated_draft_rejects_stale_edit_and_duplicate_worker(
    repo: DynamoRepository,
) -> None:
    week = date(2026, 8, 10)
    draft = make_plan(week_start=week)
    assert repo.save_generated_draft("user", draft, expected_revision=None)
    edited_meal = draft.days[0].meals[0].model_copy(update={"name": "Edit"})
    assert repo.update_meal(
        "user",
        draft.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.DRAFT,
    )

    replacement = make_plan(week_start=week, revision=1)
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=0
    )
    assert not repo.save_generated_draft(
        "user", replacement, expected_revision=None
    )


def test_generated_draft_reraises_nonconditional_dynamodb_errors(
    mocker: Any,
) -> None:
    table = mocker.MagicMock()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "PutItem",
    )
    table.put_item.side_effect = error
    repo = DynamoRepository(table)

    with pytest.raises(ClientError) as raised:
        repo.save_generated_draft("user", make_plan(), expected_revision=None)

    assert raised.value is error


def test_plan_lifecycle_writes_are_revision_checked(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.save_plan("user", plan)
    edited_meal = (
        plan.days[0].meals[0].model_copy(update={"name": "Edited lunch"})
    )
    assert repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.CONFIRMED,
    )
    assert not repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.CONFIRMED,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.revision == 1
    assert saved.grocery_status.value == "pending"
    assert repo.complete_grocery(
        "user",
        plan.week_start_date,
        1,
        [GrocerySection(name="Produce", items=["Apples"])],
    )
    assert not repo.fail_grocery("user", plan.week_start_date, 1)


def test_stale_draft_edit_is_rejected_after_confirmation(
    repo: DynamoRepository,
) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    edited_meal = (
        plan.days[0].meals[0].model_copy(update={"name": "Stale edit"})
    )
    assert repo.confirm_plan("user", plan.week_start_date, 0)

    assert not repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        edited_meal,
        expected_revision=0,
        expected_status=PlanStatus.DRAFT,
    )
    saved = repo.get_plan("user", plan.week_start_date)
    assert saved is not None
    assert saved.status is PlanStatus.CONFIRMED
    assert saved.revision == 0
    assert saved.grocery_status is GroceryStatus.PENDING
    assert saved.days[0].meals[0].name != "Stale edit"


def test_confirm_and_grocery_retry_are_conditional(
    repo: DynamoRepository,
) -> None:
    plan = make_plan()
    repo.save_plan("user", plan)
    assert repo.confirm_plan("user", plan.week_start_date, 0)
    assert not repo.confirm_plan("user", plan.week_start_date, 0)
    assert repo.fail_grocery("user", plan.week_start_date, 0)
    assert repo.retry_grocery("user", plan.week_start_date, 0)
    assert not repo.retry_grocery("user", plan.week_start_date, 0)


def test_grocery_worker_races_preserve_outcomes_and_reject_stale_edits(
    repo: DynamoRepository,
) -> None:
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.save_plan("user", plan)
    worker_revision = plan.revision
    assert repo.update_meal_outcome(
        "user", plan.week_start_date, 1, "lunch", MealOutcome.COOKED
    )
    assert repo.complete_grocery(
        "user",
        plan.week_start_date,
        worker_revision,
        [GrocerySection(name="Produce", items=["Apples"])],
    )
    ready = repo.get_plan("user", plan.week_start_date)
    assert ready is not None
    assert ready.days[0].meals[0].outcome is MealOutcome.COOKED
    assert ready.grocery_status is GroceryStatus.READY

    updated_meal = (
        ready.days[0].meals[0].model_copy(update={"name": "New lunch"})
    )
    assert repo.update_meal(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        updated_meal,
        expected_revision=worker_revision,
        expected_status=PlanStatus.CONFIRMED,
    )
    assert not repo.complete_grocery(
        "user",
        plan.week_start_date,
        worker_revision,
        [GrocerySection(name="Stale", items=["Old item"])],
    )
    edited = repo.get_plan("user", plan.week_start_date)
    assert edited is not None
    assert edited.days[0].meals[0].name == "New lunch"
    assert edited.grocery_status is GroceryStatus.PENDING
