"""Typed models for the retained meal-planner workflows."""

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
MealDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
RawProfileText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
DateValue = date

MAX_PROFILE_DIETARY_ITEMS = 20
MAX_PROFILE_DIETARY_TEXT_LENGTH = 500
MAX_PLAN_CHAT_REQUEST_LENGTH = 2_000
MAX_PLAN_CHAT_RESPONSE_LENGTH = 4_000
MAX_PLAN_CHAT_MESSAGE_LENGTH = MAX_PLAN_CHAT_RESPONSE_LENGTH
MAX_PLAN_CHAT_STATE_BYTES = 32_000

PlanChatRequest = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_PLAN_CHAT_REQUEST_LENGTH,
    ),
]
PlanChatMessage = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_PLAN_CHAT_MESSAGE_LENGTH,
    ),
]
PlanChatResponse = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_PLAN_CHAT_RESPONSE_LENGTH,
    ),
]
PlanChatUUID = Annotated[
    str,
    StringConstraints(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab]"
            r"[0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ),
]
PlanChatIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
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


def _is_no_value_phrase(value: Any, field_name: str) -> bool:
    """Return whether a value is an exact supported no-value phrase."""
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold().rstrip(".!?,;:")
    return normalized in (
        _GENERIC_NO_VALUE_PHRASES | _FIELD_NO_VALUE_PHRASES[field_name]
    )


def _raw_profile_entry(entry: Any) -> str | None:
    """Extract only source wording from a current or legacy entry."""
    if isinstance(entry, BaseModel):
        entry = entry.model_dump(mode="python")
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        source_text = entry.get("source_text")
        if source_text is None:
            return None
        if not isinstance(source_text, str):
            raise ValueError("legacy source_text must be a string or null")
        return source_text.strip()
    raise ValueError("dietary profile entries must be strings or mappings")


def _normalize_raw_profile_fields(
    value: Any, *, preserve_none: bool = False
) -> Any:
    """Convert legacy dietary entries to bounded, uninterpreted text."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if "dietary_constraints" not in normalized and (
        "allergies" in normalized or "restrictions" in normalized
    ):
        merged: list[Any] = []
        for key in ("allergies", "restrictions"):
            field_value = normalized.get(key)
            if isinstance(field_value, list):
                merged.extend(field_value)
            elif field_value is not None:
                merged.append(field_value)
        if not (
            preserve_none
            and normalized.get("allergies") is None
            and normalized.get("restrictions") is None
        ):
            normalized["dietary_constraints"] = merged

    for field_name in ("dietary_constraints", "dietary_preferences"):
        if field_name not in normalized:
            continue
        entries = normalized[field_name]
        if entries is None:
            if preserve_none:
                continue
            normalized[field_name] = []
            continue
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        result: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            source_text = _raw_profile_entry(entry)
            if not source_text or _is_no_value_phrase(source_text, field_name):
                continue
            key = source_text.casefold()
            if key not in seen:
                seen.add(key)
                result.append(source_text)
        normalized[field_name] = result
    return normalized


class MealType(str, Enum):
    """Meal types accepted by meal logging and prompt rendering."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class MealCallbackAction(str, Enum):
    """Actions supported by the meal-review keyboard."""

    CONFIRM = "confirm"
    CANCEL = "cancel"
    ADD = "add"
    DONE = "done"


class ProfileEditCategory(str, Enum):
    """Profile categories exposed by the deterministic edit workflow."""

    FAMILY = "family"
    DIETARY_CONSTRAINTS = "dietary_constraints"
    DIETARY_PREFERENCES = "dietary_preferences"


class ProfileEditOperation(str, Enum):
    """Operations available for one selected profile category."""

    ADD = "add"
    REMOVE = "remove"
    CHANGE_CALORIES = "change_calories"
    CHANGE_PROTEIN = "change_protein"
    CHANGE_FIBRE = "change_fibre"

    def is_valid_for(self, category: ProfileEditCategory) -> bool:
        """Return whether this operation belongs to the category."""
        if category is ProfileEditCategory.FAMILY:
            return True
        return self in {
            ProfileEditOperation.ADD,
            ProfileEditOperation.REMOVE,
        }


class ConversationWorkflowKind(str, Enum):
    """Kinds of retained multi-turn workflows."""

    MEAL_LOG = "meal_log"
    PROFILE_SETUP = "profile_setup"
    PROFILE_EDIT = "profile_edit"
    PLAN_CHAT = "plan_chat"


