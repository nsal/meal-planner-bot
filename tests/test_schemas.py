"""Tests for Pydantic data models and schemas."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from meal_planner.models.schemas import (
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    GrocerySection,
    GroceryStatus,
    Ingredient,
    LLMResponseMetadata,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    PlanDay,
    PlannedMeal,
    PlanRevisionContext,
    PlanStatus,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)


def test_conversation_state_validates_workflow_shape_and_expiry() -> None:
    """Meal and plan state contracts reject incompatible fields."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert state.expires_at > int(now.timestamp())

    with pytest.raises(ValidationError):
        ConversationState(
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_DATE,
            meal_draft=MealLogDraft(date=date.today()),
            preference="invented plan preference",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
        )


def _conversation_state_values() -> dict[str, object]:
    """Return valid common values for conversation-state tests."""
    now = datetime.now(timezone.utc)
    return {
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=24),
    }


@pytest.mark.parametrize(
    "step",
    [
        ConversationWorkflowStep.AWAITING_MEAL_INPUT,
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
        ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
    ],
)
def test_single_meal_workflow_steps_round_trip(
    step: ConversationWorkflowStep,
) -> None:
    """New meal states require a submission ID and matching draft shape."""
    draft = (
        MealLogDraft()
        if step is ConversationWorkflowStep.AWAITING_MEAL_INPUT
        else MealLogDraft(
            date=date(2026, 8, 22),
            meal_type="lunch",
            description="Salad",
        )
    )
    state = ConversationState(
        **_conversation_state_values(),
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=step,
        meal_draft=draft,
        request_id="submission-1",
    )

    assert state.step is step
    assert state.request_id == "submission-1"
    assert state.meal_draft == draft


@pytest.mark.parametrize(
    ("step", "draft", "request_id"),
    [
        (
            ConversationWorkflowStep.AWAITING_MEAL_INPUT,
            MealLogDraft(
                date=date(2026, 8, 22),
                meal_type="lunch",
                description="Salad",
            ),
            "submission-1",
        ),
        (
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
            MealLogDraft(),
            "submission-1",
        ),
        (
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            MealLogDraft(),
            "submission-1",
        ),
        (
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
            MealLogDraft(
                date=date(2026, 8, 22),
                meal_type="lunch",
                description="Salad",
            ),
            None,
        ),
    ],
)
def test_single_meal_workflow_rejects_wrong_draft_or_submission_id(
    step: ConversationWorkflowStep,
    draft: MealLogDraft,
    request_id: str | None,
) -> None:
    """Input and post-input states reject incomplete contracts."""
    with pytest.raises(ValidationError):
        ConversationState(
            **_conversation_state_values(),
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=step,
            meal_draft=draft,
            request_id=request_id,
        )


@pytest.mark.parametrize(
    ("step", "draft"),
    [
        (
            ConversationWorkflowStep.AWAITING_DATE,
            MealLogDraft(),
        ),
        (
            ConversationWorkflowStep.AWAITING_MEAL_TYPE,
            MealLogDraft(date=date(2026, 8, 22)),
        ),
        (
            ConversationWorkflowStep.AWAITING_DESCRIPTION,
            MealLogDraft(date=date(2026, 8, 22), meal_type="lunch"),
        ),
        (
            ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
            MealLogDraft(
                date=date(2026, 8, 22),
                meal_type="lunch",
                description="Salad",
            ),
        ),
    ],
)
def test_legacy_meal_workflow_states_remain_compatible(
    step: ConversationWorkflowStep,
    draft: MealLogDraft,
) -> None:
    """Old field-by-field meal states remain deserializable."""
    state = ConversationState(
        **_conversation_state_values(),
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=step,
        meal_draft=draft,
    )

    restored = ConversationState.model_validate_json(state.model_dump_json())
    assert restored.step is step
    assert restored.request_id is None


