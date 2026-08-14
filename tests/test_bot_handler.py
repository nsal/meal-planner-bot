"""Bot command, mutation, callback, and Lambda boundary tests."""

import base64
import json
from datetime import date, timedelta
from typing import Any

import pytest

from meal_planner.bot_handler import BotHandler, lambda_handler
from meal_planner.db.dynamo import ActivePlanSnapshot
from meal_planner.models.schemas import (
    ConversationIntent,
    GroceryStatus,
    MealOutcome,
    PlanStatus,
    ProfileUpdateEntities,
)
from meal_planner.router import RouteResult, RouteType
from meal_planner.telegram.api import TelegramAPIError
from tests.factories import make_plan, make_profile


@pytest.fixture
def handler(mocker: Any) -> BotHandler:
    return BotHandler(
        mocker.MagicMock(),
        mocker.MagicMock(),
        lambda_client=mocker.MagicMock(),
        planner_function_name="planner",
        llm_client=mocker.MagicMock(),
    )


def _command(name: str) -> RouteResult:
    return RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command=name,
    )


def test_all_commands_have_controlled_success_or_missing_state(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_active_plan.return_value = make_plan(
        status=PlanStatus.CONFIRMED,
        grocery_status=GroceryStatus.READY,
    )
    for command in (
        "start",
        "profile",
        "plan",
        "grocery",
        "today",
        "submit_meals",
    ):
        handler.handle_command(_command(command))
    handler.telegram_api.send_plan.assert_not_called()
    handler.repo.get_profile.return_value = None
    handler.repo.get_active_plan.return_value = None
    for command in ("profile", "plan", "grocery", "today", "submit_meals"):
        handler.handle_command(_command(command))
    assert handler.telegram_api.send_message.call_count >= 6


def test_plan_command_invokes_explicit_generation_event(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = make_profile()
    handler.handle_command(_command("plan"))
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["action"] == "generate_plan"
    assert payload["week_start"] == date.today().isoformat()


def test_profile_onboarding_accumulates_then_saves(handler: BotHandler) -> None:
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    partial = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Alex", "people_count": 2},
        None,
    )
    assert partial.success
    assert partial.message and "family_members" in partial.message
    handler.repo.save_profile_draft.assert_called_once()

    complete_entities = {
        "name": "Alex",
        "people_count": 2,
        "family_members": [
            {"name": "Alex", "calorie_target": 2000},
            {"name": "Sam", "calorie_target": 1800},
        ],
        "allergies": [],
        "dietary_preferences": ["balanced"],
        "restrictions": [],
        "goals": ["health"],
    }
    completed = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        complete_entities,
        None,
    )
    assert completed.success
    saved = handler.repo.save_profile.call_args.args[1]
    assert len(saved.family_members) == 2
    handler.repo.delete_profile_draft.assert_called_once_with("user")


def test_profile_update_rejects_invalid_targets_and_reports_db_failure(
    handler: BotHandler,
) -> None:
    bad = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"family_members": [{"name": "Alex", "calorie_target": -1}]},
        None,
    )
    assert not bad.success
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    handler.repo.save_profile_draft.side_effect = RuntimeError("db down")
    failed = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Alex"},
        None,
    )
    assert not failed.success
    assert failed.message and "save" in failed.message


def test_incomplete_complete_looking_profile_is_saved_as_draft(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "name": "Alex",
            "people_count": 2,
            "family_members": [{"name": "Alex", "calorie_target": 2000}],
            "allergies": [],
            "dietary_preferences": [],
            "restrictions": [],
            "goals": [],
        },
        None,
    )
    assert result.success
    assert result.message and "one name" in result.message
    handler.repo.save_profile_draft.assert_called_once()
    handler.repo.save_profile.assert_not_called()


def test_existing_profile_size_change_accumulates_replacement_members(
    handler: BotHandler,
) -> None:
    existing = make_profile()
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "people_count": 3,
            "family_members": [
                {"name": "Alex", "calorie_target": 2000},
                {"name": "Sam", "calorie_target": 1800},
                {"name": "Lee", "calorie_target": 1600},
            ],
        },
        existing,
    )
    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    assert saved.people_count == 3
    assert [member.name for member in saved.family_members] == [
        "Alex",
        "Sam",
        "Lee",
    ]


def test_existing_profile_same_size_update_preserves_members(
    handler: BotHandler,
) -> None:
    existing = make_profile()
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"people_count": 2, "allergies": ["peanuts"]},
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    assert [member.name for member in saved.family_members] == [
        "Alex",
        "Sam",
    ]
    assert saved.allergies == ["peanuts"]


