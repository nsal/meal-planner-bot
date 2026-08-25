"""Tests for evidence-based meal-plan preference validation."""

from datetime import date

import pytest

from meal_planner.models import (
    ConstraintEntry,
    DietaryRule,
    Ingredient,
    PlanDay,
    PlannedMeal,
    PreferenceRequirement,
    RuleOperator,
    RuleStrength,
    Weekday,
    WeeklyPlan,
)
from meal_planner.models.schemas import MealType
from meal_planner.preferences import (
    HorizonFeasibilityResult,
    PlanValidationResult,
    RequirementValidation,
    RuleHorizonFeasibility,
    ValidationIssue,
    format_best_effort_summary,
    format_horizon_clarification,
    format_satisfaction_summary,
    format_unmet_preference_clauses,
    match_requirement,
    matches_food,
    validate_generated_plan,
    validate_horizon_feasibility,
)
from meal_planner.telegram.api import split_text

MAX_REQUIREMENT_OUTPUT_LENGTH = 1000


def _exact_fit_unmet_clauses() -> tuple[list[str], str, str]:
    """Return two clauses whose complete terminal message is exact-fit."""
    prefix = (
        "The AI returned an invalid meal plan because these preference "
        "clauses were not met:\n"
    )
    suffix = (
        "\nNo draft was saved. Your preference is retained; use /plan to retry."
    )
    second_length = 50
    first_length = 1000 - len(prefix) - len(suffix) - 4 - 1
    first_length -= second_length
    clauses = ["a" * first_length, "b" * second_length]
    return clauses, prefix, suffix


def requirement(
    food_terms: list[str],
    exact_count: int,
    meal_type: MealType | None = None,
    identifier: str = "r1",
) -> PreferenceRequirement:
    """Build a test requirement with a concise default source clause."""
    return PreferenceRequirement(
        id=identifier,
        source_text=" or ".join(food_terms),
        foods_any_of=food_terms,
        meal_type=meal_type,
        exact_count=exact_count,
    )


def rule(
    food_terms: list[str],
    count: int,
    *,
    identifier: str = "rule-1",
    operator: RuleOperator = RuleOperator.EXACTLY,
    meal_type: MealType | None = None,
    weekdays: list[Weekday] | None = None,
    strength: RuleStrength = RuleStrength.STRICT,
) -> DietaryRule:
    """Build a generalized rule for preference validation tests."""
    return DietaryRule(
        id=identifier,
        source_text=" or ".join(food_terms),
        foods_any_of=food_terms,
        count=count,
        operator=operator,
        meal_type=meal_type,
        weekdays=weekdays or [],
        strength=strength,
    )


def constraint(
    terms: list[str], identifier: str = "constraint-1"
) -> ConstraintEntry:
    """Build a constraint for validation tests."""
    return ConstraintEntry(
        id=identifier,
        source_text=" or ".join(terms),
        forbidden_terms=terms,
    )


def meal(
    meal_type: MealType,
    name: str,
    ingredients: list[str] | None = None,
    calories: int = 500,
) -> PlannedMeal:
    """Build a planned meal for evidence and completeness tests."""
    return PlannedMeal(
        meal_type=meal_type,
        name=name,
        ingredients=[Ingredient(item=item) for item in ingredients or []],
        est_calories=calories,
    )


def plan_with(
    meals_by_day: dict[int, list[PlannedMeal]],
) -> WeeklyPlan:
    """Build a seven-day plan while allowing intentionally incomplete days."""
    return WeeklyPlan(
        week_start_date=date(2026, 8, 17),
        days=[
            PlanDay(day=day, meals=meals_by_day.get(day, []))
            for day in range(1, 8)
        ],
    )


@pytest.mark.parametrize(
    ("text", "food", "expected"),
    [
        ("Blueberry Pancakes", "pancake", True),
        ("PANCAKES", "pancake", True),
        ("crepes, with berries", "crepes", True),
        ("cookie", "cookies", True),
        ("cookies", "cookie", True),
        ("brownie", "brownies", True),
        ("brownies", "brownie", True),
        ("smoothie", "smoothies", True),
        ("smoothies", "smoothie", True),
        ("pie", "pies", True),
        ("pies", "pie", True),
        ("berry", "berries", True),
        ("berries", "berry", True),
        ("  salmon\twith rice ", "salmon", True),
        ("berry compote", "berries", True),
        ("mac-and-cheese", "mac and cheese", True),
        ("Ｒｅｄ－Ｐｅｐｐｅｒ soup", "red pepper", True),
        ("cafe\u0301 salad", "café", True),
        ("salmonella rice", "salmon", False),
        ("eggplant parmesan", "egg", False),
        ("pancake batter", "cake", False),
        ("grape salad", "rap", False),
        ("brown", "brownies", False),
        ("smooth", "smoothies", False),
        ("cookiesauce", "cookie", False),
        ("pies", "pie crusts", False),
    ],
)
def test_matches_food_uses_normalized_whole_words_and_phrases(
    text: str, food: str, expected: bool
) -> None:
    """Match names safely across formatting and conservative plurals."""
    assert matches_food(text, food) is expected


