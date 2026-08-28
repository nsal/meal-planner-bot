"""Shared typed factories for complete domain objects."""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from meal_planner.models.schemas import (
    BatchLedgerEntry,
    BatchLedgerState,
    BatchMealRole,
    BatchRule,
    ConstraintEntry,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryPreferenceEntry,
    DietaryRule,
    FamilyMember,
    GrocerySection,
    GroceryStatus,
    MealOutcome,
    PlanChatAction,
    PlanChatEvent,
    PlanDay,
    PlanDays,
    PlannedBatchLink,
    PlannedMeal,
    PlanStatus,
    SubmittedMealBatchLink,
    UserProfile,
    WeeklyPlan,
)


def make_batch_rule(
    source_text: str = "cook once for two lunches",
    *,
    identifier: str = "batch-rule-1",
    foods_any_of: list[str] | None = None,
    total_yield: int = 2,
) -> BatchRule:
    """Return a valid confirmed batch-reuse rule."""
    return BatchRule(
        id=identifier,
        source_text=source_text,
        foods_any_of=foods_any_of or ["chicken"],
        preparation_meal_types=["dinner"],
        reuse_meal_types=["lunch", "dinner"],
        total_yield=total_yield,
    )


def make_planned_batch_link(
    batch_id: str = "batch-1", *, leftover: bool = False
) -> PlannedBatchLink:
    """Return valid preparation or leftover planned batch metadata."""
    if not leftover:
        return PlannedBatchLink(
            batch_id=batch_id, role=BatchMealRole.PREPARATION
        )
    return PlannedBatchLink(
        batch_id=batch_id,
        role=BatchMealRole.LEFTOVER,
        source_date=date(2026, 8, 19),
        source_meal_type="dinner",
        portion=2,
    )


def make_batch_ledger_entry(
    batch_id: str = "batch-1",
    *,
    state: BatchLedgerState = BatchLedgerState.PROVISIONAL,
) -> BatchLedgerEntry:
    """Return a valid weekly batch-ledger entry."""
    return BatchLedgerEntry(
        batch_id=batch_id,
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 19),
        preparation_meal_type="dinner",
        food="chicken",
        meal_name="Roast chicken",
        total_portions=2,
        remaining_portions=1,
        state=state,
        week_end=date(2026, 8, 23),
    )


def make_submitted_batch_link(
    batch_id: str = "batch-1", *, leftover: bool = True
) -> SubmittedMealBatchLink:
    """Return valid optional submitted-meal batch metadata."""
    return SubmittedMealBatchLink(
        batch_id=batch_id,
        role=BatchMealRole.LEFTOVER if leftover else BatchMealRole.PREPARATION,
        portion=2 if leftover else 1,
    )


def make_constraint(
    source_text: str = "peanuts",
    *,
    identifier: str = "constraint-1",
    forbidden_terms: list[str] | None = None,
) -> ConstraintEntry:
    """Return one canonical persisted dietary constraint."""
    return ConstraintEntry(
        id=identifier,
        source_text=source_text,
        forbidden_terms=forbidden_terms or [source_text],
    )


def make_dietary_rule(
    source_text: str = "eggs for breakfast",
    *,
    identifier: str = "preference-1",
    foods_any_of: list[str] | None = None,
    meal_type: str = "breakfast",
    count: int = 1,
) -> DietaryRule:
    """Return one canonical strict dietary preference rule."""
    return DietaryRule(
        id=identifier,
        source_text=source_text,
        foods_any_of=foods_any_of or ["eggs"],
        meal_type=meal_type,
        count=count,
    )


def make_preference(
    source_text: str = "eggs for breakfast",
    *,
    identifier: str = "preference-1",
    rule: DietaryRule | None = None,
) -> DietaryPreferenceEntry:
    """Return one canonical persisted dietary preference."""
    return DietaryPreferenceEntry(
        id=identifier,
        source_text=source_text,
        rule=rule or make_dietary_rule(source_text, identifier=identifier),
    )


def make_legacy_profile_item() -> dict[str, Any]:
    """Return a raw profile document for compatibility-boundary tests."""
    return {
        "name": "Alex",
        "people_count": 1,
        "family_members": [],
        "dietary_constraints": ["Peanuts"],
        "dietary_preferences": ["More vegetables"],
        "goals": ["eat well"],
    }


def make_profile(*, with_nutrient_targets: bool = False) -> UserProfile:
    """Return a complete two-person profile.

    By default, members use the legacy calorie-only shape. Tests that need
    nutrient targets can opt into representative values explicitly.
    """
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
            FamilyMember(name="Alex", calorie_target=2000, **alex_targets),
            FamilyMember(name="Sam", calorie_target=1800, **sam_targets),
        ],
        dietary_constraints=[],
        dietary_preferences=[],
    )


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
) -> ConversationState:
    """Return a valid state for any temporary plan-chat phase."""
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    session_id = str(uuid4())
    if step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST:
        initial_request_value = None
        pending_message_value = None
        latest_response_value = None
        context_date_value = None
        request_id = None
    else:
        initial_request_value = initial_request
        request_id = str(uuid4())
        pending_message_value = pending_message or initial_request
        context_date_value = context_date
        latest_response_value = latest_response
        if step is ConversationWorkflowStep.PLAN_CHAT_READY:
            latest_response_value = latest_response or "Here is a draft."
            pending_message_value = pending_message or latest_response_value

    return ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_CHAT,
        step=step,
        session_id=session_id,
        request_id=request_id,
        initial_request=initial_request_value,
        pending_message=pending_message_value,
        latest_response=latest_response_value,
        context_date=context_date_value,
        revision=revision,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )


def make_plan_chat_event(
    *,
    session_id: str | None = None,
    request_id: str | None = None,
    state_revision: int = 0,
) -> PlanChatEvent:
    """Return a valid identifier-only plan-chat worker event."""
    return PlanChatEvent(
        action=PlanChatAction.GENERATE_PLAN_CHAT,
        user_id="12345",
        chat_id=12345,
        session_id=session_id or str(uuid4()),
        request_id=request_id or str(uuid4()),
        state_revision=state_revision,
    )


def make_plan(
    *,
    week_start: date | None = None,
    status: PlanStatus = PlanStatus.DRAFT,
    revision: int = 0,
    grocery_status: GroceryStatus = GroceryStatus.NOT_REQUESTED,
    outcome: MealOutcome = MealOutcome.UNREPORTED,
    planning_instructions: list[str] | None = None,
    plan_days: PlanDays = 7,
) -> WeeklyPlan:
    """Return a complete plan with one lunch per requested day."""
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
            for day in range(1, plan_days + 1)
        ],
    )


def make_plan_payload(week_start: date | None = None) -> dict[str, Any]:
    """Serialize a complete plan for mocked LLM JSON responses."""
    return make_plan(week_start=week_start).model_dump(
        by_alias=True, mode="json"
    )
