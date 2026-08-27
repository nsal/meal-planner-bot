"""Tests for Pydantic data models and schemas."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from meal_planner.models import (
    PlanDays as ExportedPlanDays,
)
from meal_planner.models import (
    PreferenceRequirement as ExportedPreferenceRequirement,
)
from meal_planner.models.schemas import (
    BatchLedgerEntry,
    BatchLedgerState,
    BatchMealRole,
    BatchRule,
    ConstraintEntry,
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryObligation,
    DietaryPreferenceEntry,
    DietaryRule,
    FamilyMember,
    GrocerySection,
    GroceryStatus,
    Ingredient,
    LLMResponseMetadata,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlanDay,
    PlanDays,
    PlanGenerationContext,
    PlannedBatchLink,
    PlannedMeal,
    PlanRevisionContext,
    PlanStatus,
    PreferenceRequirement,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    RuleCadence,
    RuleOperator,
    RuleStrength,
    ScheduleKind,
    SubmittedMealBatchLink,
    UserProfile,
    Weekday,
    WeeklyBatchLedger,
    WeeklyPlan,
)
from tests.factories import make_plan, make_profile


def test_weekly_rule_and_schedule_contract_round_trips() -> None:
    """Persist weekly cadence and distinguish explicit from generated days."""
    explicit = DietaryRule(
        id="eggs",
        source_text="eggs three breakfasts weekly",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY],
        cadence=RuleCadence.ISO_WEEK,
        schedule_kind=ScheduleKind.EXPLICIT,
        operator=RuleOperator.AT_LEAST,
        count=3,
    )
    generated = DietaryRule(
        id="fish",
        source_text="fish weekly",
        foods_any_of=["fish"],
        cadence=RuleCadence.ISO_WEEK,
        schedule_kind=ScheduleKind.GENERATED,
        target_weekdays=[Weekday.MONDAY, Weekday.THURSDAY],
        count=2,
    )

    restored = DietaryRule.model_validate_json(explicit.model_dump_json())

    assert restored.cadence is RuleCadence.ISO_WEEK
    assert restored.schedule_kind is ScheduleKind.EXPLICIT
    assert generated.weekdays == []
    assert generated.target_weekdays == [Weekday.MONDAY, Weekday.THURSDAY]


def test_dated_obligation_validates_exact_week_and_horizon() -> None:
    """An obligation carries independent dates and its owning rule."""
    obligation = DietaryObligation(
        id="obligation-1",
        source_rule_id="eggs",
        iso_week="2026-W34",
        horizon_start=date(2026, 8, 19),
        horizon_end=date(2026, 8, 21),
        eligible_dates=[date(2026, 8, 19), date(2026, 8, 21)],
        operator=RuleOperator.AT_LEAST,
        count=2,
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        strength=RuleStrength.STRICT,
        evidence_ids=["meal-1"],
    )

    assert obligation.week_start == date(2026, 8, 17)
    assert obligation.iso_week == "2026-W34"
    assert obligation.eligible_dates == [
        date(2026, 8, 19),
        date(2026, 8, 21),
    ]


def test_batch_rules_links_and_weekly_ledger_round_trip() -> None:
    """Batch rules, planned links, and durable weekly entries are typed."""
    rule = BatchRule(
        id="batch-rule-1",
        source_text="cook once for two lunches",
        foods_any_of=["chicken"],
        preparation_meal_types=[MealType.DINNER],
        reuse_meal_types=[MealType.LUNCH, MealType.DINNER],
        total_yield=2,
    )
    preparation = PlannedBatchLink(
        batch_id="batch-1", role=BatchMealRole.PREPARATION
    )
    leftover = PlannedBatchLink(
        batch_id="batch-1",
        role=BatchMealRole.LEFTOVER,
        source_date=date(2026, 8, 19),
        source_meal_type=MealType.DINNER,
        portion=2,
    )
    entry = BatchLedgerEntry(
        batch_id="batch-1",
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 19),
        preparation_meal_type=MealType.DINNER,
        food="chicken",
        meal_name="Roast chicken",
        total_portions=2,
        remaining_portions=1,
        state=BatchLedgerState.PROVISIONAL,
        week_end=date(2026, 8, 23),
    )
    ledger = WeeklyBatchLedger(iso_week="2026-W34", entries=[entry])

    assert rule.total_yield == 2
    assert preparation.role is BatchMealRole.PREPARATION
    assert leftover.role is BatchMealRole.LEFTOVER
    assert ledger.entries[0].week_end == date(2026, 8, 23)


def test_meal_log_batch_link_is_optional_for_existing_records() -> None:
    """Historical ordinary meal records remain valid without batch data."""
    entry = MealLogEntry(
        date=date(2026, 8, 19),
        meal_type=MealType.LUNCH,
        description="Salad",
        created_at=datetime(2026, 8, 19, 12, tzinfo=timezone.utc),
    )
    linked = entry.model_copy(
        update={
            "batch_link": SubmittedMealBatchLink(
                batch_id="batch-1", role=BatchMealRole.LEFTOVER
            )
        }
    )

    assert entry.batch_link is None
    assert linked.batch_link is not None
    assert linked.batch_link.batch_id == "batch-1"


@pytest.mark.parametrize(
    "values",
    [
        {"weekdays": [Weekday.MONDAY, Weekday.MONDAY]},
        {"target_weekdays": [Weekday.TUESDAY, Weekday.TUESDAY]},
        {"count": 8, "meal_type": MealType.DINNER},
    ],
)
def test_weekly_rules_reject_duplicate_scopes_and_over_capacity(
    values: dict[str, object],
) -> None:
    """Rules reject duplicate dates/scopes and impossible weekly quotas."""
    payload: dict[str, object] = {
        "id": "invalid",
        "source_text": "invalid weekly rule",
        "foods_any_of": ["food"],
        **values,
    }
    with pytest.raises(ValidationError):
        DietaryRule(**payload)


def test_obligations_reject_duplicate_or_out_of_horizon_dates() -> None:
    """Obligation dates are unique and bounded by both horizon and week."""
    values = {
        "id": "obligation-1",
        "source_rule_id": "rule-1",
        "iso_week": "2026-W34",
        "horizon_start": date(2026, 8, 19),
        "horizon_end": date(2026, 8, 21),
        "operator": RuleOperator.AT_LEAST,
        "count": 1,
        "foods_any_of": ["eggs"],
    }
    with pytest.raises(ValidationError):
        DietaryObligation(
            **values,
            eligible_dates=[date(2026, 8, 19), date(2026, 8, 19)],
        )
    with pytest.raises(ValidationError):
        DietaryObligation(
            **values,
            eligible_dates=[date(2026, 8, 22)],
        )


def test_batch_models_reject_bad_yields_roles_and_cross_week_entries() -> None:
    """Batch contracts enforce 2-3 yields, role consistency, and one week."""
    with pytest.raises(ValidationError):
        BatchRule(
            id="batch-rule-1",
            source_text="too small",
            foods_any_of=["beans"],
            preparation_meal_types=[MealType.DINNER],
            reuse_meal_types=[MealType.LUNCH],
            total_yield=4,
        )
    with pytest.raises(ValidationError):
        PlannedBatchLink(
            batch_id="batch-1",
            role=BatchMealRole.PREPARATION,
            source_date=date(2026, 8, 19),
        )
    entry = BatchLedgerEntry(
        batch_id="batch-1",
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 19),
        preparation_meal_type=MealType.DINNER,
        food="beans",
        total_portions=2,
        remaining_portions=1,
        state=BatchLedgerState.AVAILABLE,
        week_end=date(2026, 8, 23),
    )
    with pytest.raises(ValidationError):
        WeeklyBatchLedger(
            iso_week="2026-W34",
            entries=[
                entry.model_copy(update={"preparation_date": date(2026, 8, 24)})
            ],
        )


def test_weekly_batch_ledger_is_bounded() -> None:
    """A ledger cannot grow without bound in a persisted user item."""
    entry = BatchLedgerEntry(
        batch_id="batch-1",
        source_plan_id="plan-1",
        source_request_id="request-1",
        source_revision=1,
        preparation_date=date(2026, 8, 19),
        preparation_meal_type=MealType.DINNER,
        food="beans",
        total_portions=2,
        remaining_portions=1,
        state=BatchLedgerState.PROVISIONAL,
        week_end=date(2026, 8, 23),
    )
    with pytest.raises(ValidationError):
        WeeklyBatchLedger(
            iso_week="2026-W34",
            entries=[
                entry.model_copy(update={"batch_id": f"batch-{i}"})
                for i in range(21)
            ],
        )


@pytest.mark.parametrize(
    "legacy_entry",
    [
        "eggs",
        {"id": "raw", "source_text": "eggs"},
        {"id": "raw", "source_text": "eggs", "rule": None},
    ],
)
def test_saved_complete_profile_discards_legacy_preference_entries(
    legacy_entry: object,
) -> None:
    """Saved reads discard only known unstructured preference entries."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_preferences"] = [legacy_entry]

    profile = UserProfile.model_validate(
        profile_data, context={"saved_profile": True}
    )

    assert profile.dietary_preferences == []


