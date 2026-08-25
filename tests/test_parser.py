"""Tests for LLM response parser functions."""

import json
from typing import Any

import pytest

from meal_planner.llm.parser import (
    parse_conversational_response,
    parse_grocery_response,
    parse_plan_response,
    parse_plan_response_with_metadata,
    parse_preference_interpretation,
)
from meal_planner.models.schemas import (
    ConstraintEntry,
    ConversationIntent,
    DietaryRule,
    MealType,
    RuleOperator,
    RuleStrength,
    Weekday,
)


def _preference_requirements(count: int) -> list[dict[str, Any]]:
    """Return individually valid, non-conflicting interpretation rules."""
    return [
        {
            "id": f"requirement-{index}",
            "source_text": f"food {index} once",
            "foods_any_of": [f"food-{index}"],
            "meal_type": None,
            "exact_count": 1,
        }
        for index in range(count)
    ]


def make_plan_data() -> dict[str, Any]:
    """Return a complete valid seven-day LLM plan payload."""
    return {
        "week_start_date": "2026-08-10",
        "status": "draft",
        "days": [
            {
                "day": day,
                "meals": [
                    {
                        "meal_type": "breakfast",
                        "name": f"Oatmeal {day}",
                        "ingredients": [{"item": "Oats", "amount": "100g"}],
                        "est_calories": 300,
                        "outcome": "unreported",
                    }
                ],
            }
            for day in range(1, 8)
        ],
        "grocery_list": [],
    }


def test_parse_conversational_response_valid() -> None:
    """Test parse_conversational_response with well-formed LLM output."""
    raw_text = (
        "Logged your chicken salad lunch for today!\n\n"
        "```json\n"
        "{\n"
        '  "intent": "log_meal",\n'
        '  "entities": {"meal_type": "lunch", "description": "chicken salad"}\n'
        "}\n"
        "```"
    )
    reply, meta = parse_conversational_response(raw_text)
    assert reply == "Logged your chicken salad lunch for today!"
    assert meta.intent == ConversationIntent.LOG_MEAL
    assert meta.entities["meal_type"] == "lunch"


def test_parse_conversational_response_no_json() -> None:
    """Test parse_conversational_response when no JSON block is present."""
    raw_text = "That sounds great! I'm here to help."
    reply, meta = parse_conversational_response(raw_text)
    assert reply == "That sounds great! I'm here to help."
    assert meta.intent == ConversationIntent.CHITCHAT
    assert meta.entities == {}


def test_parse_conversational_response_malformed_json() -> None:
    """Test parse_conversational_response with malformed JSON block."""
    raw_text = "Here is your info:\n```json\n{invalid json content}\n```"
    reply, meta = parse_conversational_response(raw_text)
    assert "Here is your info:" in reply
    assert meta.intent == ConversationIntent.CHITCHAT


def test_parse_conversational_response_empty() -> None:
    """Test parse_conversational_response with empty input."""
    reply, meta = parse_conversational_response("   ")
    assert reply == ""
    assert meta.intent == ConversationIntent.CHITCHAT


def test_parse_plan_response_dict() -> None:
    """Test parse_plan_response with valid dict input."""
    data = make_plan_data()
    plan = parse_plan_response(data)
    assert plan is not None
    assert plan.week_start_date == "2026-08-10"
    assert plan.days[0].meals[0].name == "Oatmeal 1"


def test_parse_plan_response_json_string() -> None:
    """Test parse_plan_response with raw markdown JSON string."""
    data = make_plan_data()
    raw_text = f"Here is your plan:\n```json\n{json.dumps(data)}\n```"
    plan = parse_plan_response(raw_text)
    assert plan is not None
    assert plan.week_start_date == "2026-08-10"
    assert plan.days[0].meals[0].name == "Oatmeal 1"


def test_parse_plan_response_invalid() -> None:
    """Test parse_plan_response returning None on invalid inputs."""
    assert parse_plan_response("") is None
    assert parse_plan_response("No plan here") is None
    assert parse_plan_response({"invalid": "schema"}) is None


def test_parse_plan_response_rejects_incomplete_and_duplicate_days() -> None:
    """Plan parser enforces the complete-week domain invariant."""
    partial = make_plan_data()
    partial["days"] = partial["days"][:-1]
    assert parse_plan_response(partial) is None

    duplicate = make_plan_data()
    duplicate["days"] = [{"day": 1, "meals": []} for _ in range(7)]
    assert parse_plan_response(duplicate) is None


