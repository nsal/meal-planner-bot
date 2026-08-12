"""DynamoDB repository integration tests."""

from datetime import date, datetime, timezone
from typing import Any, Generator

import boto3
import pytest
from moto import mock_aws

from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
    GrocerySection,
    GroceryStatus,
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


def _meal(day: int, hour: int, description: str) -> MealLogEntry:
    return MealLogEntry(
        date=date(2026, 8, day),
        meal_type="lunch",
        description=description,
        created_at=datetime(2026, 8, day, hour, tzinfo=timezone.utc),
    )


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
    assert repo.save_generated_draft("user", draft)
    replacement = make_plan(week_start=week)
    replacement.days[0].meals[0].name = "Replacement"
    assert repo.save_generated_draft("user", replacement)
    assert repo.confirm_plan("user", draft.week_start_date, 0)
    assert not repo.save_generated_draft("user", replacement)
    saved = repo.get_plan("user", week)
    assert saved is not None
    assert saved.status is PlanStatus.CONFIRMED


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