@pytest.mark.parametrize(
    ("workflow_kind", "step", "fields"),
    [
        (
            ConversationWorkflowKind.PLAN_REQUEST,
            ConversationWorkflowStep.AWAITING_PREFERENCE,
            {"request_id": "plan-1"},
        ),
        (
            ConversationWorkflowKind.PLAN_REQUEST,
            ConversationWorkflowStep.GENERATING,
            {"request_id": "plan-1"},
        ),
        (
            ConversationWorkflowKind.PLAN_REQUEST,
            ConversationWorkflowStep.RETRY_READY,
            {"request_id": "plan-1"},
        ),
        (
            ConversationWorkflowKind.PLAN_REVISION,
            ConversationWorkflowStep.GENERATING,
            {
                "request_id": "revision-1",
                "amendment": "Avoid cauliflower",
                "target_week": date(2026, 8, 17),
                "expected_plan_revision": 2,
            },
        ),
        (
            ConversationWorkflowKind.PLAN_REVISION,
            ConversationWorkflowStep.RETRY_READY,
            {
                "request_id": "revision-1",
                "amendment": "Avoid cauliflower",
                "target_week": date(2026, 8, 17),
                "expected_plan_revision": 2,
            },
        ),
    ],
)
def test_plan_workflow_states_remain_deserializable(
    workflow_kind: ConversationWorkflowKind,
    step: ConversationWorkflowStep,
    fields: dict[str, object],
) -> None:
    """All existing plan workflow states keep their validation contract."""
    state = ConversationState(
        **_conversation_state_values(),
        workflow_kind=workflow_kind,
        step=step,
        **fields,
    )

    restored = ConversationState.model_validate_json(state.model_dump_json())
    assert restored.workflow_kind is workflow_kind
    assert restored.step is step


def test_family_member_valid() -> None:
    """Test FamilyMember model instantiation with valid data."""
    member = FamilyMember(name="Alice", calorie_target=2000)
    assert member.name == "Alice"
    assert member.calorie_target == 2000


def test_family_member_invalid_calories() -> None:
    """Test FamilyMember model with negative calorie target."""
    with pytest.raises(ValidationError):
        FamilyMember(name="Alice", calorie_target=-100)


def test_user_profile_defaults() -> None:
    """Test UserProfile model with default optional fields."""
    profile = UserProfile(name="John Doe")
    assert profile.name == "John Doe"
    assert profile.family_members == []
    assert profile.allergies == []
    assert profile.dietary_preferences == []
    assert profile.restrictions == []
    assert profile.goals == []
    assert profile.people_count == 1


