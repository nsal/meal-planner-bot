"""Pydantic models for meal-planner persistence and LLM contracts."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from meal_planner.normalization import normalize_food

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
MealDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
DateValue = date
PlanPreference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
PlanInstruction = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
PlanningInstruction = PlanInstruction
MAX_PLAN_REQUIREMENTS = 20
RequestId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
RequirementId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    ),
]
PreferenceFood = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
RepairFeedback = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=800),
]

_GENERIC_NO_VALUE_PHRASES = frozenset(
    {"none", "no", "nothing", "n/a", "not applicable"}
)
_FIELD_NO_VALUE_PHRASES = {
    "dietary_constraints": frozenset({"no dietary constraints"}),
    "dietary_preferences": frozenset(
        {"no dietary preferences", "no preferences"}
    ),
    "goals": frozenset({"no goals"}),
}


def _normalize_legacy_constraints(value: Any) -> Any:
    """Map legacy persisted constraint fields to the canonical field."""
    if (
        not isinstance(value, dict)
        or "dietary_constraints" in value
        or not {"allergies", "restrictions"} & value.keys()
    ):
        return value

    constraints: list[Any] = []
    for field_name in ("allergies", "restrictions"):
        legacy_value = value.get(field_name)
        if legacy_value is None:
            continue
        if isinstance(legacy_value, list):
            constraints.extend(legacy_value)
        else:
            constraints.append(legacy_value)

    normalized = dict(value)
    normalized["dietary_constraints"] = constraints
    normalized.pop("allergies", None)
    normalized.pop("restrictions", None)
    return normalized


class ConversationIntent(str, Enum):
    """Supported conversational mutations and response intents."""

    LOG_MEAL = "log_meal"
    EDIT_PLAN = "edit_plan"
    UPDATE_PROFILE = "update_profile"
    CONFIRM_PLAN = "confirm_plan"
    REVISE_PLAN = "revise_plan"
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


class ProfileEditCategory(str, Enum):
    """Profile categories exposed by the deterministic edit workflow."""

    FAMILY = "family"
    DIETARY_CONSTRAINTS = "dietary_constraints"
    DIETARY_PREFERENCES = "dietary_preferences"
    GOALS = "goals"


class ProfileEditOperation(str, Enum):
    """Operations available for a selected profile category."""

    ADD = "add"
    REMOVE = "remove"
    CHANGE_CALORIES = "change_calories"

    def is_valid_for(self, category: ProfileEditCategory) -> bool:
        """Return whether this operation belongs to the category."""
        if category is ProfileEditCategory.FAMILY:
            return self in {
                ProfileEditOperation.ADD,
                ProfileEditOperation.REMOVE,
                ProfileEditOperation.CHANGE_CALORIES,
            }
        return self in {
            ProfileEditOperation.ADD,
            ProfileEditOperation.REMOVE,
        }


class PreferenceRequirement(BaseModel):
    """One bounded, exact-count preference for a weekly meal plan."""

    id: RequirementId
    source_text: PlanPreference
    foods_any_of: list[PreferenceFood] = Field(min_length=1, max_length=20)
    meal_type: MealType | None = None
    exact_count: int = Field(ge=1, le=28)

    @field_validator("foods_any_of")
    @classmethod
    def reject_duplicate_foods(cls, value: list[str]) -> list[str]:
        """Reject alternatives that identify the same normalized food."""
        normalized = [normalize_food(food) for food in value]
        if any(not food_tokens for food_tokens in normalized):
            raise ValueError(
                "foods_any_of entries must contain matchable food tokens"
            )
        if len(set(normalized)) != len(value):
            raise ValueError("foods_any_of must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_count_for_scope(self) -> "PreferenceRequirement":
        """Keep exact counts within the seven-day selected meal scope."""
        if self.meal_type is not None and self.exact_count > 7:
            raise ValueError("exact_count must fit the selected meal scope")
        return self


class ConversationWorkflowKind(str, Enum):
    """Kinds of durable, multi-turn conversation workflows."""

    MEAL_LOG = "meal_log"
    PLAN_REQUEST = "plan_request"
    PLAN_REVISION = "plan_revision"
    PROFILE_EDIT = "profile_edit"


class ConversationWorkflowStep(str, Enum):
    """Steps supported by durable conversation workflows."""

    AWAITING_DATE = "awaiting_date"
    AWAITING_MEAL_TYPE = "awaiting_meal_type"
    AWAITING_DESCRIPTION = "awaiting_description"
    AWAITING_ANOTHER_MEAL = "awaiting_another_meal"
    AWAITING_PREFERENCE = "awaiting_preference"
    GENERATING = "generating"
    RETRY_READY = "retry_ready"
    PROFILE_MENU = "profile_menu"
    AWAITING_PROFILE_INPUT = "awaiting_profile_input"


class MealLogDraft(BaseModel):
    """Partially collected fields for one actual meal."""

    date: DateValue | None = None
    meal_type: MealType | None = None
    description: MealDescription | None = None


class ConversationState(BaseModel):
    """Persisted state for one user's unfinished workflow."""

    workflow_kind: ConversationWorkflowKind
    step: ConversationWorkflowStep
    meal_draft: MealLogDraft | None = None
    preference: PlanPreference | None = None
    requirements: list[PreferenceRequirement] = Field(
        default_factory=list, max_length=20
    )
    profile_category: ProfileEditCategory | None = None
    profile_operation: ProfileEditOperation | None = None
    amendment: PlanInstruction | None = None
    target_week: date | None = Field(
        default=None,
        validation_alias=AliasChoices("target_week", "week_start"),
    )
    expected_plan_revision: int | None = Field(default=None, ge=0)
    request_id: str | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    expires_at: int = Field(ge=1)
    last_update_id: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps for persisted state."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversation timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("expires_at", mode="before")
    @classmethod
    def normalize_expiry(cls, value: Any) -> Any:
        """Accept a datetime while persisting expiry as DynamoDB TTL."""
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("conversation expiry must be timezone-aware")
            return int(value.timestamp())
        return value

    @model_validator(mode="after")
    def validate_workflow_shape(self) -> "ConversationState":
        """Reject steps and fields that belong to another workflow."""
        meal_steps = {
            ConversationWorkflowStep.AWAITING_DATE,
            ConversationWorkflowStep.AWAITING_MEAL_TYPE,
            ConversationWorkflowStep.AWAITING_DESCRIPTION,
            ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
        }
        plan_steps = {
            ConversationWorkflowStep.AWAITING_PREFERENCE,
            ConversationWorkflowStep.GENERATING,
            ConversationWorkflowStep.RETRY_READY,
        }
        if self.workflow_kind is ConversationWorkflowKind.MEAL_LOG:
            if self.step not in meal_steps or self.meal_draft is None:
                raise ValueError("meal workflows require a meal draft step")
            if (
                self.preference is not None
                or self.requirements
                or self.request_id is not None
                or self.profile_category is not None
                or self.profile_operation is not None
                or self.amendment is not None
                or self.target_week is not None
                or self.expected_plan_revision is not None
            ):
                raise ValueError("meal workflows cannot contain plan fields")
        elif self.workflow_kind is ConversationWorkflowKind.PLAN_REQUEST:
            if self.step not in plan_steps or self.request_id is None:
                raise ValueError("plan workflows require a request ID step")
            if self.meal_draft is not None:
                raise ValueError("plan workflows cannot contain meal fields")
            if (
                self.amendment is not None
                or self.target_week is not None
                or self.expected_plan_revision is not None
                or self.profile_category is not None
                or self.profile_operation is not None
            ):
                raise ValueError("plan requests cannot contain revision fields")
            if self.preference is None and self.requirements:
                raise ValueError(
                    "plan requirements require a request preference"
                )
        elif self.workflow_kind is ConversationWorkflowKind.PLAN_REVISION:
            if self.step not in {
                ConversationWorkflowStep.GENERATING,
                ConversationWorkflowStep.RETRY_READY,
            }:
                raise ValueError("revision workflows require a generation step")
            if (
                self.meal_draft is not None
                or self.preference is not None
                or self.requirements
                or self.profile_category is not None
                or self.profile_operation is not None
            ):
                raise ValueError(
                    "revision workflows cannot contain other fields"
                )
            if (
                self.request_id is None
                or self.amendment is None
                or self.target_week is None
                or self.expected_plan_revision is None
            ):
                raise ValueError(
                    "revision workflows require amendment and plan snapshot"
                )
        else:
            if self.step not in {
                ConversationWorkflowStep.PROFILE_MENU,
                ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            }:
                raise ValueError("profile workflows require a profile step")
            if (
                self.meal_draft is not None
                or self.preference is not None
                or self.requirements
                or self.request_id is not None
                or self.amendment is not None
                or self.target_week is not None
                or self.expected_plan_revision is not None
            ):
                raise ValueError(
                    "profile workflows cannot contain unrelated fields"
                )
            if self.step is ConversationWorkflowStep.PROFILE_MENU:
                if (
                    self.profile_category is not None
                    or self.profile_operation is not None
                ):
                    raise ValueError(
                        "profile menus cannot contain a selected operation"
                    )
            else:
                if (
                    self.profile_category is None
                    or self.profile_operation is None
                ):
                    raise ValueError(
                        "profile input requires category and operation"
                    )
                if not self.profile_operation.is_valid_for(
                    self.profile_category
                ):
                    raise ValueError(
                        "profile operation is invalid for its category"
                    )
        if self.updated_at < self.created_at:
            raise ValueError("conversation state timestamps are out of order")
        if self.expires_at <= int(self.updated_at.timestamp()):
            raise ValueError(
                "conversation state must expire after it is updated"
            )
        if self.workflow_kind is ConversationWorkflowKind.MEAL_LOG:
            assert self.meal_draft is not None
            expected_step = (
                ConversationWorkflowStep.AWAITING_DATE
                if self.meal_draft.date is None
                else ConversationWorkflowStep.AWAITING_MEAL_TYPE
                if self.meal_draft.meal_type is None
                else ConversationWorkflowStep.AWAITING_DESCRIPTION
                if self.meal_draft.description is None
                else ConversationWorkflowStep.AWAITING_ANOTHER_MEAL
            )
            if self.step is not expected_step:
                raise ValueError("meal workflow step does not match its draft")
        return self

    @property
    def week_start(self) -> date | None:
        """Return the revision's target week using plan terminology."""
        return self.target_week