def test_saved_profile_keeps_valid_preferences_and_constraints() -> None:
    """Saved compatibility filtering preserves valid profile content."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_constraints"] = ["peanuts", "shellfish"]
    profile_data["dietary_preferences"] = [
        {
            "id": "valid",
            "source_text": "eggs",
            "rule": {
                "id": "valid-rule",
                "source_text": "eggs",
                "foods_any_of": ["eggs"],
                "count": 1,
            },
        },
        "legacy prose",
        {"id": "missing", "source_text": "missing rule"},
        {"id": "null", "source_text": "null rule", "rule": None},
    ]

    profile = UserProfile.model_validate(
        profile_data, context={"saved_profile": True}
    )

    assert [entry.source_text for entry in profile.dietary_preferences] == [
        "eggs"
    ]
    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "peanuts",
        "shellfish",
    ]


def test_saved_profile_does_not_discard_invalid_non_null_rules() -> None:
    """Compatibility filtering must not hide structurally invalid rules."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_preferences"] = [
        {
            "id": "invalid",
            "source_text": "eggs",
            "rule": {
                "id": "invalid-rule",
                "source_text": "eggs",
                "foods_any_of": [],
                "count": 1,
            },
        }
    ]

    with pytest.raises(ValidationError):
        UserProfile.model_validate(
            profile_data, context={"saved_profile": True}
        )


def test_profile_update_rejects_unstructured_preference_shapes() -> None:
    """Profile updates cannot accept raw, missing, or null rules."""
    with pytest.raises(ValidationError):
        ProfileUpdateEntities.model_validate(
            {"dietary_preferences": ["peanuts"]}
        )
    for entry in [
        {"id": "missing", "source_text": "peanuts"},
        {"id": "null", "source_text": "peanuts", "rule": None},
    ]:
        with pytest.raises(ValidationError):
            ProfileUpdateEntities.model_validate(
                {"dietary_preferences": [entry]}
            )


