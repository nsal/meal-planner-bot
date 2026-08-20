"""Tests for Pydantic data models and schemas."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from meal_planner.models import (
    PreferenceRequirement as ExportedPreferenceRequirement,
)
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
    PlanGenerationContext,
    PlannedMeal,
    PlanRevisionContext,
    PlanStatus,
    PreferenceRequirement,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)


def test_preference_requirement_valid_exact_count_and_optional_scope() -> None:
    """Accept bounded exact-count rules with or without meal scopes."""
    scoped = PreferenceRequirement(
        id="r1",
        source_text="crepes or pancakes on a breakfast once",
        foods_any_of=["crepes", "pancakes"],
        meal_type="breakfast",
        exact_count=1,
    )
    unscoped = PreferenceRequirement(
        id="r2",
        source_text="salmon twice",
        foods_any_of=["salmon"],
        exact_count=2,
    )

    assert isinstance(scoped, ExportedPreferenceRequirement)
    assert scoped.meal_type.value == "breakfast"
    assert unscoped.meal_type is None
    assert unscoped.exact_count == 2


@pytest.mark.parametrize(
    "values",
    [
        {"foods_any_of": []},
        {"foods_any_of": ["Salmon", " salmon "]},
        {"foods_any_of": ["egg", "eggs"]},
        {"foods_any_of": ["cookie", "cookies"]},
        {"foods_any_of": ["brownie", "brownies"]},
        {"foods_any_of": ["smoothie", "smoothies"]},
        {"foods_any_of": ["pie", "pies"]},
        {"foods_any_of": ["berry", "berries"]},
        {"foods_any_of": ["red-pepper", "red pepper"]},
        {"foods_any_of": ["ｅｇｇ", "egg"]},
        {"foods_any_of": ["red  pepper", "red pepper"]},
        {"foods_any_of": ["---"]},
        {"foods_any_of": ["!!! ???"]},
        {"foods_any_of": ["$$$"]},
        {"foods_any_of": ["🍕🍔"]},
        {"foods_any_of": ["eggs", "---"]},
        {"foods_any_of": ["rice", "!!!"]},
        {"id": ""},
        {"id": "bad id"},
        {"id": "x" * 65},
        {"source_text": "x" * 501},
        {"foods_any_of": ["x" * 101]},
        {"foods_any_of": ["food"] * 21},
        {"exact_count": 0},
        {"exact_count": 29},
        {"meal_type": "brunch"},
    ],
)
def test_preference_requirement_rejects_invalid_values(
    values: dict[str, object],
) -> None:
    """Reject empty, duplicated, malformed, and oversized rule values."""
    defaults: dict[str, object] = {
        "id": "r1",
        "source_text": "salmon twice",
        "foods_any_of": ["salmon"],
        "exact_count": 2,
    }

    with pytest.raises(ValidationError):
        PreferenceRequirement(**{**defaults, **values})


@pytest.mark.parametrize(
    "foods_any_of",
    [
        ["red-pepper"],
        ["vitamin B-12"],
        ["meal 2"],
        ["123/456"],
    ],
)
def test_preference_requirement_accepts_punctuation_separated_food_tokens(
    foods_any_of: list[str],
) -> None:
    """Accept alternatives that retain letters or digits after normalization."""
    requirement = PreferenceRequirement(
        id="r1",
        source_text="a measurable preference",
        foods_any_of=foods_any_of,
        exact_count=1,
    )

    assert requirement.foods_any_of == foods_any_of


@pytest.mark.parametrize(
    ("meal_type", "exact_count"),
    [("breakfast", 8), ("lunch", 8), ("dinner", 8), ("snack", 8)],
)
def test_preference_requirement_rejects_count_beyond_scoped_week(
    meal_type: str, exact_count: int
) -> None:
    """A scoped rule cannot match more than once per day."""
    with pytest.raises(ValidationError, match="selected meal scope"):
        PreferenceRequirement(
            id="r1",
            source_text="food",
            foods_any_of=["food"],
            meal_type=meal_type,
            exact_count=exact_count,
        )


def test_plan_generation_context_carries_bounded_preference_metadata() -> None:
    """Generation events carry rules and at most one repair attempt."""
    context = PlanGenerationContext(
        preference="eggs three times for breakfast",
        requirements=[
            PreferenceRequirement(
                id="r1",
                source_text="eggs three times for breakfast",
                foods_any_of=["eggs"],
                meal_type="breakfast",
                exact_count=3,
            )
        ],
        attempt=2,
        repair_feedback="breakfast rule r1 matched 2; expected exactly 3",
        repair_id="repair-123",
    )

    assert context.requirements[0].id == "r1"
    assert context.attempt == 2
    assert context.repair_feedback is not None
    assert context.repair_id == "repair-123"


def test_tracked_attempt_two_may_omit_repair_id() -> None:
    """Tracked retries use request ownership instead of a repair marker."""
    context = PlanGenerationContext(
        attempt=2,
        repair_feedback="retry feedback",
        request_id="request-1",
        state_revision=3,
    )

    assert context.repair_id is None


def test_untracked_attempt_two_requires_repair_id() -> None:
    """Untracked retries require a durable replay token."""
    with pytest.raises(ValidationError, match="repair ID"):
        PlanGenerationContext(attempt=2, repair_feedback="retry feedback")


def test_untracked_attempt_one_carries_repair_id_into_redelivery() -> None:
    """An initial untracked event establishes its stable repair token."""
    context = PlanGenerationContext(attempt=1, repair_id="repair-123")

    assert context.repair_id == "repair-123"


@pytest.mark.parametrize("repair_id", ["   ", "x" * 101])
def test_plan_generation_context_rejects_unbounded_repair_id(
    repair_id: str,
) -> None:
    """Repair tokens use the bounded request-id wire contract."""
    with pytest.raises(ValidationError):
        PlanGenerationContext(
            attempt=2,
            repair_feedback="retry feedback",
            repair_id=repair_id,
        )


def test_plan_generation_context_allows_attempt_one_without_feedback() -> None:
    """The initial generation attempt has no repair feedback."""
    context = PlanGenerationContext(attempt=1)

    assert context.attempt == 1
    assert context.repair_feedback is None


@pytest.mark.parametrize(
    "values",
    [
        {"attempt": 0},
        {"attempt": 3},
        {"repair_feedback": "feedback"},
        {"repair_feedback": "x" * 801},
        {"attempt": 2},
        {"attempt": 2, "repair_feedback": None},
        {"attempt": 2, "repair_feedback": "   "},
        {
            "requirements": [
                {
                    "id": "bad id",
                    "source_text": "eggs once",
                    "foods_any_of": ["eggs"],
                    "exact_count": 1,
                }
            ]
        },
    ],
)
def test_plan_generation_context_rejects_invalid_nested_metadata(
    values: dict[str, object],
) -> None:
    """Reject invalid attempts, feedback, and nested rule metadata."""
    with pytest.raises(ValidationError):
        PlanGenerationContext(**values)


def test_preference_requirement_keeps_legacy_plan_and_conversation_models() -> (
    None
):
    """Adding the requirement contract does not change old model payloads."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        preference="vegetarian",
        request_id="request-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    plan = WeeklyPlan(
        week_start_date="2026-08-10",
        days=[PlanDay(day=day) for day in range(1, 8)],
    )

    restored_state = ConversationState.model_validate_json(
        state.model_dump_json()
    )
    restored_plan = WeeklyPlan.model_validate_json(plan.model_dump_json())

    assert restored_state.preference == "vegetarian"
    assert restored_state.request_id == "request-1"
    assert restored_plan.week_start_date == "2026-08-10"
    assert len(restored_plan.days) == 7


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


def test_plan_preference_can_be_retained_while_awaiting_clarification() -> None:
    """Pending clarification keeps the raw preference in the same workflow."""
    now = datetime.now(timezone.utc)

    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.AWAITING_PREFERENCE,
        preference="eggs three times, make it healthy",
        request_id="request-clarification",
        revision=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    assert state.preference == "eggs three times, make it healthy"


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
