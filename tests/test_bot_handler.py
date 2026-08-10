"""Unit tests for Bot Lambda handler and command processing."""

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from meal_planner.bot_handler import BotHandler, lambda_handler
from meal_planner.models.schemas import (
    FamilyMember,
    GrocerySection,
    PlanDay,
    PlannedMeal,
    UserProfile,
    WeeklyPlan,
)


def make_update(
    text: str | None = None,
    user_id: int = 12345,
    chat_id: int = 12345,
    callback_data: str | None = None,
) -> dict[str, Any]:
    """Helper to generate mock Telegram update dict."""
    if callback_data:
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cb_1",
                "from": {"id": user_id},
                "message": {"chat": {"id": chat_id}},
                "data": callback_data,
            },
        }
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_cmd_start_new_user(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = None
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)
    update = make_update("/start")
    res = handler.handle_update(update)

    assert res == {"statusCode": 200, "body": "ok"}
    mock_api.send_message.assert_called_once()
    assert (
        "Welcome to Meal Planner Bot" in mock_api.send_message.call_args[0][1]
    )


def test_cmd_start_existing_user(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)
    update = make_update("/start")
    handler.handle_update(update)

    assert "Welcome back, Alice" in mock_api.send_message.call_args[0][1]


def test_cmd_profile(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    # Missing profile scenario
    mock_repo.get_profile.return_value = None
    handler.handle_update(make_update("/profile"))
    assert "No profile found" in mock_api.send_message.call_args[0][1]

    # Existing profile scenario
    mock_api.reset_mock()
    mock_repo.get_profile.return_value = UserProfile(
        name="Alice",
        people_count=2,
        family_members=[FamilyMember(name="Bob", calorie_target=2000)],
        allergies=["Peanuts"],
        dietary_preferences=["Keto"],
        restrictions=["No pork"],
        goals=["Muscle gain"],
    )
    handler.handle_update(make_update("/profile"))
    msg_text = mock_api.send_message.call_args[0][1]
    assert "Profile: Alice" in msg_text
    assert "Bob (2000 kcal/day)" in msg_text
    assert "Peanuts" in msg_text
    assert "Keto" in msg_text


def test_cmd_plan(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_lambda = mocker.MagicMock()

    handler = BotHandler(
        repo=mock_repo,
        telegram_api=mock_api,
        lambda_client=mock_lambda,
        planner_function_name="planner_func",
    )

    # No profile
    mock_repo.get_profile.return_value = None
    handler.handle_update(make_update("/plan"))
    assert "Please set up your profile" in mock_api.send_message.call_args[0][1]

    # With profile
    mock_api.reset_mock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    handler.handle_update(make_update("/plan"))

    mock_lambda.invoke.assert_called_once()
    call_kwargs = mock_lambda.invoke.call_args[1]
    assert call_kwargs["FunctionName"] == "planner_func"
    assert call_kwargs["InvocationType"] == "Event"
    payload = json.loads(call_kwargs["Payload"])
    assert payload["user_id"] == "12345"


def test_cmd_plan_lambda_error(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    mock_api = mocker.MagicMock()
    mock_lambda = mocker.MagicMock()
    mock_lambda.invoke.side_effect = RuntimeError("Lambda invoke failed")

    handler = BotHandler(
        repo=mock_repo,
        telegram_api=mock_api,
        lambda_client=mock_lambda,
        planner_function_name="planner_func",
    )
    handler.handle_update(make_update("/plan"))
    assert (
        "Error generating plan"
        in mock_api.send_message.call_args_list[-1][0][1]
    )


def test_cmd_grocery(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    # No plan
    mock_repo.get_current_plan.return_value = None
    handler.handle_update(make_update("/grocery"))
    assert "No active meal plan found" in mock_api.send_message.call_args[0][1]

    # With plan
    mock_repo.get_current_plan.return_value = WeeklyPlan(
        week_start_date="2026-08-10",
        grocery_list=[GrocerySection(name="Produce", items=["Apples"])],
    )
    handler.handle_update(make_update("/grocery"))
    mock_api.send_grocery_list.assert_called_once()


def test_cmd_today(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    # No plan
    mock_repo.get_current_plan.return_value = None
    handler.handle_update(make_update("/today"))
    assert "No active meal plan found" in mock_api.send_message.call_args[0][1]

    # With plan
    mock_repo.get_current_plan.return_value = WeeklyPlan(
        week_start_date="2026-08-10",
        days=[
            PlanDay(
                day=1,
                meals=[
                    PlannedMeal(
                        meal_type="breakfast",
                        name="Oatmeal",
                        est_calories=350,
                    )
                ],
            )
        ],
    )
    handler.handle_update(make_update("/today"))
    assert "Today's Planned Meals" in mock_api.send_message.call_args[0][1]
    assert "Oatmeal" in mock_api.send_message.call_args[0][1]


def test_cmd_submit_meals(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    # No plan
    mock_repo.get_current_plan.return_value = None
    handler.handle_update(make_update("/submit_meals"))
    assert "No active meal plan found" in mock_api.send_message.call_args[0][1]

    # With plan
    mock_repo.get_current_plan.return_value = WeeklyPlan(
        week_start_date="2026-08-10",
        days=[
            PlanDay(
                day=1,
                meals=[
                    PlannedMeal(
                        meal_type="lunch",
                        name="Salad",
                        est_calories=400,
                    )
                ],
            )
        ],
    )
    handler.handle_update(make_update("/submit_meals"))
    mock_api.send_meal_checkin.assert_called_once()


def test_unknown_command(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)
    handler.handle_update(make_update("/foobar"))
    assert "Unknown command: /foobar" in mock_api.send_message.call_args[0][1]


def test_handle_callback_query(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_repo.get_current_plan.return_value = WeeklyPlan(
        week_start_date="2026-08-10"
    )

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    # Cooked
    handler.handle_update(make_update(callback_data="checkin:1:lunch:cooked"))
    mock_repo.update_meal_status.assert_called_with(
        "12345", "2026-08-10", 1, "lunch", was_cooked=True
    )
    assert "cooked" in mock_api.send_message.call_args[0][1]

    # Skipped
    handler.handle_update(make_update(callback_data="checkin:1:lunch:skipped"))
    mock_repo.update_meal_status.assert_called_with(
        "12345", "2026-08-10", 1, "lunch", was_cooked=False
    )
    assert "skipped" in mock_api.send_message.call_args[0][1]

    # Swapped
    handler.handle_update(make_update(callback_data="checkin:1:lunch:swapped"))
    mock_repo.update_meal_status.assert_called_with(
        "12345", "2026-08-10", 1, "lunch", was_cooked=False
    )
    assert "swapped" in mock_api.send_message.call_args[0][1]


def test_handle_callback_malformed_day(mocker: Any) -> None:
    """handle_callback silently ignores malformed day values.

    A tampered callback_data with a non-integer day field must NOT raise
    ValueError and must NOT call update_meal_status or send any message.
    """
    mock_repo = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)

    malformed_payloads = [
        "checkin:abc:lunch:cooked",  # non-integer day
        "checkin::lunch:cooked",  # empty day
        "checkin:1.5:lunch:cooked",  # float string
    ]
    for payload in malformed_payloads:
        handler.handle_update(make_update(callback_data=payload))

    mock_repo.update_meal_status.assert_not_called()
    mock_api.send_message.assert_not_called()


def test_conversational_log_meal_intent(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    mock_api = mocker.MagicMock()
    mock_llm = mocker.MagicMock()

    llm_resp = (
        "Logged your lunch! 🥗\n"
        '```json\n{"intent": "log_meal", "entities": {'
        '"date": "2026-08-05", "meal_type": "lunch", '
        '"description": "Chicken salad"}}\n```'
    )
    mock_llm.chat_sync = MagicMock(return_value=llm_resp)

    handler = BotHandler(
        repo=mock_repo, telegram_api=mock_api, llm_client=mock_llm
    )
    handler.handle_update(make_update("I had chicken salad for lunch today"))

    mock_repo.log_meal.assert_called_once()
    logged_entry = mock_repo.log_meal.call_args[0][1]
    assert logged_entry.meal_type == "lunch"
    assert logged_entry.description == "Chicken salad"
    mock_api.send_message.assert_called_once()
    assert "Logged your lunch!" in mock_api.send_message.call_args[0][1]


def test_conversational_edit_plan_intent(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    existing_plan = WeeklyPlan(
        week_start_date="2026-08-10",
        days=[
            PlanDay(
                day=1,
                meals=[
                    PlannedMeal(
                        meal_type="dinner", name="Pasta", est_calories=600
                    )
                ],
            )
        ],
    )
    mock_repo.get_current_plan.return_value = existing_plan
    mock_api = mocker.MagicMock()
    mock_llm = mocker.MagicMock()

    llm_resp = (
        "Swapped dinner on day 1 to Fish Tacos!\n"
        '```json\n{"intent": "edit_plan", "entities": {'
        '"day": 1, "meal_type": "dinner", "name": "Fish Tacos", '
        '"est_calories": 550}}\n```'
    )
    mock_llm.chat_sync = MagicMock(return_value=llm_resp)

    handler = BotHandler(
        repo=mock_repo, telegram_api=mock_api, llm_client=mock_llm
    )
    handler.handle_update(make_update("Swap Day 1 dinner to Fish Tacos"))

    mock_repo.save_plan.assert_called_once()
    updated_plan = mock_repo.save_plan.call_args[0][1]
    assert updated_plan.days[0].meals[0].name == "Fish Tacos"
    assert updated_plan.days[0].meals[0].est_calories == 550


def test_conversational_update_profile_intent(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(
        name="Alice", allergies=["Peanuts"]
    )
    mock_api = mocker.MagicMock()
    mock_llm = mocker.MagicMock()

    llm_resp = (
        "Updated your allergies!\n"
        '```json\n{"intent": "update_profile", "entities": {'
        '"allergies": ["Peanuts", "Shellfish"]}}\n```'
    )
    mock_llm.chat_sync = MagicMock(return_value=llm_resp)

    handler = BotHandler(
        repo=mock_repo, telegram_api=mock_api, llm_client=mock_llm
    )
    handler.handle_update(make_update("I am also allergic to shellfish"))

    mock_repo.save_profile.assert_called_once()
    updated_prof = mock_repo.save_profile.call_args[0][1]
    assert "Shellfish" in updated_prof.allergies


def test_conversational_error_handling(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.side_effect = RuntimeError("DB connection error")
    mock_api = mocker.MagicMock()

    handler = BotHandler(repo=mock_repo, telegram_api=mock_api)
    handler.handle_update(make_update("Hello"))

    mock_api.send_message.assert_called_once()
    assert (
        "Sorry, I had trouble understanding that"
        in mock_api.send_message.call_args[0][1]
    )


def test_lambda_handler_b64_and_json(mocker: Any, mock_env: None) -> None:
    mocker.patch("boto3.resource")
    mocker.patch("boto3.client")
    mock_bot_handler = mocker.patch("meal_planner.bot_handler.BotHandler")
    instance = MagicMock()
    instance.handle_update.return_value = {"statusCode": 200, "body": "ok"}
    mock_bot_handler.return_value = instance

    update_payload = {"update_id": 1, "message": {"text": "/start"}}
    b64_body = base64.b64encode(
        json.dumps(update_payload).encode("utf-8")
    ).decode("utf-8")

    event = {
        "isBase64Encoded": True,
        "body": b64_body,
        "headers": {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
    }
    res = lambda_handler(event, None)

    assert res == {"statusCode": 200, "body": "ok"}
    instance.handle_update.assert_called_once_with(update_payload)


def test_lambda_handler_invalid_json(mocker: Any) -> None:
    mocker.patch.dict(
        "os.environ", {"TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret"}
    )
    event = {
        "body": "invalid json{",
        "headers": {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
    }
    res = lambda_handler(event, None)
    assert res == {"statusCode": 200, "body": "ok"}


def test_lambda_handler_accepts_case_insensitive_secret_header(
    mocker: Any, mock_env: None
) -> None:
    """Header names are accepted regardless of API Gateway casing."""
    mocker.patch("boto3.resource")
    mocker.patch("boto3.client")
    mock_bot_handler = mocker.patch("meal_planner.bot_handler.BotHandler")
    instance = MagicMock()
    instance.handle_update.return_value = {"statusCode": 200, "body": "ok"}
    mock_bot_handler.return_value = instance

    update_payload = {"update_id": 1}
    event = {
        "body": json.dumps(update_payload),
        "headers": {"x-telegram-bot-api-secret-token": "test-webhook-secret"},
    }

    result = lambda_handler(event, None)

    assert result == {"statusCode": 200, "body": "ok"}
    instance.handle_update.assert_called_once_with(update_payload)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Telegram-Bot-Api-Secret-Token": ["test-webhook-secret"]},
        {"X-Telegram-Bot-Api-Secret-Token": "é"},
        {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    ],
)
def test_lambda_handler_rejects_invalid_webhook_headers(
    mocker: Any, mock_env: None, headers: dict[str, Any]
) -> None:
    """Invalid secrets are forbidden before body decoding or client setup."""
    decode = mocker.patch("base64.b64decode")
    resource = mocker.patch("boto3.resource")
    client = mocker.patch("boto3.client")
    webhook_secret = mocker.patch(
        "meal_planner.bot_handler.get_webhook_secret",
        return_value="test-webhook-secret",
    )

    result = lambda_handler(
        {"isBase64Encoded": True, "body": "not-base64", "headers": headers},
        None,
    )

    assert result == {"statusCode": 403, "body": "forbidden"}
    decode.assert_not_called()
    resource.assert_not_called()
    client.assert_not_called()
    webhook_secret.assert_called()


# ---------------------------------------------------------------------------
# _get_todays_plan_day unit tests
# ---------------------------------------------------------------------------


def _make_plan_with_days(*day_numbers: int) -> WeeklyPlan:
    """Helper: build a WeeklyPlan with the given day numbers, started 2026-08-04
    (a Monday) so day 1 = Mon, day 2 = Tue, …, day 7 = Sun."""
    days = [
        PlanDay(
            day=n,
            meals=[
                PlannedMeal(
                    meal_type="lunch",
                    name=f"Meal for day {n}",
                    est_calories=500,
                )
            ],
        )
        for n in day_numbers
    ]
    return WeeklyPlan(week_start_date="2026-08-04", days=days)


def test_get_todays_plan_day_exact_match(mocker: Any) -> None:
    """When today is day 2 of the plan, the day-2 PlanDay is returned."""
    from datetime import date

    mocker.patch(
        "meal_planner.bot_handler.date",
        **{
            "today.return_value": date(2026, 8, 5),
            "fromisoformat": date.fromisoformat,
        },
    )
    plan = _make_plan_with_days(1, 2, 3)
    result = BotHandler._get_todays_plan_day(plan)
    assert result.day == 2
    assert result.meals[0].name == "Meal for day 2"


def test_get_todays_plan_day_before_window_fallback(mocker: Any) -> None:
    """When today is before the plan's week_start, falls back to days[0]."""
    from datetime import date

    mocker.patch(
        "meal_planner.bot_handler.date",
        **{
            "today.return_value": date(2026, 8, 1),
            "fromisoformat": date.fromisoformat,
        },
    )
    plan = _make_plan_with_days(1, 2, 3)
    result = BotHandler._get_todays_plan_day(plan)
    assert result.day == 1  # fallback to days[0]


def test_get_todays_plan_day_after_window_fallback(mocker: Any) -> None:
    """When today is beyond day 7 of the plan, falls back to days[0]."""
    from datetime import date

    mocker.patch(
        "meal_planner.bot_handler.date",
        **{
            "today.return_value": date(2026, 8, 20),
            "fromisoformat": date.fromisoformat,
        },
    )
    plan = _make_plan_with_days(1, 2)
    result = BotHandler._get_todays_plan_day(plan)
    assert result.day == 1  # fallback to days[0]


def test_get_todays_plan_day_invalid_week_start_fallback() -> None:
    """A malformed week_start string should not raise; falls back to days[0]."""
    plan = WeeklyPlan(week_start_date="not-a-date", days=[PlanDay(day=3)])
    result = BotHandler._get_todays_plan_day(plan)
    assert result.day == 3  # fallback to days[0]