def test_profile_construction_rejects_unstructured_preference_shapes() -> None:
    """New profile construction remains strict outside saved reads."""
    with pytest.raises(ValidationError):
        UserProfile(name="Alex", dietary_preferences=["eggs"])
    with pytest.raises(ValidationError):
        UserProfile(
            name="Alex",
            dietary_preferences=[
                {"id": "raw", "source_text": "eggs", "rule": None}
            ],
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


def test_dietary_rule_supports_operators_strength_and_weekdays() -> None:
    """Serialize the shared rule contract without losing its scope."""
    rule = DietaryRule(
        id="r1",
        source_text="eggs for breakfast twice",
        foods_any_of=["egg"],
        meal_type="breakfast",
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
        operator=RuleOperator.AT_LEAST,
        count=2,
        strength=RuleStrength.BEST_EFFORT,
    )

    restored = DietaryRule.model_validate_json(rule.model_dump_json())

    assert restored == rule
    assert restored.operator is RuleOperator.AT_LEAST
    assert restored.weekdays == [Weekday.MONDAY, Weekday.WEDNESDAY]
    assert restored.strength is RuleStrength.BEST_EFFORT


@pytest.mark.parametrize(
    "operator", [RuleOperator.EXACTLY, RuleOperator.AT_LEAST]
)
def test_dietary_rule_rejects_impossible_strict_named_day_count(
    operator: RuleOperator,
) -> None:
    """Reject strict counts that exceed selected weekday capacity."""
    with pytest.raises(ValidationError, match="named weekday"):
        DietaryRule(
            id="r1",
            source_text="eggs twice for breakfast on Monday and Wednesday",
            foods_any_of=["eggs"],
            meal_type=MealType.BREAKFAST,
            weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
            operator=operator,
            count=3,
            strength=RuleStrength.STRICT,
        )


def test_dietary_rule_uses_bounded_unscoped_daily_capacity() -> None:
    """Meal-unscoped weekday rules use four supported daily meals."""
    valid = DietaryRule(
        id="r1",
        source_text="eggs four times on Monday",
        foods_any_of=["eggs"],
        weekdays=[Weekday.MONDAY],
        operator=RuleOperator.EXACTLY,
        count=4,
        strength=RuleStrength.STRICT,
    )

    assert valid.count == 4
    with pytest.raises(ValidationError, match="named weekday"):
        DietaryRule(
            id="r2",
            source_text="eggs five times on Monday",
            foods_any_of=["eggs"],
            weekdays=[Weekday.MONDAY],
            operator=RuleOperator.AT_LEAST,
            count=5,
            strength=RuleStrength.STRICT,
        )


@pytest.mark.parametrize(
    ("operator", "strength"),
    [
        (RuleOperator.AT_MOST, RuleStrength.STRICT),
        (RuleOperator.EXACTLY, RuleStrength.BEST_EFFORT),
    ],
)
def test_dietary_rule_preserves_non_strict_weekday_controls(
    operator: RuleOperator,
    strength: RuleStrength,
) -> None:
    """Maxima and best-effort rules retain their existing flexibility."""
    rule = DietaryRule(
        id="r1",
        source_text="eggs four times on Monday",
        foods_any_of=["eggs"],
        weekdays=[Weekday.MONDAY],
        operator=operator,
        count=4,
        strength=strength,
    )

    assert rule.count == 4


@pytest.mark.parametrize("operator", list(RuleOperator))
def test_dietary_rule_allows_zero_count_exclusions(
    operator: RuleOperator,
) -> None:
    """Allow zero for every operator, including explicit exclusions."""
    rule = DietaryRule(
        id="r1",
        source_text="no eggs this week",
        foods_any_of=["eggs"],
        operator=operator,
        count=0,
    )

    assert rule.count == 0


@pytest.mark.parametrize(
    "values",
    [
        {"count": -1},
        {"count": 29},
        {"weekdays": [Weekday.MONDAY, Weekday.MONDAY]},
        {"weekdays": list(Weekday) + [Weekday.MONDAY]},
        {
            "meal_type": MealType.BREAKFAST,
            "weekdays": [Weekday.MONDAY, Weekday.WEDNESDAY],
            "count": 3,
        },
    ],
)
def test_dietary_rule_rejects_invalid_bounds_and_duplicate_weekdays(
    values: dict[str, object],
) -> None:
    """Reject counts outside bounds and scopes that cannot satisfy them."""
    defaults: dict[str, object] = {
        "id": "r1",
        "source_text": "eggs",
        "foods_any_of": ["eggs"],
        "count": 1,
    }

    with pytest.raises(ValidationError):
        DietaryRule(**{**defaults, **values})


def test_constraint_entry_normalizes_and_deduplicates_forbidden_terms() -> None:
    """Constraint terms are normalized at the schema boundary."""
    entry = ConstraintEntry(
        id="c1",
        source_text="Peanut butter allergy",
        forbidden_terms=["Peanuts", " peanut ", "Peanut Butter"],
    )

    assert entry.forbidden_terms == ["peanut", "peanut butter"]
    assert ConstraintEntry.model_validate_json(entry.model_dump_json()) == entry


def test_constraint_entry_requires_a_nonempty_unique_term_collection() -> None:
    """Constraints cannot be represented without a matchable term."""
    with pytest.raises(ValidationError):
        ConstraintEntry(id="c1", source_text="unknown", forbidden_terms=[])
    entry = ConstraintEntry(
        id="c1",
        source_text="peanuts",
        forbidden_terms=["peanuts", " peanut "],
    )
    assert entry.forbidden_terms == ["peanut"]


@pytest.mark.parametrize(
    ("source_text", "expected_terms", "uninterpretable"),
    [
        ("allergic to peanuts", ["peanut"], False),
        ("gluten-free", ["gluten", "wheat", "barley", "rye"], False),
        ("vegan", [], True),
    ],
)
def test_legacy_semantic_constraints_are_normalized_to_terms(
    source_text: str,
    expected_terms: list[str],
    uninterpretable: bool,
) -> None:
    """Legacy semantic phrases retain only complete reviewed evidence."""
    profile = UserProfile.model_validate(
        {"name": "Alex", "dietary_constraints": [source_text]}
    )

    assert profile.dietary_constraints[0].forbidden_terms == expected_terms
    assert profile.dietary_constraints[0].uninterpretable is uninterpretable


def test_unknown_legacy_constraint_is_explicitly_uninterpretable() -> None:
    """Unknown semantic prose cannot become safety evidence by copying it."""
    profile = UserProfile.model_validate(
        {
            "name": "Alex",
            "dietary_constraints": ["I react badly to mystery foods"],
        }
    )

    constraint = profile.dietary_constraints[0]
    assert constraint.uninterpretable
    assert constraint.forbidden_terms == []


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


def test_plan_generation_context_carries_typed_batch_rules() -> None:
    """Planner events preserve confirmed batch rules independently."""
    rule = BatchRule(
        id="batch-1",
        source_text="cook chicken for two dinners",
        foods_any_of=["chicken"],
        preparation_meal_types=[MealType.DINNER],
        reuse_meal_types=[MealType.DINNER],
        total_yield=2,
    )

    context = PlanGenerationContext(batch_rules=[rule])

    assert context.batch_rules == [rule]


@pytest.mark.parametrize(
    "plan_days",
    [True, False, "1", "7", 1.0, 3.5, None, [], {}],
)
def test_plan_generation_context_rejects_non_integer_plan_days(
    plan_days: object,
) -> None:
    """Planner events reject non-integer durations before coercion."""
    with pytest.raises(ValidationError):
        PlanGenerationContext(plan_days=plan_days)


@pytest.mark.parametrize("plan_days", [1, 7])
def test_plan_generation_context_retains_valid_integer_plan_days(
    plan_days: int,
) -> None:
    """Planner events retain each valid integer duration exactly."""
    context = PlanGenerationContext(plan_days=plan_days)

    assert context.plan_days == plan_days
    assert type(context.plan_days) is int


def test_plan_generation_context_defaults_omitted_plan_days_to_seven() -> None:
    """Historical planner events retain the seven-day default."""
    context = PlanGenerationContext()

    assert context.plan_days == 7


@pytest.mark.parametrize("plan_days", [0, 8, -1, 10])
def test_plan_generation_context_rejects_out_of_range_integer_plan_days(
    plan_days: int,
) -> None:
    """Planner events retain the one-through-seven duration bounds."""
    with pytest.raises(ValidationError):
        PlanGenerationContext(plan_days=plan_days)


def test_generation_context_round_trips_prioritized_rule_snapshots() -> None:
    """Retries retain each rule tier and constraint without reinterpretation."""
    stored = DietaryRule(
        id="stored-1",
        source_text="eggs twice",
        foods_any_of=["eggs"],
        operator=RuleOperator.EXACTLY,
        count=2,
    )
    current = DietaryRule(
        id="current-1",
        source_text="eggs at most once",
        foods_any_of=["eggs"],
        operator=RuleOperator.AT_MOST,
        count=1,
    )
    constraint = ConstraintEntry(
        id="constraint-1",
        source_text="peanut allergy",
        forbidden_terms=["peanuts"],
    )
    context = PlanGenerationContext(
        preference="eggs at most once",
        stored_rules=[stored],
        current_rules=[current],
        effective_rules=[current],
        constraint_rules=[constraint],
        attempt=1,
    )

    restored = PlanGenerationContext.model_validate_json(
        context.model_dump_json()
    )

    assert restored == context
    assert restored.stored_rules[0].id == "stored-1"
    assert restored.current_rules[0].operator is RuleOperator.AT_MOST
    assert restored.constraint_rules[0].forbidden_terms == ["peanut"]


def test_generation_context_rejects_cross_tier_rule_id_collisions() -> None:
    """A provider ID cannot identify rules in two planning tiers."""
    stored = DietaryRule(
        id="r1",
        source_text="eggs",
        foods_any_of=["eggs"],
        count=1,
    )
    current = DietaryRule(
        id="r1",
        source_text="tofu",
        foods_any_of=["tofu"],
        count=1,
    )

    with pytest.raises(ValidationError, match="unique IDs"):
        PlanGenerationContext(
            stored_rules=[stored],
            current_rules=[current],
            effective_rules=[stored],
        )


def test_conversation_state_round_trips_prioritized_rule_snapshots() -> None:
    """Durable conversation state carries the same retry-safe rule snapshot."""
    now = datetime.now(timezone.utc)
    stored = DietaryRule(
        id="stored-1",
        source_text="salmon once",
        foods_any_of=["salmon"],
        count=1,
    )
    current = stored.model_copy(update={"id": "current-1"})
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.GENERATING,
        preference="salmon once",
        stored_rules=[stored],
        current_rules=[current],
        effective_rules=[current],
        constraint_rules=[],
        request_id="request-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    restored = ConversationState.model_validate_json(state.model_dump_json())

    assert restored == state
    assert restored.effective_rules[0].source_text == "salmon once"


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


def test_plan_request_duration_phase_round_trips_explicit_values() -> None:
    """Collected and uncollected plan phases survive serialization."""
    common = _conversation_state_values()
    uncollected = ConversationState(
        **common,
        workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
        step=ConversationWorkflowStep.AWAITING_PREFERENCE,
        request_id="plan-uncollected",
        plan_days=3,
        duration_collected=False,
    )
    collected = uncollected.model_copy(
        update={
            "request_id": "plan-collected",
            "step": ConversationWorkflowStep.GENERATING,
            "duration_collected": True,
        }
    )

    restored_uncollected = ConversationState.model_validate_json(
        uncollected.model_dump_json()
    )
    restored_collected = ConversationState.model_validate_json(
        collected.model_dump_json()
    )

    assert restored_uncollected.plan_days == 3
    assert not restored_uncollected.duration_collected
    assert restored_collected.plan_days == 3
    assert restored_collected.duration_collected


def test_legacy_plan_request_defaults_to_collected_seven_day_request() -> None:
    """Historical state payloads enter the preference-only phase safely."""
    payload = {
        **_conversation_state_values(),
        "workflow_kind": ConversationWorkflowKind.PLAN_REQUEST,
        "step": ConversationWorkflowStep.AWAITING_PREFERENCE,
        "request_id": "legacy-plan",
    }

    state = ConversationState.model_validate(payload)

    assert state.plan_days == 7
    assert state.duration_collected
    assert "plan_days" not in payload
    assert "duration_collected" not in payload


@pytest.mark.parametrize(
    "step",
    [
        ConversationWorkflowStep.GENERATING,
        ConversationWorkflowStep.RETRY_READY,
    ],
)
def test_plan_generation_steps_require_collected_duration(
    step: ConversationWorkflowStep,
) -> None:
    """Generation cannot start while the initial duration is uncollected."""
    with pytest.raises(ValidationError, match="collected duration"):
        ConversationState(
            **_conversation_state_values(),
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=step,
            request_id="plan-uncollected",
            plan_days=3,
            duration_collected=False,
        )


@pytest.mark.parametrize(
    "workflow_kind",
    [
        ConversationWorkflowKind.MEAL_LOG,
        ConversationWorkflowKind.PLAN_REVISION,
        ConversationWorkflowKind.PROFILE_EDIT,
    ],
)
def test_uncollected_duration_is_only_valid_for_plan_requests(
    workflow_kind: ConversationWorkflowKind,
) -> None:
    """The phase marker cannot leak into unrelated workflows."""
    fields: dict[str, object]
    if workflow_kind is ConversationWorkflowKind.MEAL_LOG:
        fields = {
            "step": ConversationWorkflowStep.AWAITING_DATE,
            "meal_draft": MealLogDraft(),
        }
    elif workflow_kind is ConversationWorkflowKind.PLAN_REVISION:
        fields = {
            "step": ConversationWorkflowStep.GENERATING,
            "amendment": "Avoid cauliflower",
            "target_week": date(2026, 8, 17),
            "expected_plan_revision": 1,
            "request_id": "revision-1",
        }
    else:
        fields = {"step": ConversationWorkflowStep.PROFILE_MENU}

    with pytest.raises(ValidationError, match="only be uncollected"):
        ConversationState(
            **_conversation_state_values(),
            workflow_kind=workflow_kind,
            duration_collected=False,
            **fields,
        )


@pytest.mark.parametrize("plan_days", [0, 8, True, False])
def test_conversation_state_rejects_invalid_plan_duration(
    plan_days: object,
) -> None:
    """Plan duration is a strict bounded integer rather than a loose bool."""
    with pytest.raises(ValidationError):
        ConversationState(
            **_conversation_state_values(),
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=ConversationWorkflowStep.AWAITING_PREFERENCE,
            request_id="invalid-duration",
            plan_days=plan_days,
        )


def test_plan_days_export_is_a_bounded_typed_contract() -> None:
    """The public duration type can be used by planner-facing callers."""
    assert ExportedPlanDays is PlanDays


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


def test_profile_edit_menu_state_has_no_selected_operation() -> None:
    """A profile edit menu records no category or operation selection."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
        step=ConversationWorkflowStep.PROFILE_MENU,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    assert state.profile_category is None
    assert state.profile_operation is None


@pytest.mark.parametrize(
    ("category", "operation"),
    [
        (ProfileEditCategory.FAMILY, ProfileEditOperation.ADD),
        (ProfileEditCategory.FAMILY, ProfileEditOperation.REMOVE),
        (ProfileEditCategory.FAMILY, ProfileEditOperation.CHANGE_CALORIES),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.CHANGE_PROTEIN,
        ),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.CHANGE_FIBRE,
        ),
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            ProfileEditOperation.ADD,
        ),
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            ProfileEditOperation.REMOVE,
        ),
        (ProfileEditCategory.DIETARY_PREFERENCES, ProfileEditOperation.ADD),
        (
            ProfileEditCategory.DIETARY_PREFERENCES,
            ProfileEditOperation.REMOVE,
        ),
    ],
)
def test_profile_edit_awaiting_input_accepts_valid_operation(
    category: ProfileEditCategory,
    operation: ProfileEditOperation,
) -> None:
    """An awaiting profile edit must carry a category-valid operation."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
        step=ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
        profile_category=category,
        profile_operation=operation,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )

    assert state.profile_category is category
    assert state.profile_operation is operation