def test_match_requirement_uses_name_and_ingredients_once() -> None:
    """Repeated evidence in one meal still yields one distinct match."""
    plan = plan_with(
        {
            1: [
                meal(
                    MealType.BREAKFAST,
                    "Egg and spinach omelette",
                    ["eggs", "spinach"],
                ),
                meal(MealType.LUNCH, "Lentil soup", ["lentils"]),
                meal(MealType.DINNER, "Roasted vegetables", ["carrots"]),
            ],
            2: [
                meal(
                    MealType.BREAKFAST,
                    "Spinach omelette",
                    ["egg whites"],
                ),
                meal(MealType.LUNCH, "Bean salad", ["beans"]),
                meal(MealType.DINNER, "Rice bowl", ["rice"]),
            ],
        }
    )

    matches = match_requirement(plan, requirement(["egg"], 2))

    assert [(item.day, item.meal_type) for item in matches] == [
        (1, MealType.BREAKFAST),
        (2, MealType.BREAKFAST),
    ]
    assert matches[0].matched_foods == ("egg",)


def test_satisfaction_summary_compacts_maximum_requirement_shape() -> None:
    """Maximum requirement output stays bounded and deterministic."""
    requirements = [
        PreferenceRequirement(
            id=f"requirement-{index}",
            source_text="source " + ("x" * 493),
            foods_any_of=[
                (
                    f"food {index} {alternative}"
                    + ("x" * (100 - len(f"food {index} {alternative}")))
                )
                for alternative in range(20)
            ],
            exact_count=1,
        )
        for index in range(20)
    ]
    validation = PlanValidationResult(
        valid=True,
        requirements=tuple(
            RequirementValidation(
                requirement_id=requirement.id,
                expected_count=1,
                actual_count=1,
                possible_count=21,
                matched_meals=(),
            )
            for requirement in requirements
        ),
        issues=(),
    )

    summary = format_satisfaction_summary(validation, requirements)

    assert summary == format_satisfaction_summary(validation, requirements)
    assert len(summary) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert len(split_text(summary)) == 1
    assert "(+17 alternatives)" in summary
    assert "requirements omitted" in summary
    assert "x" * 493 not in summary


def test_unmet_clause_message_compacts_maximum_source_clauses() -> None:
    """Maximum compliance clauses stay bounded and one Telegram chunk."""
    requirements = [
        PreferenceRequirement(
            id=f"requirement-{index}",
            source_text=f"clause {index} " + ("x" * 487),
            foods_any_of=[f"food-{index}"],
            exact_count=1,
        )
        for index in range(20)
    ]
    validation = PlanValidationResult(
        valid=False,
        requirements=(),
        issues=tuple(
            ValidationIssue(
                code="requirement_count_mismatch",
                message="bounded test issue",
                requirement_id=requirement.id,
            )
            for requirement in requirements
        ),
    )

    from meal_planner.planner_handler import PlannerHandler

    message = PlannerHandler._generation_failure_message(
        "initial",
        "invalid plan",
        category="compliance",
        validation=validation,
        requirements=requirements,
    )

    assert len(message) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert len(split_text(message)) == 1
    assert "preference clauses omitted" in message
    assert "clause 0" in message


def test_unmet_clause_message_keeps_every_exact_fit_clause() -> None:
    """A complete terminal message at the cap keeps all whole clauses."""
    clauses, prefix, suffix = _exact_fit_unmet_clauses()

    message = format_unmet_preference_clauses(clauses)

    assert len(message) == MAX_REQUIREMENT_OUTPUT_LENGTH
    assert message == prefix + f"• {clauses[0]}\n• {clauses[1]}" + suffix
    assert "preference clauses omitted" not in message
    assert len(split_text(message)) == 1


def test_unmet_clause_message_omits_whole_clause_one_character_over() -> None:
    """One character over the cap produces deterministic whole omission."""
    clauses, prefix, suffix = _exact_fit_unmet_clauses()
    clauses[-1] += "b"

    message = format_unmet_preference_clauses(clauses)

    expected = (
        prefix
        + f"• {clauses[0]}\n• ... and 1 preference clauses omitted."
        + suffix
    )
    assert len(message) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert message == expected
    assert clauses[0] in message
    assert clauses[1] not in message
    assert len(split_text(message)) == 1


