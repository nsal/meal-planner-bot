"""Unit tests for Planner Lambda handler."""

from typing import Any
from unittest.mock import MagicMock

from meal_planner.models.schemas import UserProfile
from meal_planner.planner_handler import PlannerHandler, lambda_handler


def test_generate_plan_success(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(
        name="Alice", people_count=2
    )
    mock_repo.get_meal_history.return_value = []
    mock_repo.get_current_plan.return_value = None
    mock_api = mocker.MagicMock()
    mock_llm = mocker.MagicMock()

    plan_json = {
        "week_start_date": "2026-08-10",
        "status": "draft",
        "days": [
            {
                "day": 1,
                "meals": [
                    {
                        "meal_type": "lunch",
                        "name": "Chicken Salad",
                        "ingredients": [{"item": "Chicken", "amount": "200g"}],
                        "est_calories": 450,
                        "was_cooked": False,
                    }
                ],
            }
        ],
    }

    grocery_json = {
        "sections": [
            {"name": "Meat & Poultry", "items": ["400g Chicken breast"]}
        ]
    }

    mock_llm.chat_json_sync = MagicMock(side_effect=[plan_json, grocery_json])

    planner = PlannerHandler(
        repo=mock_repo, telegram_api=mock_api, llm_client=mock_llm
    )
    planner.generate_plan("12345", 12345)

    mock_repo.save_plan.assert_called_once()
    saved_plan = mock_repo.save_plan.call_args[0][1]
    assert saved_plan.days[0].meals[0].name == "Chicken Salad"
    assert saved_plan.grocery_list[0].name == "Meat & Poultry"
    mock_api.send_plan.assert_called_once()


def test_generate_plan_no_profile(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = None
    mock_api = mocker.MagicMock()

    planner = PlannerHandler(repo=mock_repo, telegram_api=mock_api)
    planner.generate_plan("12345", 12345)

    mock_api.send_message.assert_called_once()
    assert "No profile found" in mock_api.send_message.call_args[0][1]


def test_generate_plan_invalid_llm_plan(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.return_value = UserProfile(name="Alice")
    mock_api = mocker.MagicMock()
    mock_llm = mocker.MagicMock()
    mock_llm.chat_json_sync = MagicMock(return_value={})

    planner = PlannerHandler(
        repo=mock_repo, telegram_api=mock_api, llm_client=mock_llm
    )
    planner.generate_plan("12345", 12345)

    mock_api.send_message.assert_called_once()
    assert "couldn't generate" in mock_api.send_message.call_args[0][1]


def test_generate_plan_exception_handling(mocker: Any) -> None:
    mock_repo = mocker.MagicMock()
    mock_repo.get_profile.side_effect = RuntimeError("DB error")
    mock_api = mocker.MagicMock()

    planner = PlannerHandler(repo=mock_repo, telegram_api=mock_api)
    planner.generate_plan("12345", 12345)

    mock_api.send_message.assert_called_once()
    assert "an error occurred" in mock_api.send_message.call_args[0][1]


def test_lambda_handler(mocker: Any, mock_env: None, monkeypatch: Any) -> None:
    """Planner initialization does not require the bot webhook secret."""
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    mocker.patch("boto3.resource")

    mock_planner = mocker.patch("meal_planner.planner_handler.PlannerHandler")
    instance = MagicMock()
    mock_planner.return_value = instance

    event = {"user_id": "12345", "chat_id": 12345}
    res = lambda_handler(event, None)

    assert res == {"statusCode": 200, "body": "ok"}
    instance.generate_plan.assert_called_once_with("12345", 12345)


def test_lambda_handler_invalid_event(mocker: Any) -> None:
    res = lambda_handler({}, None)
    assert res["statusCode"] == 400