@pytest.mark.parametrize(
    ("operation", "expected_value"),
    [
        (ProfileEditOperation.CHANGE_PROTEIN, "change_protein"),
        (ProfileEditOperation.CHANGE_FIBRE, "change_fibre"),
    ],
)
def test_nutrient_operations_have_stable_values_and_are_family_only(
    operation: ProfileEditOperation, expected_value: str
) -> None:
    """Nutrient changes use stable values and belong only to Family."""
    assert operation.value == expected_value
    assert operation.is_valid_for(ProfileEditCategory.FAMILY)


@pytest.mark.parametrize(
    "category",
    [
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditCategory.DIETARY_PREFERENCES,
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        ProfileEditOperation.CHANGE_PROTEIN,
        ProfileEditOperation.CHANGE_FIBRE,
    ],
)
def test_nutrient_operations_are_invalid_for_non_family_categories(
    category: ProfileEditCategory,
    operation: ProfileEditOperation,
) -> None:
    """Nutrient changes cannot be selected for unrelated categories."""
    assert not operation.is_valid_for(category)


@pytest.mark.parametrize(
    "values",
    [
        {
            "step": ConversationWorkflowStep.PROFILE_MENU,
            "profile_category": ProfileEditCategory.FAMILY,
        },
        {
            "step": ConversationWorkflowStep.PROFILE_MENU,
            "profile_operation": ProfileEditOperation.ADD,
        },
        {
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": ProfileEditCategory.FAMILY,
        },
        {
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": ProfileEditCategory.FAMILY,
            "profile_operation": ProfileEditOperation.ADD,
            "meal_draft": MealLogDraft(),
        },
        {
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": ProfileEditCategory.DIETARY_CONSTRAINTS,
            "profile_operation": ProfileEditOperation.CHANGE_CALORIES,
        },
        {
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": ProfileEditCategory.FAMILY,
            "profile_operation": ProfileEditOperation.ADD,
            "preference": "vegetarian",
        },
        {
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": ProfileEditCategory.FAMILY,
            "profile_operation": ProfileEditOperation.ADD,
            "expires_at": 1,
        },
    ],
)
def test_profile_edit_rejects_invalid_state_shapes(
    values: dict[str, object],
) -> None:
    """Reject missing, unrelated, expired, and category-invalid state data."""
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = {
        "workflow_kind": ConversationWorkflowKind.PROFILE_EDIT,
        "step": ConversationWorkflowStep.PROFILE_MENU,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=24),
    }

    with pytest.raises(ValidationError):
        ConversationState(**{**defaults, **values})


