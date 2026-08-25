"""Tests for prompt builders and context assembly."""

from datetime import date, datetime, timedelta, timezone

import pytest

from meal_planner.llm.prompts import (
    build_conversational_prompt,
    build_grocery_prompt,
    build_plan_prompt,
    build_plan_revision_prompt,
    build_preference_interpretation_prompt,
)
from meal_planner.models.schemas import (
    ConstraintEntry,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryRule,
    FamilyMember,
    Ingredient,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    PlanDay,
    PlannedMeal,
    PreferenceRequirement,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)


def test_build_conversational_prompt_empty() -> None:
    """Test build_conversational_prompt with None/empty parameters."""
    prompt = build_conversational_prompt()
    assert "No user profile established yet." in prompt
    assert "No active meal plan." in prompt


def test_conversational_prompt_renders_pending_meal_without_defaults() -> None:
    """Pending meal context gives extraction rules and today's date."""
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.MEAL_LOG,
        step=ConversationWorkflowStep.AWAITING_DATE,
        meal_draft=MealLogDraft(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    prompt = build_conversational_prompt(
        conversation_state=state, current_date=date(2026, 8, 15)
    )
    assert "Today's date is 2026-08-15" in prompt
    assert "never invent a date" in prompt
    assert "Step: awaiting_date" in prompt
    assert "No recent meal history logged." in prompt
    assert "log_meal" in prompt


def test_build_conversational_prompt_with_context() -> None:
    """Test build_conversational_prompt with full user context."""
    member = FamilyMember(name="Alice", calorie_target=1800)
    profile = UserProfile(
        name="John",
        family_members=[
            member,
            FamilyMember(name="John", calorie_target=2100),
        ],
        dietary_constraints=["peanuts", "dairy-free"],
        dietary_preferences=["low-carb"],
        people_count=2,
    )
    meal = PlannedMeal(meal_type="lunch", name="Salad")
    day = PlanDay(day=1, meals=[meal])
    days = [day, *(PlanDay(day=value) for value in range(2, 8))]
    plan = WeeklyPlan(week_start="2026-08-10", status="confirmed", days=days)
    history = [
        MealLogEntry(
            date="2026-08-04",
            meal_type="dinner",
            description="Tacos",
            created_at="2026-08-04T18:00:00Z",
        )
    ]

    prompt = build_conversational_prompt(
        profile=profile, current_plan=plan, recent_meals=history
    )
    assert "John" in prompt
    assert "Alice (1800 kcal)" in prompt
    assert "Dietary constraints: peanuts, dairy-free" in prompt
    assert "Allergies:" not in prompt
    assert "Restrictions:" not in prompt
    assert "low-carb" in prompt
    assert "Status: confirmed" in prompt
    assert "Day 1: lunch: Salad" in prompt
    assert "Tacos" in prompt


def test_build_conversational_prompt_with_partial_profile_draft() -> None:
    prompt = build_conversational_prompt(
        profile_draft=ProfileUpdateEntities(name="Alex", people_count=2)
    )

    assert "--- Saved Profile ---" in prompt
    assert "--- Pending Profile Updates ---" in prompt
    assert "Family Name: Alex" in prompt
    assert "People Count: 2" in prompt
    assert "Family Members: Missing" in prompt
    assert "Dietary Constraints: Missing" in prompt
    assert "Allergies:" not in prompt
    assert "Restrictions:" not in prompt
    assert "Family Members: None specified" not in prompt


def test_conversational_prompt_distinguishes_family_and_member_names() -> None:
    prompt = build_conversational_prompt(
        profile_draft=ProfileUpdateEntities(
            name="Nick",
            family_members=[{"name": "Val", "calorie_target": 1800}],
        )
    )

    assert "Family Name: Nick" in prompt
    assert "Family Members: Val (1800 kcal)" in prompt
    assert "top-level 'name' field means the household's family name" in prompt
    assert "individual member's name" in prompt
    assert "'name'" in prompt
    assert "people_count" in prompt


def test_conversational_prompt_contracts_optional_member_targets() -> None:
    """Profile extraction keeps optional targets on each member."""
    prompt = build_conversational_prompt()
    normalized = prompt.casefold()

    assert "family_members" in normalized
    assert "protein_target" in normalized
    assert "fibre_target" in normalized
    assert "optional" in normalized
    assert "grams/day" in normalized
    assert "only when the user explicitly provides" in normalized
    assert "never invent" in normalized
    assert "top-level 'protein_target'" not in normalized
    assert "top-level 'fibre_target'" not in normalized


def test_conversational_prompt_keeps_targets_with_correct_member() -> None:
    """Different optional targets remain attached to their source member."""
    prompt = build_conversational_prompt(
        profile_draft=ProfileUpdateEntities(
            family_members=[
                {"name": "Alex", "calorie_target": 2000},
                {
                    "name": "Sam",
                    "calorie_target": 1800,
                    "protein_target": 120,
                },
                {
                    "name": "Lee",
                    "calorie_target": 1600,
                    "fibre_target": 30,
                },
            ]
        )
    )

    assert (
        "Alex (2000 kcal) [protein target: not set; fibre target: not set]"
        in prompt
    )
    assert (
        "Sam (1800 kcal) [protein target: 120 g/day; fibre target: not set]"
        in prompt
    )
    assert (
        "Lee (1600 kcal) [protein target: not set; fibre target: 30 g/day]"
        in prompt
    )


def test_prompt_separates_saved_and_pending_values() -> None:
    profile = UserProfile(name="Alex", dietary_constraints=["shellfish"])
    draft = ProfileUpdateEntities(dietary_constraints=["peanuts"])

    prompt = build_conversational_prompt(
        profile=profile,
        profile_draft=draft,
    )

    saved_start = prompt.index("--- Saved Profile ---")
    pending_start = prompt.index("--- Pending Profile Updates ---")
    assert "Dietary constraints: shellfish" in prompt[saved_start:pending_start]
    assert "Dietary Constraints: peanuts" in prompt[pending_start:]
    assert "Allergies:" not in prompt
    assert "Restrictions:" not in prompt


def test_build_conversational_prompt_renders_pending_family_members() -> None:
    prompt = build_conversational_prompt(
        profile_draft=ProfileUpdateEntities(
            family_members=[{"name": "Sam", "calorie_target": 1800}]
        )
    )

    assert "Family Members: Sam (1800 kcal)" in prompt


@pytest.mark.parametrize(
    ("protein_target", "fibre_target", "protein_text", "fibre_text"),
    [
        (None, None, "not set", "not set"),
        (120, None, "120 g/day", "not set"),
        (None, 30, "not set", "30 g/day"),
        (120, 30, "120 g/day", "30 g/day"),
    ],
)
def test_conversational_prompts_render_all_member_targets(
    protein_target: int | None,
    fibre_target: int | None,
    protein_text: str,
    fibre_text: str,
) -> None:
    """Saved and pending members expose every target without defaults."""
    member = FamilyMember(
        name="Sam",
        calorie_target=1800,
        protein_target=protein_target,
        fibre_target=fibre_target,
    )
    profile = UserProfile(name="Household", family_members=[member])

    saved_prompt = build_conversational_prompt(profile=profile)
    pending_prompt = build_conversational_prompt(
        profile_draft=ProfileUpdateEntities(family_members=[member])
    )

    for prompt in (saved_prompt, pending_prompt):
        assert "Sam (1800 kcal)" in prompt
        assert f"protein target: {protein_text}" in prompt
        assert f"fibre target: {fibre_text}" in prompt
    assert "protein target: 0" not in saved_prompt
    assert "fibre target: 0" not in pending_prompt


def test_build_plan_prompt_empty() -> None:
    """Test build_plan_prompt with default/empty parameters."""
    prompt = build_plan_prompt()
    assert "General profile" in prompt
    assert "2000 kcal/day" in prompt
    assert "Week Start Date: 2026-08-10" in prompt
    assert "at most four meals per day" in prompt
    assert "OUTPUT JSON SCHEMA" in prompt


@pytest.mark.parametrize(
    ("plan_days", "end_date"),
    [(1, "2026-08-10"), (3, "2026-08-12"), (7, "2026-08-16")],
)
def test_build_plan_prompt_uses_exact_requested_duration(
    plan_days: int, end_date: str
) -> None:
    """Generation prompts describe the exact dates and day sequence."""
    prompt = build_plan_prompt(
        week_start="2026-08-10",
        plan_days=plan_days,
    )

    assert f"Generate a {plan_days}-day meal plan" in prompt
    assert f"Inclusive Plan Date Range: 2026-08-10 through {end_date}" in prompt
    day_sequence = ", ".join(str(day) for day in range(1, plan_days + 1))
    assert f"with day numbers: {day_sequence}" in prompt
    if plan_days < 7:
        assert "7-day meal plan" not in prompt
        assert "of the 7 days" not in prompt


def test_build_plan_prompt_states_complete_generated_plan_contract() -> None:
    """Initial generation prompts state all validator-aligned invariants."""
    prompt = build_plan_prompt()

    assert "exactly one breakfast, one lunch, and one dinner" in prompt
    assert "each of the 7 days" in prompt
    assert "Snack is optional" in prompt
    assert "at least one non-empty ingredient item" in prompt
    assert "positive est_calories" in prompt


def test_repair_plan_prompt_states_same_complete_generated_plan_contract() -> (
    None
):
    """Repair prompts retain the complete generation contract."""
    prompt = build_plan_prompt(repair_feedback="fix the missing meal")

    assert "exactly one breakfast, one lunch, and one dinner" in prompt
    assert "each of the 7 days" in prompt
    assert "Snack is optional" in prompt
    assert "at least one non-empty ingredient item" in prompt
    assert "positive est_calories" in prompt


def test_build_plan_prompt_with_context() -> None:
    """Test build_plan_prompt with full profile, history, and previous plan."""
    member = FamilyMember(name="Bob", calorie_target=2200)
    profile = UserProfile(
        name="Alice",
        family_members=[
            member,
            FamilyMember(name="Alice", calorie_target=1800),
            FamilyMember(name="Charlie", calorie_target=2000),
        ],
        dietary_constraints=["shellfish"],
        dietary_preferences=["keto"],
        people_count=3,
    )
    history = [
        MealLogEntry(
            date="2026-08-03",
            meal_type="dinner",
            description="Salmon",
            created_at="2026-08-03T18:00:00Z",
        )
    ]
    prev_meal_cooked = PlannedMeal(
        meal_type="dinner", name="Steak", outcome=MealOutcome.COOKED
    )
    prev_meal_skipped = PlannedMeal(
        meal_type="lunch", name="Soup", outcome=MealOutcome.SKIPPED
    )
    prev_meal_swapped = PlannedMeal(
        meal_type="dinner", name="Pasta", outcome=MealOutcome.SWAPPED
    )
    prev_meal_unreported = PlannedMeal(
        meal_type="snack", name="Fruit", outcome=MealOutcome.UNREPORTED
    )
    prev_plan = WeeklyPlan(
        week_start="2026-08-03",
        status="confirmed",
        days=[
            PlanDay(day=1, meals=[prev_meal_cooked]),
            PlanDay(day=2, meals=[prev_meal_skipped]),
            PlanDay(day=3, meals=[prev_meal_swapped, prev_meal_unreported]),
            *(PlanDay(day=value) for value in range(4, 8)),
        ],
    )

    prompt = build_plan_prompt(
        profile=profile,
        meal_history=history,
        previous_plan=prev_plan,
        week_start="2026-08-10",
    )
    assert "Family Name: Alice" in prompt
    assert "Bob (2200 kcal/day)" in prompt
    assert "shellfish" in prompt
    assert "Salmon" in prompt
    assert "Cooked: Steak" in prompt
    assert "Skipped: Soup" in prompt
    assert "Swapped: Pasta" in prompt
    assert "Fruit" not in prompt


@pytest.mark.parametrize(
    ("protein_target", "fibre_target", "protein_text", "fibre_text"),
    [
        (None, None, "not set", "not set"),
        (120, None, "120 g/day", "not set"),
        (None, 30, "not set", "30 g/day"),
        (120, 30, "120 g/day", "30 g/day"),
    ],
)
def test_plan_prompt_renders_all_member_targets(
    protein_target: int | None,
    fibre_target: int | None,
    protein_text: str,
    fibre_text: str,
) -> None:
    """Initial generation receives saved targets without inferred values."""
    profile = UserProfile(
        name="Household",
        family_members=[
            FamilyMember(
                name="Sam",
                calorie_target=1800,
                protein_target=protein_target,
                fibre_target=fibre_target,
            )
        ],
    )

    prompt = build_plan_prompt(profile=profile)

    assert "Sam (1800 kcal/day)" in prompt
    assert f"protein target: {protein_text}" in prompt
    assert f"fibre target: {fibre_text}" in prompt


def test_plan_prompt_guides_best_effort_adjustment_and_priority() -> None:
    """Generation explains target adjustment and safety precedence."""
    prompt = build_plan_prompt(
        profile=UserProfile(
            name="Household",
            family_members=[
                FamilyMember(
                    name="Sam",
                    calorie_target=1800,
                    protein_target=120,
                    fibre_target=30,
                )
            ],
        )
    )

    assert "best-effort" in prompt
    assert "meal choices and portions" in prompt
    assert "protein" in prompt and "fibre" in prompt
    assert "Dietary constraints" in prompt
    assert "safety" in prompt
    assert "does not calculate, validate, detect, or repair" in prompt
    assert '"est_calories"' in prompt
    assert "protein_total" not in prompt


def test_plan_prompt_renders_preference_and_constraints() -> None:
    """A request preference is visible without mutating profile context."""
    prompt = build_plan_prompt(
        profile=UserProfile(name="Alice", dietary_constraints=["peanuts"]),
        preference="Indian and pasta",
    )
    assert "REQUEST-SPECIFIC PREFERENCE" in prompt
    assert "Indian and pasta" in prompt
    assert "Dietary constraints: peanuts" in prompt
    assert "always take precedence" in prompt


def test_plan_prompt_separates_constraint_strict_and_best_effort_rules() -> (
    None
):
    """Effective prompt sections preserve rule priority and strictness."""
    strict = DietaryRule(
        id="strict-1",
        source_text="eggs twice",
        foods_any_of=["eggs"],
        count=2,
    )
    best_effort = DietaryRule(
        id="best-1",
        source_text="beans once if convenient",
        foods_any_of=["beans"],
        count=1,
        strength="best_effort",
    )
    constraint = ConstraintEntry(
        id="constraint-1", source_text="no peanuts", forbidden_terms=["peanuts"]
    )

    prompt = build_plan_prompt(
        preference="eggs twice",
        constraints=[constraint],
        effective_rules=[strict, best_effort],
    )

    assert "=== DIETARY CONSTRAINTS (HIGHEST PRIORITY) ===" in prompt
    assert "=== EFFECTIVE STRICT RULES ===" in prompt
    assert "=== EFFECTIVE BEST-EFFORT RULES ===" in prompt
    assert prompt.index("HIGHEST PRIORITY") < prompt.index(
        "EFFECTIVE STRICT RULES"
    )
    assert prompt.index("EFFECTIVE STRICT RULES") < prompt.index(
        "EFFECTIVE BEST-EFFORT RULES"
    )
    assert "no peanuts" in prompt
    assert "strict-1" in prompt
    assert "best-1" in prompt
    assert "Goals" not in prompt


def test_plan_prompt_renders_raw_preference_and_every_exact_rule() -> None:
    """The planner sees raw wording and the application interpretation."""
    prompt = build_plan_prompt(
        profile=UserProfile(name="Alice", dietary_constraints=["peanuts"]),
        preference="crepes or pancakes once at breakfast, eggs three times",
        requirements=[
            PreferenceRequirement(
                id="r1",
                source_text="crepes or pancakes once at breakfast",
                foods_any_of=["crepes", "pancakes"],
                meal_type="breakfast",
                exact_count=1,
            ),
            PreferenceRequirement(
                id="r2",
                source_text="eggs three times",
                foods_any_of=["eggs"],
                exact_count=3,
            ),
        ],
    )

    assert "crepes or pancakes once at breakfast, eggs three times" in prompt
    assert "r1" in prompt
    assert "crepes or pancakes once at breakfast" in prompt
    assert "foods_any_of: crepes, pancakes" in prompt
    assert "meal_type: breakfast" in prompt
    assert "exact_count: 1" in prompt
    assert "r2" in prompt
    assert "foods_any_of: eggs" in prompt
    assert "meal_type: any meal" in prompt
    assert "exact_count: 3" in prompt
    assert "Dietary constraints: peanuts" in prompt
    assert "always take precedence" in prompt


def test_plan_prompt_renders_bounded_repair_feedback() -> None:
    """Repair prompts add bounded feedback without changing the schema."""
    prompt = build_plan_prompt(
        preference="eggs three times",
        requirements=[
            PreferenceRequirement(
                id="r1",
                source_text="eggs three times",
                foods_any_of=["eggs"],
                exact_count=3,
            )
        ],
        repair_feedback="r1 matched 2 meals; expected exactly 3",
    )

    assert "BOUNDED REPAIR FEEDBACK" in prompt
    assert "r1 matched 2 meals; expected exactly 3" in prompt
    assert "OUTPUT JSON SCHEMA" in prompt
    assert '"week_start_date"' in prompt


def test_plan_revision_prompt_does_not_receive_generation_rules() -> None:
    """Plan revisions retain their existing amendment-only contract."""
    profile = UserProfile(name="Alex")
    plan = WeeklyPlan(
        week_start="2026-08-10",
        planning_instructions=["Keep it vegetarian"],
        days=[PlanDay(day=value) for value in range(1, 8)],
    )

    prompt = build_plan_revision_prompt(profile, plan, "Avoid mushrooms")

    assert "Keep it vegetarian" in prompt
    assert "Avoid mushrooms" in prompt
    assert "INTERPRETED PREFERENCE RULES" not in prompt


def test_preference_interpretation_prompt_defines_measurable_contract() -> None:
    prompt = build_preference_interpretation_prompt(
        "Have crepes or pancakes once at breakfast, eggs three times, "
        "and salmon for dinner once."
    )

    assert "Have crepes or pancakes once at breakfast" in prompt
    assert "foods_any_of" in prompt
    assert "exact_count" in prompt
    assert "meal_type" in prompt
    assert "Combine alternative foods" in prompt
    assert "Every meaningful clause" in prompt
    assert "unparsed_text" in prompt


@pytest.mark.parametrize(
    ("mode", "required_text"),
    [
        ("constraint", "forbidden_terms"),
        ("stored_preference", "stored dietary preference"),
        ("current_plan_preference", "current plan preference"),
    ],
)
def test_interpretation_prompt_has_mode_specific_rule_contract(
    mode: str, required_text: str
) -> None:
    """Interpretation prompts describe the selected shared-rule mode."""
    prompt = build_preference_interpretation_prompt(
        "I'd like eggs for breakfast twice on Monday and Wednesday if "
        "convenient; no peanuts",
        mode=mode,
    )

    assert f'"mode": "{mode}"' in prompt
    assert required_text in prompt
    assert "exactly" in prompt
    assert "at_least" in prompt
    assert "at_most" in prompt
    assert "weekdays" in prompt
    assert "best_effort" in prompt
    assert "unparsed_text" in prompt
    assert "never silently discard" in prompt


def test_interpretation_prompt_documents_wording_strength_defaults() -> None:
    """Prompt makes implicit strictness rules explicit to the provider."""
    prompt = build_preference_interpretation_prompt(
        "I'd like eggs for breakfast if convenient"
    )

    assert "I'd like" in prompt
    assert "at_least 1" in prompt
    assert "if convenient" in prompt
    assert "best_effort" in prompt


def test_preference_interpretation_prompt_requires_clarification() -> None:
    prompt = build_preference_interpretation_prompt("Make it healthy and fun")

    assert "ambiguous" in prompt
    assert "conflicting" in prompt
    assert "unsupported" in prompt
    assert "subjective" in prompt
    assert "clarification" in prompt
    assert "silently discard" in prompt


def test_preference_interpretation_does_not_change_conversational_intent() -> (
    None
):
    prompt = build_conversational_prompt()

    assert "append a JSON block" in prompt
    assert "foods_any_of" not in prompt
    assert "unparsed_text" not in prompt


def test_conversational_prompt_contracts_draft_revision() -> None:
    prompt = build_conversational_prompt(
        current_plan=WeeklyPlan(
            week_start="2026-08-10",
            days=[PlanDay(day=value) for value in range(1, 8)],
        )
    )
    assert "revise_plan" in prompt
    assert "only {'amendment': '<faithful request>'}" in prompt
    assert "Keep edit_plan for one targeted day and meal" in prompt


def test_build_plan_revision_prompt_contains_trusted_full_context() -> None:
    profile = UserProfile(
        name="Alex",
        family_members=[FamilyMember(name="Alex", calorie_target=2000)],
        dietary_constraints=["peanuts"],
    )
    plan = WeeklyPlan(
        week_start="2026-08-10",
        planning_instructions=["Three egg breakfasts"],
        days=[
            PlanDay(
                day=1,
                meals=[
                    PlannedMeal(
                        meal_type="breakfast",
                        name="Eggs",
                        ingredients=[Ingredient(item="Eggs", amount="2")],
                    )
                ],
            ),
            *(PlanDay(day=value) for value in range(2, 8)),
        ],
    )
    amendment = (
        "Make breakfasts waffles, crepes, or eggs three times, keep an open "
        "day, and avoid cauliflower"
    )
    prompt = build_plan_revision_prompt(profile, plan, amendment)
    assert "Dietary constraints: peanuts" in prompt
    assert "Allergies:" not in prompt
    assert "Restrictions:" not in prompt
    assert '"ingredients"' in prompt
    assert "Three egg breakfasts" in prompt
    assert amendment in prompt
    assert "2026-08-10" in prompt
    assert "seven days" in prompt
    assert "Do not return a patch" in prompt


@pytest.mark.parametrize("plan_days", [1, 3, 7])
def test_revision_prompt_preserves_existing_length_and_date_range(
    plan_days: int,
) -> None:
    week_start = date(2026, 8, 10)
    plan = WeeklyPlan(
        week_start=week_start,
        days=[PlanDay(day=value) for value in range(1, plan_days + 1)],
    )

    prompt = build_plan_revision_prompt(
        UserProfile(name="Alex"), plan, "Avoid mushrooms"
    )

    week_end = week_start + timedelta(days=plan_days - 1)
    assert f"{plan_days}-day draft" in prompt
    assert f"days 1 through {plan_days}" in prompt
    assert (
        f"from {week_start.isoformat()} through {week_end.isoformat()}"
        in prompt
    )
    if plan_days < 7:
        assert "seven-day" not in prompt
        assert "seven days" not in prompt


@pytest.mark.parametrize(
    ("protein_target", "fibre_target", "protein_text", "fibre_text"),
    [
        (None, None, "not set", "not set"),
        (120, None, "120 g/day", "not set"),
        (None, 30, "not set", "30 g/day"),
        (120, 30, "120 g/day", "30 g/day"),
    ],
)
def test_revision_prompt_renders_all_member_targets(
    protein_target: int | None,
    fibre_target: int | None,
    protein_text: str,
    fibre_text: str,
) -> None:
    """Whole-plan revision receives every saved target."""
    profile = UserProfile(
        name="Household",
        family_members=[
            FamilyMember(
                name="Sam",
                calorie_target=1800,
                protein_target=protein_target,
                fibre_target=fibre_target,
            )
        ],
    )
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[PlanDay(day=value) for value in range(1, 8)],
    )

    prompt = build_plan_revision_prompt(profile, plan, "Keep the plan simple")

    assert "Sam (1800 kcal/day)" in prompt
    assert f"protein target: {protein_text}" in prompt
    assert f"fibre target: {fibre_text}" in prompt