class ConversationWorkflowStep(str, Enum):
    """Steps supported by retained workflows."""

    AWAITING_MEAL_INPUT = "awaiting_meal_input"
    AWAITING_MEAL_CONFIRMATION = "awaiting_meal_confirmation"
    AWAITING_MEAL_CONTINUATION = "awaiting_meal_continuation"
    AWAITING_DATE = "awaiting_date"
    AWAITING_MEAL_TYPE = "awaiting_meal_type"
    AWAITING_DESCRIPTION = "awaiting_description"
    AWAITING_ANOTHER_MEAL = "awaiting_another_meal"
    PROFILE_MENU = "profile_menu"
    AWAITING_PROFILE_INPUT = "awaiting_profile_input"
    AWAITING_PROFILE_FAMILY_NAME = "awaiting_profile_family_name"
    AWAITING_PROFILE_HOUSEHOLD_SIZE = "awaiting_profile_household_size"
    AWAITING_PROFILE_MEMBERS = "awaiting_profile_members"
    AWAITING_PROFILE_CONSTRAINTS = "awaiting_profile_constraints"
    AWAITING_PROFILE_PREFERENCES = "awaiting_profile_preferences"
    PLAN_CHAT_GENERATING = "plan_chat_generating"
    PLAN_CHAT_READY = "plan_chat_ready"
    AWAITING_PLAN_REQUEST = "awaiting_plan_request"

    PROFILE_SETUP_FAMILY_NAME = "awaiting_profile_family_name"
    PROFILE_SETUP_HOUSEHOLD_SIZE = "awaiting_profile_household_size"
    PROFILE_SETUP_MEMBERS = "awaiting_profile_members"
    PROFILE_SETUP_CONSTRAINTS = "awaiting_profile_constraints"
    PROFILE_SETUP_PREFERENCES = "awaiting_profile_preferences"


class PlanChatAction(str, Enum):
    """Actions accepted by the asynchronous plan-chat worker."""

    GENERATE_PLAN_CHAT = "generate_plan_chat"


class PlanChatEvent(BaseModel):
    """Identifier-only event contract for one plan-chat generation."""

    model_config = ConfigDict(extra="forbid")

    action: PlanChatAction
    user_id: PlanChatIdentifier
    chat_id: int | PlanChatIdentifier
    session_id: PlanChatUUID
    request_id: PlanChatUUID
    state_revision: int = Field(ge=0)

    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, value: int | str) -> int | str:
        """Keep Telegram chat identifiers bounded and positive."""
        if isinstance(value, bool):
            raise ValueError("chat_id must be an identifier")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError("chat_id must be positive")
            return value
        if not value:
            raise ValueError("chat_id must not be blank")
        return value

    @field_validator("session_id", "request_id")
    @classmethod
    def validate_uuid_spelling(cls, value: str) -> str:
        """Require canonical lowercase UUID spelling."""
        if str(UUID(value)) != value:
            raise ValueError("plan-chat identifiers must use canonical UUIDs")
        return value


class MealLogDraft(BaseModel):
    """Partially collected fields for one actual meal."""

    model_config = ConfigDict(extra="forbid")

    date: DateValue | None = None
    meal_type: MealType | None = None
    description: MealDescription | None = None