def test_alternatives_and_meal_scope_share_one_exact_count() -> None:
    """Alternative foods form a union limited to the requested meal type."""
    plan = plan_with(
        {
            1: [
                meal(MealType.BREAKFAST, "Crepes", ["flour"]),
                meal(MealType.LUNCH, "Trout salad", ["trout"]),
                meal(MealType.DINNER, "Salmon", ["salmon"]),
            ],
            2: [
                meal(MealType.BREAKFAST, "Pancakes", ["flour"]),
                meal(MealType.LUNCH, "Pasta", ["pasta"]),
                meal(MealType.DINNER, "Tofu", ["tofu"]),
            ],
        }
    )

    matches = match_requirement(
        plan,
        requirement(
            ["crepe", "pancake"],
            1,
            meal_type=MealType.BREAKFAST,
        ),
    )

    assert [(item.day, item.meal_type) for item in matches] == [
        (1, MealType.BREAKFAST),
        (2, MealType.BREAKFAST),
    ]


def test_reported_preference_example_has_exact_evidence_counts() -> None:
    """The reported pancake, egg, and salmon example is exact."""
    meals_by_day = {
        1: [
            meal(MealType.BREAKFAST, "Crepes", ["flour"]),
            meal(MealType.LUNCH, "Bean salad", ["beans"]),
            meal(MealType.DINNER, "Salmon with rice", ["salmon"]),
        ],
        2: [
            meal(MealType.BREAKFAST, "Egg toast", ["eggs"]),
            meal(MealType.LUNCH, "Lentil soup", ["lentils"]),
            meal(MealType.DINNER, "Tofu bowl", ["tofu"]),
        ],
        3: [
            meal(MealType.BREAKFAST, "Shakshuka", ["eggs"]),
            meal(MealType.LUNCH, "Bean salad", ["beans"]),
            meal(MealType.DINNER, "Rice bowl", ["rice"]),
        ],
        4: [
            meal(MealType.BREAKFAST, "Frittata", ["egg"]),
            meal(MealType.LUNCH, "Lentil soup", ["lentils"]),
            meal(MealType.DINNER, "Tofu bowl", ["tofu"]),
        ],
    }
    for day in range(5, 8):
        meals_by_day[day] = [
            meal(MealType.BREAKFAST, "Oatmeal", ["oats"]),
            meal(MealType.LUNCH, "Bean salad", ["beans"]),
            meal(MealType.DINNER, "Rice bowl", ["rice"]),
        ]
    plan = plan_with(meals_by_day)
    requirements = [
        requirement(
            ["pancake", "crepe"],
            1,
            MealType.BREAKFAST,
            "pancakes",
        ),
        requirement(["egg"], 3, MealType.BREAKFAST, "eggs"),
        requirement(
            ["salmon", "trout"],
            1,
            MealType.DINNER,
            "salmon",
        ),
    ]

    result = validate_generated_plan(plan, requirements)

    assert result.is_valid
    assert [item.actual_count for item in result.requirements] == [1, 3, 1]
    assert format_satisfaction_summary(result, requirements) == (
        "Preferences satisfied:\n"
        "• Pancake or Crepe: 1 breakfast\n"
        "• Egg: 3 breakfasts\n"
        "• Salmon or Trout: 1 dinner"
    )


def test_validation_allows_one_meal_to_satisfy_compatible_rules() -> None:
    """Each compatible rule counts the same distinct meal independently."""
    plan = plan_with(
        {
            day: [
                meal(MealType.BREAKFAST, "Egg pancakes", ["eggs"]),
                meal(MealType.LUNCH, "Bean salad", ["beans"]),
                meal(MealType.DINNER, "Rice", ["rice"]),
            ]
            for day in range(1, 8)
        }
    )

    result = validate_generated_plan(
        plan,
        [
            requirement(["egg"], 7, MealType.BREAKFAST, "eggs"),
            requirement(["pancake"], 7, MealType.BREAKFAST, "pancakes"),
        ],
    )

    assert result.is_valid
    assert [item.actual_count for item in result.requirements] == [7, 7]


