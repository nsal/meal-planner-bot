"""Pydantic models for meal-planner persistence and LLM contracts."""

import hashlib
import json
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
MAX_MEALS_PER_DAY = 4
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
    "dietary_constraints": frozenset(
        {"no dietary constraints", "no allergies", "no restrictions"}
    ),
    "dietary_preferences": frozenset(
        {"no dietary preferences", "no preferences"}
    ),
}
_LEGACY_CONSTRAINT_NO_VALUE_PHRASES = {
    "allergies": frozenset({"no dietary constraints", "no allergies"}),
    "restrictions": frozenset({"no dietary constraints", "no restrictions"}),
}


def _is_no_value_phrase(value: Any, field_name: str) -> bool:
    """Return whether a value is an exact supported no-value phrase."""
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold().rstrip(".!?,;:")
    phrases = _GENERIC_NO_VALUE_PHRASES | _FIELD_NO_VALUE_PHRASES[field_name]
    return normalized in phrases


def _merge_legacy_constraint_field(
    value: Any, field_name: str
) -> tuple[bool, list[Any]]:
    """Return whether a legacy field was answered and its real values."""
    if value is None:
        return False, []
    values = value if isinstance(value, list) else [value]
    phrases = (
        _GENERIC_NO_VALUE_PHRASES
        | (_LEGACY_CONSTRAINT_NO_VALUE_PHRASES[field_name])
    )
    real_values = [
        item
        for item in values
        if not (
            isinstance(item, str)
            and item.strip().casefold().rstrip(".!?,;:") in phrases
        )
    ]
    return True, real_values


def _merge_legacy_constraints(
    value: dict[str, Any],
) -> tuple[bool, list[Any]]:
    """Merge legacy fields in storage order and preserve their answer state."""
    answered = False
    constraints: list[Any] = []
    for field_name in ("allergies", "restrictions"):
        field_answered, field_values = _merge_legacy_constraint_field(
            value.get(field_name), field_name
        )
        answered = answered or field_answered
        constraints.extend(field_values)
    return answered, constraints


def _normalize_legacy_constraints(
    value: Any, *, preserve_unanswered: bool
) -> Any:
    """Map legacy fields while adapting complete and partial model semantics."""
    if (
        not isinstance(value, dict)
        or "dietary_constraints" in value
        or not {"allergies", "restrictions"} & value.keys()
    ):
        return value

    answered, constraints = _merge_legacy_constraints(value)

    normalized = dict(value)
    normalized["dietary_constraints"] = (
        constraints if answered or not preserve_unanswered else None
    )
    normalized.pop("allergies", None)
    normalized.pop("restrictions", None)
    return normalized


def _normalize_profile_entries(
    value: Any, field_name: str, *, preserve_unanswered: bool
) -> Any:
    """Give old string profile entries deterministic structured shapes."""
    if not isinstance(value, dict) or field_name not in value:
        return value
    entries = value[field_name]
    if entries is None:
        return value
    if not isinstance(entries, list):
        if isinstance(entries, str) and _is_no_value_phrase(
            entries, field_name
        ):
            normalized = dict(value)
            normalized[field_name] = []
            return normalized
        return value

    normalized_entries: list[Any] = []
    for index, entry in enumerate(entries, start=1):
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        if isinstance(entry, str):
            source_text = entry.strip()
            if field_name == "dietary_constraints":
                from meal_planner.dietary_rules import expand_constraint_terms

                expansion = expand_constraint_terms([source_text])
                normalized_entries.append(
                    {
                        "id": f"legacy-constraint-{index}",
                        "source_text": source_text,
                        "forbidden_terms": list(expansion.terms),
                        "uninterpretable": not expansion.is_safe,
                    }
                )
            else:
                normalized_entries.append(
                    {
                        "id": f"legacy-preference-{index}",
                        "source_text": source_text,
                        "rule": None,
                    }
                )
            continue
        if isinstance(entry, dict):
            item = dict(entry)
            entry_kind = (
                "constraint"
                if field_name == "dietary_constraints"
                else "preference"
            )
            item.setdefault(
                "id",
                f"legacy-{entry_kind}-{index}",
            )
            if field_name == "dietary_constraints":
                raw_source_text: Any = item.get("source_text")
                if isinstance(raw_source_text, str) and (
                    "forbidden_terms" not in item
                    or item.get("forbidden_terms") == [raw_source_text]
                ):
                    from meal_planner.dietary_rules import (
                        expand_constraint_terms,
                    )

                    expansion = expand_constraint_terms([raw_source_text])
                    item["forbidden_terms"] = list(expansion.terms)
                    item["uninterpretable"] = not expansion.is_safe
            elif "rule" not in item and "foods_any_of" in item:
                rule = dict(item)
                rule["id"] = item["id"]
                item = {
                    "id": item["id"],
                    "source_text": item.get("source_text", "legacy"),
                    "rule": rule,
                }
            normalized_entries.append(item)
            continue
        normalized_entries.append(entry)

    normalized = dict(value)
    normalized[field_name] = normalized_entries
    return normalized


