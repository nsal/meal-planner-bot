"""Telegram update and callback routing tests."""

import pytest

from meal_planner.models.schemas import MealOutcome, MealType
from meal_planner.router import RouteType, parse_checkin_callback, route_update


def test_route_command_and_conversation() -> None:
    command = route_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 2},
                "text": "/plan@bot next week",
            }
        }
    )
    assert command.route_type is RouteType.COMMAND
    assert command.chat_type is None
    assert command.command == "plan"
    assert command.args == "next week"
    conversation = route_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 2},
                "text": "swap lunch",
            }
        }
    )
    assert conversation.route_type is RouteType.CONVERSATIONAL


def test_route_callback_preserves_query_id() -> None:
    routed = route_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 1},
                "message": {"chat": {"id": 2}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        }
    )
    assert routed.route_type is RouteType.CALLBACK
    assert routed.callback_query_id == "callback-1"
    assert routed.chat_type is None


@pytest.mark.parametrize("chat_type", ["private", "group", "supergroup"])
def test_route_message_preserves_chat_type(chat_type: str) -> None:
    """Message routes retain Telegram's source chat type."""
    routed = route_update(
        {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": chat_type},
                "text": "/start",
            }
        }
    )

    assert routed.chat_type == chat_type


def test_route_callback_preserves_private_chat_type() -> None:
    """Callback routes retain the callback message's chat type."""
    routed = route_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        }
    )

    assert routed.chat_type == "private"


@pytest.mark.parametrize(
    "update",
    [
        {
            "message": {
                "from": {"id": 123},
                "chat": {"type": "private"},
                "text": "/start",
            }
        },
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {"chat": {"type": "private"}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        },
    ],
)
def test_route_missing_chat_id_remains_missing(
    update: dict[str, object],
) -> None:
    """Do not synthesize a private chat ID from the sender ID."""
    assert route_update(update).chat_id is None


@pytest.mark.parametrize(
    "update",
    [
        {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": 42},
                "text": "/start",
            }
        },
        {
            "callback_query": {
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": None}},
                "data": "callback",
            }
        },
        {
            "callback_query": {
                "from": {"id": 123},
                "message": "malformed",
                "data": "callback",
            }
        },
    ],
)
def test_route_malformed_chat_type_fails_open_to_no_type(
    update: dict[str, object],
) -> None:
    """Malformed or absent Telegram chat context is represented as missing."""
    assert route_update(update).chat_type is None


def test_parse_plan_specific_checkin_callback() -> None:
    callback = parse_checkin_callback("checkin:2026-08-10:7:dinner:swapped")
    assert callback is not None
    assert callback.week_start == "2026-08-10"
    assert callback.day == 7
    assert callback.meal_type is MealType.DINNER
    assert callback.outcome is MealOutcome.SWAPPED


@pytest.mark.parametrize(
    "payload",
    [
        "checkin:1:lunch:cooked",
        "checkin:not-a-date:1:lunch:cooked",
        "checkin:2026-08-10:0:lunch:cooked",
        "checkin:2026-08-10:1:brunch:cooked",
        "checkin:2026-08-10:1:lunch:liked",
        "x" * 65,
    ],
)
def test_reject_malformed_or_old_callbacks(payload: str) -> None:
    assert parse_checkin_callback(payload) is None


def test_route_unknown_updates() -> None:
    assert route_update({}).route_type is RouteType.UNKNOWN
    assert route_update("bad").route_type is RouteType.UNKNOWN
