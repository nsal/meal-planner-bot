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
    MealOutcome,
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
        family_members=[
            member,
            FamilyMember(name="John", calorie_target=2100),
        ],
        allergies=["peanuts"],
        dietary_preferences=["low-carb"],
        restrictions=["dairy-free"],
        goals=["weight-loss"],
        people_count=2,
    )
    meal = PlannedMeal(meal_type="lunch", name="Salad")
    day = PlanDay(day=1, meals=[meal])
    days = [day, *(PlanDay(day=value) for value in range(2, 8))]
    plan = WeeklyPlan(week_start="2026-08-10", status="confirmed", days=days)
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
    assert "at most four meals per day" in prompt
    assert "OUTPUT JSON SCHEMA" in prompt


def test_build_plan_prompt_with_context() -> None:
    """Test build_plan_prompt with full profile, history, and previous plan."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="Alice",
        family_members=[
            member,
            FamilyMember(name="Alice", calorie_target=1800),
            FamilyMember(name="Charlie", calorie_target=2000),
        ],
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
        meal_type="dinner", name="Steak", outcome=MealOutcome.COOKED
    )
    prev_meal_skipped = PlannedMeal(
        meal_type="lunch", name="Soup", outcome=MealOutcome.SKIPPED
    )
    prev_meal_swapped = PlannedMeal(
        meal_type="dinner", name="Pasta", outcome=MealOutcome.SWAPPED
    )
    prev_meal_unreported = PlannedMeal(
        meal_type="snack", name="Fruit", outcome=MealOutcome.UNREPORTED
    )
    prev_plan = WeeklyPlan(
        week_start="2026-08-03",
        status="confirmed",
        days=[
            PlanDay(day=1, meals=[prev_meal_cooked]),
            PlanDay(day=2, meals=[prev_meal_skipped]),
            PlanDay(day=3, meals=[prev_meal_swapped, prev_meal_unreported]),
            *(PlanDay(day=value) for value in range(4, 8)),
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
    assert "Swapped: Pasta" in prompt
    assert "Fruit" not in prompt


def test_build_grocery_prompt() -> None:
    """Test build_grocery_prompt context scaling and formatting."""
    ing1 = Ingredient(item="Chicken", amount="500g")
    ing2 = Ingredient(item="Rice", amount="200g")
    meal = PlannedMeal(
        meal_type="dinner", name="Chicken Rice", ingredients=[ing1, ing2]
    )
    day = PlanDay(day=1, meals=[meal])
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[day, *(PlanDay(day=value) for value in range(2, 8))],
    )

    prompt = build_grocery_prompt(plan=plan, people_count=4)
    assert "Scale quantities for 4 people." in prompt
    assert "Day 1 dinner (Chicken Rice): 500g Chicken, 200g Rice" in prompt
    assert "Produce, Dairy, Pantry" in prompt