def _normalize_profile_models(value: Any, *, preserve_unanswered: bool) -> Any:
    """Normalize legacy profile fields before Pydantic validates them."""
    normalized = _normalize_legacy_constraints(
        value, preserve_unanswered=preserve_unanswered
    )
    normalized = _normalize_profile_entries(
        normalized,
        "dietary_constraints",
        preserve_unanswered=preserve_unanswered,
    )
    return _normalize_profile_entries(
        normalized,
        "dietary_preferences",
        preserve_unanswered=preserve_unanswered,
    )


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


def daily_meal_capacity(meal_type: MealType | None) -> int:
    """Return the bounded number of eligible meals on one day."""
    return 1 if meal_type is not None else MAX_MEALS_PER_DAY


class RuleOperator(str, Enum):
    """Count operators supported by dietary preference rules."""

    EXACTLY = "exactly"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class RuleStrength(str, Enum):
    """Whether a dietary preference participates in safety validation."""

    STRICT = "strict"
    BEST_EFFORT = "best_effort"


class Weekday(int, Enum):
    """ISO weekday values used in persisted dietary rule scopes."""

    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7


class ProfileEditCategory(str, Enum):
    """Profile categories exposed by the deterministic edit workflow."""

    FAMILY = "family"
    DIETARY_CONSTRAINTS = "dietary_constraints"
    DIETARY_PREFERENCES = "dietary_preferences"


class ProfileEditOperation(str, Enum):
    """Operations available for a selected profile category."""

    ADD = "add"
    REMOVE = "remove"
    CHANGE_CALORIES = "change_calories"
    CHANGE_PROTEIN = "change_protein"
    CHANGE_FIBRE = "change_fibre"

    def is_valid_for(self, category: ProfileEditCategory) -> bool:
        """Return whether this operation belongs to the category."""
        if category is ProfileEditCategory.FAMILY:
            return self in {
                ProfileEditOperation.ADD,
                ProfileEditOperation.REMOVE,
                ProfileEditOperation.CHANGE_CALORIES,
                ProfileEditOperation.CHANGE_PROTEIN,
                ProfileEditOperation.CHANGE_FIBRE,
            }
        return self in {
            ProfileEditOperation.ADD,
            ProfileEditOperation.REMOVE,
        }


