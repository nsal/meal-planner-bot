"""Tests for prompt builders and context assembly."""

from meal_planner.llm.prompts import (
    build_conversational_prompt,
    build_grocery_prompt,
    build_plan_prompt,
)
from meal_planner.models.schemas import (
    FamilyMember,
    Ingredient,
    MealLogEntry,
    PlanDay,
    PlannedMeal,
    UserProfile,
    WeeklyPlan,
)


def test_build_conversational_prompt_empty() -> None:
    """Test build_conversational_prompt with None/empty parameters."""
    prompt = build_conversational_prompt()
    assert "No user profile established yet." in prompt
    assert "No active meal plan." in prompt
    assert "No recent meal history logged." in prompt
    assert "log_meal" in prompt


def test_build_conversational_prompt_with_context() -> None:
    """Test build_conversational_prompt with full user context."""
    member = FamilyMember(name="Alice", calorie_target=1800)
    profile = UserProfile(
        name="John",
        family_members=[member],
        allergies=["peanuts"],
        dietary_preferences=["low-carb"],
        restrictions=["dairy-free"],
        goals=["weight-loss"],
        people_count=2,
    )
    meal = PlannedMeal(meal_type="lunch", name="Salad")
    day = PlanDay(day=1, meals=[meal])
    plan = WeeklyPlan(week_start="2026-08-10", status="confirmed", days=[day])
    history = [
        MealLogEntry(
            date="2026-08-04",
            meal_type="dinner",
            description="Tacos",
            created_at="2026-08-04T18:00:00Z",
        )
    ]

    prompt = build_conversational_prompt(
        profile=profile, current_plan=plan, recent_meals=history
    )
    assert "John" in prompt
    assert "Alice (1800 kcal)" in prompt
    assert "peanuts" in prompt
    assert "low-carb" in prompt
    assert "Status: confirmed" in prompt
    assert "Day 1: lunch: Salad" in prompt
    assert "Tacos" in prompt


def test_build_plan_prompt_empty() -> None:
    """Test build_plan_prompt with default/empty parameters."""
    prompt = build_plan_prompt()
    assert "General profile" in prompt
    assert "2000 kcal/day" in prompt
    assert "Week Start Date: 2026-08-10" in prompt
    assert "OUTPUT JSON SCHEMA" in prompt


def test_build_plan_prompt_with_context() -> None:
    """Test build_plan_prompt with full profile, history, and previous plan."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="Alice",
        family_members=[member],
        allergies=["shellfish"],
        dietary_preferences=["keto"],
        people_count=3,
    )
    history = [
        MealLogEntry(
            date="2026-08-03",
            meal_type="dinner",
            description="Salmon",
            created_at="2026-08-03T18:00:00Z",
        )
    ]
    prev_meal_cooked = PlannedMeal(
        meal_type="dinner", name="Steak", was_cooked=True
    )
    prev_meal_skipped = PlannedMeal(
        meal_type="lunch", name="Soup", was_cooked=False
    )
    prev_plan = WeeklyPlan(
        week_start="2026-08-03",
        status="confirmed",
        days=[
            PlanDay(day=1, meals=[prev_meal_cooked]),
            PlanDay(day=2, meals=[prev_meal_skipped]),
        ],
    )

    prompt = build_plan_prompt(
        profile=profile,
        meal_history=history,
        previous_plan=prev_plan,
        week_start="2026-08-10",
    )
    assert "Primary User: Alice" in prompt
    assert "Bob (2200 kcal/day)" in prompt
    assert "shellfish" in prompt
    assert "Salmon" in prompt
    assert "Cooked: Steak" in prompt
    assert "Skipped: Soup" in prompt


def test_build_grocery_prompt() -> None:
    """Test build_grocery_prompt context scaling and formatting."""
    ing1 = Ingredient(item="Chicken", amount="500g")
    ing2 = Ingredient(item="Rice", amount="200g")
    meal = PlannedMeal(
        meal_type="dinner", name="Chicken Rice", ingredients=[ing1, ing2]
    )
    day = PlanDay(day=1, meals=[meal])
    plan = WeeklyPlan(week_start="2026-08-10", days=[day])

    prompt = build_grocery_prompt(plan=plan, people_count=4)
    assert "Scale quantities for 4 people." in prompt
    assert "Day 1 dinner (Chicken Rice): 500g Chicken, 200g Rice" in prompt
    assert "Produce, Dairy, Pantry" in prompt
