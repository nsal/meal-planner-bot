"""DynamoDB repository integration tests."""

from datetime import date, datetime, timezone
from typing import Any, Generator

import boto3
import pytest
from moto import mock_aws

from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
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
    assert repo.get_profile_draft("user").name is None


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
