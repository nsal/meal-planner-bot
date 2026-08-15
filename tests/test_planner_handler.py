"""Planner generation and grocery-finalization workflow tests."""

from datetime import date
from typing import Any

import pytest

from meal_planner.llm.client import LLMTransientError
from meal_planner.models.schemas import GroceryStatus, MealOutcome, PlanStatus
from meal_planner.planner_handler import PlannerHandler, lambda_handler
from tests.factories import make_plan, make_plan_payload, make_profile


def test_generate_plan_saves_draft_without_groceries(mocker: Any) -> None:
    repo = mocker.MagicMock()
    events: list[str] = []
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = make_plan_payload(week)
    repo.save_generated_draft.side_effect = lambda *_args, **_kwargs: (
        events.append("persist") or True
    )
    api.send_plan.side_effect = lambda *_args: events.append("send_plan")
    api.send_message.side_effect = lambda *_args: events.append("send_message")
    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)
    saved = repo.save_generated_draft.call_args.args[1]
    assert saved.status is PlanStatus.DRAFT
    assert saved.grocery_status is GroceryStatus.NOT_REQUESTED
    assert saved.grocery_list == []
    repo.save_generated_draft.assert_called_once_with(
        "user", saved, expected_revision=None
    )
    api.send_plan.assert_called_once()
    api.send_message.assert_called_once_with(
        1, "Review this draft, request edits, then tell me to confirm it."
    )
    assert events == ["persist", "send_plan", "send_message"]