def test_validation_reports_exact_count_excess_with_stable_feedback() -> None:
    """An excess match is invalid and its feedback is deterministic."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Egg toast", ["egg"]),
            meal(MealType.LUNCH, "Salad", ["greens"]),
            meal(MealType.DINNER, "Egg rice", ["egg"]),
        ]
        for day in range(1, 8)
    }
    plan = plan_with(meals)

    result = validate_generated_plan(plan, [requirement(["egg"], 3)])

    assert not result.is_valid
    assert [(issue.code, issue.requirement_id) for issue in result.issues] == [
        ("requirement_count_mismatch", "r1")
    ]
    assert result.issues[0].message == (
        "Requirement 'r1' matched 14 distinct meals; expected exactly 3."
    )


def test_completeness_requires_three_meals_ingredients_and_calories() -> None:
    """Generated plans require meals, ingredients, and positive calories."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Oats", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    meals[2] = [
        meal(MealType.BREAKFAST, "Oats", [], calories=0),
        meal(MealType.DINNER, "Rice", ["rice"]),
    ]

    result = validate_generated_plan(plan_with(meals))

    assert not result.is_valid
    assert [
        (issue.day, issue.meal_type, issue.code) for issue in result.issues
    ] == [
        (2, MealType.BREAKFAST, "empty_ingredients"),
        (2, MealType.BREAKFAST, "nonpositive_calories"),
        (2, MealType.LUNCH, "missing_meal_type"),
    ]


@pytest.mark.parametrize(
    ("ingredients", "calories", "expected_code"),
    [
        ([], 500, "empty_ingredients"),
        (["banana"], 0, "nonpositive_calories"),
        (["banana"], -1, "nonpositive_calories"),
    ],
)
def test_completeness_validates_present_optional_snacks(
    ingredients: list[str], calories: int, expected_code: str
) -> None:
    """Present snacks require ingredients and positive calories."""
    snack = (
        meal(MealType.SNACK, "Banana snack", ingredients, calories)
        if calories >= 0
        else PlannedMeal.model_construct(
            meal_type=MealType.SNACK,
            name="Banana snack",
            ingredients=[Ingredient(item=item) for item in ingredients],
            est_calories=calories,
        )
    )
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Oats", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
            snack if day == 1 else meal(MealType.SNACK, "Fruit", ["apple"]),
        ]
        for day in range(1, 8)
    }

    result = validate_generated_plan(plan_with(meals))

    assert not result.is_valid
    assert [
        (issue.day, issue.meal_type, issue.code) for issue in result.issues
    ] == [(1, MealType.SNACK, expected_code)]


def test_invalid_snack_cannot_satisfy_unscoped_requirement() -> None:
    """An invalid snack cannot make an otherwise matching rule valid."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Oats", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    meals[1].append(meal(MealType.SNACK, "Salmon snack", [], calories=0))

    result = validate_generated_plan(
        plan_with(meals),
        [requirement(["salmon"], 1, meal_type=None)],
    )

    assert not result.is_valid
    assert result.requirements[0].actual_count == 1
    assert [issue.code for issue in result.issues] == [
        "empty_ingredients",
        "nonpositive_calories",
    ]


def test_valid_optional_snack_can_satisfy_unscoped_requirement() -> None:
    """A complete optional snack remains eligible for unscoped evidence."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Oats", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    meals[1].append(
        meal(MealType.SNACK, "Salmon snack", ["salmon"], calories=150)
    )

    result = validate_generated_plan(
        plan_with(meals),
        [requirement(["salmon"], 1, meal_type=None)],
    )

    assert result.is_valid
    assert result.requirements[0].actual_count == 1
    assert result.requirements[0].matched_meals[0].meal_type is MealType.SNACK


def test_legacy_meal_payload_keeps_default_fields_on_deserialization() -> None:
    """Legacy meal records still deserialize with model defaults."""
    payload = {
        "week_start_date": "2026-08-17",
        "days": [
            {
                "day": day,
                "meals": [{"meal_type": "breakfast", "name": "Oats"}],
            }
            for day in range(1, 8)
        ],
    }

    restored = WeeklyPlan.model_validate(payload)

    assert restored.days[0].meals[0].ingredients == []
    assert restored.days[0].meals[0].est_calories == 0


def test_empty_days_are_reported_in_day_and_meal_order() -> None:
    """Empty generated days produce stable missing-meal feedback."""
    result = validate_generated_plan(plan_with({}))

    assert len(result.issues) == 21
    assert result.issues[:3] == (
        result.issues[0],
        result.issues[1],
        result.issues[2],
    )
    assert [issue.meal_type for issue in result.issues[:3]] == [
        MealType.BREAKFAST,
        MealType.LUNCH,
        MealType.DINNER,
    ]
    assert [issue.day for issue in result.issues[:6]] == [1, 1, 1, 2, 2, 2]