def test_confirm_and_edit_refresh_exact_week(handler: BotHandler) -> None:
    draft = make_plan()
    handler.repo.get_latest_plan.return_value = draft
    confirmed = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )
    assert confirmed.success
    assert draft.status is PlanStatus.CONFIRMED
    assert draft.grocery_status is GroceryStatus.PENDING
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["action"] == "finalize_grocery"
    assert payload["week_start"] == draft.week_start_date

    handler.repo.get_active_plan.return_value = draft
    edited = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": 1, "meal_type": "lunch", "name": "New lunch"},
        None,
    )
    assert edited.success
    assert draft.days[0].meals[0].name == "New lunch"
    assert draft.grocery_status is GroceryStatus.PENDING


def test_confirm_retries_only_failed_grocery_generation(
    handler: BotHandler,
) -> None:
    failed = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.ERROR
    )
    handler.repo.get_latest_plan.return_value = failed
    handler.repo.get_active_plan.return_value = failed
    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )
    assert result.success
    assert result.message and "Retrying" in result.message
    handler.repo.retry_grocery.assert_called_once_with(
        "user", failed.week_start_date, failed.revision
    )


def test_confirm_retries_active_error_when_latest_error_is_inactive(
    handler: BotHandler,
) -> None:
    latest = make_plan(
        week_start=date.today() + timedelta(days=8),
        status=PlanStatus.CONFIRMED,
        grocery_status=GroceryStatus.ERROR,
    )
    active = make_plan(
        status=PlanStatus.CONFIRMED, grocery_status=GroceryStatus.ERROR
    )
    handler.repo.get_latest_plan.return_value = latest
    handler.repo.get_active_plan.return_value = active
    handler.repo.retry_grocery.return_value = True

    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )

    assert result.success
    handler.repo.retry_grocery.assert_called_once_with(
        "user", active.week_start_date, active.revision
    )


def test_confirm_rejects_expired_draft_without_mutation(
    handler: BotHandler,
) -> None:
    expired = make_plan(week_start=date.today() - timedelta(days=8))
    handler.repo.get_latest_plan.return_value = expired
    handler.repo.get_active_plan.return_value = None

    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )

    assert not result.success
    assert result.message and "expired" in result.message
    handler.repo.confirm_plan.assert_not_called()
    handler.repo.retry_grocery.assert_not_called()
    handler.repo.fail_grocery.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("week_offset", [-14, 8])
def test_confirm_rejects_inactive_error_without_retry(
    handler: BotHandler, week_offset: int
) -> None:
    inactive = make_plan(
        week_start=date.today() + timedelta(days=week_offset),
        status=PlanStatus.CONFIRMED,
        grocery_status=GroceryStatus.ERROR,
    )
    handler.repo.get_latest_plan.return_value = inactive
    handler.repo.get_active_plan.return_value = None

    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )

    assert not result.success
    handler.repo.retry_grocery.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("week_offset", [-14, 8])
def test_edit_rejects_inactive_confirmed_plan_without_mutation(
    handler: BotHandler, week_offset: int
) -> None:
    inactive = make_plan(
        week_start=date.today() + timedelta(days=week_offset),
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_latest_plan.return_value = inactive
    handler.repo.get_active_plan.return_value = None

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": 1, "meal_type": "lunch", "name": "New"},
        None,
    )

    assert not result.success
    assert result.message and "inactive" in result.message
    handler.repo.update_meal.assert_not_called()
    handler.repo.fail_grocery.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("week_offset", [0, 8])
def test_edit_accepts_current_and_future_drafts(
    handler: BotHandler, week_offset: int
) -> None:
    draft = make_plan(week_start=date.today() + timedelta(days=week_offset))
    handler.repo.get_latest_plan.return_value = draft
    handler.repo.update_meal.return_value = True

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": 1, "meal_type": "lunch", "name": "New"},
        None,
    )

    assert result.success
    handler.repo.update_meal.assert_called_once()
    handler.repo.get_active_plan.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("status", [GroceryStatus.PENDING, GroceryStatus.READY])
def test_confirm_rejects_active_non_error_grocery_state(
    handler: BotHandler, status: GroceryStatus
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED, grocery_status=status)
    handler.repo.get_latest_plan.return_value = plan
    handler.repo.get_active_plan.return_value = plan
    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )
    assert not result.success
    handler.repo.retry_grocery.assert_not_called()