def test_parse_plan_response_rejects_duplicate_meal_types_in_a_day() -> None:
    data = make_plan_data()
    data["days"][0]["meals"].append(
        {
            "meal_type": "breakfast",
            "name": "Second breakfast",
            "ingredients": [],
            "est_calories": 200,
            "outcome": "unreported",
        }
    )

    assert parse_plan_response(data) is None


def test_plan_response_metadata_has_safe_codes_and_locations() -> None:
    """Repair feedback does not include provider values or raw response text."""
    data = make_plan_data()
    data["days"][0]["meals"][0].pop("name")
    data["days"][0]["meals"][0]["ingredients"] = [{"item": "secret ingredient"}]

    plan, feedback = parse_plan_response_with_metadata(data)

    assert plan is None
    assert feedback is not None
    assert feedback.category == "structural"
    assert feedback.render()
    assert "code=missing location=days[0].meals[0].name" in feedback.render()
    assert "secret ingredient" not in feedback.render()
    assert "name" in feedback.render()


def test_parse_plan_response_metadata_classifies_format_failures_safely() -> (
    None
):
    """Malformed JSON becomes a bounded structural repair outcome."""
    plan, feedback = parse_plan_response_with_metadata(
        '{"secret_preference": "eggs",'
    )

    assert plan is None
    assert feedback is not None
    assert feedback.category == "structural"
    assert feedback.render() == "code=invalid_json location=$"
    assert "secret_preference" not in feedback.render()


def test_parse_preference_interpretation_complete_requirements() -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "crepes or pancakes once at breakfast",
                "foods_any_of": ["crepes", "pancakes"],
                "meal_type": "breakfast",
                "exact_count": 1,
            },
            {
                "id": "r2",
                "source_text": "eggs three times",
                "foods_any_of": ["eggs"],
                "meal_type": None,
                "exact_count": 3,
            },
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert [item.id for item in requirements] == ["r1", "r2"]
    assert requirements[0].meal_type is MealType.BREAKFAST
    assert requirements[0].foods_any_of == ["crepes", "pancakes"]


