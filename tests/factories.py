"""Shared typed factories for complete domain objects."""

from datetime import date
from typing import Any

from meal_planner.models.schemas import (
    FamilyMember,
    GrocerySection,
    GroceryStatus,
    MealOutcome,
    PlanDay,
    PlannedMeal,
    PlanStatus,
    UserProfile,
    WeeklyPlan,
)


def make_profile() -> UserProfile:
    """Return a complete two-person profile."""
    return UserProfile(
        name="Alex",
        people_count=2,
        family_members=[
            FamilyMember(name="Alex", calorie_target=2000),
            FamilyMember(name="Sam", calorie_target=1800),
        ],
        allergies=[],
        dietary_preferences=["balanced"],
        restrictions=[],
        goals=["eat well"],
    )


def make_plan(
    *,
    week_start: date | None = None,
    status: PlanStatus = PlanStatus.DRAFT,
    revision: int = 0,
    grocery_status: GroceryStatus = GroceryStatus.NOT_REQUESTED,
    outcome: MealOutcome = MealOutcome.UNREPORTED,
    planning_instructions: list[str] | None = None,
) -> WeeklyPlan:
    """Return a complete seven-day plan with one lunch per day."""
    groceries = (
        [GrocerySection(name="Produce", items=["Apples"])]
        if grocery_status is GroceryStatus.READY
        else []
    )
    return WeeklyPlan(
        week_start=week_start or date.today(),
        status=status,
        revision=revision,
        grocery_status=grocery_status,
        grocery_list=groceries,
        planning_instructions=planning_instructions or [],
        days=[
            PlanDay(
                day=day,
                meals=[
                    PlannedMeal(
                        meal_type="lunch",
                        name=f"Lunch {day}",
                        est_calories=500,
                        outcome=outcome,
                    )
                ],
            )
            for day in range(1, 8)
        ],
    )


def make_plan_payload(week_start: date | None = None) -> dict[str, Any]:
    """Serialize a complete plan for mocked LLM JSON responses."""
    return make_plan(week_start=week_start).model_dump(
        by_alias=True, mode="json"
    )