class DietaryRule(BaseModel):
    """One bounded, structured dietary preference rule."""

    id: RequirementId
    source_text: PlanPreference
    foods_any_of: list[PreferenceFood] = Field(min_length=1, max_length=20)
    meal_type: MealType | None = None
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    operator: RuleOperator = RuleOperator.EXACTLY
    count: int = Field(ge=0, le=28)
    strength: RuleStrength = RuleStrength.STRICT

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

    @field_validator("weekdays")
    @classmethod
    def reject_duplicate_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        """Reject duplicate weekday scopes while retaining input order."""
        if len(value) != len(set(value)):
            raise ValueError("weekdays must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_count_for_scope(self) -> "DietaryRule":
        """Keep strict weekday rules within one day's capacity."""
        if self.weekdays:
            if (
                self.strength is RuleStrength.STRICT
                and self.operator
                in {RuleOperator.EXACTLY, RuleOperator.AT_LEAST}
                and self.count > daily_meal_capacity(self.meal_type)
            ):
                raise ValueError("count must fit per named weekday capacity")
        elif self.meal_type is not None and self.count > 7:
            raise ValueError("count must fit the selected meal scope")
        return self


class ConstraintEntry(BaseModel):
    """One persisted dietary constraint and its normalized forbidden terms."""

    id: RequirementId
    source_text: PlanPreference
    forbidden_terms: list[PreferenceFood] = Field(
        default_factory=list, max_length=20
    )
    uninterpretable: bool = False

    @field_validator("forbidden_terms", mode="before")
    @classmethod
    def normalize_forbidden_terms(cls, value: Any) -> Any:
        """Normalize terms without making network calls or guessing aliases."""
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        seen: set[tuple[str, ...]] = set()
        for term in value:
            if not isinstance(term, str):
                normalized.append(term)
                continue
            tokens = normalize_food(term)
            if tokens and tokens not in seen:
                seen.add(tokens)
                normalized.append(" ".join(tokens))
        return normalized

    @field_validator("forbidden_terms")
    @classmethod
    def reject_unmatchable_terms(cls, value: list[str]) -> list[str]:
        """Reject malformed terms while allowing explicit unknown input."""
        if any(not normalize_food(term) for term in value):
            raise ValueError("forbidden_terms must contain matchable terms")
        return value

    @model_validator(mode="after")
    def validate_interpretation(self) -> "ConstraintEntry":
        """Require an explicit marker when no safe terms are available."""
        if not self.forbidden_terms and not self.uninterpretable:
            raise ValueError(
                "constraints without terms must be marked uninterpretable"
            )
        if self.uninterpretable and self.forbidden_terms:
            raise ValueError(
                "uninterpretable constraints cannot contain forbidden terms"
            )
        return self


class DietaryPreferenceEntry(BaseModel):
    """Stored preference wording with an optional interpreted rule."""

    id: RequirementId
    source_text: PlanPreference
    rule: DietaryRule | None = None


class PreferenceRequirement(BaseModel):
    """One bounded, exact-count preference for a weekly meal plan.

    This legacy contract remains available while planner consumers migrate to
    :class:`DietaryRule`.
    """

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


class MealCallbackAction(str, Enum):
    """Actions supported by the single-meal submission keyboard."""

    CONFIRM = "confirm"
    CANCEL = "cancel"
    ADD = "add"
    DONE = "done"


class ConversationWorkflowKind(str, Enum):
    """Kinds of durable, multi-turn conversation workflows."""

    MEAL_LOG = "meal_log"
    PLAN_REQUEST = "plan_request"
    PLAN_REVISION = "plan_revision"
    PROFILE_EDIT = "profile_edit"


class ConversationWorkflowStep(str, Enum):
    """Steps supported by durable conversation workflows."""

    AWAITING_MEAL_INPUT = "awaiting_meal_input"
    AWAITING_MEAL_CONFIRMATION = "awaiting_meal_confirmation"
    AWAITING_MEAL_CONTINUATION = "awaiting_meal_continuation"
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
    stored_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=20,
        validation_alias=AliasChoices("stored_rules", "stored_preferences"),
    )
    current_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=20,
        validation_alias=AliasChoices("current_rules", "current_preferences"),
    )
    effective_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=20,
        validation_alias=AliasChoices(
            "effective_rules", "effective_preference_rules"
        ),
    )
    constraint_rules: list[ConstraintEntry] = Field(
        default_factory=list,
        max_length=20,
        validation_alias=AliasChoices("constraint_rules", "constraints"),
    )
    profile_category: ProfileEditCategory | None = None
    profile_operation: ProfileEditOperation | None = None
    amendment: PlanInstruction | None = None
    target_week: date | None = Field(
        default=None,
        validation_alias=AliasChoices("target_week", "week_start"),
    )
    expected_plan_revision: int | None = Field(default=None, ge=0)
    request_id: RequestId | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    expires_at: int = Field(ge=1)
    last_update_id: str | None = None

    @property
    def stored_preferences(self) -> list[DietaryRule]:
        """Return stored preference rules using profile terminology."""
        return self.stored_rules

    @property
    def current_preferences(self) -> list[DietaryRule]:
        """Return current plan preference rules."""
        return self.current_rules

    @property
    def constraints(self) -> list[ConstraintEntry]:
        """Return independent constraint rules."""
        return self.constraint_rules

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
        tier_ids = [
            rule.id
            for rules in (
                self.constraint_rules,
                self.stored_rules,
                self.current_rules,
            )
            for rule in rules
        ]
        if len(tier_ids) != len(set(tier_ids)):
            raise ValueError("planning rule tiers must have unique IDs")
        meal_steps = {
            ConversationWorkflowStep.AWAITING_MEAL_INPUT,
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
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
                or self.stored_rules
                or self.current_rules
                or self.effective_rules
                or self.constraint_rules
                or self.profile_category is not None
                or self.profile_operation is not None
                or self.amendment is not None
                or self.target_week is not None
                or self.expected_plan_revision is not None
            ):
                raise ValueError("meal workflows cannot contain plan fields")

            draft_is_empty = (
                self.meal_draft.date is None
                and self.meal_draft.meal_type is None
                and self.meal_draft.description is None
            )
            draft_is_complete = (
                self.meal_draft.date is not None
                and self.meal_draft.meal_type is not None
                and self.meal_draft.description is not None
            )
            if self.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT:
                if self.request_id is None or not draft_is_empty:
                    raise ValueError(
                        "meal input states require an empty draft and "
                        "request ID"
                    )
            elif self.step in {
                ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
                ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            }:
                if self.request_id is None or not draft_is_complete:
                    raise ValueError(
                        "meal review states require a complete draft and "
                        "request ID"
                    )
            elif self.request_id is not None:
                raise ValueError(
                    "legacy meal states cannot contain a request ID"
                )
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
                or self.stored_rules
                or self.current_rules
                or self.effective_rules
                or self.constraint_rules
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
                or self.stored_rules
                or self.current_rules
                or self.effective_rules
                or self.constraint_rules
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
            if self.step in {
                ConversationWorkflowStep.AWAITING_MEAL_INPUT,
                ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
                ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            }:
                return self
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
    stored_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=MAX_PLAN_REQUIREMENTS,
        validation_alias=AliasChoices("stored_rules", "stored_preferences"),
    )
    current_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=MAX_PLAN_REQUIREMENTS,
        validation_alias=AliasChoices("current_rules", "current_preferences"),
    )
    effective_rules: list[DietaryRule] = Field(
        default_factory=list,
        max_length=MAX_PLAN_REQUIREMENTS,
        validation_alias=AliasChoices(
            "effective_rules", "effective_preference_rules"
        ),
    )
    constraint_rules: list[ConstraintEntry] = Field(
        default_factory=list,
        max_length=MAX_PLAN_REQUIREMENTS,
        validation_alias=AliasChoices("constraint_rules", "constraints"),
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
        for rules, label in (
            (self.stored_rules, "stored rules"),
            (self.current_rules, "current rules"),
            (self.effective_rules, "effective rules"),
            (self.constraint_rules, "constraint rules"),
        ):
            rule_ids = [rule.id for rule in rules]
            if len(rule_ids) != len(set(rule_ids)):
                raise ValueError(f"{label} must have unique IDs")
        tier_ids = [
            rule.id
            for rules in (
                self.constraint_rules,
                self.stored_rules,
                self.current_rules,
            )
            for rule in rules
        ]
        if len(tier_ids) != len(set(tier_ids)):
            raise ValueError("planning rule tiers must have unique IDs")
        return self

    @property
    def stored_preferences(self) -> list[DietaryRule]:
        """Return stored preference rules using profile terminology."""
        return self.stored_rules

    @property
    def current_preferences(self) -> list[DietaryRule]:
        """Return current plan preference rules."""
        return self.current_rules

    @property
    def constraints(self) -> list[ConstraintEntry]:
        """Return independent constraint rules."""
        return self.constraint_rules


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
    """Family member with optional daily nutrition targets."""

    name: ShortText
    calorie_target: int = Field(ge=1, le=10_000)
    protein_target: int | None = Field(default=None, ge=1, le=1_000)
    fibre_target: int | None = Field(default=None, ge=1, le=1_000)


class UserProfile(BaseModel):
    """Persisted user profile."""

    name: ShortText
    family_members: list[FamilyMember] = Field(default_factory=list)
    dietary_constraints: list[ConstraintEntry] = Field(
        default_factory=list, max_length=MAX_PLAN_REQUIREMENTS
    )
    dietary_preferences: list[DietaryPreferenceEntry] = Field(
        default_factory=list, max_length=MAX_PLAN_REQUIREMENTS
    )
    people_count: int = Field(default=1, ge=1, le=20)
    profile_revision: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Map legacy persisted constraint fields to the canonical field."""
        return _normalize_profile_models(value, preserve_unanswered=False)

    @field_validator("dietary_constraints")
    @classmethod
    def deduplicate_constraints(
        cls, value: list[ConstraintEntry]
    ) -> list[ConstraintEntry]:
        """Keep the first spelling of each case-insensitive constraint."""
        deduplicated: list[ConstraintEntry] = []
        seen: set[str] = set()
        for constraint in value:
            key = constraint.source_text.casefold()
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


def _application_owned_id(namespace: str, payload: dict[str, Any]) -> str:
    """Return a deterministic, opaque ID owned by this application."""
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"r-{namespace}-{digest}"


def _canonical_foods(foods: list[str]) -> list[str]:
    """Return normalized food alternatives in deterministic order."""
    return sorted(" ".join(normalize_food(food)) for food in foods)


def application_owned_dietary_rule_id(
    rule: DietaryRule, *, namespace: str
) -> str:
    """Build a stable ID from rule content and its owning application tier."""
    return _application_owned_id(
        namespace,
        {
            "source_text": rule.source_text.casefold().strip(),
            "foods_any_of": _canonical_foods(rule.foods_any_of),
            "meal_type": rule.meal_type.value if rule.meal_type else None,
            "weekdays": sorted(day.value for day in rule.weekdays),
            "operator": rule.operator.value,
            "count": rule.count,
            "strength": rule.strength.value,
        },
    )


def application_owned_constraint_id(
    constraint: ConstraintEntry, *, namespace: str
) -> str:
    """Build a stable ID from canonical constraint content and its tier."""
    return _application_owned_id(
        namespace,
        {
            "source_text": constraint.source_text.casefold().strip(),
            "forbidden_terms": _canonical_foods(constraint.forbidden_terms),
            "uninterpretable": constraint.uninterpretable,
        },
    )


def application_owned_text_id(source_text: str, *, namespace: str) -> str:
    """Build a stable ID for an uninterpreted persisted profile entry."""
    return _application_owned_id(
        namespace, {"source_text": source_text.casefold().strip()}
    )


def canonicalize_dietary_rule(
    rule: DietaryRule, *, namespace: str
) -> DietaryRule:
    """Replace provider identity with a stable application-owned identity."""
    return rule.model_copy(
        update={
            "id": application_owned_dietary_rule_id(rule, namespace=namespace)
        }
    )


def canonicalize_constraint_entry(
    constraint: ConstraintEntry, *, namespace: str
) -> ConstraintEntry:
    """Replace a constraint provider ID with an application-owned identity."""
    return constraint.model_copy(
        update={
            "id": application_owned_constraint_id(
                constraint, namespace=namespace
            )
        }
    )


def canonicalize_profile_rule_ids(profile: UserProfile) -> UserProfile:
    """Canonicalize every persisted dietary rule without dropping content."""
    validated = UserProfile.model_validate(
        profile.model_dump(mode="json", warnings=False)
    )
    constraints = []
    for constraint in validated.dietary_constraints:
        canonical = canonicalize_constraint_entry(
            constraint, namespace="profile-constraint"
        )
        constraints.append(canonical.model_dump(mode="json"))
    preferences: list[dict[str, Any]] = []
    for preference in validated.dietary_preferences:
        if preference.rule is None:
            identifier = application_owned_text_id(
                preference.source_text, namespace="profile-preference"
            )
            rule_data = None
        else:
            rule = canonicalize_dietary_rule(
                preference.rule, namespace="profile-preference"
            )
            identifier = rule.id
            rule_data = rule.model_dump(mode="json")
        preferences.append(
            {
                "id": identifier,
                "source_text": preference.source_text,
                "rule": rule_data,
            }
        )
    data = validated.model_dump(mode="json", warnings=False)
    data["dietary_constraints"] = constraints
    data["dietary_preferences"] = preferences
    return UserProfile.model_validate(data)


class ProfileUpdateEntities(BaseModel):
    """Explicit fields accepted from an LLM profile-update intent."""

    name: ShortText | None = None
    people_count: int | None = Field(default=None, ge=1, le=20)
    family_members: list[FamilyMember] | None = None
    dietary_constraints: list[ConstraintEntry] | None = Field(
        default=None, max_length=MAX_PLAN_REQUIREMENTS
    )
    dietary_preferences: list[DietaryPreferenceEntry] | None = Field(
        default=None, max_length=MAX_PLAN_REQUIREMENTS
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Map legacy profile-update fields to the canonical field."""
        return _normalize_profile_models(value, preserve_unanswered=True)

    @field_validator(
        "dietary_constraints",
        "dietary_preferences",
        mode="before",
    )
    @classmethod
    def normalize_no_value_phrase(cls, value: Any, info: ValidationInfo) -> Any:
        """Convert explicit no-value answers to empty lists."""
        field_name = info.field_name
        if field_name is None:
            return value
        return [] if _is_no_value_phrase(value, field_name) else value

    @field_validator("dietary_constraints")
    @classmethod
    def deduplicate_constraints(
        cls, value: list[ConstraintEntry] | None
    ) -> list[ConstraintEntry] | None:
        """Keep the first spelling of each case-insensitive constraint."""
        if value is None:
            return None
        deduplicated: list[ConstraintEntry] = []
        seen: set[str] = set()
        for constraint in value:
            key = constraint.source_text.casefold()
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