def test_impossible_requirement_count_is_reported() -> None:
    """A rule asking for more eligible meals than exist is explicit."""
    plan = plan_with(
        {
            day: [
                meal(MealType.BREAKFAST, "Oats", ["oats"]),
                meal(MealType.LUNCH, "Soup", ["beans"]),
                meal(MealType.DINNER, "Rice", ["rice"]),
            ]
            for day in range(1, 8)
        }
    )

    result = validate_generated_plan(
        plan,
        [requirement(["salmon"], 22)],
    )

    assert not result.is_valid
    assert result.issues[-1].code == "impossible_requirement_count"
    assert result.issues[-1].message == (
        "Requirement 'r1' needs 22 distinct eligible meals, "
        "but the plan contains only 21."
    )


def test_requirement_results_are_stable_and_include_missing_count() -> None:
    """Validation exposes typed counts even when a requirement is unmet."""
    plan = plan_with(
        {
            day: [
                meal(MealType.BREAKFAST, "Oats", ["oats"]),
                meal(MealType.LUNCH, "Soup", ["beans"]),
                meal(MealType.DINNER, "Rice", ["rice"]),
            ]
            for day in range(1, 8)
        }
    )

    result = validate_generated_plan(plan, [requirement(["egg"], 2)])

    assert result.requirements[0].expected_count == 2
    assert result.requirements[0].actual_count == 0
    assert result.requirements[0].matched_meals == ()


def test_format_satisfaction_summary_uses_validated_evidence_counts() -> None:
    """Accepted rules render a compact, user-visible evidence summary."""
    plan = plan_with(
        {
            day: [
                meal(MealType.BREAKFAST, "Egg toast", ["egg"]),
                meal(MealType.LUNCH, "Bean soup", ["beans"]),
                meal(MealType.DINNER, "Rice", ["rice"]),
            ]
            for day in range(1, 8)
        }
    )
    egg_requirement = requirement(["eggs"], 7, MealType.BREAKFAST)
    result = validate_generated_plan(plan, [egg_requirement])

    assert result.is_valid
    assert format_satisfaction_summary(result, [egg_requirement]) == (
        "Preferences satisfied:\n• Eggs: 7 breakfasts"
    )


def test_constraints_match_names_ingredients_aliases_and_alternatives() -> None:
    """Constraints inspect declared evidence with reviewed aliases."""
    meals = {
        1: [
            meal(MealType.BREAKFAST, "Dairy-free oats", ["oat milk"]),
            meal(MealType.LUNCH, "Cheese-free salad", ["greens"]),
            meal(MealType.DINNER, "Tofu bowl", ["tofu"]),
        ],
        2: [
            meal(MealType.BREAKFAST, "Berry bowl", ["berries"]),
            meal(MealType.LUNCH, "Chicken wrap", ["chicken"]),
            meal(MealType.DINNER, "Butter beans", ["beans"]),
        ],
    }

    result = validate_generated_plan(
        plan_with(meals),
        rules=[rule(["salmon", "trout"], 1, identifier="fish")],
        constraints=[constraint(["dairy"], "no-dairy")],
    )

    assert not result.is_valid
    assert result.constraints[0].matched_terms == (
        "milk",
        "cheese",
        "butter",
    )
    assert [
        (item.day, item.meal_type)
        for item in result.constraints[0].matched_meals
    ] == [
        (1, MealType.BREAKFAST),
        (1, MealType.LUNCH),
        (2, MealType.DINNER),
    ]


def test_constraint_matching_avoids_substring_false_positives() -> None:
    """Constraint evidence does not match substrings or punctuation noise."""
    meals = {
        1: [
            meal(MealType.BREAKFAST, "Eggplant parmesan", ["eggplant"]),
            meal(MealType.LUNCH, "Salmonella rice", ["rice"]),
            meal(MealType.DINNER, "Café salad", ["cafe\u0301"]),
        ],
    }

    result = validate_generated_plan(
        plan_with(meals),
        constraints=[constraint(["egg", "salmon", "café"])],
    )

    assert not result.is_valid
    assert [item.meal_type for item in result.constraints[0].matched_meals] == [
        MealType.DINNER
    ]
    assert result.constraints[0].matched_terms == ("café",)