def test_confirmation_invocation_failure_restores_error_state(
    handler: BotHandler,
) -> None:
    plan = make_plan()
    handler.repo.get_latest_plan.return_value = plan
    handler.lambda_client.invoke.side_effect = RuntimeError("unavailable")
    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )
    assert not result.success
    handler.repo.fail_grocery.assert_called_once_with(
        "user", plan.week_start_date, plan.revision
    )


def test_edit_never_creates_missing_day_or_meal(handler: BotHandler) -> None:
    handler.repo.get_latest_plan.return_value = make_plan()
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": 1, "meal_type": "dinner", "name": "New"},
        None,
    )
    assert not result.success
    handler.repo.save_plan.assert_not_called()


def test_edit_conflict_does_not_start_grocery_refresh(
    handler: BotHandler,
) -> None:
    plan = make_plan(status=PlanStatus.DRAFT)
    handler.repo.get_latest_plan.return_value = plan
    handler.repo.update_meal.return_value = False

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": 1, "meal_type": "lunch", "name": "Stale edit"},
        None,
    )

    assert not result.success
    assert result.message and "changed" in result.message
    update_call = handler.repo.update_meal.call_args
    assert update_call is not None
    assert update_call.args[:4] == (
        "user",
        plan.week_start_date,
        1,
        "lunch",
    )
    assert update_call.args[5] == plan.revision
    assert update_call.kwargs["expected_status"] is PlanStatus.DRAFT
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("outcome", list(MealOutcome)[1:])
def test_callback_updates_every_outcome_and_acknowledges(
    handler: BotHandler, outcome: MealOutcome
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=None
    )
    handler.repo.update_meal_outcome.return_value = True
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=(
            f"checkin:{plan.week_start_date}:1:lunch:{outcome.value}"
        ),
    )
    handler.handle_callback(route)
    update_call = handler.repo.update_meal_outcome.call_args
    assert update_call is not None
    assert update_call.args[-1] is outcome
    assert update_call.kwargs["expected_epoch"] is None
    handler.repo.get_active_plan.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once()


def test_callback_rejects_superseded_overlapping_plan(
    handler: BotHandler,
) -> None:
    older = make_plan(
        week_start=date.today() - timedelta(days=2),
        status=PlanStatus.CONFIRMED,
    )
    active = make_plan(
        week_start=date.today() - timedelta(days=1),
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=active, active_epoch=1
    )
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"checkin:{older.week_start_date}:1:lunch:cooked",
    )

    handler.handle_callback(route)

    handler.repo.update_meal_outcome.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Inactive plan"
    )
    assert (
        "inactive plan" in handler.telegram_api.send_message.call_args.args[1]
    )


def test_callback_epoch_conflict_notifies_and_acknowledges(
    handler: BotHandler,
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=4
    )
    handler.repo.update_meal_outcome.return_value = False
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"checkin:{plan.week_start_date}:1:lunch:cooked",
    )

    handler.handle_callback(route)

    assert (
        "changed before" in handler.telegram_api.send_message.call_args.args[1]
    )
    handler.repo.update_meal_outcome.assert_called_once_with(
        "user",
        plan.week_start_date,
        1,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=4,
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Meal changed"
    )


def test_callback_persistence_error_notifies_and_acknowledges(
    handler: BotHandler,
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=2
    )
    handler.repo.update_meal_outcome.side_effect = RuntimeError("db down")
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"checkin:{plan.week_start_date}:1:lunch:cooked",
    )

    handler.handle_callback(route)

    assert (
        "couldn't update" in handler.telegram_api.send_message.call_args.args[1]
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Unable to update meal"
    )


def test_callback_delivery_failure_keeps_committed_update_successful(
    handler: BotHandler,
) -> None:
    plan = make_plan(status=PlanStatus.CONFIRMED)
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=2
    )
    handler.repo.update_meal_outcome.return_value = True
    handler.telegram_api.send_message.side_effect = TelegramAPIError(
        "delivery failed"
    )
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=f"checkin:{plan.week_start_date}:1:lunch:cooked",
    )

    handler.handle_callback(route)

    handler.repo.update_meal_outcome.assert_called_once()
    assert handler.telegram_api.send_message.call_count == 1
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Meal updated"
    )


def test_callback_rejects_old_missing_and_persistence_failure(
    handler: BotHandler,
) -> None:
    old_route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data="checkin:1:lunch:cooked",
    )
    handler.repo.get_active_plan_snapshot.return_value = None
    handler.handle_callback(old_route)
    handler.telegram_api.answer_callback_query.assert_called_once()
    handler.telegram_api.answer_callback_query.reset_mock()
    expired = make_plan(
        week_start=date.today() - timedelta(days=8),
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=expired, active_epoch=None
    )
    old_route.callback_data = (
        f"checkin:{expired.week_start_date}:1:lunch:cooked"
    )
    handler.handle_callback(old_route)
    handler.repo.update_meal_outcome.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once()