def test_user_profile_full() -> None:
    """Test UserProfile model with full data."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="John Doe",
        family_members=[
            member,
            FamilyMember(name="Jane Doe", calorie_target=1800),
        ],
        allergies=["peanuts"],
        dietary_preferences=["keto"],
        restrictions=["gluten-free"],
        goals=["weight-loss"],
        people_count=2,
    )
    assert len(profile.family_members) == 2
    assert profile.allergies == ["peanuts"]
    assert profile.people_count == 2


def test_user_profile_invalid_people_count() -> None:
    """Test UserProfile with invalid people_count (< 1)."""
    with pytest.raises(ValidationError):
        UserProfile(name="John", people_count=0)


@pytest.mark.parametrize(
    "field",
    [
        "allergies",
        "dietary_preferences",
        "restrictions",
        "goals",
    ],
)
@pytest.mark.parametrize(
    "value",
    ["none", " NO ", "nothing.", "N/A!", "not applicable?"],
)
def test_profile_update_normalizes_generic_no_value_phrases(
    field: str, value: str
) -> None:
    """Normalize exact generic no-value answers to empty lists."""
    update = ProfileUpdateEntities.model_validate({field: value})

    assert getattr(update, field) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allergies", "no allergies"),
        ("dietary_preferences", "no dietary preferences."),
        ("dietary_preferences", "NO PREFERENCES"),
        ("restrictions", " No restrictions! "),
        ("goals", "not applicable"),
        ("goals", "no goals"),
    ],
)
def test_profile_update_normalizes_field_specific_no_value_phrases(
    field: str, value: str
) -> None:
    """Normalize field-specific exact no-value answers."""
    update = ProfileUpdateEntities.model_validate({field: value})

    assert getattr(update, field) == []


@pytest.mark.parametrize(
    "field",
    [
        "allergies",
        "dietary_preferences",
        "restrictions",
        "goals",
    ],
)
@pytest.mark.parametrize("value", [None, [], ["peanuts"]])
def test_profile_update_preserves_none_and_lists(
    field: str, value: object
) -> None:
    """Keep missing values and list values on their existing code paths."""
    update = ProfileUpdateEntities.model_validate({field: value})

    assert getattr(update, field) == value


@pytest.mark.parametrize(
    "field",
    [
        "allergies",
        "dietary_preferences",
        "restrictions",
        "goals",
    ],
)
@pytest.mark.parametrize(
    "value",
    ["no peanuts", "vegetarian", "", "no allergies for now"],
)
def test_profile_update_rejects_ambiguous_scalar_values(
    field: str, value: str
) -> None:
    """Reject scalar values that could contain meaningful profile data."""
    with pytest.raises(ValidationError):
        ProfileUpdateEntities.model_validate({field: value})


def test_ingredient_and_planned_meal() -> None:
    """Test Ingredient and PlannedMeal model instantiation."""
    ing = Ingredient(item="Chicken breast", amount="500g")
    meal = PlannedMeal(
        meal_type="dinner",
        name="Grilled Chicken",
        ingredients=[ing],
        est_calories=650,
        outcome=MealOutcome.COOKED,
    )
    assert meal.meal_type == "dinner"
    assert meal.name == "Grilled Chicken"
    assert len(meal.ingredients) == 1
    assert meal.ingredients[0].item == "Chicken breast"
    assert meal.outcome is MealOutcome.COOKED


def test_plan_day_validation() -> None:
    """Test PlanDay model validation."""
    day = PlanDay(day=3)
    assert day.day == 3
    assert day.meals == []

    with pytest.raises(ValidationError):
        PlanDay(day=0)

    with pytest.raises(ValidationError):
        PlanDay(day=8)

    with pytest.raises(ValidationError):
        PlanDay(
            day=3,
            meals=[
                PlannedMeal(meal_type="lunch", name=f"Lunch {number}")
                for number in range(5)
            ],
        )

    distinct_meals = PlanDay(
        day=3,
        meals=[
            PlannedMeal(meal_type=meal_type, name=meal_type)
            for meal_type in ("breakfast", "lunch", "dinner", "snack")
        ],
    )
    assert len(distinct_meals.meals) == 4

    with pytest.raises(
        ValidationError,
        match="at most one meal of each meal type",
    ):
        PlanDay(
            day=3,
            meals=[
                PlannedMeal(meal_type="lunch", name="Soup"),
                PlannedMeal(meal_type="lunch", name="Salad"),
            ],
        )


def test_weekly_plan_and_grocery_section() -> None:
    """Test WeeklyPlan and GrocerySection instantiation."""
    sec = GrocerySection(name="Produce", items=["Apples", "Bananas"])
    days = [PlanDay(day=day) for day in range(1, 8)]
    plan = WeeklyPlan(
        week_start="2026-08-10",
        status=PlanStatus.CONFIRMED,
        days=days,
        grocery_status=GroceryStatus.READY,
        grocery_list=[sec],
    )
    assert plan.week_start.isoformat() == "2026-08-10"
    assert plan.week_start_date == "2026-08-10"
    assert plan.status is PlanStatus.CONFIRMED
    assert len(plan.days) == 7
    assert len(plan.grocery_list) == 1


def test_weekly_plan_revision_defaults_and_round_trips() -> None:
    days = [PlanDay(day=day) for day in range(1, 8)]
    plan = WeeklyPlan(week_start="2026-08-10", days=days)
    assert plan.revision == 0
    restored = WeeklyPlan.model_validate_json(plan.model_dump_json())
    assert restored.revision == 0
    assert restored.planning_instructions == []


def test_weekly_plan_planning_instructions_are_bounded() -> None:
    days = [PlanDay(day=day) for day in range(1, 8)]
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=days,
        planning_instructions=["Avoid cauliflower"],
    )
    assert plan.planning_instructions == ["Avoid cauliflower"]
    with pytest.raises(ValidationError):
        WeeklyPlan(
            week_start="2026-08-10",
            days=days,
            planning_instructions=["x" * 501],
        )
    with pytest.raises(ValidationError):
        WeeklyPlan(
            week_start="2026-08-10",
            days=days,
            planning_instructions=["instruction"] * 21,
        )


def test_plan_revision_state_and_event_require_complete_snapshot() -> None:
    now = datetime.now(timezone.utc)
    values = {
        "workflow_kind": ConversationWorkflowKind.PLAN_REVISION,
        "step": ConversationWorkflowStep.GENERATING,
        "amendment": "Avoid cauliflower",
        "target_week": date(2026, 8, 10),
        "expected_plan_revision": 4,
        "request_id": "request-1",
        "revision": 2,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=24),
    }
    state = ConversationState(**values)
    assert state.week_start == date(2026, 8, 10)
    assert state.step is ConversationWorkflowStep.GENERATING
    retry = state.model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "revision": 3,
            "updated_at": now + timedelta(seconds=1),
        }
    )
    assert retry.step is ConversationWorkflowStep.RETRY_READY
    with pytest.raises(ValidationError):
        ConversationState(**{**values, "amendment": None})
    with pytest.raises(ValidationError):
        ConversationState(
            **{**values, "workflow_kind": ConversationWorkflowKind.MEAL_LOG}
        )

    context = PlanRevisionContext(
        amendment="Avoid cauliflower",
        request_id="request-1",
        state_revision=2,
        expected_plan_revision=4,
        week_start="2026-08-10",
    )
    assert context.week_start == date(2026, 8, 10)
    with pytest.raises(ValidationError):
        PlanRevisionContext.model_validate(
            {"amendment": "Avoid cauliflower", "request_id": "request-1"}
        )


def test_weekly_plan_rejects_negative_revision() -> None:
    days = [PlanDay(day=day) for day in range(1, 8)]
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", days=days, revision=-1)


def test_meal_log_entry() -> None:
    """Test MealLogEntry model instantiation."""
    entry = MealLogEntry(
        date="2026-08-05",
        meal_type="lunch",
        description="Salad with olive oil",
        created_at="2026-08-05T12:30:00Z",
    )
    assert entry.date.isoformat() == "2026-08-05"
    assert entry.meal_type.value == "lunch"


@pytest.mark.parametrize(
    "days",
    [
        [PlanDay(day=day) for day in range(1, 7)],
        [PlanDay(day=1) for _ in range(7)],
    ],
)
def test_weekly_plan_requires_complete_unique_week(
    days: list[PlanDay],
) -> None:
    """Plans must contain exactly one entry for every day of the week."""
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", days=days)


def test_weekly_plan_rejects_invalid_date_status_and_outcome() -> None:
    """Typed plan fields reject malformed LLM values."""
    days = [PlanDay(day=day) for day in range(1, 8)]
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="not-a-date", days=days)
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", status="active", days=days)
    with pytest.raises(ValidationError):
        PlannedMeal(meal_type="lunch", name="Soup", outcome="maybe")


def test_llm_response_metadata_and_intent() -> None:
    """Test ConversationIntent enum and LLMResponseMetadata."""
    meta = LLMResponseMetadata(
        intent=ConversationIntent.LOG_MEAL,
        entities={"meal_type": "lunch"},
    )
    assert meta.intent == "log_meal"
    assert meta.entities["meal_type"] == "lunch"


def test_llm_response_metadata_invalid_intent() -> None:
    """Test LLMResponseMetadata with an invalid intent."""
    with pytest.raises(ValidationError):
        LLMResponseMetadata(intent="invalid_intent")  # type: ignore[arg-type]
