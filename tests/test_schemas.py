"""Tests for Pydantic data models and schemas."""

import pytest
from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationIntent,
    FamilyMember,
    GrocerySection,
    GroceryStatus,
    Ingredient,
    LLMResponseMetadata,
    MealLogEntry,
    MealOutcome,
    PlanDay,
    PlannedMeal,
    PlanStatus,
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
        family_members=[
            member,
            FamilyMember(name="Jane Doe", calorie_target=1800),
        ],
        allergies=["peanuts"],
        dietary_preferences=["keto"],
        restrictions=["gluten-free"],
        goals=["weight-loss"],
        people_count=2,
    )
    assert len(profile.family_members) == 2
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
        outcome=MealOutcome.COOKED,
    )
    assert meal.meal_type == "dinner"
    assert meal.name == "Grilled Chicken"
    assert len(meal.ingredients) == 1
    assert meal.ingredients[0].item == "Chicken breast"
    assert meal.outcome is MealOutcome.COOKED


def test_plan_day_validation() -> None:
    """Test PlanDay model validation."""
    day = PlanDay(day=3)
    assert day.day == 3
    assert day.meals == []

    with pytest.raises(ValidationError):
        PlanDay(day=0)

    with pytest.raises(ValidationError):
        PlanDay(day=8)

    with pytest.raises(ValidationError):
        PlanDay(
            day=3,
            meals=[
                PlannedMeal(meal_type="lunch", name=f"Lunch {number}")
                for number in range(5)
            ],
        )

    distinct_meals = PlanDay(
        day=3,
        meals=[
            PlannedMeal(meal_type=meal_type, name=meal_type)
            for meal_type in ("breakfast", "lunch", "dinner", "snack")
        ],
    )
    assert len(distinct_meals.meals) == 4

    with pytest.raises(
        ValidationError,
        match="at most one meal of each meal type",
    ):
        PlanDay(
            day=3,
            meals=[
                PlannedMeal(meal_type="lunch", name="Soup"),
                PlannedMeal(meal_type="lunch", name="Salad"),
            ],
        )


def test_weekly_plan_and_grocery_section() -> None:
    """Test WeeklyPlan and GrocerySection instantiation."""
    sec = GrocerySection(name="Produce", items=["Apples", "Bananas"])
    days = [PlanDay(day=day) for day in range(1, 8)]
    plan = WeeklyPlan(
        week_start="2026-08-10",
        status=PlanStatus.CONFIRMED,
        days=days,
        grocery_status=GroceryStatus.READY,
        grocery_list=[sec],
    )
    assert plan.week_start.isoformat() == "2026-08-10"
    assert plan.week_start_date == "2026-08-10"
    assert plan.status is PlanStatus.CONFIRMED
    assert len(plan.days) == 7
    assert len(plan.grocery_list) == 1


def test_weekly_plan_revision_defaults_and_round_trips() -> None:
    days = [PlanDay(day=day) for day in range(1, 8)]
    plan = WeeklyPlan(week_start="2026-08-10", days=days)
    assert plan.revision == 0
    restored = WeeklyPlan.model_validate_json(plan.model_dump_json())
    assert restored.revision == 0


def test_weekly_plan_rejects_negative_revision() -> None:
    days = [PlanDay(day=day) for day in range(1, 8)]
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", days=days, revision=-1)


def test_meal_log_entry() -> None:
    """Test MealLogEntry model instantiation."""
    entry = MealLogEntry(
        date="2026-08-05",
        meal_type="lunch",
        description="Salad with olive oil",
        created_at="2026-08-05T12:30:00Z",
    )
    assert entry.date.isoformat() == "2026-08-05"
    assert entry.meal_type.value == "lunch"


@pytest.mark.parametrize(
    "days",
    [
        [PlanDay(day=day) for day in range(1, 7)],
        [PlanDay(day=1) for _ in range(7)],
    ],
)
def test_weekly_plan_requires_complete_unique_week(
    days: list[PlanDay],
) -> None:
    """Plans must contain exactly one entry for every day of the week."""
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", days=days)


def test_weekly_plan_rejects_invalid_date_status_and_outcome() -> None:
    """Typed plan fields reject malformed LLM values."""
    days = [PlanDay(day=day) for day in range(1, 8)]
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="not-a-date", days=days)
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", status="active", days=days)
    with pytest.raises(ValidationError):
        PlannedMeal(meal_type="lunch", name="Soup", outcome="maybe")


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