# Short aliases keep the public contract convenient for callers and tests.
WorkflowKind = ConversationWorkflowKind
WorkflowStep = ConversationWorkflowStep
PartialMealLog = MealLogDraft


class PlanGenerationContext(BaseModel):
    """Validated request-specific context carried to the planner Lambda."""

    preference: PlanPreference | None = None
    requirements: list[PreferenceRequirement] = Field(
        default_factory=list, max_length=MAX_PLAN_REQUIREMENTS
    )
    attempt: int = Field(default=1, ge=1, le=2)
    repair_feedback: RepairFeedback | None = None
    request_id: RequestId | None = None
    state_revision: int | None = Field(default=None, ge=0)
    repair_id: RequestId | None = None

    @model_validator(mode="after")
    def validate_request_pair(self) -> "PlanGenerationContext":
        """Require all lifecycle fields together for stateful requests."""
        if (self.request_id is None) != (self.state_revision is None):
            raise ValueError(
                "request_id and state_revision must be supplied together"
            )
        tracked_request = (
            self.request_id is not None and self.state_revision is not None
        )
        if self.repair_id is not None and tracked_request:
            raise ValueError("repair ID is only valid for untracked generation")
        if self.preference is None and self.requirements:
            raise ValueError("plan requirements require a request preference")
        if self.repair_feedback is not None and self.attempt != 2:
            raise ValueError(
                "repair feedback requires the second generation attempt"
            )
        if self.attempt == 2 and self.repair_feedback is None:
            raise ValueError(
                "repair feedback is required for the second generation attempt"
            )
        if self.attempt == 2 and not tracked_request and self.repair_id is None:
            raise ValueError(
                "repair ID is required for an untracked second attempt"
            )
        requirement_ids = [requirement.id for requirement in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("plan requirements must have unique IDs")
        return self


class PlanRevisionContext(BaseModel):
    """Validated context carried by an asynchronous draft revision event."""

    amendment: PlanInstruction
    request_id: RequestId
    state_revision: int = Field(ge=0)
    expected_plan_revision: int = Field(ge=0)
    week_start: date


# Keep the event-oriented name available to callers that prefer explicitness.
RevisionEventContext = PlanRevisionContext


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
    revision: int = Field(default=0, ge=0)
    family_members: list[FamilyMember] = Field(default_factory=list)
    dietary_constraints: list[ShortText] = Field(default_factory=list)
    dietary_preferences: list[ShortText] = Field(default_factory=list)
    goals: list[ShortText] = Field(default_factory=list)
    people_count: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Map legacy persisted constraint fields to the canonical field."""
        return _normalize_legacy_constraints(value)

    @field_validator("dietary_constraints")
    @classmethod
    def deduplicate_constraints(cls, value: list[str]) -> list[str]:
        """Keep the first spelling of each case-insensitive constraint."""
        deduplicated: list[str] = []
        seen: set[str] = set()
        for constraint in value:
            key = constraint.casefold()
            if key not in seen:
                seen.add(key)
                deduplicated.append(constraint)
        return deduplicated

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
    dietary_constraints: list[ShortText] | None = None
    dietary_preferences: list[ShortText] | None = None
    goals: list[ShortText] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Map legacy profile-update fields to the canonical field."""
        return _normalize_legacy_constraints(value)

    @field_validator(
        "dietary_constraints",
        "dietary_preferences",
        "goals",
        mode="before",
    )
    @classmethod
    def normalize_no_value_phrase(cls, value: Any, info: ValidationInfo) -> Any:
        """Convert explicit no-value answers to empty lists."""
        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold().rstrip(".!?,;:")
        field_name = info.field_name
        if field_name is None:
            return value
        no_value_phrases = (
            _GENERIC_NO_VALUE_PHRASES | (_FIELD_NO_VALUE_PHRASES[field_name])
        )
        return [] if normalized in no_value_phrases else value

    @field_validator("dietary_constraints")
    @classmethod
    def deduplicate_constraints(
        cls, value: list[str] | None
    ) -> list[str] | None:
        """Keep the first spelling of each case-insensitive constraint."""
        if value is None:
            return None
        deduplicated: list[str] = []
        seen: set[str] = set()
        for constraint in value:
            key = constraint.casefold()
            if key not in seen:
                seen.add(key)
                deduplicated.append(constraint)
        return deduplicated


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

    @model_validator(mode="after")
    def validate_unique_meal_types(self) -> "PlanDay":
        """Keep each meal type uniquely addressable within the day."""
        meal_types = [meal.meal_type.value for meal in self.meals]
        if len(meal_types) != len(set(meal_types)):
            raise ValueError(
                "meals must contain at most one meal of each meal type"
            )
        return self


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
    planning_instructions: list[PlanInstruction] = Field(
        default_factory=list, max_length=20
    )

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
