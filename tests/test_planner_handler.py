"""Planner generation and grocery-finalization workflow tests."""

from datetime import date
from typing import Any

import pytest

from meal_planner.models.schemas import GroceryStatus, PlanStatus
from meal_planner.planner_handler import PlannerHandler, lambda_handler
from tests.factories import make_plan, make_plan_payload, make_profile


def test_generate_plan_saves_draft_without_groceries(mocker: Any) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = make_plan_payload(week)
    repo.save_generated_draft.return_value = True
    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)
    saved = repo.save_generated_draft.call_args.args[1]
    assert saved.status is PlanStatus.DRAFT
    assert saved.grocery_status is GroceryStatus.NOT_REQUESTED
    assert saved.grocery_list == []
    api.send_plan.assert_called_once()


def test_generate_plan_rejects_missing_profile_and_malformed_plan(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    repo.get_profile.return_value = None
    PlannerHandler(repo, api).generate_plan("user", 1)
    repo.save_plan.assert_not_called()
    repo.get_profile.return_value = make_profile()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )
    repo.save_generated_draft.assert_not_called()


def test_late_generation_result_does_not_replace_confirmed_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = make_plan_payload(date(2026, 8, 10))
    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )
    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_not_called()
    assert "already confirmed" in api.send_message.call_args.args[1]


def test_finalize_grocery_success(mocker: Any) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {
        "sections": [{"name": "Produce", "items": ["Apples"]}]
    }
    repo.complete_grocery.return_value = True
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.complete_grocery.assert_called_once()
    assert repo.complete_grocery.call_args.args[2] == plan.revision
    assert repo.complete_grocery.call_args.args[3][0].name == "Produce"


@pytest.mark.parametrize(
    "grocery_status", [GroceryStatus.READY, GroceryStatus.ERROR]
)
def test_duplicate_non_pending_grocery_event_is_silent(
    mocker: Any, grocery_status: GroceryStatus
) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(status=PlanStatus.CONFIRMED, grocery_status=grocery_status)
    repo.get_plan.return_value = plan
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )

    repo.get_profile.assert_not_called()
    llm.chat_json_sync.assert_not_called()
    repo.complete_grocery.assert_not_called()
    repo.fail_grocery.assert_not_called()
    api.send_message.assert_not_called()


def test_finalize_grocery_marks_errors_and_rejects_stale_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    repo.get_plan.return_value = None
    PlannerHandler(repo, api).finalize_grocery("user", 1, "2026-08-10")
    repo.save_plan.assert_not_called()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {"sections": []}
    repo.fail_grocery.return_value = True
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.fail_grocery.assert_called_once_with(
        "user", plan.week_start_date, plan.revision
    )


def test_ready_groceries_survive_notification_failure(mocker: Any) -> None:
    repo = mocker.MagicMock()
    plan = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.PENDING
    )
    repo.get_plan.return_value = plan
    repo.get_profile.return_value = make_profile()
    repo.complete_grocery.return_value = True
    api = mocker.MagicMock()
    api.send_message.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {
        "sections": [{"name": "Produce", "items": ["Apples"]}]
    }
    PlannerHandler(repo, api, llm).finalize_grocery(
        "user", 1, plan.week_start_date
    )
    repo.complete_grocery.assert_called_once()
    repo.fail_grocery.assert_not_called()


def test_handle_event_validates_actions(mocker: Any) -> None:
    planner = PlannerHandler(mocker.MagicMock(), mocker.MagicMock())
    generate = mocker.patch.object(planner, "generate_plan")
    finalize = mocker.patch.object(planner, "finalize_grocery")
    assert planner.handle_event(
        {
            "action": "generate_plan",
            "user_id": "user",
            "chat_id": 1,
            "week_start": "2026-08-10",
        }
    )
    generate.assert_called_once()
    assert planner.handle_event(
        {
            "action": "finalize_grocery",
            "user_id": "user",
            "chat_id": 1,
            "week_start": "2026-08-10",
        }
    )
    finalize.assert_called_once()
    assert not planner.handle_event({"action": "unknown"})


def test_lambda_handler_dispatches_and_rejects_invalid_event(
    mocker: Any, mock_env: None
) -> None:
    mocker.patch("boto3.resource")
    planner_class = mocker.patch("meal_planner.planner_handler.PlannerHandler")
    planner_class.return_value.handle_event.return_value = True
    assert (
        lambda_handler({"user_id": "user", "chat_id": 1}, None)["statusCode"]
        == 200
    )
    planner_class.return_value.handle_event.return_value = False
    assert lambda_handler({}, None)["statusCode"] == 400