class ConversationState(BaseModel):
    """Persisted state for one unfinished retained workflow."""

    model_config = ConfigDict(extra="forbid")

    workflow_kind: ConversationWorkflowKind
    step: ConversationWorkflowStep
    meal_draft: MealLogDraft | None = None
    profile_category: ProfileEditCategory | None = None
    profile_operation: ProfileEditOperation | None = None
    session_id: PlanChatUUID | None = None
    initial_request: PlanChatRequest | None = None
    pending_message: PlanChatMessage | None = None
    latest_response: PlanChatResponse | None = None
    context_date: date | None = None
    request_id: PlanChatUUID | str | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    expires_at: int = Field(ge=1)
    last_update_id: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps and store them in UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversation timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("expires_at", mode="before")
    @classmethod
    def normalize_expiry(cls, value: Any) -> Any:
        """Accept a timezone-aware datetime for DynamoDB TTL values."""
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("conversation expiry must be timezone-aware")
            return int(value.timestamp())
        return value

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str | None) -> str | None:
        """Require UUID request IDs for plan chat and bounded meal IDs."""
        if value is not None and len(value) > 100:
            raise ValueError("request_id is too long")
        return value

    @model_validator(mode="after")
    def validate_workflow_shape(self) -> "ConversationState":
        """Reject cross-workflow fields and incomplete state transitions."""
        if self.updated_at < self.created_at:
            raise ValueError("conversation state timestamps are out of order")
        if self.expires_at <= int(self.updated_at.timestamp()):
            raise ValueError(
                "conversation state must expire after it is updated"
            )

        if self.workflow_kind is ConversationWorkflowKind.MEAL_LOG:
            meal_steps = {
                ConversationWorkflowStep.AWAITING_MEAL_INPUT,
                ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
                ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
                ConversationWorkflowStep.AWAITING_DATE,
                ConversationWorkflowStep.AWAITING_MEAL_TYPE,
                ConversationWorkflowStep.AWAITING_DESCRIPTION,
                ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
            }
            if self.step not in meal_steps or self.meal_draft is None:
                raise ValueError("meal workflows require a meal draft step")
            if any(
                value is not None
                for value in (
                    self.profile_category,
                    self.profile_operation,
                    self.session_id,
                    self.initial_request,
                    self.pending_message,
                    self.latest_response,
                    self.context_date,
                )
            ):
                raise ValueError("meal workflows cannot contain other fields")
            complete = (
                self.meal_draft.date is not None
                and self.meal_draft.meal_type is not None
                and self.meal_draft.description is not None
            )
            empty = (
                self.meal_draft.date is None
                and self.meal_draft.meal_type is None
                and self.meal_draft.description is None
            )
            if self.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT:
                if self.request_id is None or not empty:
                    raise ValueError("meal input states require an empty draft")
            elif self.step in {
                ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
                ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            }:
                if self.request_id is None or not complete:
                    raise ValueError(
                        "meal review states require a complete draft"
                    )
            else:
                if self.request_id is not None:
                    raise ValueError(
                        "partial meal states cannot have a request"
                    )
                expected = (
                    ConversationWorkflowStep.AWAITING_DATE
                    if self.meal_draft.date is None
                    else ConversationWorkflowStep.AWAITING_MEAL_TYPE
                    if self.meal_draft.meal_type is None
                    else ConversationWorkflowStep.AWAITING_DESCRIPTION
                    if self.meal_draft.description is None
                    else ConversationWorkflowStep.AWAITING_ANOTHER_MEAL
                )
                if self.step is not expected:
                    raise ValueError(
                        "meal workflow step does not match its draft"
                    )
        elif self.workflow_kind is ConversationWorkflowKind.PROFILE_SETUP:
            setup_steps = {
                ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
                ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
                ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS,
                ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS,
                ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
            }
            if self.step not in setup_steps:
                raise ValueError("profile setup requires a setup step")
            self._reject_non_plan_fields("profile setup")
        elif self.workflow_kind is ConversationWorkflowKind.PROFILE_EDIT:
            if self.step not in {
                ConversationWorkflowStep.PROFILE_MENU,
                ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            }:
                raise ValueError("profile editing requires a profile step")
            if any(
                value is not None
                for value in (
                    self.meal_draft,
                    self.session_id,
                    self.initial_request,
                    self.pending_message,
                    self.latest_response,
                    self.context_date,
                    self.request_id,
                )
            ):
                raise ValueError("profile editing cannot contain other fields")
            if self.step is ConversationWorkflowStep.PROFILE_MENU:
                if self.profile_category or self.profile_operation:
                    raise ValueError("profile menus cannot select an operation")
            elif (
                self.profile_category is None
                or self.profile_operation is None
                or not self.profile_operation.is_valid_for(
                    self.profile_category
                )
            ):
                raise ValueError("profile input requires a valid operation")
        else:
            if self.step not in {
                ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
                ConversationWorkflowStep.PLAN_CHAT_GENERATING,
                ConversationWorkflowStep.PLAN_CHAT_READY,
            }:
                raise ValueError("plan chat requires a plan-chat step")
            if any(
                value is not None
                for value in (
                    self.meal_draft,
                    self.profile_category,
                    self.profile_operation,
                )
            ):
                raise ValueError("plan chat cannot contain unrelated fields")
            if self.session_id is None:
                raise ValueError("plan chat requires a session ID")
            if self.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST:
                if any(
                    value is not None
                    for value in (
                        self.request_id,
                        self.initial_request,
                        self.pending_message,
                        self.latest_response,
                        self.context_date,
                    )
                ):
                    raise ValueError(
                        "awaiting plan chat cannot contain request data"
                    )
            else:
                if any(
                    value is None
                    for value in (
                        self.request_id,
                        self.initial_request,
                        self.pending_message,
                        self.context_date,
                    )
                ):
                    raise ValueError(
                        "active plan chat requires request context"
                    )
                if self.step is ConversationWorkflowStep.PLAN_CHAT_READY and (
                    self.latest_response is None
                ):
                    raise ValueError("ready plan chat requires a response")
                if self.request_id is None or len(self.request_id) != 36:
                    raise ValueError("plan chat request ID must be a UUID")
                if str(UUID(self.request_id)) != self.request_id:
                    raise ValueError("plan chat request ID must be canonical")

        if self.workflow_kind is ConversationWorkflowKind.PLAN_CHAT:
            state_size = len(
                json.dumps(
                    self.model_dump(mode="json"), separators=(",", ":")
                ).encode("utf-8")
            )
            if state_size > MAX_PLAN_CHAT_STATE_BYTES:
                raise ValueError("plan chat state exceeds its size limit")
        return self

    def _reject_non_plan_fields(self, workflow: str) -> None:
        """Reject fields that are not meaningful for profile setup."""
        if any(
            value is not None
            for value in (
                self.meal_draft,
                self.profile_category,
                self.profile_operation,
                self.session_id,
                self.initial_request,
                self.pending_message,
                self.latest_response,
                self.context_date,
                self.request_id,
            )
        ):
            raise ValueError(f"{workflow} cannot contain unrelated fields")


