"""Pydantic data models for meal planner bot entities and LLM schemas."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ConversationIntent(str, Enum):
    """Enumeration of user conversation intents."""

    LOG_MEAL = "log_meal"
    EDIT_PLAN = "edit_plan"
    UPDATE_PROFILE = "update_profile"
    SUGGESTION = "suggestion"
    CHITCHAT = "chitchat"


class LLMResponseMetadata(BaseModel):
    """Metadata extracted from conversational LLM response."""

    intent: ConversationIntent
    entities: dict[str, Any] = Field(default_factory=dict)


class FamilyMember(BaseModel):
    """Family member representation with calorie targets."""

    name: str
    calorie_target: int = Field(ge=0)


class UserProfile(BaseModel):
    """User profile entity."""

    name: str
    family_members: list[FamilyMember] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    dietary_preferences: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    people_count: int = Field(default=1, ge=1)


class Ingredient(BaseModel):
    """Ingredient item with quantity/amount."""

    item: str
    amount: str


class PlannedMeal(BaseModel):
    """Individual meal within a daily plan."""

    meal_type: str
    name: str
    ingredients: list[Ingredient] = Field(default_factory=list)
    est_calories: int = Field(default=0, ge=0)
    was_cooked: bool = False


class PlanDay(BaseModel):
    """Single day in a weekly meal plan."""

    day: int = Field(ge=1, le=7)
    meals: list[PlannedMeal] = Field(default_factory=list)


class GrocerySection(BaseModel):
    """Section in a grocery list (e.g. Produce, Dairy)."""

    name: str
    items: list[str] = Field(default_factory=list)


class WeeklyPlan(BaseModel):
    """Weekly meal plan entity."""

    week_start: str = Field(alias="week_start_date")
    status: str = "draft"
    days: list[PlanDay] = Field(default_factory=list)
    grocery_list: list[GrocerySection] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def week_start_date(self) -> str:
        return self.week_start


class MealLogEntry(BaseModel):
    """Meal log entry entity."""

    date: str
    meal_type: str
    description: str
    created_at: str