def test_conversation_replaces_false_success_reply(handler: BotHandler) -> None:
    handler.repo.get_profile.return_value = None
    handler.repo.get_latest_plan.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.llm_client.chat_sync.return_value = (
        "Saved!\n```json\n"
        '{"intent":"update_profile","entities":{"people_count":0}}'
        "\n```"
    )
    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="we are zero people",
        )
    )
    sent = handler.telegram_api.send_message.call_args.args[1]
    assert sent != "Saved!"


def test_repeated_conversation_update_passes_same_source_id(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = None
    handler.repo.get_profile_draft.return_value = None
    handler.repo.get_latest_plan.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.llm_client.chat_sync.return_value = (
        'Logged.\n```json\n{"intent":"log_meal","entities":'
        '{"date":"2026-08-05","meal_type":"lunch",'
        '"description":"Chicken salad"}}\n```'
    )
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="I had chicken salad",
        raw_update={"update_id": 42},
    )

    handler.handle_conversational(route)
    handler.handle_conversational(route)

    assert handler.repo.log_meal.call_count == 2
    assert [
        call.kwargs["source_update_id"]
        for call in handler.repo.log_meal.call_args_list
    ] == ["42", "42"]


@pytest.mark.parametrize("update_id", [True, False, "42", 42.0, None])
def test_invalid_conversation_update_id_uses_timestamp_fallback(
    handler: BotHandler,
    update_id: Any,
) -> None:
    handler.repo.get_profile.return_value = None
    handler.repo.get_profile_draft.return_value = None
    handler.repo.get_latest_plan.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.llm_client.chat_sync.return_value = (
        'Logged.\n```json\n{"intent":"log_meal","entities":'
        '{"meal_type":"lunch","description":"Soup"}}\n```'
    )
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="I had soup",
        raw_update={"update_id": update_id},
    )

    handler.handle_conversational(route)

    assert handler.repo.log_meal.call_args.kwargs["source_update_id"] is None


def test_missing_conversation_update_id_uses_timestamp_fallback(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = None
    handler.repo.get_profile_draft.return_value = None
    handler.repo.get_latest_plan.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.llm_client.chat_sync.return_value = (
        'Logged.\n```json\n{"intent":"log_meal","entities":'
        '{"meal_type":"lunch","description":"Soup"}}\n```'
    )
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="I had soup",
        raw_update={},
    )

    handler.handle_conversational(route)

    assert handler.repo.log_meal.call_args.kwargs["source_update_id"] is None


def test_conversation_passes_persisted_profile_draft_to_llm(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = None
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities(
        name="Alex", people_count=2
    )
    handler.repo.get_latest_plan.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.llm_client.chat_sync.return_value = "I still need member details."

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="continue onboarding",
        )
    )

    prompt = handler.llm_client.chat_sync.call_args.args[0]
    assert "Name: Alex" in prompt
    assert "People Count: 2" in prompt
    assert "Family Members: Missing" in prompt
    handler.repo.get_profile_draft.assert_called_once_with("user")


def test_telegram_failure_is_controlled_at_update_boundary(
    handler: BotHandler,
) -> None:
    handler.telegram_api.send_message.side_effect = TelegramAPIError("failed")
    result = handler.handle_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1},
                "text": "/start",
            }
        }
    )
    assert result["statusCode"] == 200


def test_lambda_handler_authenticates_before_decode(mocker: Any) -> None:
    decode = mocker.patch("base64.b64decode")
    mocker.patch(
        "meal_planner.bot_handler.get_webhook_secret", return_value="secret"
    )
    assert (
        lambda_handler(
            {"headers": {}, "isBase64Encoded": True, "body": "bad"}, None
        )["statusCode"]
        == 403
    )
    decode.assert_not_called()


def test_lambda_handler_valid_base64_event(mocker: Any, mock_env: None) -> None:
    mocker.patch("boto3.resource")
    mocker.patch("boto3.client")
    handler_class = mocker.patch("meal_planner.bot_handler.BotHandler")
    handler_class.return_value.handle_update.return_value = {
        "statusCode": 200,
        "body": "ok",
    }
    body = base64.b64encode(json.dumps({"update_id": 1}).encode()).decode()
    result = lambda_handler(
        {
            "headers": {
                "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"
            },
            "isBase64Encoded": True,
            "body": body,
        },
        None,
    )
    assert result["statusCode"] == 200
