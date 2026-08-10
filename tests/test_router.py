"""Unit tests for Telegram message router."""

from meal_planner.router import RouteType, route_update


def test_route_start_command() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 123456, "first_name": "Alice"},
            "chat": {"id": 123456, "type": "private"},
            "text": "/start",
        },
    }
    res = route_update(update)
    assert res.route_type == RouteType.COMMAND
    assert res.command == "start"
    assert res.args == ""
    assert res.user_id == "123456"
    assert res.chat_id == 123456


def test_route_commands_with_args_and_bot_name() -> None:
    commands = ["start", "profile", "plan", "grocery", "today", "submit_meals"]
    for cmd in commands:
        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 999},
                "chat": {"id": 888},
                "text": f"/{cmd}@my_meal_bot extra details",
            },
        }
        res = route_update(update)
        assert res.route_type == RouteType.COMMAND
        assert res.command == cmd
        assert res.args == "extra details"
        assert res.user_id == "999"
        assert res.chat_id == 888


def test_route_conversational_text() -> None:
    update = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "from": {"id": 123},
            "chat": {"id": 123},
            "text": "Can you swap Thursday dinner for tacos?",
        },
    }
    res = route_update(update)
    assert res.route_type == RouteType.CONVERSATIONAL
    assert res.text == "Can you swap Thursday dinner for tacos?"
    assert res.user_id == "123"
    assert res.chat_id == 123


def test_route_callback_query() -> None:
    update = {
        "update_id": 3,
        "callback_query": {
            "id": "cb123",
            "from": {"id": 456},
            "message": {"chat": {"id": 789}},
            "data": "checkin:1:lunch:cooked",
        },
    }
    res = route_update(update)
    assert res.route_type == RouteType.CALLBACK
    assert res.callback_data == "checkin:1:lunch:cooked"
    assert res.callback_query_id == "cb123"
    assert res.user_id == "456"
    assert res.chat_id == 789


def test_route_malformed_and_unknown() -> None:
    assert route_update({}).route_type == RouteType.UNKNOWN
    assert route_update("invalid").route_type == RouteType.UNKNOWN

    # Message without text (e.g. photo)
    update_photo = {
        "update_id": 4,
        "message": {
            "from": {"id": 100},
            "chat": {"id": 100},
            "photo": [],
        },
    }
    res = route_update(update_photo)
    assert res.route_type == RouteType.UNKNOWN
    assert res.user_id == "100"
    assert res.chat_id == 100