def test_non_profile_workflows_reject_profile_fields() -> None:
    """Profile selections are invalid on meal, plan, and revision states."""
    now = datetime.now(timezone.utc)
    common = {
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=24),
    }

    with pytest.raises(ValidationError):
        ConversationState(
            **common,
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_DATE,
            meal_draft=MealLogDraft(),
            profile_category=ProfileEditCategory.FAMILY,
        )
    with pytest.raises(ValidationError):
        ConversationState(
            **common,
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=ConversationWorkflowStep.AWAITING_PREFERENCE,
            preference="vegetarian",
            request_id="request-1",
            profile_operation=ProfileEditOperation.ADD,
        )
    with pytest.raises(ValidationError):
        ConversationState(
            **common,
            workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
            step=ConversationWorkflowStep.GENERATING,
            amendment="Avoid cauliflower",
            target_week=date(2026, 8, 10),
            expected_plan_revision=1,
            request_id="request-1",
            profile_category=ProfileEditCategory.DIETARY_CONSTRAINTS,
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


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {"protein_target": 120},
        {"fibre_target": 30},
        {"protein_target": 120, "fibre_target": 30},
    ],
)
def test_family_member_optional_nutrient_targets(
    targets: dict[str, int],
) -> None:
    """Allow either optional target to be supplied independently."""
    member = FamilyMember(name="Alice", calorie_target=2000, **targets)

    assert member.protein_target == targets.get("protein_target")
    assert member.fibre_target == targets.get("fibre_target")


