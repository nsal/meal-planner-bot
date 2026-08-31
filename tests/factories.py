"""Shared factories for retained domain objects."""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    MealLogEntry,
    MealType,
    PlanChatAction,
    PlanChatEvent,
    ProfileDraft,
    UserProfile,
)


def make_legacy_profile_item() -> dict[str, Any]:
    """Return a saved profile using the supported legacy shape."""
    return {
        "name": "Alex",
        "people_count": 1,
        "family_members": [],
        "dietary_constraints": ["Peanuts"],
        "dietary_preferences": ["More vegetables"],
        "batch_rules": [{"source_text": "obsolete"}],
    }


def make_profile(
    *,
    with_nutrient_targets: bool = False,
    dietary_constraints: list[str] | None = None,
    dietary_preferences: list[str] | None = None,
) -> UserProfile:
    """Return a complete two-person profile."""
    alex_targets = (
        {"protein_target": 120, "fibre_target": 30}
        if with_nutrient_targets
        else {}
    )
    sam_targets = {"protein_target": 100} if with_nutrient_targets else {}
    return UserProfile(
        name="Alex",
        people_count=2,
        family_members=[
            FamilyMember(name="Alex", calorie_target=2_000, **alex_targets),
            FamilyMember(name="Sam", calorie_target=1_800, **sam_targets),
        ],
        dietary_constraints=dietary_constraints or [],
        dietary_preferences=dietary_preferences or [],
    )


def make_profile_draft() -> ProfileDraft:
    """Return an empty deterministic setup draft."""
    return ProfileDraft()


def make_plan_chat_state(
    step: ConversationWorkflowStep = (
        ConversationWorkflowStep.AWAITING_PLAN_REQUEST
    ),
    *,
    initial_request: str = "Plan three family dinners",
    pending_message: str | None = None,
    latest_response: str | None = None,
    context_date: date = date(2026, 8, 28),
    revision: int = 0,
    now: datetime | None = None,
) -> ConversationState:
    """Return a valid state for any temporary plan-chat phase."""
    state_time = now or datetime.now(timezone.utc)
    session_id = str(uuid4())
    if step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST:
        values: dict[str, Any] = {
            "session_id": session_id,
        }
    else:
        response = latest_response or (
            "Here is a draft."
            if step is ConversationWorkflowStep.PLAN_CHAT_READY
            else None
        )
        values = {
            "session_id": session_id,
            "request_id": str(uuid4()),
            "initial_request": initial_request,
            "pending_message": pending_message or initial_request,
            "latest_response": response,
            "context_date": context_date,
        }
    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_CHAT,
        step=step,
        revision=revision,
        created_at=state_time,
        updated_at=state_time,
        expires_at=state_time + timedelta(hours=24),
        **values,
    )


def make_plan_chat_event(
    *,
    session_id: str | None = None,
    request_id: str | None = None,
    state_revision: int = 0,
) -> PlanChatEvent:
    """Return a valid identifier-only plan-chat event."""
    return PlanChatEvent(
        action=PlanChatAction.GENERATE_PLAN_CHAT,
        user_id="12345",
        chat_id=12345,
        session_id=session_id or str(uuid4()),
        request_id=request_id or str(uuid4()),
        state_revision=state_revision,
    )


def make_meal(
    meal_date: date = date(2026, 8, 28),
    meal_type: MealType = MealType.DINNER,
    description: str = "Pasta",
) -> MealLogEntry:
    """Return one submitted meal-history entry."""
    return MealLogEntry(
        date=meal_date,
        meal_type=meal_type,
        description=description,
        created_at=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
    )
