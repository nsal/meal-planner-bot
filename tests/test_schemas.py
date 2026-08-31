"""Contract tests for the small retained model surface."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from meal_planner.models import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    MealLogDraft,
    MealLogEntry,
    MealType,
    PlanChatAction,
    PlanChatEvent,
    ProfileDraft,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
)
from tests.factories import make_plan_chat_state


def _timestamps() -> tuple[datetime, datetime, int]:
    """Return valid ordered timestamps and a future TTL."""
    created = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    updated = created + timedelta(minutes=1)
    return created, updated, int((updated + timedelta(hours=24)).timestamp())


def test_public_models_cover_retained_workflows() -> None:
    """Profiles, meals, plan chat, and routing have typed contracts."""
    member = FamilyMember(name="Alex", calorie_target=2_000)
    profile = UserProfile(
        name="Household",
        people_count=1,
        family_members=[member],
        dietary_constraints=["peanuts"],
        dietary_preferences=["simple meals"],
    )
    meal = MealLogEntry(
        date=date(2026, 8, 28),
        meal_type=MealType.DINNER,
        description="Pasta",
        created_at=datetime.now(timezone.utc),
    )
    event = PlanChatEvent(
        action=PlanChatAction.GENERATE_PLAN_CHAT,
        user_id="user",
        chat_id=1,
        session_id=make_plan_chat_state().session_id,
        request_id=make_plan_chat_state(
            step=ConversationWorkflowStep.PLAN_CHAT_GENERATING
        ).request_id,
        state_revision=0,
    )

    assert profile.is_complete
    assert meal.date_key == "2026-08-28"
    assert event.action is PlanChatAction.GENERATE_PLAN_CHAT


@pytest.mark.parametrize(
    "batch_link",
    [
        None,
        {
            "batch_id": "batch-2026-08-28",
            "role": "leftover",
            "source_date": "2026-08-27",
            "source_meal_type": "dinner",
            "portion": 1,
        },
    ],
)
def test_meal_log_entry_reads_legacy_batch_link_metadata(
    batch_link: Any,
) -> None:
    """Retired meal metadata is discarded at the current model boundary."""
    meal = MealLogEntry.model_validate(
        {
            "date": "2026-08-28",
            "meal_type": "dinner",
            "description": "Pasta",
            "created_at": "2026-08-28T12:00:00+00:00",
            "batch_link": batch_link,
        }
    )

    assert meal.description == "Pasta"
    assert "batch_link" not in meal.model_dump()


def test_meal_log_entry_still_rejects_unrelated_extra_fields() -> None:
    """Compatibility must not weaken strict rejection of unknown fields."""
    with pytest.raises(ValidationError):
        MealLogEntry.model_validate(
            {
                "date": "2026-08-28",
                "meal_type": "dinner",
                "description": "Pasta",
                "created_at": "2026-08-28T12:00:00+00:00",
                "unrelated_extra": "reject me",
            }
        )


@pytest.mark.parametrize(
    "step",
    [
        ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
        ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        ConversationWorkflowStep.PLAN_CHAT_READY,
    ],
)
def test_plan_chat_states_enforce_step_fields(
    step: ConversationWorkflowStep,
) -> None:
    """Each temporary plan-chat step has exactly its required context."""
    state = make_plan_chat_state(step=step)
    assert state.workflow_kind is ConversationWorkflowKind.PLAN_CHAT
    if step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST:
        assert state.request_id is None
    else:
        assert state.request_id is not None
        assert state.initial_request is not None
        assert state.pending_message is not None
        assert state.context_date is not None
    if step is ConversationWorkflowStep.PLAN_CHAT_READY:
        assert state.latest_response is not None


def test_plan_chat_rejects_cross_workflow_and_unbounded_state() -> None:
    """Legacy plan fields and oversized plan-chat text are not accepted."""
    state = make_plan_chat_state()
    with pytest.raises(ValidationError):
        ConversationState.model_validate(
            {
                **state.model_dump(),
                "workflow_kind": ConversationWorkflowKind.PROFILE_EDIT,
                "step": ConversationWorkflowStep.PROFILE_MENU,
            }
        )
    with pytest.raises(ValidationError):
        make_plan_chat_state(
            step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
            initial_request="x" * 2_001,
        )
    with pytest.raises(ValidationError):
        PlanChatEvent(
            action=PlanChatAction.GENERATE_PLAN_CHAT,
            user_id="user",
            chat_id=1,
            session_id="123E4567-e89b-12d3-a456-426614174000",
            request_id="123e4567-e89b-12d3-a456-426614174000",
            state_revision=0,
        )


def test_conversation_state_rejects_naive_or_stale_timestamps() -> None:
    """State timestamps are aware, ordered, and expire in the future."""
    created, updated, expires_at = _timestamps()
    values = {
        "workflow_kind": ConversationWorkflowKind.PROFILE_SETUP,
        "step": ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
        "created_at": created,
        "updated_at": updated,
        "expires_at": expires_at,
    }
    state = ConversationState(**values)
    assert state.updated_at.tzinfo is not None
    for invalid in (
        {**values, "created_at": created.replace(tzinfo=None)},
        {**values, "updated_at": created - timedelta(minutes=1)},
        {**values, "expires_at": int(updated.timestamp())},
    ):
        with pytest.raises(ValidationError):
            ConversationState(**invalid)


def test_profile_legacy_read_keeps_only_raw_source_text() -> None:
    """Legacy mappings and obsolete fields are handled at the read boundary."""
    profile = UserProfile.model_validate(
        {
            "name": "Household",
            "people_count": 1,
            "family_members": [{"name": "Alex", "calorie_target": 2_000}],
            "dietary_constraints": [
                {"source_text": "Peanuts", "forbidden_terms": ["peanut"]},
                {"source_text": "PEANUTS", "rule": {}},
                {"source_text": None},
            ],
            "dietary_preferences": ["none", "Simple meals"],
            "batch_rules": [{"source_text": "discard me"}],
        }
    )
    assert profile.dietary_constraints == ["Peanuts"]
    assert profile.dietary_preferences == ["Simple meals"]
    assert "batch_rules" not in profile.model_dump()


def test_profile_draft_supports_deterministic_setup() -> None:
    """The profile draft is not an LLM intent envelope."""
    draft = ProfileDraft(
        name="Household",
        people_count=1,
        family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        dietary_constraints=["peanuts"],
    )
    assert draft.dietary_preferences is None
    with pytest.raises(ValidationError):
        ProfileDraft(
            people_count=2,
            family_members=[FamilyMember(name="Alex", calorie_target=2_000)],
        )


def test_profile_edit_operations_are_workflow_scoped() -> None:
    """Family-only target operations cannot be used on dietary lists."""
    assert ProfileEditOperation.CHANGE_PROTEIN.is_valid_for(
        ProfileEditCategory.FAMILY
    )
    assert not ProfileEditOperation.CHANGE_PROTEIN.is_valid_for(
        ProfileEditCategory.DIETARY_PREFERENCES
    )
    assert MealLogDraft().description is None