@pytest.mark.parametrize("target", [1, 1_000])
@pytest.mark.parametrize("field_name", ["protein_target", "fibre_target"])
def test_family_member_accepts_nutrient_target_boundaries(
    field_name: str, target: int
) -> None:
    """Accept inclusive lower and upper bounds for nutrient targets."""
    member = FamilyMember(
        name="Alice", calorie_target=2000, **{field_name: target}
    )

    assert getattr(member, field_name) == target


@pytest.mark.parametrize("target", [-1, 0, 1_001])
@pytest.mark.parametrize("field_name", ["protein_target", "fibre_target"])
def test_family_member_rejects_invalid_nutrient_targets(
    field_name: str, target: int
) -> None:
    """Reject nutrient targets outside the inclusive gram bounds."""
    with pytest.raises(ValidationError):
        FamilyMember(
            name="Alice",
            calorie_target=2000,
            **{field_name: target},
        )


def test_family_member_targets_serialize_and_load_from_legacy_profile() -> None:
    """Serialize targets and load profiles that predate those fields."""
    profile = make_profile(with_nutrient_targets=True)

    serialized_members = profile.model_dump()["family_members"]
    assert serialized_members[0]["protein_target"] == 120
    assert serialized_members[0]["fibre_target"] == 30

    legacy_profile = UserProfile.model_validate(
        {
            "name": "Legacy",
            "people_count": 1,
            "family_members": [{"name": "Legacy", "calorie_target": 2000}],
        }
    )
    assert legacy_profile.family_members[0].protein_target is None
    assert legacy_profile.family_members[0].fibre_target is None


def test_family_member_targets_do_not_change_profile_completeness() -> None:
    """Completeness remains based on member count and calorie targets."""
    legacy_complete = UserProfile(
        name="Legacy",
        people_count=1,
        family_members=[FamilyMember(name="Legacy", calorie_target=2000)],
    )
    targeted_complete = UserProfile(
        name="Targeted",
        people_count=1,
        family_members=[
            FamilyMember(
                name="Targeted",
                calorie_target=2000,
                protein_target=120,
                fibre_target=30,
            )
        ],
    )
    incomplete = UserProfile(name="Incomplete", people_count=2)

    assert legacy_complete.is_complete
    assert targeted_complete.is_complete
    assert not incomplete.is_complete