@pytest.mark.parametrize(
    ("operator", "count", "matched", "expected_valid"),
    [
        (RuleOperator.EXACTLY, 2, 2, True),
        (RuleOperator.EXACTLY, 2, 3, False),
        (RuleOperator.AT_LEAST, 2, 3, True),
        (RuleOperator.AT_LEAST, 2, 1, False),
        (RuleOperator.AT_MOST, 2, 1, True),
        (RuleOperator.AT_MOST, 2, 3, False),
        (RuleOperator.EXACTLY, 0, 0, True),
        (RuleOperator.EXACTLY, 0, 1, False),
    ],
)
def test_generalized_rule_operators_use_distinct_meal_evidence(
    operator: RuleOperator,
    count: int,
    matched: int,
    expected_valid: bool,
) -> None:
    """Exact, minimum, maximum, and zero rules count each meal once."""
    meals = {
        day: [
            meal(
                MealType.BREAKFAST,
                "Egg toast" if day <= matched else "Oat toast",
                ["egg"] if day <= matched else ["oat"],
            ),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                count,
                operator=operator,
                meal_type=MealType.BREAKFAST,
            )
        ],
    )

    assert result.is_valid is expected_valid
    assert result.rules[0].actual_count == matched
    assert len(result.rules[0].matched_meals) == matched


def test_rule_weekday_and_meal_scope_filter_eligible_distinct_meals() -> None:
    """Weekday and meal scopes constrain both evidence and capacity."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Egg toast", ["egg"]),
            meal(MealType.LUNCH, "Egg salad", ["egg"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                1,
                meal_type=MealType.LUNCH,
                weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
            )
        ],
    )

    assert result.is_valid
    assert [
        (item.day, item.meal_type) for item in result.rules[0].matched_meals
    ] == [
        (1, MealType.LUNCH),
        (3, MealType.LUNCH),
    ]
    assert result.rules[0].possible_count == 2


def test_weekday_rule_uses_bounded_daily_capacity_for_unscoped_rules() -> None:
    """Accepted unscoped weekday counts fit four supported daily meals."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Egg toast", ["egg"]),
            meal(MealType.LUNCH, "Egg salad", ["egg"]),
            meal(MealType.DINNER, "Egg rice", ["egg"]),
            meal(MealType.SNACK, "Egg snack", ["egg"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                4,
                weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
            )
        ],
    )

    assert result.is_valid
    assert result.rules[0].weekday_counts == (
        (Weekday.MONDAY, 4),
        (Weekday.WEDNESDAY, 4),
    )
    assert not any(
        issue.code == "impossible_rule_count" for issue in result.issues
    )


def test_strict_weekday_rule_requires_evidence_on_every_named_day() -> None:
    """A strict weekday rule reports each named day without evidence."""
    meals = {
        1: [
            meal(MealType.BREAKFAST, "Egg toast", ["egg"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ],
        3: [
            meal(MealType.BREAKFAST, "Oat toast", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ],
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                1,
                operator=RuleOperator.AT_LEAST,
                meal_type=MealType.BREAKFAST,
                weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
            )
        ],
    )

    assert not result.is_valid
    assert result.rules[0].missing_weekdays == (Weekday.WEDNESDAY,)
    assert any(
        issue.code == "strict_rule_mismatch"
        and issue.day == Weekday.WEDNESDAY
        and "Wednesday" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("operator", "count", "matched_days"),
    [
        (RuleOperator.EXACTLY, 1, [1, 3]),
        (RuleOperator.AT_LEAST, 1, [1, 3]),
        (RuleOperator.AT_MOST, 1, [1, 3]),
        (RuleOperator.EXACTLY, 0, []),
    ],
)
def test_weekday_rule_operator_is_applied_per_named_day(
    operator: RuleOperator,
    count: int,
    matched_days: list[int],
) -> None:
    """Each named weekday independently satisfies its count operator."""
    meals = {
        day: [
            meal(
                MealType.BREAKFAST,
                "Egg toast" if day in matched_days else "Oat toast",
                ["egg"] if day in matched_days else ["oats"],
            ),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                count,
                operator=operator,
                meal_type=MealType.BREAKFAST,
                weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
            )
        ],
    )

    assert result.is_valid
    assert result.rules[0].is_satisfied
    assert result.rules[0].missing_weekdays == ()


def test_best_effort_weekday_miss_is_non_blocking_and_reported() -> None:
    """Best-effort weekday omissions remain summarized without invalidation."""
    meals = {
        day: [
            meal(
                MealType.BREAKFAST,
                "Egg toast" if day == 1 else "Oat toast",
                ["egg"] if day == 1 else ["oats"],
            ),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["egg"],
                1,
                operator=RuleOperator.AT_LEAST,
                meal_type=MealType.BREAKFAST,
                weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
                strength=RuleStrength.BEST_EFFORT,
            )
        ],
    )

    assert result.is_valid
    assert result.rules[0].missing_weekdays == (Weekday.WEDNESDAY,)
    assert "not met" in format_best_effort_summary(result)