def test_parse_interpretation_supports_shared_rule_operators_and_scopes() -> (
    None
):
    """The extended wire format becomes a shared DietaryRule contract."""
    data = {
        "mode": "current_plan_preference",
        "requirements": [
            {
                "id": "r1",
                "source_text": (
                    "eggs at least once for breakfast on Monday and Wednesday"
                ),
                "foods_any_of": ["eggs"],
                "meal_type": "breakfast",
                "weekdays": ["monday", "wednesday"],
                "operator": "at_least",
                "count": 1,
                "strength": "strict",
            },
            {
                "id": "r2",
                "source_text": "fish at most once on Wednesday if convenient",
                "foods_any_of": ["fish"],
                "weekdays": [3],
                "operator": "at_most",
                "count": 1,
                "strength": "strict",
            },
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert all(isinstance(rule, DietaryRule) for rule in requirements)
    assert requirements[0].operator is RuleOperator.AT_LEAST
    assert requirements[0].weekdays == [Weekday.MONDAY, Weekday.WEDNESDAY]
    assert requirements[1].operator is RuleOperator.AT_MOST
    assert requirements[1].weekdays == [Weekday.WEDNESDAY]
    assert requirements[1].strength is RuleStrength.BEST_EFFORT


@pytest.mark.parametrize("operator", ["exactly", "at_least"])
def test_parse_interpretation_rejects_impossible_strict_named_day_count(
    operator: str,
) -> None:
    """Schema failures become bounded clarification before dispatch."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": (
                    "eggs twice for breakfast on Monday and Wednesday"
                ),
                "foods_any_of": ["eggs"],
                "meal_type": "breakfast",
                "weekdays": ["monday", "wednesday"],
                "operator": operator,
                "count": 2,
                "strength": "strict",
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "malformed" in clarification.lower()


def test_parse_interpretation_turns_id_like_into_strict_minimum_one() -> None:
    """A bare positive request is never silently reduced to guidance."""
    data = {
        "mode": "stored_preference",
        "requirements": [
            {
                "id": "r1",
                "source_text": "I'd like eggs for breakfast",
                "foods_any_of": ["eggs"],
                "meal_type": "breakfast",
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert requirements[0].operator is RuleOperator.AT_LEAST
    assert requirements[0].count == 1
    assert requirements[0].strength is RuleStrength.STRICT


@pytest.mark.parametrize(
    ("source_text", "operator", "count", "expected_operator"),
    [
        (
            "I'd like eggs exactly twice if convenient",
            "exactly",
            2,
            RuleOperator.EXACTLY,
        ),
        (
            "please include beans at most three times",
            "at_most",
            3,
            RuleOperator.AT_MOST,
        ),
    ],
)
def test_positive_wording_preserves_explicit_operator_and_count(
    source_text: str,
    operator: str,
    count: int,
    expected_operator: RuleOperator,
) -> None:
    """Positive wording cannot replace an explicit count contract."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": source_text,
                "foods_any_of": ["eggs" if "eggs" in source_text else "beans"],
                "operator": operator,
                "count": count,
                "strength": "strict",
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert requirements[0].operator is expected_operator
    assert requirements[0].count == count
    expected_strength = (
        RuleStrength.BEST_EFFORT
        if "convenient" in source_text
        else RuleStrength.STRICT
    )
    assert requirements[0].strength is expected_strength


def test_positive_wording_without_count_defaults_to_strict_minimum_one() -> (
    None
):
    """Only an unqualified positive request gets the default minimum."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "please include lentils for dinner",
                "foods_any_of": ["lentils"],
                "meal_type": "dinner",
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert requirements[0].operator is RuleOperator.AT_LEAST
    assert requirements[0].count == 1
    assert requirements[0].strength is RuleStrength.STRICT


def test_explicit_flexibility_overrides_generic_positive_wording() -> None:
    """An omission qualifier makes a positive request best effort."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "I'd like tofu if possible",
                "foods_any_of": ["tofu"],
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert requirements[0].operator is RuleOperator.AT_LEAST
    assert requirements[0].count == 1
    assert requirements[0].strength is RuleStrength.BEST_EFFORT


@pytest.mark.parametrize(
    "source_text",
    [
        "I'd like eggs exactly twice if convenient but strictly required",
        "please include beans at least two times or at most three times",
    ],
)
def test_contradictory_or_ambiguous_strength_wording_clarifies(
    source_text: str,
) -> None:
    """Contradictory strength or operator wording fails closed."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": source_text,
                "foods_any_of": ["eggs" if "eggs" in source_text else "beans"],
                "operator": "exactly",
                "count": 2,
                "strength": "strict",
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None


@pytest.mark.parametrize(
    ("operator", "count"),
    [("exactly", "twice"), ("at least or at most", 2)],
)
def test_malformed_counts_and_ambiguous_operators_clarify(
    operator: str,
    count: object,
) -> None:
    """Malformed structured count semantics remain bounded failures."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "eggs with an unclear count",
                "foods_any_of": ["eggs"],
                "operator": operator,
                "count": count,
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None


def test_parse_constraint_mode_rejects_bad_terms() -> None:
    """Constraint mode uses ConstraintEntry and fails closed on bad terms."""
    data = {
        "mode": "constraint",
        "requirements": [],
        "exclusions": [
            {
                "id": "c1",
                "source_text": "no dairy",
                "forbidden_terms": ["dairy"],
            }
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    exclusions, clarification = parse_preference_interpretation(
        data, mode="constraint"
    )

    assert clarification is None
    assert isinstance(exclusions[0], ConstraintEntry)
    assert exclusions[0].forbidden_terms == ["dairies"]

    malformed = {
        **data,
        "exclusions": [
            {"id": "c1", "source_text": "no ???", "forbidden_terms": ["???"]}
        ],
    }
    parsed, clarification = parse_preference_interpretation(
        malformed, mode="constraint"
    )
    assert parsed == []
    assert clarification is not None


@pytest.mark.parametrize(
    "data",
    [
        {
            "mode": "stored_preference",
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs exactly twice",
                    "foods_any_of": ["eggs"],
                    "operator": "exactly",
                    "count": 2,
                },
                {
                    "id": "r2",
                    "source_text": "eggs at most once",
                    "foods_any_of": ["eggs"],
                    "operator": "at_most",
                    "count": 1,
                },
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        },
        {
            "mode": "stored_preference",
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs twice",
                    "foods_any_of": ["eggs"],
                    "operator": "exactly",
                    "count": 2,
                }
            ],
            "exclusions": [],
            "clarification": "Which days?",
            "unparsed_text": ["and tofu"],
        },
    ],
)
def test_parse_interpretation_rejects_contradictions_and_partial_results(
    data: dict[str, Any],
) -> None:
    """Ambiguous or contradictory responses never return partial rules."""
    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert len(clarification) <= 500


def test_parse_interpretation_rejects_wrong_mode_and_malformed_json() -> None:
    """Mode mismatches and malformed output get bounded clarifications."""
    wrong_mode = {
        "mode": "constraint",
        "requirements": [
            {
                "id": "r1",
                "source_text": "eggs twice",
                "foods_any_of": ["eggs"],
                "operator": "exactly",
                "count": 2,
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }
    requirements, clarification = parse_preference_interpretation(wrong_mode)
    assert requirements == []
    assert clarification is not None

    requirements, clarification = parse_preference_interpretation(
        "```json\n{not valid}\n```"
    )
    assert requirements == []
    assert clarification is not None
    assert len(clarification) <= 500


@pytest.mark.parametrize("count", [20, 21])
def test_parse_preference_interpretation_requirement_count_boundary(
    count: int,
) -> None:
    """The parser enforces the durable requirement-count contract."""
    data = {
        "requirements": _preference_requirements(count),
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    if count == 20:
        assert len(requirements) == 20
        assert clarification is None
    else:
        assert requirements == []
        assert clarification is not None
        assert "combine" in clarification.lower()
        assert "prioritize" in clarification.lower()
        assert "500" not in clarification


def test_parse_preference_interpretation_rejects_vacuous_success() -> None:
    data = {
        "requirements": [],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification == "Please provide a measurable meal preference."


def test_parse_preference_interpretation_accepts_json_text() -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "salmon once for dinner",
                "foods_any_of": ["salmon"],
                "meal_type": "dinner",
                "exact_count": 1,
            }
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(
        f"```json\n{json.dumps(data)}\n```"
    )

    assert clarification is None
    assert len(requirements) == 1


def test_parse_preference_interpretation_returns_clarification() -> None:
    data = {
        "requirements": [],
        "clarification": "How many times should the meal include eggs?",
        "unparsed_text": ["some egg breakfasts"],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification == "How many times should the meal include eggs?"


@pytest.mark.parametrize("length", [499, 500, 501])
def test_preference_clarification_provider_length_boundary(length: int) -> None:
    """Provider clarification text has a hard final-render boundary."""
    data = {
        "requirements": [],
        "clarification": "x" * length,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert len(clarification) <= 500
    if length <= 500:
        assert clarification == "x" * length
    else:
        assert "rephrase" in clarification.lower()


@pytest.mark.parametrize("clause_count", [8, 9])
def test_preference_unparsed_clause_count_boundary(
    clause_count: int,
) -> None:
    """The parser bounds the number of provider clauses it renders."""
    data = {
        "requirements": [],
        "clarification": None,
        "unparsed_text": [f"clause {index}" for index in range(clause_count)],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert len(clarification) <= 500
    if clause_count == 8:
        assert "clause 7" in clarification
    else:
        assert "rephrase" in clarification.lower()


@pytest.mark.parametrize("clause_length", [160, 161])
def test_preference_unparsed_clause_length_boundary(
    clause_length: int,
) -> None:
    """Each provider clause is bounded before it is rendered."""
    data = {
        "requirements": [],
        "clarification": None,
        "unparsed_text": ["x" * clause_length],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert len(clarification) <= 500
    if clause_length == 160:
        assert clarification.endswith("x" * clause_length)
    else:
        assert "rephrase" in clarification.lower()


def test_preference_unparsed_render_length_boundary() -> None:
    """Individually valid clauses still cannot overflow the final cap."""
    prefix = "Please clarify these preference clauses: "
    clauses = ["a" * 113, "b" * 113, "c" * 113, "d" * 114]
    assert len(prefix + "; ".join(clauses)) == 500
    data = {
        "requirements": [],
        "clarification": None,
        "unparsed_text": clauses,
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification == prefix + "; ".join(clauses)

    data["unparsed_text"] = [*clauses[:-1], clauses[-1] + "x"]
    _, overflow_clarification = parse_preference_interpretation(data)
    assert overflow_clarification is not None
    assert len(overflow_clarification) <= 500
    assert "rephrase" in overflow_clarification.lower()


def test_preference_unparsed_text_needs_clarification() -> None:
    data = {
        "requirements": [],
        "clarification": None,
        "unparsed_text": ["make the plan cozy"],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "make the plan cozy" in clarification


def test_parse_preference_interpretation_rejects_malformed_object() -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "eggs twice",
                "foods_any_of": [],
                "meal_type": None,
                "exact_count": 2,
            }
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "requirements" in clarification


@pytest.mark.parametrize("food", ["---", "!!! ???", "$$$", "🍕🍔"])
def test_parse_preference_interpretation_rejects_normalized_empty_food(
    food: str,
) -> None:
    """Turn impossible normalized-empty requirements into a clarification."""
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": f"include {food}",
                "foods_any_of": [food],
                "meal_type": None,
                "exact_count": 1,
            }
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification == "One or more preference requirements are malformed."


def test_parse_preference_interpretation_rejects_duplicate_ids() -> None:
    requirement = {
        "id": "r1",
        "source_text": "eggs twice",
        "foods_any_of": ["eggs"],
        "meal_type": None,
        "exact_count": 2,
    }
    data = {
        "requirements": [requirement, {**requirement, "source_text": "eggs"}],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "duplicate" in clarification


@pytest.mark.parametrize(
    ("left_foods", "right_foods"),
    [
        (["eggs"], ["eggs"]),
        (["pancakes", "crepes"], ["crepes", "pancakes"]),
        (["egg"], ["eggs"]),
        (["cookie"], ["cookies"]),
        (["brownie"], ["brownies"]),
        (["smoothie"], ["smoothies"]),
        (["pie"], ["pies"]),
        (["berry"], ["berries"]),
        (["red-pepper"], ["red pepper"]),
    ],
)
def test_parse_preference_interpretation_rejects_direct_count_conflicts(
    left_foods: list[str], right_foods: list[str]
) -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "first requirement",
                "foods_any_of": left_foods,
                "meal_type": "dinner",
                "exact_count": 1,
            },
            {
                "id": "r2",
                "source_text": "second requirement",
                "foods_any_of": right_foods,
                "meal_type": "dinner",
                "exact_count": 2,
            },
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "conflict" in clarification.lower()


@pytest.mark.parametrize(
    (
        "left_foods",
        "right_foods",
        "left_scope",
        "right_scope",
        "left_count",
        "right_count",
    ),
    [
        (["eggs"], ["eggs"], "breakfast", "breakfast", 1, 1),
        (["eggs"], ["eggs"], "breakfast", "dinner", 1, 2),
        (["eggs"], ["tofu"], "dinner", "dinner", 1, 2),
    ],
)
def test_parse_preference_interpretation_accepts_compatible_rules(
    left_foods: list[str],
    right_foods: list[str],
    left_scope: str,
    right_scope: str,
    left_count: int,
    right_count: int,
) -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "first requirement",
                "foods_any_of": left_foods,
                "meal_type": left_scope,
                "exact_count": left_count,
            },
            {
                "id": "r2",
                "source_text": "second requirement",
                "foods_any_of": right_foods,
                "meal_type": right_scope,
                "exact_count": right_count,
            },
        ],
        "clarification": None,
        "unparsed_text": [],
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert clarification is None
    assert [requirement.id for requirement in requirements] == ["r1", "r2"]


def test_parse_preference_interpretation_rejects_omitted_clauses() -> None:
    data = {
        "requirements": [
            {
                "id": "r1",
                "source_text": "eggs twice",
                "foods_any_of": ["eggs"],
                "meal_type": None,
                "exact_count": 2,
            }
        ],
        "clarification": None,
    }

    requirements, clarification = parse_preference_interpretation(data)

    assert requirements == []
    assert clarification is not None
    assert "unparsed_text" in clarification


def test_parse_grocery_response_dict() -> None:
    """Test parse_grocery_response with valid dict input."""
    data = {
        "sections": [
            {"name": "Produce", "items": ["Apples", "Spinach"]},
            {"name": "Dairy", "items": ["Milk"]},
        ]
    }
    sections = parse_grocery_response(data)
    assert len(sections) == 2
    assert sections[0].name == "Produce"
    assert sections[0].items == ["Apples", "Spinach"]


def test_parse_grocery_response_json_string() -> None:
    """Test parse_grocery_response with markdown JSON string."""
    raw_text = (
        "```json\n"
        "{\n"
        '  "sections": [{"name": "Pantry", "items": ["Rice", "Beans"]}]\n'
        "}\n"
        "```"
    )
    sections = parse_grocery_response(raw_text)
    assert len(sections) == 1
    assert sections[0].name == "Pantry"
    assert sections[0].items == ["Rice", "Beans"]


def test_parse_grocery_response_invalid() -> None:
    """Test parse_grocery_response returning empty list on invalid input."""
    assert parse_grocery_response("") == []
    assert parse_grocery_response("Not JSON") == []
