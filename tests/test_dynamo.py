"""Tests for DynamoDB repository using moto mock."""

from typing import Any, Generator

import boto3
import pytest
from moto import mock_aws

from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
    FamilyMember,
    GrocerySection,
    Ingredient,
    MealLogEntry,
    PlanDay,
    PlannedMeal,
    UserProfile,
    WeeklyPlan,
)


@pytest.fixture
def dynamodb_table() -> Generator[Any, None, None]:
    """Create a mock DynamoDB table for testing."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
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
    """Return DynamoRepository instance connected to mock table."""
    return DynamoRepository(dynamodb_table)


def test_get_profile_not_found(repo: DynamoRepository) -> None:
    """Test get_profile when no profile exists."""
    profile = repo.get_profile("user123")
    assert profile is None


def test_save_and_get_profile(repo: DynamoRepository) -> None:
    """Test saving and retrieving user profile."""
    member = FamilyMember(name="Alice", calorie_target=2000)
    profile = UserProfile(
        name="User One",
        family_members=[member],
        allergies=["nuts"],
        dietary_preferences=["vegetarian"],
        restrictions=["low-sodium"],
        goals=["maintain-weight"],
        people_count=2,
    )
    repo.save_profile("user123", profile)

    retrieved = repo.get_profile("user123")
    assert retrieved is not None
    assert retrieved.name == "User One"
    assert len(retrieved.family_members) == 1
    assert retrieved.family_members[0].name == "Alice"
    assert retrieved.allergies == ["nuts"]
    assert retrieved.people_count == 2


def test_log_meal_and_get_history(repo: DynamoRepository) -> None:
    """Test logging meals and querying history sorted by date."""
    entry1 = MealLogEntry(
        date="2026-08-01",
        meal_type="lunch",
        description="Salad",
        created_at="2026-08-01T12:00:00Z",
    )
    entry2 = MealLogEntry(
        date="2026-08-03",
        meal_type="dinner",
        description="Pasta",
        created_at="2026-08-03T18:00:00Z",
    )
    repo.log_meal("user123", entry1)
    repo.log_meal("user123", entry2)

    history = repo.get_meal_history("user123", days=14)
    assert len(history) == 2
    # Ensure sorted by date descending
    assert history[0].date == "2026-08-03"
    assert history[1].date == "2026-08-01"


def test_get_meal_history_empty(repo: DynamoRepository) -> None:
    """Test meal history when no meals logged."""
    history = repo.get_meal_history("user123")
    assert history == []


def test_save_and_get_current_plan(repo: DynamoRepository) -> None:
    """Test saving plan and fetching current plan."""
    ing = Ingredient(item="Rice", amount="200g")
    meal = PlannedMeal(
        meal_type="lunch",
        name="Fried Rice",
        ingredients=[ing],
        est_calories=450,
        was_cooked=False,
    )
    day1 = PlanDay(day=1, meals=[meal])
    section = GrocerySection(name="Pantry", items=["Rice"])
    plan = WeeklyPlan(
        week_start="2026-08-10",
        status="draft",
        days=[day1],
        grocery_list=[section],
    )

    repo.save_plan("user123", plan)

    current = repo.get_current_plan("user123")
    assert current is not None
    assert current.week_start_date == "2026-08-10"
    assert current.status == "draft"
    assert len(current.days) == 1
    assert current.days[0].meals[0].name == "Fried Rice"


def test_get_current_plan_empty(repo: DynamoRepository) -> None:
    """Test get_current_plan when no plan exists."""
    plan = repo.get_current_plan("user123")
    assert plan is None


def test_update_meal_status(repo: DynamoRepository) -> None:
    """Test updating was_cooked meal status within a weekly plan."""
    meal = PlannedMeal(
        meal_type="dinner",
        name="Steak",
        ingredients=[],
        est_calories=700,
        was_cooked=False,
    )
    day1 = PlanDay(day=1, meals=[meal])
    plan = WeeklyPlan(
        week_start="2026-08-10",
        status="confirmed",
        days=[day1],
        grocery_list=[],
    )
    repo.save_plan("user123", plan)

    # Update status to True
    updated = repo.update_meal_status(
        user_id="user123",
        week_start="2026-08-10",
        day=1,
        meal_type="dinner",
        was_cooked=True,
    )
    assert updated is True

    # Verify updated in DB
    current = repo.get_current_plan("user123")
    assert current is not None
    assert current.days[0].meals[0].was_cooked is True


def test_update_meal_status_not_found(repo: DynamoRepository) -> None:
    """Test update_meal_status returns False when plan does not exist."""
    updated = repo.update_meal_status(
        user_id="user123",
        week_start="2026-08-10",
        day=1,
        meal_type="dinner",
        was_cooked=True,
    )
    assert updated is False