class FamilyMember(BaseModel):
    """Household member with optional daily nutrition targets."""

    name: ShortText
    calorie_target: int = Field(ge=1, le=10_000)
    protein_target: int | None = Field(default=None, ge=1, le=1_000)
    fibre_target: int | None = Field(default=None, ge=1, le=1_000)


class UserProfile(BaseModel):
    """Persisted household profile with raw dietary wording."""

    model_config = ConfigDict(extra="ignore")

    name: ShortText
    family_members: list[FamilyMember] = Field(default_factory=list)
    dietary_constraints: list[RawProfileText] = Field(
        default_factory=list, max_length=MAX_PROFILE_DIETARY_ITEMS
    )
    dietary_preferences: list[RawProfileText] = Field(
        default_factory=list, max_length=MAX_PROFILE_DIETARY_ITEMS
    )
    people_count: int = Field(default=1, ge=1, le=20)
    profile_revision: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Read old mappings while discarding interpreted rule structures."""
        return _normalize_raw_profile_fields(value)

    @field_validator("dietary_constraints", "dietary_preferences")
    @classmethod
    def deduplicate_dietary_text(cls, value: list[str]) -> list[str]:
        """Keep the first case-insensitive spelling of each entry."""
        result: list[str] = []
        seen: set[str] = set()
        for entry in value:
            key = entry.casefold()
            if key not in seen:
                seen.add(key)
                result.append(entry)
        return result

    @model_validator(mode="after")
    def validate_member_count(self) -> "UserProfile":
        """Require one member record per declared household member."""
        if (
            self.family_members
            and len(self.family_members) != self.people_count
        ):
            raise ValueError("family_members must match people_count")
        return self

    @property
    def is_complete(self) -> bool:
        """Return whether every household member has a calorie target."""
        return len(self.family_members) == self.people_count


class ProfileDraft(BaseModel):
    """Partially collected deterministic profile setup data."""

    model_config = ConfigDict(extra="ignore")

    name: ShortText | None = None
    people_count: int | None = Field(default=None, ge=1, le=20)
    family_members: list[FamilyMember] | None = Field(
        default=None, max_length=20
    )
    dietary_constraints: list[RawProfileText] | None = Field(
        default=None, max_length=MAX_PROFILE_DIETARY_ITEMS
    )
    dietary_preferences: list[RawProfileText] | None = Field(
        default=None, max_length=MAX_PROFILE_DIETARY_ITEMS
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_constraints(cls, value: Any) -> Any:
        """Apply the same narrow legacy read conversion as profiles."""
        return _normalize_raw_profile_fields(value, preserve_none=True)

    @field_validator("dietary_constraints", "dietary_preferences")
    @classmethod
    def deduplicate_dietary_text(
        cls, value: list[str] | None
    ) -> list[str] | None:
        """Keep the first case-insensitive spelling in draft input."""
        if value is None:
            return None
        result: list[str] = []
        seen: set[str] = set()
        for entry in value:
            key = entry.casefold()
            if key not in seen:
                seen.add(key)
                result.append(entry)
        return result

    @model_validator(mode="after")
    def validate_member_count(self) -> "ProfileDraft":
        """Keep a collected member list aligned with household size."""
        if (
            self.family_members is not None
            and self.people_count is not None
            and len(self.family_members) != self.people_count
        ):
            raise ValueError("family_members must match people_count")
        return self


class MealLogEntry(BaseModel):
    """Persisted meal-history entry."""

    model_config = ConfigDict(extra="forbid")

    date: date
    meal_type: MealType
    description: MealDescription
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_batch_link(cls, value: Any) -> Any:
        """Discard retired batch metadata before strict validation."""
        if not isinstance(value, dict) or "batch_link" not in value:
            return value
        normalized = dict(value)
        normalized.pop("batch_link")
        return normalized

    @property
    def date_key(self) -> str:
        """Return the stable ISO date used in DynamoDB keys."""
        return self.date.isoformat()
