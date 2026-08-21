"""Telegram update and callback routing tests."""

import pytest

from meal_planner.models.schemas import MealOutcome, MealType
from meal_planner.router import (
    ProfileCallbackAction,
    RouteType,
    parse_checkin_callback,
    parse_profile_callback,
    route_update,
)


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
        "profile:root",
        "profile:back",
        "profile:done",
        "profile:close",
        "profile:category:family",
        "profile:category:dietary_constraints",
        "profile:category:dietary_preferences",
        "profile:category:goals",
        "profile:operation:family:add",
        "profile:operation:family:remove",
        "profile:operation:family:change_calories",
        "profile:operation:dietary_constraints:add",
        "profile:operation:dietary_constraints:remove",
        "profile:operation:dietary_preferences:add",
        "profile:operation:dietary_preferences:remove",
        "profile:operation:goals:add",
        "profile:operation:goals:remove",
    ],
)
def test_parse_every_accepted_profile_callback(payload: str) -> None:
    """Accept only the documented profile navigation and operation actions."""
    callback = parse_profile_callback(payload)

    assert callback is not None
    assert callback.action in {
        ProfileCallbackAction.ROOT,
        ProfileCallbackAction.BACK,
        ProfileCallbackAction.DONE,
        ProfileCallbackAction.CLOSE,
        ProfileCallbackAction.CATEGORY,
        ProfileCallbackAction.OPERATION,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "profile",
        "profile:",
        "profile:unknown",
        "profile:root:extra",
        "profile:category",
        "profile:category:unknown",
        "profile:category:family:extra",
        "profile:operation",
        "profile:operation:family",
        "profile:operation:family:unknown",
        "profile:operation:unknown:add",
        "profile:operation:dietary_constraints:change_calories",
        "profile:operation:family:add:extra",
        "checkin:2026-08-10:1:lunch:cooked",
        "profile:category:family:with:arbitrary:data",
    ],
)
def test_parse_profile_callback_rejects_malformed_or_wrong_operations(
    payload: str,
) -> None:
    """Reject malformed payloads and operations for unrelated categories."""
    assert parse_profile_callback(payload) is None


def test_profile_callback_parser_enforces_telegram_byte_limit() -> None:
    """Reject callback data above Telegram's 64-byte UTF-8 limit."""
    oversized = "profile:category:" + "é" * 25

    assert len(oversized.encode("utf-8")) > 64
    assert parse_profile_callback(oversized) is None


def test_profile_callbacks_remain_within_telegram_byte_limit() -> None:
    """The longest documented profile operation fits the wire limit."""
    payload = "profile:operation:dietary_preferences:remove"

    assert len(payload.encode("utf-8")) <= 64
    assert parse_profile_callback(payload) is not None


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