def test_best_effort_miss_is_reported_but_does_not_invalidate_plan() -> None:
    """Best-effort rules retain typed evidence without triggering failure."""
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Oats", ["oats"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[
            rule(
                ["salmon"],
                1,
                strength=RuleStrength.BEST_EFFORT,
            )
        ],
    )

    assert result.is_valid
    assert not result.rules[0].is_satisfied
    assert result.rules[0].strength is RuleStrength.BEST_EFFORT
    assert result.issues == ()


def test_rule_summaries_are_bounded_and_do_not_include_raw_source_text() -> (
    None
):
    """User summaries expose bounded labels rather than untrusted payloads."""
    long_source = "private source " + ("x" * 480)
    strict_rule = DietaryRule(
        id="strict-long",
        source_text=long_source,
        foods_any_of=["salmon", "trout", "tuna", "cod"],
        count=7,
    )
    best_effort_rule = rule(
        ["sardine"],
        1,
        identifier="best-long",
        strength=RuleStrength.BEST_EFFORT,
    )
    meals = {
        day: [
            meal(MealType.BREAKFAST, "Salmon", ["salmon"]),
            meal(MealType.LUNCH, "Soup", ["beans"]),
            meal(MealType.DINNER, "Rice", ["rice"]),
        ]
        for day in range(1, 8)
    }
    result = validate_generated_plan(
        plan_with(meals),
        rules=[strict_rule, best_effort_rule],
    )

    summary = format_satisfaction_summary(result, [])

    assert len(summary) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert long_source not in summary
    assert "Strict-long" in summary
    best_effort_summary = format_best_effort_summary(result)
    assert len(best_effort_summary) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert "Best-long" in best_effort_summary
    assert long_source not in best_effort_summary


def test_absent_weekday_upper_bound_and_exact_zero_are_feasible() -> None:
    """Zero eligible slots do not make non-positive obligations impossible."""
    monday = date(2026, 8, 17)
    rules = [
        rule(
            ["salmon"],
            2,
            identifier="absent-upper",
            operator=RuleOperator.AT_MOST,
            weekdays=[Weekday.MONDAY],
        ),
        rule(
            ["trout"],
            0,
            identifier="absent-zero",
            weekdays=[Weekday.WEDNESDAY],
        ),
    ]

    result = validate_horizon_feasibility(
        rules,
        start_date=monday + date.resolution,
        end_date=monday + date.resolution,
    )

    assert result.is_feasible
    assert result.rules[0].weekday_capacities == ((Weekday.MONDAY, 0),)
    assert result.rules[1].weekday_capacities == ((Weekday.WEDNESDAY, 0),)
    assert result.clarification is None


@pytest.mark.parametrize(
    "operator", [RuleOperator.EXACTLY, RuleOperator.AT_LEAST]
)
def test_positive_rule_count_cannot_exceed_whole_horizon_capacity(
    operator: RuleOperator,
) -> None:
    """Positive lower obligations fail when the selected horizon is full."""
    rule_to_check = rule(
        ["salmon"],
        2,
        identifier=f"whole-{operator.value}",
        operator=operator,
        meal_type=MealType.BREAKFAST,
    )

    result = validate_horizon_feasibility(
        [rule_to_check],
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 17),
    )

    assert not result.is_feasible
    assert result.rules == (
        RuleHorizonFeasibility(
            rule_id=rule_to_check.id,
            operator=operator,
            requested_count=2,
            eligible_slot_capacity=1,
            weekday_capacities=(),
            infeasible_weekdays=(),
        ),
    )
    assert result.clarification is not None


def test_positive_weekday_rule_is_evaluated_for_each_named_weekday() -> None:
    """A partly covered scope reports only its absent named weekday."""
    rule_to_check = rule(
        ["salmon"],
        1,
        identifier="partial-weekday",
        operator=RuleOperator.AT_LEAST,
        meal_type=MealType.LUNCH,
        weekdays=[Weekday.MONDAY, Weekday.WEDNESDAY],
    )

    result = validate_horizon_feasibility(
        [rule_to_check],
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 18),
    )

    assert not result.is_feasible
    assert result.rules[0].eligible_slot_capacity == 1
    assert result.rules[0].weekday_capacities == (
        (Weekday.MONDAY, 1),
        (Weekday.WEDNESDAY, 0),
    )
    assert result.rules[0].infeasible_weekdays == (Weekday.WEDNESDAY,)