def test_user_profile_defaults() -> None:
    """Test UserProfile model with default optional fields."""
    profile = UserProfile(name="John Doe")
    assert profile.name == "John Doe"
    assert profile.family_members == []
    assert profile.dietary_constraints == []
    assert profile.dietary_preferences == []
    assert profile.people_count == 1


def test_user_profile_batch_rules_round_trip_and_bounded() -> None:
    """Batch rules default safely and round-trip as bounded typed data."""
    historical = UserProfile(name="Historical")
    assert historical.batch_rules == []

    rule = BatchRule(
        id="provider-batch",
        source_text="cook chicken for two dinners",
        foods_any_of=["chicken"],
        preparation_meal_types=[MealType.DINNER],
        reuse_meal_types=[MealType.DINNER],
        total_yield=2,
    )
    profile = UserProfile(name="Current", batch_rules=[rule])
    restored = UserProfile.model_validate(profile.model_dump(mode="json"))
    assert restored.batch_rules == [rule]

    with pytest.raises(ValidationError):
        UserProfile.model_validate(
            {"name": "Malformed", "batch_rules": [{"id": "bad"}]}
        )
    with pytest.raises(ValidationError):
        UserProfile.model_validate(
            {
                "name": "Oversized",
                "batch_rules": [rule.model_dump(mode="json")] * 21,
            }
        )


