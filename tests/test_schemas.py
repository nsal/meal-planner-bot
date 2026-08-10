"""Tests for Pydantic data models and schemas."""

import pytest
from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationIntent,
    FamilyMember,
    GrocerySection,
    Ingredient,
    LLMResponseMetadata,
    MealLogEntry,
    PlanDay,
    PlannedMeal,
    UserProfile,
    WeeklyPlan,
)


def test_family_member_valid() -> None:
    """Test FamilyMember model instantiation with valid data."""
    member = FamilyMember(name="Alice", calorie_target=2000)
    assert member.name == "Alice"
    assert member.calorie_target == 2000


def test_family_member_invalid_calories() -> None:
    """Test FamilyMember model with negative calorie target."""
    with pytest.raises(ValidationError):
        FamilyMember(name="Alice", calorie_target=-100)


def test_user_profile_defaults() -> None:
    """Test UserProfile model with default optional fields."""
    profile = UserProfile(name="John Doe")
    assert profile.name == "John Doe"
    assert profile.family_members == []
    assert profile.allergies == []
    assert profile.dietary_preferences == []
    assert profile.restrictions == []
    assert profile.goals == []
    assert profile.people_count == 1


def test_user_profile_full() -> None:
    """Test UserProfile model with full data."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="John Doe",
        family_members=[member],
        allergies=["peanuts"],
        dietary_preferences=["keto"],
        restrictions=["gluten-free"],
        goals=["weight-loss"],
        people_count=2,
    )
    assert len(profile.family_members) == 1
    assert profile.allergies == ["peanuts"]
    assert profile.people_count == 2


def test_user_profile_invalid_people_count() -> None:
    """Test UserProfile with invalid people_count (< 1)."""
    with pytest.raises(ValidationError):
        UserProfile(name="John", people_count=0)


def test_ingredient_and_planned_meal() -> None:
    """Test Ingredient and PlannedMeal model instantiation."""
    ing = Ingredient(item="Chicken breast", amount="500g")
    meal = PlannedMeal(
        meal_type="dinner",
        name="Grilled Chicken",
        ingredients=[ing],
        est_calories=650,
        was_cooked=True,
    )
    assert meal.meal_type == "dinner"
    assert meal.name == "Grilled Chicken"
    assert len(meal.ingredients) == 1
    assert meal.ingredients[0].item == "Chicken breast"
    assert meal.was_cooked is True


def test_plan_day_validation() -> None:
    """Test PlanDay model validation."""
    day = PlanDay(day=3)
    assert day.day == 3
    assert day.meals == []

    with pytest.raises(ValidationError):
        PlanDay(day=0)

    with pytest.raises(ValidationError):
        PlanDay(day=8)


def test_weekly_plan_and_grocery_section() -> None:
    """Test WeeklyPlan and GrocerySection instantiation."""
    sec = GrocerySection(name="Produce", items=["Apples", "Bananas"])
    day1 = PlanDay(day=1)
    plan = WeeklyPlan(
        week_start="2026-08-10",
        status="confirmed",
        days=[day1],
        grocery_list=[sec],
    )
    assert plan.week_start == "2026-08-10"
    assert plan.week_start_date == "2026-08-10"
    assert plan.status == "confirmed"
    assert len(plan.days) == 1
    assert len(plan.grocery_list) == 1


def test_meal_log_entry() -> None:
    """Test MealLogEntry model instantiation."""
    entry = MealLogEntry(
        date="2026-08-05",
        meal_type="lunch",
        description="Salad with olive oil",
        created_at="2026-08-05T12:30:00Z",
    )
    assert entry.date == "2026-08-05"
    assert entry.meal_type == "lunch"


def test_llm_response_metadata_and_intent() -> None:
    """Test ConversationIntent enum and LLMResponseMetadata."""
    meta = LLMResponseMetadata(
        intent=ConversationIntent.LOG_MEAL,
        entities={"meal_type": "lunch"},
    )
    assert meta.intent == "log_meal"
    assert meta.entities["meal_type"] == "lunch"


def test_llm_response_metadata_invalid_intent() -> None:
    """Test LLMResponseMetadata with an invalid intent."""
    with pytest.raises(ValidationError):
        LLMResponseMetadata(intent="invalid_intent")  # type: ignore[arg-type]