@pytest.mark.parametrize(
    ("horizon_days", "meal_type", "count"),
    [
        (1, MealType.DINNER, 1),
        (3, MealType.DINNER, 3),
        (7, MealType.DINNER, 7),
        (1, None, 4),
        (3, None, 12),
        (7, None, 28),
    ],
)
def test_capacity_matches_horizon_and_meal_scope(
    horizon_days: int,
    meal_type: MealType | None,
    count: int,
) -> None:
    """Capacity is one scoped slot or four unscoped slots per date."""
    result = validate_horizon_feasibility(
        [rule(["salmon"], count, meal_type=meal_type)],
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 17 + horizon_days - 1),
    )

    assert result.is_feasible
    assert result.rules[0].eligible_slot_capacity == count


def test_horizon_clarification_is_bounded_and_excludes_raw_rule_source() -> (
    None
):
    """Horizon feedback is deterministic, bounded, and application-owned."""
    raw_source = "private preference source " + ("x" * 450)
    rules = [
        DietaryRule(
            id=f"impossible-{index}",
            source_text=raw_source,
            foods_any_of=["salmon"],
            operator=RuleOperator.EXACTLY,
            count=2,
            meal_type=MealType.BREAKFAST,
        )
        for index in range(20)
    ]
    result = validate_horizon_feasibility(
        rules,
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 17),
    )

    message = format_horizon_clarification(result)

    assert isinstance(result, HorizonFeasibilityResult)
    assert message == format_horizon_clarification(result)
    assert len(message) <= MAX_REQUIREMENT_OUTPUT_LENGTH
    assert raw_source not in message
    assert "impossible-0" in message
    assert "rules omitted" in message


@pytest.mark.parametrize(
    ("operator", "rule_kwargs", "count", "start_date", "end_date"),
    [
        (
            RuleOperator.EXACTLY,
            {},
            5,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {},
            5,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.EXACTLY,
            {"weekdays": [Weekday.WEDNESDAY]},
            1,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {"weekdays": [Weekday.MONDAY, Weekday.WEDNESDAY]},
            1,
            date(2026, 8, 17),
            date(2026, 8, 18),
        ),
        (
            RuleOperator.EXACTLY,
            {"meal_type": MealType.BREAKFAST},
            2,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {"meal_type": MealType.BREAKFAST},
            2,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
    ],
)
def test_best_effort_exact_rule_does_not_block_short_horizon(
    operator: RuleOperator,
    rule_kwargs: dict[str, object],
    count: int,
    start_date: date,
    end_date: date,
) -> None:
    """Best-effort capacity shortfalls do not block generation."""
    rule_to_check = rule(
        ["eggs"],
        count,
        identifier=f"best-effort-{operator.value}",
        operator=operator,
        strength=RuleStrength.BEST_EFFORT,
        **rule_kwargs,
    )

    result = validate_horizon_feasibility(
        [rule_to_check], start_date=start_date, end_date=end_date
    )

    assert result.is_feasible
    assert result.infeasible_rules == ()
    assert result.advisory_shortfalls == (result.rules[0],)
    assert not result.rules[0].is_feasible
    assert result.rules[0].strength is RuleStrength.BEST_EFFORT
    assert result.clarification is None


@pytest.mark.parametrize(
    ("operator", "rule_kwargs", "count", "start_date", "end_date"),
    [
        (
            RuleOperator.EXACTLY,
            {},
            5,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {},
            5,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.EXACTLY,
            {"weekdays": [Weekday.WEDNESDAY]},
            1,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {"weekdays": [Weekday.MONDAY, Weekday.WEDNESDAY]},
            1,
            date(2026, 8, 17),
            date(2026, 8, 18),
        ),
        (
            RuleOperator.EXACTLY,
            {"meal_type": MealType.BREAKFAST},
            2,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
        (
            RuleOperator.AT_LEAST,
            {"meal_type": MealType.BREAKFAST},
            2,
            date(2026, 8, 17),
            date(2026, 8, 17),
        ),
    ],
)
def test_strict_horizon_shortfall_remains_blocking(
    operator: RuleOperator,
    rule_kwargs: dict[str, object],
    count: int,
    start_date: date,
    end_date: date,
) -> None:
    """Strict positive obligations still require bounded clarification."""
    rule_to_check = rule(
        ["eggs"],
        count,
        identifier=f"strict-{operator.value}",
        operator=operator,
        **rule_kwargs,
    )

    result = validate_horizon_feasibility(
        [rule_to_check], start_date=start_date, end_date=end_date
    )

    assert not result.is_feasible
    assert result.infeasible_rules == (result.rules[0],)
    assert result.advisory_shortfalls == ()
    assert result.rules[0].strength is RuleStrength.STRICT
    assert result.clarification is not None
