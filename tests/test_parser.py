"""Tests for LLM response parser functions."""

from meal_planner.llm.parser import (
    parse_conversational_response,
    parse_grocery_response,
    parse_plan_response,
)
from meal_planner.models.schemas import ConversationIntent


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
    data = {
        "week_start_date": "2026-08-10",
        "status": "draft",
        "days": [
            {
                "day": 1,
                "meals": [
                    {
                        "meal_type": "breakfast",
                        "name": "Oatmeal",
                        "ingredients": [{"item": "Oats", "amount": "100g"}],
                        "est_calories": 300,
                        "was_cooked": False,
                    }
                ],
            }
        ],
        "grocery_list": [],
    }
    plan = parse_plan_response(data)
    assert plan is not None
    assert plan.week_start_date == "2026-08-10"
    assert plan.days[0].meals[0].name == "Oatmeal"


def test_parse_plan_response_json_string() -> None:
    """Test parse_plan_response with raw markdown JSON string."""
    raw_text = (
        "Here is your 7-day meal plan:\n"
        "```json\n"
        "{\n"
        '  "week_start_date": "2026-08-10",\n'
        '  "status": "draft",\n'
        '  "days": [\n'
        '    {"day": 1, "meals": [{"meal_type": "dinner", "name": "Tacos"}]}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    plan = parse_plan_response(raw_text)
    assert plan is not None
    assert plan.week_start_date == "2026-08-10"
    assert plan.days[0].meals[0].name == "Tacos"


def test_parse_plan_response_invalid() -> None:
    """Test parse_plan_response returning None on invalid inputs."""
    assert parse_plan_response("") is None
    assert parse_plan_response("No plan here") is None
    assert parse_plan_response({"invalid": "schema"}) is None


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
