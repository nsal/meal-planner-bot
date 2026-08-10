"""Unit tests for Telegram API helper."""

import json
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

from meal_planner.models.schemas import (
    GrocerySection,
    Ingredient,
    PlanDay,
    PlannedMeal,
    WeeklyPlan,
)
from meal_planner.telegram.api import TelegramAPI, split_text


def make_mock_resp(data: dict[str, Any]) -> BytesIO:
    """Helper to return fresh BytesIO for urlopen mock."""
    return BytesIO(json.dumps(data).encode("utf-8"))


def test_split_text_short() -> None:
    text = "Hello world"
    chunks = split_text(text, max_length=50)
    assert chunks == ["Hello world"]


def test_split_text_long_multiline() -> None:
    lines = [f"Line {i}" for i in range(100)]
    text = "\n".join(lines)
    chunks = split_text(text, max_length=100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100
    assert "\n".join(chunks) == text


def test_split_text_single_long_line() -> None:
    long_line = "a" * 150
    chunks = split_text(long_line, max_length=50)
    assert len(chunks) == 3
    assert chunks == ["a" * 50, "a" * 50, "a" * 50]


def test_send_message_success(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp(
            {"ok": True, "result": {"message_id": 123}}
        ),
    )

    res = api.send_message(12345, "Hello")

    assert len(res) == 1
    assert res[0]["ok"] is True
    assert mock_urlopen.call_count == 1

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.telegram.org/botdummy_token/sendMessage"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["chat_id"] == 12345
    assert payload["text"] == "Hello"


def test_send_message_http_error(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    err_body = BytesIO(
        json.dumps({"ok": False, "description": "Bad Request"}).encode("utf-8")
    )
    http_err = HTTPError("url", 400, "Bad Request", {}, err_body)
    mocker.patch("urllib.request.urlopen", side_effect=http_err)

    res = api.send_message(12345, "Error test")
    assert len(res) == 1
    assert res[0]["ok"] is False
    assert res[0]["description"] == "Bad Request"


def test_send_message_url_error(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    url_err = URLError("Network unreachable")
    mocker.patch("urllib.request.urlopen", side_effect=url_err)

    res = api.send_message(12345, "Error test")
    assert len(res) == 1
    assert res[0]["ok"] is False
    assert "Network unreachable" in res[0]["description"]


def test_send_message_splitting(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    long_text = "x" * 5000
    res = api.send_message(
        12345, long_text, reply_markup={"inline_keyboard": []}
    )
    assert len(res) == 2


def test_send_plan(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    plan = WeeklyPlan(
        week_start_date="2026-08-10",
        status="draft",
        days=[
            PlanDay(
                day=1,
                meals=[
                    PlannedMeal(
                        meal_type="breakfast",
                        name="Eggs",
                        ingredients=[Ingredient(item="Egg", amount="2")],
                        est_calories=300,
                        was_cooked=True,
                    )
                ],
            )
        ],
    )
    res = api.send_plan(12345, plan)
    assert len(res) == 1

    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "Weekly Meal Plan" in payload["text"]
    assert "Eggs" in payload["text"]
    assert "[Cooked]" in payload["text"]


def test_send_grocery_list_empty(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    res = api.send_grocery_list(12345, [])
    assert len(res) == 1
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "empty" in payload["text"]


def test_send_grocery_list(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    sections = [
        GrocerySection(name="Produce", items=["Apples", "Bananas"]),
        GrocerySection(name="Dairy", items=["Milk"]),
    ]
    res = api.send_grocery_list(12345, sections)
    assert len(res) == 1
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "Produce" in payload["text"]
    assert "Apples" in payload["text"]


def test_send_meal_checkin_empty(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    res = api.send_meal_checkin(12345, [])
    assert len(res) == 1
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "No meals planned" in payload["text"]


def test_send_meal_checkin(mocker: Any) -> None:
    api = TelegramAPI("dummy_token")
    mock_urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda req: make_mock_resp({"ok": True}),
    )

    meals = [
        PlannedMeal(
            meal_type="lunch",
            name="Chicken Salad",
            ingredients=[],
            est_calories=500,
        )
    ]
    res = api.send_meal_checkin(12345, meals, day=2)
    assert len(res) == 1
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "Daily Meal Check-in" in payload["text"]
    assert "reply_markup" in payload
    kb = payload["reply_markup"]["inline_keyboard"]
    assert len(kb) == 1
    assert kb[0][0]["callback_data"] == "checkin:2:lunch:cooked"
