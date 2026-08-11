"""Pydantic models for meal-planner persistence and LLM contracts."""

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, model_validator

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
MealDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ConversationIntent(str, Enum):
    """Supported conversational mutations and response intents."""

    LOG_MEAL = "log_meal"
    EDIT_PLAN = "edit_plan"
    UPDATE_PROFILE = "update_profile"
    CONFIRM_PLAN = "confirm_plan"
    SUGGESTION = "suggestion"
    CHITCHAT = "chitchat"


class PlanStatus(str, Enum):
    """Lifecycle state for a weekly meal plan."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"


class MealOutcome(str, Enum):
    """A user's reported outcome for a planned meal."""

    UNREPORTED = "unreported"
    COOKED = "cooked"
    SKIPPED = "skipped"
    SWAPPED = "swapped"


class GroceryStatus(str, Enum):
    """Asynchronous grocery-list generation state."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    READY = "ready"
    ERROR = "error"


class MealType(str, Enum):
    """Meal types safe for prompts and Telegram callback payloads."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class LLMResponseMetadata(BaseModel):
    """Metadata extracted from a conversational LLM response."""

    intent: ConversationIntent
    entities: dict[str, Any] = Field(default_factory=dict)


class FamilyMember(BaseModel):
    """Family member with an explicit daily calorie target."""

    name: ShortText
    calorie_target: int = Field(ge=1, le=10_000)


class UserProfile(BaseModel):
    """Persisted user profile."""

    name: ShortText
    family_members: list[FamilyMember] = Field(default_factory=list)
    allergies: list[ShortText] = Field(default_factory=list)
    dietary_preferences: list[ShortText] = Field(default_factory=list)
    restrictions: list[ShortText] = Field(default_factory=list)
    goals: list[ShortText] = Field(default_factory=list)
    people_count: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def validate_member_count(self) -> "UserProfile":
        """Keep populated per-person targets consistent with people count."""
        if (
            self.family_members
            and len(self.family_members) != self.people_count
        ):
            raise ValueError("family_members must match people_count")
        return self

    @property
    def is_complete(self) -> bool:
        """Return whether onboarding has a calorie target for every person."""
        return len(self.family_members) == self.people_count


class ProfileUpdateEntities(BaseModel):
    """Explicit fields accepted from an LLM profile-update intent."""

    name: ShortText | None = None
    people_count: int | None = Field(default=None, ge=1, le=20)
    family_members: list[FamilyMember] | None = None
    allergies: list[ShortText] | None = None
    dietary_preferences: list[ShortText] | None = None
    restrictions: list[ShortText] | None = None
    goals: list[ShortText] | None = None


class Ingredient(BaseModel):
    """Ingredient item with a human-readable amount."""

    item: ShortText
    amount: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=60),
    ] = ""


class PlannedMeal(BaseModel):
    """Individual meal within a daily plan."""

    meal_type: MealType
    name: ShortText
    ingredients: list[Ingredient] = Field(default_factory=list)
    est_calories: int = Field(default=0, ge=0, le=10_000)
    outcome: MealOutcome = MealOutcome.UNREPORTED


class PlanDay(BaseModel):
    """Single day in a weekly meal plan."""

    day: int = Field(ge=1, le=7)
    meals: list[PlannedMeal] = Field(default_factory=list, max_length=4)


class GrocerySection(BaseModel):
    """A non-empty supermarket section in a grocery list."""

    name: ShortText
    items: list[ShortText] = Field(min_length=1)


class WeeklyPlan(BaseModel):
    """Complete persisted weekly meal plan."""

    week_start: date = Field(alias="week_start_date")
    status: PlanStatus = PlanStatus.DRAFT
    revision: int = Field(default=0, ge=0)
    days: list[PlanDay]
    grocery_status: GroceryStatus = GroceryStatus.NOT_REQUESTED
    grocery_list: list[GrocerySection] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_complete_week(self) -> "WeeklyPlan":
        """Require exactly one plan day for every day from one to seven."""
        day_numbers = [plan_day.day for plan_day in self.days]
        if len(day_numbers) != 7 or set(day_numbers) != set(range(1, 8)):
            raise ValueError("days must contain each day from 1 through 7")
        if self.grocery_status is GroceryStatus.READY and not self.grocery_list:
            raise ValueError("ready grocery lists must contain a section")
        return self

    @property
    def week_start_date(self) -> str:
        """Return the ISO date used in DynamoDB sort keys and callbacks."""
        return self.week_start.isoformat()

    @property
    def week_end(self) -> date:
        """Return the final date covered by this seven-day plan."""
        return self.week_start.fromordinal(self.week_start.toordinal() + 6)


class MealLogEntry(BaseModel):
    """Persisted meal-history entry."""

    date: date
    meal_type: MealType
    description: MealDescription
    created_at: datetime

    @property
    def date_key(self) -> str:
        """Return a stable ISO date for DynamoDB keys."""
        return self.date.isoformat()
