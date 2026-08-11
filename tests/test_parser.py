"""Tests for LLM response parser functions."""

import json
from typing import Any

from meal_planner.llm.parser import (
    parse_conversational_response,
    parse_grocery_response,
    parse_plan_response,
)
from meal_planner.models.schemas import ConversationIntent


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