def test_user_profile_full() -> None:
    """Test UserProfile model with full data."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="John Doe",
        family_members=[
            member,
            FamilyMember(name="Jane Doe", calorie_target=1800),
        ],
        dietary_constraints=["peanuts", "gluten-free"],
        dietary_preferences=[
            DietaryPreferenceEntry(
                id="keto",
                source_text="keto",
                rule=DietaryRule(
                    id="keto",
                    source_text="keto",
                    foods_any_of=["vegetables"],
                    operator=RuleOperator.AT_LEAST,
                    count=1,
                ),
            )
        ],
        goals=["weight-loss"],  # legacy input is intentionally discarded
        people_count=2,
    )
    assert len(profile.family_members) == 2
    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "peanuts",
        "gluten-free",
    ]
    assert [entry.source_text for entry in profile.dietary_preferences] == [
        "keto"
    ]
    assert "goals" not in profile.model_dump()
    assert profile.people_count == 2


def test_user_profile_merges_legacy_constraints_in_order_and_deduplicates() -> (
    None
):
    """Merge legacy fields while retaining the first display spelling."""
    profile = UserProfile.model_validate(
        {
            "name": "John Doe",
            "allergies": ["Peanuts", "Shellfish"],
            "restrictions": ["peanuts", "Gluten-free", "SHELLFISH"],
        }
    )

    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "Peanuts",
        "Shellfish",
        "Gluten-free",
    ]
    dumped = profile.model_dump()
    assert dumped["dietary_constraints"] == [
        entry.model_dump() for entry in profile.dietary_constraints
    ]
    assert "allergies" not in dumped
    assert "restrictions" not in dumped


def test_user_profile_prefers_present_canonical_constraints_over_legacy() -> (
    None
):
    """An explicit canonical field takes precedence over legacy fields."""
    profile = UserProfile.model_validate(
        {
            "name": "John Doe",
            "dietary_constraints": ["Vegan"],
            "allergies": ["peanuts"],
            "restrictions": ["dairy-free"],
        }
    )

    assert [entry.source_text for entry in profile.dietary_constraints] == [
        "Vegan"
    ]


def test_user_profile_has_no_constraints_when_legacy_data_is_empty() -> None:
    """Profiles without either legacy field get an empty canonical list."""
    profile = UserProfile.model_validate(
        {"name": "John Doe", "allergies": [], "restrictions": []}
    )

    assert profile.dietary_constraints == []


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ({}, None),
        ({"allergies": None, "restrictions": None}, None),
        ({"allergies": None, "restrictions": []}, []),
        ({"allergies": "none", "restrictions": None}, []),
        ({"allergies": "nothing", "restrictions": None}, []),
        ({"allergies": None, "restrictions": "N/A!"}, []),
        ({"allergies": "not applicable", "restrictions": None}, []),
        ({"allergies": "no allergies", "restrictions": None}, []),
        (
            {"allergies": "no dietary constraints", "restrictions": None},
            [],
        ),
        (
            {
                "allergies": ["Peanuts", "none", "peanuts"],
                "restrictions": ["Vegan", "NO RESTRICTIONS", "vegan"],
            },
            ["Peanuts", "Vegan"],
        ),
        (
            {"allergies": "shellfish", "restrictions": "Dairy-free"},
            ["shellfish", "Dairy-free"],
        ),
    ],
)
def test_profile_update_normalizes_legacy_constraints(
    legacy: dict[str, object], expected: list[str] | None
) -> None:
    """Partial legacy drafts preserve unanswered and explicit-empty states."""
    update = ProfileUpdateEntities.model_validate(legacy)

    assert (
        None
        if update.dietary_constraints is None
        else [entry.source_text for entry in update.dietary_constraints]
    ) == expected
    dumped = update.model_dump()
    assert "allergies" not in dumped
    assert "restrictions" not in dumped


def test_profile_update_canonical_constraints_are_authoritative() -> None:
    """A present canonical value wins over legacy constraint fields."""
    update = ProfileUpdateEntities.model_validate(
        {
            "dietary_constraints": None,
            "allergies": ["peanuts"],
            "restrictions": ["vegan"],
        }
    )

    assert update.dietary_constraints is None


def test_profile_and_update_discard_legacy_goals_from_canonical_dumps() -> None:
    """Legacy goal input is accepted for reads but never remains canonical."""
    profile = UserProfile.model_validate(
        {"name": "Alex", "goals": ["lose weight"]}
    )
    update = ProfileUpdateEntities.model_validate(
        {"goals": ["lose weight"], "name": "Alex"}
    )

    assert "goals" not in profile.model_dump()
    assert "goals" not in update.model_dump()
    assert "goals" not in profile.model_dump_json()
    assert "goals" not in update.model_dump_json()
    assert not hasattr(profile, "goals")
    assert not hasattr(update, "goals")


@pytest.mark.parametrize(
    "legacy",
    [
        {},
        {"allergies": None, "restrictions": None},
        {"allergies": [], "restrictions": None},
        {"allergies": "none", "restrictions": "no restrictions"},
    ],
)
def test_user_profile_normalizes_legacy_constraints_to_complete_list(
    legacy: dict[str, object],
) -> None:
    """Complete profiles always expose a canonical constraint list."""
    profile = UserProfile.model_validate({"name": "John Doe", **legacy})

    assert profile.dietary_constraints == []
    dumped = profile.model_dump()
    assert "allergies" not in dumped
    assert "restrictions" not in dumped


def test_user_profile_invalid_people_count() -> None:
    """Test UserProfile with invalid people_count (< 1)."""
    with pytest.raises(ValidationError):
        UserProfile(name="John", people_count=0)


def test_user_profile_ignores_legacy_revision_on_read() -> None:
    """Legacy profile revisions are ignored by the canonical model."""
    profile = UserProfile.model_validate(
        {
            "name": "John Doe",
            "allergies": ["peanuts"],
            "restrictions": ["vegetarian"],
            "revision": 7,
        }
    )

    assert "revision" not in profile.model_dump()


@pytest.mark.parametrize(
    "field",
    ["dietary_constraints", "dietary_preferences"],
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
        ("dietary_constraints", "no dietary constraints"),
        ("dietary_preferences", "no dietary preferences."),
        ("dietary_preferences", "NO PREFERENCES"),
        ("dietary_constraints", " No dietary constraints! "),
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
    ["dietary_constraints", "dietary_preferences"],
)
@pytest.mark.parametrize("value", [None, []])
def test_profile_update_preserves_none_and_lists(
    field: str, value: object
) -> None:
    """Keep missing values and list values on their existing code paths."""
    update = ProfileUpdateEntities.model_validate({field: value})

    actual = getattr(update, field)
    if value is None or value == []:
        assert actual == value
    else:
        assert [entry.source_text for entry in actual] == value


def test_profile_update_rejects_raw_dietary_preference_lists() -> None:
    """Profile updates cannot persist unconfirmed preference prose."""
    with pytest.raises(ValidationError):
        ProfileUpdateEntities.model_validate(
            {"dietary_preferences": ["peanuts"]}
        )


@pytest.mark.parametrize(
    "field",
    ["dietary_constraints", "dietary_preferences"],
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
        [],
        [PlanDay(day=day) for day in [1, 2, 3, 5]],
        [PlanDay(day=1) for _ in range(7)],
        [PlanDay(day=2)],
        [PlanDay(day=day) for day in range(1, 8)] + [PlanDay(day=7)],
    ],
)
def test_weekly_plan_requires_contiguous_unique_days(
    days: list[PlanDay],
) -> None:
    """Plans reject empty, malformed, non-one, and overlong day sequences."""
    with pytest.raises(ValidationError):
        WeeklyPlan(week_start="2026-08-10", days=days)


@pytest.mark.parametrize("plan_days", range(1, 8))
def test_weekly_plan_accepts_every_contiguous_horizon(plan_days: int) -> None:
    """Plans accept each contiguous horizon from one through seven days."""
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[PlanDay(day=day) for day in range(1, plan_days + 1)],
    )

    assert [plan_day.day for plan_day in plan.days] == list(
        range(1, plan_days + 1)
    )


@pytest.mark.parametrize(
    ("plan_days", "expected_week_end"),
    [
        (1, date(2026, 8, 10)),
        (3, date(2026, 8, 12)),
        (7, date(2026, 8, 16)),
    ],
)
def test_weekly_plan_derives_dynamic_end_date(
    plan_days: int, expected_week_end: date
) -> None:
    """The final covered date is derived from the actual plan length."""
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[PlanDay(day=day) for day in range(1, plan_days + 1)],
    )

    assert plan.week_end == expected_week_end


@pytest.mark.parametrize("plan_days", range(1, 8))
def test_make_plan_constructs_exact_contiguous_horizon(plan_days: int) -> None:
    """The shared factory creates exactly the requested day sequence."""
    plan = make_plan(plan_days=plan_days)

    assert [plan_day.day for plan_day in plan.days] == list(
        range(1, plan_days + 1)
    )


def test_make_plan_defaults_remain_legacy_compatible() -> None:
    """Default factory values preserve the historical complete plan shape."""
    plan = make_plan()

    assert len(plan.days) == 7
    assert plan.status is PlanStatus.DRAFT
    assert plan.revision == 0
    assert plan.grocery_status is GroceryStatus.NOT_REQUESTED
    assert plan.grocery_list == []
    assert plan.planning_instructions == []
    assert all(len(plan_day.meals) == 1 for plan_day in plan.days)
    assert all(
        plan_day.meals[0].meal_type is MealType.LUNCH for plan_day in plan.days
    )
    assert all(plan_day.meals[0].est_calories == 500 for plan_day in plan.days)
    assert all(
        plan_day.meals[0].outcome is MealOutcome.UNREPORTED
        for plan_day in plan.days
    )


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