def test_revision_prompt_guides_best_effort_adjustment_and_priority() -> None:
    """Revision explains target adjustment without changing output schema."""
    profile = UserProfile(
        name="Household",
        family_members=[
            FamilyMember(
                name="Sam",
                calorie_target=1800,
                protein_target=120,
                fibre_target=30,
            )
        ],
    )
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[PlanDay(day=value) for value in range(1, 8)],
    )

    prompt = build_plan_revision_prompt(profile, plan, "Keep the plan simple")

    assert "best-effort" in prompt
    assert "meal choices and portions" in prompt
    assert "Dietary constraints" in prompt
    assert "safety" in prompt
    assert "does not calculate, validate, detect, or repair" in prompt
    assert '"est_calories"' in prompt
    assert "protein_total" not in prompt


def test_build_grocery_prompt() -> None:
    """Test build_grocery_prompt context scaling and formatting."""
    ing1 = Ingredient(item="Chicken", amount="500g")
    ing2 = Ingredient(item="Rice", amount="200g")
    meal = PlannedMeal(
        meal_type="dinner", name="Chicken Rice", ingredients=[ing1, ing2]
    )
    day = PlanDay(day=1, meals=[meal])
    plan = WeeklyPlan(
        week_start="2026-08-10",
        days=[day, *(PlanDay(day=value) for value in range(2, 8))],
    )

    prompt = build_grocery_prompt(plan=plan, people_count=4)
    assert "Scale quantities for 4 people." in prompt
    assert "Day 1 dinner (Chicken Rice): 500g Chicken, 200g Rice" in prompt
    assert "Produce, Dairy, Pantry" in prompt