def test_generate_plan_delivery_failure_keeps_persisted_draft(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    api.send_plan.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = make_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_not_called()


def test_generate_plan_follow_up_delivery_failure_keeps_persisted_draft(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    api.send_message.side_effect = RuntimeError("offline")
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    llm.chat_json_sync.return_value = make_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    repo.save_generated_draft.assert_called_once()
    api.send_plan.assert_called_once()
    api.send_message.assert_called_once_with(
        1, "Review this draft, request edits, then tell me to confirm it."
    )


def test_generate_plan_normalizes_provider_lifecycle_fields(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    week = date(2026, 8, 10)
    payload = make_plan_payload(week)
    payload["status"] = PlanStatus.CONFIRMED.value
    payload["revision"] = 9
    payload["grocery_status"] = GroceryStatus.READY.value
    payload["grocery_list"] = [{"name": "Produce", "items": ["Apples"]}]
    payload["days"][0]["meals"][0]["outcome"] = MealOutcome.COOKED.value
    payload["days"][1]["meals"][0]["outcome"] = MealOutcome.SKIPPED.value
    payload["days"][2]["meals"][0]["outcome"] = MealOutcome.SWAPPED.value
    llm.chat_json_sync.return_value = payload
    repo.save_generated_draft.return_value = True

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    saved = repo.save_generated_draft.call_args.args[1]
    sent = api.send_plan.call_args.args[1]
    assert saved.status is PlanStatus.DRAFT
    assert saved.revision == 0
    assert saved.grocery_status is GroceryStatus.NOT_REQUESTED
    assert saved.grocery_list == []
    assert all(
        meal.outcome is MealOutcome.UNREPORTED
        for plan_day in saved.days
        for meal in plan_day.meals
    )
    assert sent is saved


def test_generate_plan_rejects_missing_profile_and_malformed_plan(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    api = mocker.MagicMock()
    repo.get_profile.return_value = None
    PlannerHandler(repo, api).generate_plan("user", 1)
    repo.save_plan.assert_not_called()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}
    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )
    repo.save_generated_draft.assert_not_called()


def test_generate_plan_rejects_ambiguous_daily_meals(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    payload = make_plan_payload(date(2026, 8, 10))
    payload["days"][0]["meals"].append(payload["days"][0]["meals"][0])
    llm.chat_json_sync.return_value = payload

    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    assert "valid meal plan" in api.send_message.call_args.args[1]


def test_late_generation_result_does_not_replace_confirmed_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
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
    assert "discarded the stale result" in api.send_message.call_args.args[1]


def test_generate_plan_uses_draft_revision_snapshot(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    existing = make_plan(week_start=week, revision=4)
    events: list[str] = []
    repo.get_profile.return_value = make_profile()
    repo.get_plan.side_effect = lambda *_args, **_kwargs: (
        events.append("snapshot") or existing
    )
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = existing
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = lambda *_args: (
        events.append("llm") or make_plan_payload(week)
    )

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    saved = repo.save_generated_draft.call_args.args[1]
    assert saved.revision == 5
    assert events == ["snapshot", "llm"]
    assert repo.get_plan.call_args.kwargs == {"consistent_read": True}
    repo.save_generated_draft.assert_called_once_with(
        "user", saved, expected_revision=4
    )


def test_generate_plan_skips_llm_for_confirmed_exact_week(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    llm.chat_json_sync.assert_not_called()
    repo.get_plan.assert_called_once_with("user", week, consistent_read=True)
    repo.get_meal_history.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    api.send_plan.assert_not_called()
    repo.clear_conversation_state_if_matches.assert_not_called()


def test_confirmed_stateful_plan_clears_matching_request(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    api = mocker.MagicMock()

    PlannerHandler(repo, api).generate_plan(
        "user",
        1,
        week_start=week,
        request_id="request-1",
        state_revision=3,
    )

    repo.clear_conversation_state_if_matches.assert_called_once_with(
        "user", request_id="request-1", expected_revision=3
    )
    assert "already confirmed" in api.send_message.call_args.args[1]


def test_confirmed_stateful_plan_does_not_delete_when_cleanup_loses_race(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(
        week_start=week, status=PlanStatus.CONFIRMED
    )
    repo.clear_conversation_state_if_matches.return_value = False
    api = mocker.MagicMock()

    PlannerHandler(repo, api).generate_plan(
        "user",
        1,
        week_start=week,
        request_id="request-1",
        state_revision=3,
    )

    repo.clear_conversation_state_if_matches.assert_called_once()
    assert "already confirmed" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_one_stops_after_transient_failure(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = LLMTransientError("temporary")

    PlannerHandler(repo, api, llm, max_attempts=1).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    assert llm.chat_json_sync.call_count == 1
    repo.save_generated_draft.assert_not_called()
    assert "temporarily unavailable" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_one_does_not_repair_invalid_output(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = {}

    PlannerHandler(repo, api, llm, max_attempts=1).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    assert llm.chat_json_sync.call_count == 1
    assert "invalid meal plan" in api.send_message.call_args.args[1]


def test_planner_attempt_limit_two_keeps_one_repair_attempt(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = None
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = True
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.side_effect = [{}, make_plan_payload(week)]

    PlannerHandler(repo, api, llm, max_attempts=2).generate_plan(
        "user", 1, week_start=week
    )

    assert llm.chat_json_sync.call_count == 2
    assert (
        "Repair the previous response" in llm.chat_json_sync.call_args.args[1]
    )


def test_planner_rejects_non_positive_attempt_limit(mocker: Any) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        PlannerHandler(mocker.MagicMock(), mocker.MagicMock(), max_attempts=0)


def test_generate_plan_notifies_when_snapshot_read_fails(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    repo.get_profile.return_value = make_profile()
    repo.get_plan.side_effect = RuntimeError("database unavailable")
    api = mocker.MagicMock()
    llm = mocker.MagicMock()

    PlannerHandler(repo, api, llm).generate_plan(
        "user", 1, week_start=date(2026, 8, 10)
    )

    repo.get_plan.assert_called_once_with(
        "user", date(2026, 8, 10), consistent_read=True
    )
    llm.chat_json_sync.assert_not_called()
    repo.save_generated_draft.assert_not_called()
    assert (
        api.send_message.call_args.args[1]
        == "Sorry, an error occurred while generating your plan."
    )


def test_generate_plan_does_not_send_rejected_stale_result(
    mocker: Any,
) -> None:
    repo = mocker.MagicMock()
    week = date(2026, 8, 10)
    repo.get_profile.return_value = make_profile()
    repo.get_plan.return_value = make_plan(week_start=week, revision=2)
    repo.get_meal_history.return_value = []
    repo.get_latest_plan.return_value = None
    repo.save_generated_draft.return_value = False
    api = mocker.MagicMock()
    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = make_plan_payload(week)

    PlannerHandler(repo, api, llm).generate_plan("user", 1, week_start=week)

    api.send_plan.assert_not_called()
    assert "discarded the stale result" in api.send_message.call_args.args[1]


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
    repo.get_plan.assert_called_once_with(
        "user", plan.week_start_date, consistent_read=True
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
    assert planner_class.call_args.kwargs["max_attempts"] == 2
    planner_class.return_value.handle_event.return_value = False
    assert lambda_handler({}, None)["statusCode"] == 400
