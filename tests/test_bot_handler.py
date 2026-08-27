"""Bot command, mutation, callback, and Lambda boundary tests."""

import base64
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import call

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from meal_planner.bot_handler import (
    MEAL_INPUT_PROMPT,
    BotHandler,
    lambda_handler,
)
from meal_planner.db.dynamo import ActivePlanSnapshot, DynamoRepository
from meal_planner.llm.client import (
    LLMPermanentError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.models.schemas import (
    BatchLedgerState,
    BatchMealRole,
    BatchRule,
    ConstraintEntry,
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryObligation,
    DietaryPreferenceEntry,
    DietaryRule,
    FamilyMember,
    GroceryStatus,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlanGenerationContext,
    PlannedBatchLink,
    PlanStatus,
    PreferenceRequirement,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    RuleOperator,
    RuleStrength,
    UserProfile,
    Weekday,
    WeeklyBatchLedger,
    canonicalize_profile_rule_ids,
)
from meal_planner.planner_handler import PlannerHandler
from meal_planner.preferences import validate_horizon_feasibility
from meal_planner.router import RouteResult, RouteType
from meal_planner.telegram.access import TelegramAccessPolicy
from meal_planner.telegram.api import TelegramAPIError, split_text
from meal_planner.telegram.commands import BOT_COMMANDS, render_help
from tests.factories import (
    make_batch_ledger_entry,
    make_batch_rule,
    make_plan,
    make_preference,
    make_profile,
)


def test_mutation_error_logs_are_privacy_safe(
    handler: BotHandler, caplog: pytest.LogCaptureFixture
) -> None:
    """Rejected meal content never enters warning or error records."""
    secret_values = (
        "secret meal description",
        "secret foods",
        "secret source text",
        "secret-batch-id",
        "secret raw payload",
    )
    entities = {
        "date": date(2026, 8, 26),
        "meal_type": "not-a-meal-type",
        "description": " | ".join(secret_values),
    }

    with caplog.at_level(logging.WARNING, logger="meal_planner.bot_handler"):
        result = handler._apply_intent_metadata(
            "secret-user", 42, ConversationIntent.LOG_MEAL, entities, None
        )

    assert not result.success
    assert not any(
        secret in caplog.text for secret in (*secret_values, "secret-user")
    )
    assert all(
        record.message == "Rejected conversational mutation reason_code=invalid"
        for record in caplog.records
    )


@pytest.fixture
def handler(mocker: Any) -> BotHandler:
    bot_handler = BotHandler(
        mocker.MagicMock(),
        mocker.MagicMock(),
        lambda_client=mocker.MagicMock(),
        planner_function_name="planner",
        llm_client=mocker.MagicMock(),
        access_policy=TelegramAccessPolicy(frozenset({"1"})),
    )

    def collected_plan_state() -> ConversationState:
        """Build a collected state for legacy preference-flow tests."""
        return BotHandler._new_plan_state().model_copy(
            update={"duration_collected": True}
        )

    bot_handler._new_plan_state = collected_plan_state
    return bot_handler


@pytest.fixture
def real_profile_handler(
    mocker: Any,
) -> Generator[tuple[BotHandler, DynamoRepository], None, None]:
    """Create a handler backed by a real moto DynamoDB table."""
    with mock_aws():
        table = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).create_table(
            TableName="test-meal-planner-handler",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        repo = DynamoRepository(table)
        handler = BotHandler(
            repo,
            mocker.MagicMock(),
            lambda_client=mocker.MagicMock(),
            planner_function_name="planner",
            llm_client=mocker.MagicMock(),
            access_policy=TelegramAccessPolicy(frozenset({"1"})),
        )
        yield handler, repo


def _command(name: str) -> RouteResult:
    return RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command=name,
    )


def _plan_command_at(day: date) -> RouteResult:
    """Build a plan command with a controlled UTC processing date."""
    timestamp = int(
        datetime.combine(
            day, datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
    )
    return RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command="plan",
        raw_update={"message": {"date": timestamp}},
    )


def _wednesday_profile_fixture(
    *, total_yield: int = 2
) -> tuple[UserProfile, BatchRule]:
    """Return the exact typed profile and batch rule for the E2E scenario."""
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": [
                ConstraintEntry(
                    id="no-mushrooms",
                    source_text="no mushrooms",
                    forbidden_terms=["mushroom"],
                )
            ],
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="eggs-three-breakfasts",
                    source_text="eggs three breakfasts weekly",
                    rule=DietaryRule(
                        id="eggs-three-breakfasts",
                        source_text="eggs three breakfasts weekly",
                        foods_any_of=["egg"],
                        meal_type=MealType.BREAKFAST,
                        operator=RuleOperator.AT_LEAST,
                        count=3,
                    ),
                ),
                DietaryPreferenceEntry(
                    id="pancakes-saturday",
                    source_text="pancakes or crepes Saturday",
                    rule=DietaryRule(
                        id="pancakes-saturday",
                        source_text="pancakes or crepes Saturday",
                        foods_any_of=["pancake", "crepe"],
                        meal_type=MealType.BREAKFAST,
                        weekdays=[Weekday.SATURDAY],
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                ),
                DietaryPreferenceEntry(
                    id="fish-one-dinner",
                    source_text="fish at least one dinner weekly",
                    rule=DietaryRule(
                        id="fish-one-dinner",
                        source_text="fish at least one dinner weekly",
                        foods_any_of=["fish"],
                        meal_type=MealType.DINNER,
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                ),
            ],
        }
    )
    batch_rule = BatchRule(
        id="batch-two-lunch-dinner-meals",
        source_text="batch cooking covers two lunch or dinner meals",
        foods_any_of=["chicken"],
        preparation_meal_types=[MealType.LUNCH, MealType.DINNER],
        reuse_meal_types=[MealType.LUNCH, MealType.DINNER],
        total_yield=total_yield,
    )
    return profile, batch_rule


def _route_on(day: date, text: str, update_id: int) -> RouteResult:
    """Build one conversational update with a deterministic UTC date."""
    return RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=text,
        raw_update={
            "update_id": update_id,
            "message": {
                "date": int(
                    datetime.combine(
                        day, datetime.min.time(), tzinfo=timezone.utc
                    ).timestamp()
                )
            },
        },
    )


def _three_portion_provider_payload(
    week: date, *, reversed_ordinals: bool
) -> dict[str, Any]:
    """Build a complete provider plan with dated batch leftovers."""
    days: list[dict[str, Any]] = []
    for day in range(1, 8):
        days.append(
            {
                "day": day,
                "meals": [
                    {
                        "meal_type": "breakfast",
                        "name": f"Oats breakfast {day}",
                        "ingredients": [{"item": "oats"}],
                        "est_calories": 400,
                    },
                    {
                        "meal_type": "lunch",
                        "name": f"Beans lunch {day}",
                        "ingredients": [{"item": "beans"}],
                        "est_calories": 500,
                    },
                    {
                        "meal_type": "dinner",
                        "name": f"Rice dinner {day}",
                        "ingredients": [{"item": "rice"}],
                        "est_calories": 600,
                    },
                ],
            }
        )
    days[0]["meals"][1] = {
        "meal_type": "lunch",
        "name": "Chicken preparation",
        "ingredients": [{"item": "chicken"}],
        "est_calories": 500,
        "batch_link": {
            "batch_id": "provider-chicken",
            "role": "preparation",
            "total_yield": 3,
        },
    }
    portions = (3, 2) if reversed_ordinals else (2, 3)
    for day, portion in zip((2, 3), portions, strict=True):
        days[day - 1]["meals"][1] = {
            "meal_type": "lunch",
            "name": "Chicken leftover",
            "ingredients": [{"item": "chicken"}],
            "est_calories": 500,
            "batch_link": {
                "batch_id": "provider-chicken",
                "role": "leftover",
                "source_date": week.isoformat(),
                "source_meal_type": "lunch",
                "portion": portion,
            },
        }
    return {"week_start_date": week.isoformat(), "days": days}


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


def test_start_requests_family_name_separately_from_member_names(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = None

    handler.handle_command(_command("start"))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "family name" in message
    assert "each household member's name" in message
    assert "dietary constraints" in message
    assert "allergies" not in message
    assert "restrictions" not in message
    assert "tell me your name" not in message


def test_start_explains_optional_nutrient_targets(
    handler: BotHandler,
) -> None:
    """Onboarding makes calories required and nutrient targets optional."""
    handler.repo.get_profile.return_value = None

    handler.handle_command(_command("start"))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "calorie target" in message
    assert "protein" in message
    assert "fibre" in message
    assert "grams/day" in message
    assert "optional" in message
    assert "not required" in message


def test_help_renders_catalogue_without_repository_interaction(
    handler: BotHandler,
) -> None:
    handler.handle_command(_command("help"))

    handler.telegram_api.send_message.assert_called_once_with(1, render_help())
    assert "/plan — Create or retry a meal plan" in render_help().splitlines()
    assert "weekly meal plan" not in render_help().lower()
    handler.repo.assert_not_called()


def test_unknown_command_points_to_help(handler: BotHandler) -> None:
    handler.handle_command(_command("unsupported"))

    message = handler.telegram_api.send_message.call_args.args[1]
    assert "Unknown command: /unsupported" in message
    assert "Type /help for options." in message
    assert "/start for options" not in message


def test_catalogue_commands_reach_their_dispatch_handlers(
    handler: BotHandler, mocker: Any
) -> None:
    for command in BOT_COMMANDS:
        command_handler = mocker.patch.object(handler, f"_cmd_{command.name}")

        handler.handle_command(_command(command.name))

        command_handler.assert_called_once_with(1, "user")


def test_profile_displays_family_name_and_individual_members(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_command(_command("profile"))

    sent_profile = handler.telegram_api.send_profile.call_args.args[1]
    assert sent_profile.name == "Alex"
    assert sent_profile.family_members[0].name == "Alex"


def _profile_callback(
    data: str,
    *,
    query_id: str | None = "profile-query",
) -> RouteResult:
    """Build a callback route for profile navigation tests."""
    return RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_data=data,
        callback_query_id=query_id,
    )


def test_profile_command_replaces_existing_workflow_with_profile_menu(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = make_profile()
    previous = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = previous

    handler.handle_command(_command("profile"))

    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.workflow_kind is ConversationWorkflowKind.PROFILE_EDIT
    assert saved.step is ConversationWorkflowStep.PROFILE_MENU
    assert (
        handler.repo.save_conversation_state.call_args.kwargs[
            "expected_revision"
        ]
        == previous.revision
    )
    handler.repo.save_profile.assert_not_called()


def test_cancel_clears_active_profile_edit_state(
    handler: BotHandler,
) -> None:
    """Cancel removes an active profile edit with its revision guard."""
    state = handler._new_profile_state().model_copy(update={"revision": 3})
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True

    handler.handle_command(_command("cancel"))

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user", expected_revision=3
    )
    assert (
        "cancelled"
        in handler.telegram_api.send_message.call_args.args[1].lower()
    )


@pytest.mark.parametrize(
    ("command", "workflow_kind", "expected_text"),
    [
        ("plan", ConversationWorkflowKind.PLAN_REQUEST, "preference"),
        (
            "submit_meals",
            ConversationWorkflowKind.MEAL_LOG,
            "submit one meal",
        ),
    ],
)
def test_plan_and_submit_meals_replace_active_profile_edit(
    handler: BotHandler,
    command: str,
    workflow_kind: ConversationWorkflowKind,
    expected_text: str,
) -> None:
    """New command workflows replace profile editing through CAS."""
    handler.repo.get_profile.return_value = make_profile()
    active_edit = handler._new_profile_state().model_copy(
        update={"revision": 4}
    )
    handler.repo.get_conversation_state.return_value = active_edit
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command(command))

    replacement = handler.repo.save_conversation_state.call_args.args[1]
    assert replacement.workflow_kind is workflow_kind
    assert (
        handler.repo.save_conversation_state.call_args.kwargs[
            "expected_revision"
        ]
        == active_edit.revision
    )
    assert (
        expected_text
        in handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_profile_replaces_active_profile_edit_with_fresh_menu(
    handler: BotHandler,
) -> None:
    """Reopening /profile replaces the active edit through CAS."""
    handler.repo.get_profile.return_value = make_profile()
    active_edit = handler._new_profile_state().model_copy(
        update={"revision": 5}
    )
    handler.repo.get_conversation_state.return_value = active_edit
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command("profile"))

    replacement = handler.repo.save_conversation_state.call_args.args[1]
    assert replacement.workflow_kind is ConversationWorkflowKind.PROFILE_EDIT
    assert replacement.step is ConversationWorkflowStep.PROFILE_MENU
    assert replacement.revision == active_edit.revision + 1
    handler.telegram_api.send_profile.assert_called_once_with(1, make_profile())
    assert (
        handler.repo.save_conversation_state.call_args.kwargs[
            "expected_revision"
        ]
        == active_edit.revision
    )


def test_profile_root_callback_renders_categories_and_acknowledges(
    handler: BotHandler,
) -> None:
    handler.repo.get_conversation_state.return_value = (
        handler._new_profile_state()
    )

    handler.handle_callback(_profile_callback("profile:root"))

    handler.telegram_api.send_profile_root.assert_called_once_with(1)
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )
    handler.repo.save_profile.assert_not_called()


def test_profile_callback_acknowledges_before_state_read_and_rendering(
    handler: BotHandler,
) -> None:
    events: list[str] = []
    state = handler._new_profile_state()

    def acknowledge(query_id: str, text: str) -> None:
        del query_id, text
        events.append("acknowledge")

    def read_state(user_id: str, *, consistent_read: bool) -> ConversationState:
        del user_id, consistent_read
        events.append("state read")
        return state

    def render_root(chat_id: int | str) -> None:
        del chat_id
        events.append("render")

    handler.telegram_api.answer_callback_query.side_effect = acknowledge
    handler.repo.get_conversation_state.side_effect = read_state
    handler.telegram_api.send_profile_root.side_effect = render_root

    handler.handle_callback(_profile_callback("profile:root"))

    assert events == ["acknowledge", "state read", "render"]
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )


def test_profile_callback_acknowledges_before_stale_state_message(
    handler: BotHandler,
) -> None:
    events: list[str] = []

    def acknowledge(query_id: str, text: str) -> None:
        del query_id, text
        events.append("acknowledge")

    def read_state(user_id: str, *, consistent_read: bool) -> None:
        del user_id, consistent_read
        events.append("state read")
        return None

    def send_message(
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del chat_id, text, reply_markup
        events.append("message")

    handler.telegram_api.answer_callback_query.side_effect = acknowledge
    handler.repo.get_conversation_state.side_effect = read_state
    handler.telegram_api.send_message.side_effect = send_message

    handler.handle_callback(_profile_callback("profile:root"))

    assert events == ["acknowledge", "state read", "message"]
    handler.telegram_api.answer_callback_query.assert_called_once()


@pytest.mark.parametrize("failure_source", ["repository", "telegram delivery"])
def test_profile_callback_acknowledges_once_when_processing_fails(
    handler: BotHandler, failure_source: str
) -> None:
    events: list[str] = []
    state = handler._new_profile_state()

    def acknowledge(query_id: str, text: str) -> None:
        del query_id, text
        events.append("acknowledge")

    def send_error(
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del chat_id, text, reply_markup
        events.append("error message")

    handler.telegram_api.answer_callback_query.side_effect = acknowledge
    handler.telegram_api.send_message.side_effect = send_error
    if failure_source == "repository":

        def read_state(
            user_id: str, *, consistent_read: bool
        ) -> ConversationState:
            del user_id, consistent_read
            events.append("state read")
            raise RuntimeError("database unavailable")

        handler.repo.get_conversation_state.side_effect = read_state
        expected_events = ["acknowledge", "state read", "error message"]
    else:

        def read_state(
            user_id: str, *, consistent_read: bool
        ) -> ConversationState:
            del user_id, consistent_read
            events.append("state read")
            return state

        def render_root(chat_id: int | str) -> None:
            del chat_id
            events.append("render")
            raise TelegramAPIError("delivery unavailable")

        handler.repo.get_conversation_state.side_effect = read_state
        handler.telegram_api.send_profile_root.side_effect = render_root
        expected_events = [
            "acknowledge",
            "state read",
            "render",
            "error message",
        ]

    handler.handle_callback(_profile_callback("profile:root"))

    assert events == expected_events
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )


def test_profile_callback_continues_when_acknowledgement_fails(
    handler: BotHandler,
) -> None:
    events: list[str] = []
    state = handler._new_profile_state()

    def acknowledge(query_id: str, text: str) -> None:
        del query_id, text
        events.append("acknowledge")
        raise TelegramAPIError("acknowledgement unavailable")

    def read_state(user_id: str, *, consistent_read: bool) -> ConversationState:
        del user_id, consistent_read
        events.append("state read")
        return state

    def render_root(chat_id: int | str) -> None:
        del chat_id
        events.append("render")

    handler.repo.get_conversation_state.side_effect = read_state
    handler.telegram_api.answer_callback_query.side_effect = acknowledge
    handler.telegram_api.send_profile_root.side_effect = render_root

    handler.handle_callback(_profile_callback("profile:root"))

    assert events == ["acknowledge", "state read", "render"]
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )
    handler.repo.get_conversation_state.assert_called_once_with(
        "user", consistent_read=True
    )
    handler.telegram_api.send_profile_root.assert_called_once_with(1)


def test_profile_callback_without_query_id_does_not_process_profile_action(
    handler: BotHandler,
) -> None:
    handler.handle_callback(
        _profile_callback("profile:operation:family:add", query_id=None)
    )

    handler.telegram_api.answer_callback_query.assert_not_called()
    handler.repo.get_conversation_state.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.telegram_api.send_message.assert_not_called()


def test_profile_category_and_operation_callbacks_use_cas_without_profile_write(
    handler: BotHandler,
) -> None:
    state = handler._new_profile_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_callback(
        _profile_callback("profile:category:dietary_constraints")
    )
    handler.telegram_api.send_profile_category.assert_called_once_with(
        1, ProfileEditCategory.DIETARY_CONSTRAINTS
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )
    handler.repo.save_profile.assert_not_called()

    handler.telegram_api.reset_mock()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.handle_callback(
        _profile_callback("profile:operation:family:change_calories")
    )
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PROFILE_INPUT
    assert saved.profile_category is ProfileEditCategory.FAMILY
    assert saved.profile_operation is ProfileEditOperation.CHANGE_CALORIES
    handler.telegram_api.send_profile_operation.assert_called_once_with(
        1,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "profile-query", "Processing profile action"
    )
    handler.repo.save_profile.assert_not_called()


@pytest.mark.parametrize(
    "payload", ["profile:back", "profile:done", "profile:close"]
)
def test_profile_navigation_callbacks_do_not_write_profile(
    handler: BotHandler, payload: str
) -> None:
    state = handler._new_profile_state()
    if payload == "profile:back":
        state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
                "profile_category": ProfileEditCategory.FAMILY,
                "profile_operation": ProfileEditOperation.ADD,
            }
        )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True

    handler.handle_callback(_profile_callback(payload))

    if payload == "profile:back":
        handler.telegram_api.send_profile_root.assert_called_once_with(1)
        handler.repo.transition_conversation_state.assert_called_once()
    else:
        handler.repo.delete_conversation_state.assert_called_once_with(
            "user", expected_revision=state.revision
        )
    handler.repo.save_profile.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once()


def _profile_input_state(
    handler: BotHandler,
    category: ProfileEditCategory,
    operation: ProfileEditOperation,
) -> ConversationState:
    """Return an active profile input state for amendment tests."""
    return handler._new_profile_state().model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
            "profile_category": category,
            "profile_operation": operation,
        }
    )


def _profile_text(text: str) -> RouteResult:
    """Build a conversational route for profile amendment input."""
    return RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=text,
    )


def _confirm_pending_profile_rule(
    handler: BotHandler, state: ConversationState
) -> None:
    """Confirm the token stored in a pending profile interpretation."""
    assert state.last_update_id is not None
    payload = json.loads(state.last_update_id.removeprefix("profile-pending:"))
    handler.handle_callback(
        _profile_callback(f"profile:confirm:{payload['token']}")
    )


def _constraint_interpretation(text: str) -> str:
    """Return one complete provider response for a constraint test."""
    return json.dumps(
        {
            "mode": "constraint",
            "requirements": [],
            "exclusions": [
                {
                    "id": "constraint-1",
                    "source_text": text,
                    "forbidden_terms": [text],
                }
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )


def _preference_interpretation(text: str, food: str = "eggs") -> str:
    """Return one complete provider response for a preference test."""
    return json.dumps(
        {
            "mode": "stored_preference",
            "requirements": [
                {
                    "id": "preference-new",
                    "source_text": text,
                    "foods_any_of": [food],
                    "meal_type": "breakfast",
                    "weekdays": [],
                    "operator": "at_least",
                    "count": 1,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )


def _strict_preference_interpretation(
    text: str, food: str = "eggs"
) -> dict[str, Any]:
    """Return the object expected from the strict JSON client boundary."""
    return json.loads(_preference_interpretation(text, food))


def _strict_constraint_interpretation(text: str) -> dict[str, Any]:
    """Return a constraint interpretation as a decoded JSON object."""
    return json.loads(_constraint_interpretation(text))


def test_profile_add_uses_strict_json_and_reviews_complete_typed_rules(
    handler: BotHandler,
) -> None:
    """Profile dietary input is reviewed from one strict JSON response."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_preference_interpretation("eggs for breakfast")
    )

    handler.handle_conversational(_profile_text("eggs for breakfast"))

    handler.llm_client.chat_json_strict_sync.assert_called_once()
    handler.llm_client.chat_sync.assert_not_called()
    review = handler.telegram_api.send_profile_rule_review.call_args.args
    assert review[2] == "eggs for breakfast"
    assert len(review[3]) == 1
    assert isinstance(review[3][0], DietaryRule)
    handler.repo.save_profile_and_transition_state.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        LLMTimeoutError("provider timeout"),
        LLMTransientError("provider unavailable"),
        LLMPermanentError("provider rejected"),
        LLMResponseFormatError("invalid JSON"),
    ],
)
def test_profile_interpretation_failures_leave_profile_unchanged(
    handler: BotHandler, failure: Exception
) -> None:
    """Provider and strict-format failures cannot stage or save a profile."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.llm_client.chat_json_strict_sync.side_effect = failure

    handler.handle_conversational(_profile_text("eggs for breakfast"))

    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "provider" not in message.lower()
    assert "invalid json" not in message.lower()


def test_profile_invalid_typed_interpretation_leaves_profile_unchanged(
    handler: BotHandler,
) -> None:
    """Malformed and unsupported provider objects require clarification."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.llm_client.chat_json_strict_sync.return_value = {
        "mode": "stored_preference",
        "requirements": [{"source_text": "make it healthy"}],
        "batch_rules": [],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    handler.handle_conversational(_profile_text("make it healthy"))

    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.save_profile_and_transition_state.assert_not_called()
    assert "specific foods" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_profile_batch_interpretation_is_reviewed_as_a_batch_rule(
    handler: BotHandler,
) -> None:
    """Batch cooking clauses remain typed and visible before confirmation."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = {
        "mode": "stored_preference",
        "requirements": [],
        "batch_rules": [
            {
                "id": "provider-batch-id",
                "source_text": "cook once for two lunches",
                "foods_any_of": ["chicken"],
                "preparation_meal_types": ["dinner"],
                "reuse_meal_types": ["lunch"],
                "total_yield": 2,
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    handler.handle_conversational(_profile_text("cook once for two lunches"))

    reviewed = handler.telegram_api.send_profile_rule_review.call_args.args[3]
    assert len(reviewed) == 1
    assert isinstance(reviewed[0], BatchRule)
    assert reviewed[0].source_text == "cook once for two lunches"
    handler.repo.save_profile_and_transition_state.assert_not_called()


def test_conversational_profile_update_cannot_write_dietary_entries(
    handler: BotHandler,
) -> None:
    """The generic intent path cannot bypass the /profile boundary."""
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"dietary_preferences": ["eggs for breakfast"]},
        make_profile(),
    )

    assert not result.success
    handler.repo.save_profile.assert_not_called()
    handler.repo.save_profile_draft.assert_not_called()


def test_profile_rule_interpretation_is_durable_until_confirmation(
    handler: BotHandler,
) -> None:
    """A parsed preference is reviewed, then saved exactly once."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.transition_conversation_state.return_value = True
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_preference_interpretation("I would like eggs for breakfast")
    )

    handler.handle_conversational(
        _profile_text("I would like eggs for breakfast")
    )

    pending = handler.repo.transition_conversation_state.call_args.args[1]
    assert pending.last_update_id is not None
    assert pending.last_update_id.startswith("profile-pending:")
    handler.repo.get_conversation_state.return_value = pending
    _confirm_pending_profile_rule(handler, pending)

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    assert saved.dietary_preferences[-1].rule is not None
    assert saved.dietary_preferences[-1].rule.count == 1
    assert saved.dietary_preferences[-1].rule.strength.value == "strict"


def test_stale_profile_confirmation_reloads_latest_profile(
    handler: BotHandler,
) -> None:
    """A rejected confirmation refreshes state without replaying the edit."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_preference_interpretation("eggs for breakfast")
    )

    handler.handle_conversational(_profile_text("eggs for breakfast"))
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending
    handler.repo.get_profile.side_effect = [
        make_profile(),
        make_profile().model_copy(
            update={
                "dietary_constraints": [
                    ConstraintEntry(
                        id="constraint-1",
                        source_text="peanuts",
                        forbidden_terms=["peanut"],
                    )
                ]
            }
        ),
    ]
    handler.repo.save_profile_and_transition_state.return_value = False

    _confirm_pending_profile_rule(handler, pending)

    assert handler.repo.get_profile.call_args_list == [
        call("user", consistent_read=True),
        call("user", consistent_read=True),
    ]
    assert "stale" in handler.telegram_api.send_message.call_args.args[1]


def test_sequential_profile_confirmations_replace_provider_ids(
    handler: BotHandler,
) -> None:
    """Unrelated confirmed rules receive distinct application IDs."""
    profile = make_profile().model_copy(update={"dietary_preferences": []})
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )

    def provider_response(text: str) -> str:
        return json.dumps(
            {
                "mode": "stored_preference",
                "requirements": [
                    {
                        "id": "r1",
                        "source_text": text,
                        "foods_any_of": ["eggs" if "eggs" in text else "tofu"],
                        "meal_type": (
                            "breakfast" if "eggs" in text else "dinner"
                        ),
                        "weekdays": [],
                        "operator": "at_least",
                        "count": 1,
                        "strength": "strict",
                    }
                ],
                "exclusions": [],
                "clarification": None,
                "unparsed_text": [],
            }
        )

    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.transition_conversation_state.return_value = True
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.side_effect = [
        json.loads(provider_response("eggs for breakfast")),
        json.loads(provider_response("tofu for dinner")),
    ]

    handler.handle_conversational(_profile_text("eggs for breakfast"))
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending
    _confirm_pending_profile_rule(handler, pending)
    first_saved = handler.repo.save_profile_and_transition_state.call_args.args[
        1
    ]

    second_state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = second_state
    handler.repo.get_profile.return_value = first_saved
    handler.handle_conversational(_profile_text("tofu for dinner"))
    second_pending = handler.repo.transition_conversation_state.call_args.args[
        1
    ]
    handler.repo.get_conversation_state.return_value = second_pending
    _confirm_pending_profile_rule(handler, second_pending)
    second_saved = (
        handler.repo.save_profile_and_transition_state.call_args.args[1]
    )

    ids = [
        preference.rule.id
        for preference in second_saved.dietary_preferences
        if preference.rule is not None
    ]
    assert len(ids) == len(set(ids))
    assert all(identifier != "r1" for identifier in ids)


def test_stored_and_current_provider_id_collision_is_repaired_before_dispatch(
    handler: BotHandler,
) -> None:
    """Stored/current rule collisions do not become length feedback."""
    stored = DietaryRule(
        id="r1",
        source_text="eggs for breakfast",
        foods_any_of=["eggs"],
        meal_type="breakfast",
        count=1,
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="r1",
                    source_text=stored.source_text,
                    rule=stored,
                )
            ]
        }
    )
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "tofu for dinner",
                    "foods_any_of": ["tofu"],
                    "meal_type": "dinner",
                    "weekdays": [],
                    "operator": "at_least",
                    "count": 1,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="tofu for dinner",
            raw_update={"update_id": 9001},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    effective_ids = [rule.id for rule in saved.effective_rules]
    assert len(effective_ids) == len(set(effective_ids))
    assert handler.lambda_client.invoke.called
    assert "too long" not in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_profile_rule_confirmation_rejects_latest_constraint_conflict(
    handler: BotHandler,
) -> None:
    """A concurrent constraint makes a pending preference non-saveable."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": [
                ConstraintEntry(
                    id="constraint-eggs",
                    source_text="no eggs",
                    forbidden_terms=["eggs"],
                )
            ]
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_preference_interpretation("eggs for breakfast")
    )

    handler.handle_conversational(_profile_text("eggs for breakfast"))
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending
    handler.handle_callback(
        _profile_callback(
            "profile:confirm:"
            + json.loads(
                pending.last_update_id.removeprefix("profile-pending:")
            )["token"]
        )
    )

    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "conflicts" in message


def test_constraint_confirmation_reports_atomic_preference_removal(
    handler: BotHandler,
) -> None:
    """Constraint confirmation reports preferences removed by the guard."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.ADD,
    )
    preference_rule = DietaryRule(
        id="preference-eggs",
        source_text="eggs for breakfast",
        foods_any_of=["eggs"],
        meal_type="breakfast",
        count=1,
    )
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="preference-eggs",
                    source_text="eggs for breakfast",
                    rule=preference_rule,
                )
            ]
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("eggs")
    )

    handler.handle_conversational(_profile_text("no eggs"))
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending
    _confirm_pending_profile_rule(handler, pending)

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    assert saved.dietary_constraints
    assert saved.dietary_preferences
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "eggs for breakfast" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("John 1500", ("John", 1500, None, None)),
        ("John Smith 2000 1 1000", ("John Smith", 2000, 1, 1000)),
    ],
)
def test_profile_member_parser_accepts_legacy_and_full_target_forms(
    text: str, expected: tuple[str, int, int | None, int | None]
) -> None:
    """Member creation parses both supported suffix shapes safely."""
    assert BotHandler._parse_profile_name_calories(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "John 2000 1",
        "John 2000 protein 30",
        "John 2000 1 fibre",
        "John 2000 1 2 3",
        "John 2000 0 30",
        "John 2000 -1 30",
        "John 2000 1001 30",
        "John 2000 1 0",
        "John 2000 1 -1",
        "John 2000 1 1001",
    ],
)
def test_profile_member_parser_rejects_malformed_target_forms(
    text: str,
) -> None:
    """Partial, non-integer, and out-of-range targets are rejected."""
    assert BotHandler._parse_profile_name_calories(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sam 1", ("Sam", 1)),
        ("Sam 1000", ("Sam", 1000)),
        ("Sam none", ("Sam", None)),
        ("Sam NONE", ("Sam", None)),
    ],
)
def test_profile_target_parser_accepts_integer_or_case_insensitive_none(
    text: str, expected: tuple[str, int | None]
) -> None:
    """Single nutrient edits support bounded integers and exact ``none``."""
    assert BotHandler._parse_profile_target_change(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Sam none now",
        "Sam 1 2",
        "Sam protein",
        "Sam 0",
        "Sam -1",
        "Sam 1001",
    ],
)
def test_profile_target_parser_rejects_malformed_clear_and_values(
    text: str,
) -> None:
    """Malformed clear phrases and invalid target values are rejected."""
    assert BotHandler._parse_profile_target_change(text) is None


@pytest.mark.parametrize(
    ("category", "operation", "text", "expected"),
    [
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.ADD,
            "Taylor 1600",
            ["Alex", "Sam", "Taylor"],
        ),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.REMOVE,
            "sam",
            ["Alex"],
        ),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.CHANGE_CALORIES,
            "sam 2100",
            [2000, 2100],
        ),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.CHANGE_PROTEIN,
            "sam 1",
            [120, 1],
        ),
        (
            ProfileEditCategory.FAMILY,
            ProfileEditOperation.CHANGE_FIBRE,
            "alex NONE",
            [None, None],
        ),
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            ProfileEditOperation.REMOVE,
            "PEANUTS",
            [],
        ),
        (
            ProfileEditCategory.DIETARY_PREFERENCES,
            ProfileEditOperation.REMOVE,
            "BALANCED",
            [],
        ),
    ],
)
def test_profile_amendment_successes_return_to_category_menu(
    handler: BotHandler,
    category: ProfileEditCategory,
    operation: ProfileEditOperation,
    text: str,
    expected: list[Any],
) -> None:
    """Successful deterministic amendments save and render the category."""
    profile = make_profile(
        with_nutrient_targets=operation
        in {
            ProfileEditOperation.CHANGE_PROTEIN,
            ProfileEditOperation.CHANGE_FIBRE,
        }
    )
    if (
        operation is ProfileEditOperation.REMOVE
        and category is not ProfileEditCategory.FAMILY
    ):
        existing = {
            ProfileEditCategory.DIETARY_CONSTRAINTS: ["Peanuts"],
            ProfileEditCategory.DIETARY_PREFERENCES: ["balanced"],
        }[category]
        profile = profile.model_copy(update={category.value: existing})
    state = _profile_input_state(handler, category, operation)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True

    handler.handle_conversational(_profile_text(text))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    if category is ProfileEditCategory.FAMILY:
        if operation is ProfileEditOperation.CHANGE_CALORIES:
            assert [
                member.calorie_target for member in saved.family_members
            ] == expected
        elif operation is ProfileEditOperation.CHANGE_PROTEIN:
            assert [
                member.protein_target for member in saved.family_members
            ] == expected
        elif operation is ProfileEditOperation.CHANGE_FIBRE:
            assert [
                member.fibre_target for member in saved.family_members
            ] == expected
        else:
            assert [member.name for member in saved.family_members] == expected
    else:
        assert getattr(saved, category.value) == expected
    assert saved is not profile
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    transitioned = (
        handler.repo.save_profile_and_transition_state.call_args.args[2]
    )
    assert transitioned.step is ConversationWorkflowStep.PROFILE_MENU
    assert transitioned.profile_category is None
    assert transitioned.profile_operation is None
    handler.telegram_api.send_profile_category.assert_called_once_with(
        1, category
    )
    handler.llm_client.chat_sync.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "text", "message_part"),
    [
        (ProfileEditOperation.ADD, "Sam 1800", "already"),
        (ProfileEditOperation.CHANGE_CALORIES, "Unknown 1800", "find"),
        (ProfileEditOperation.CHANGE_PROTEIN, "Unknown 100", "find"),
        (ProfileEditOperation.CHANGE_FIBRE, "Unknown 100", "find"),
        (ProfileEditOperation.CHANGE_PROTEIN, "Sam none now", "format"),
        (ProfileEditOperation.CHANGE_FIBRE, "Sam 0", "format"),
        (ProfileEditOperation.REMOVE, "Unknown", "find"),
    ],
)
def test_family_profile_amendment_errors_do_not_write(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
    message_part: str,
) -> None:
    """Duplicate, missing, and malformed family edits are controlled."""
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_conversational(_profile_text(text))

    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert message_part in message
    handler.llm_client.chat_sync.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "text"),
    [
        (ProfileEditOperation.CHANGE_PROTEIN, "Sam 101"),
        (ProfileEditOperation.CHANGE_FIBRE, "Sam 20"),
    ],
)
def test_family_target_amendments_preserve_other_member_targets(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
) -> None:
    """Each nutrient edit changes only its selected target."""
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    profile = make_profile(
        with_nutrient_targets=operation
        in {
            ProfileEditOperation.CHANGE_PROTEIN,
            ProfileEditOperation.CHANGE_FIBRE,
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.handle_conversational(_profile_text(text))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    alex, sam = saved.family_members
    assert alex.calorie_target == 2000
    assert alex.protein_target == 120
    assert alex.fibre_target == 30
    assert sam.calorie_target == 1800
    assert sam.protein_target == (
        101 if operation is ProfileEditOperation.CHANGE_PROTEIN else 100
    )
    assert sam.fibre_target == (
        20 if operation is ProfileEditOperation.CHANGE_FIBRE else None
    )


def test_calorie_amendment_preserves_nutrient_targets(
    handler: BotHandler,
) -> None:
    """Changing calories does not reconstruct away optional targets."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile(
        with_nutrient_targets=True
    )
    handler.repo.save_profile_and_transition_state.return_value = True

    handler.handle_conversational(_profile_text("Alex 2100"))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    alex = saved.family_members[0]
    assert alex.calorie_target == 2100
    assert alex.protein_target == 120
    assert alex.fibre_target == 30


@pytest.mark.parametrize(
    ("operation", "text", "calories", "protein", "fibre"),
    [
        (
            ProfileEditOperation.CHANGE_CALORIES,
            "Child 1 1800",
            1800,
            120,
            30,
        ),
        (
            ProfileEditOperation.CHANGE_PROTEIN,
            "Child 1 150",
            2000,
            150,
            30,
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            "Child 1 40",
            2000,
            120,
            40,
        ),
    ],
)
def test_numeric_member_name_amendments_update_only_requested_target(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
    calories: int,
    protein: int,
    fibre: int,
) -> None:
    """Stored numeric-name identities resolve deterministic amendments."""
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    profile = UserProfile(
        name="Household",
        people_count=2,
        family_members=[
            FamilyMember(
                name="Child 1",
                calorie_target=2000,
                protein_target=120,
                fibre_target=30,
            ),
            FamilyMember(
                name="Alex",
                calorie_target=1900,
                protein_target=90,
                fibre_target=25,
            ),
        ],
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True

    handler.handle_conversational(_profile_text(text))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    child, alex = saved.family_members
    assert child.name == "Child 1"
    assert child.calorie_target == calories
    assert child.protein_target == protein
    assert child.fibre_target == fibre
    assert alex == profile.family_members[1]
    assert saved is not profile
    handler.repo.save_profile_and_transition_state.assert_called_once()
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "text", "expected_protein", "expected_fibre"),
    [
        (
            ProfileEditOperation.CHANGE_PROTEIN,
            "cHiLd 1 NoNe",
            None,
            30,
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            "CHILD 1 NONE",
            120,
            None,
        ),
    ],
)
def test_numeric_member_name_amendments_clear_target_case_insensitively(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
    expected_protein: int | None,
    expected_fibre: int | None,
) -> None:
    """Nutrient clear tokens work with case-insensitive numeric names."""
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    profile = UserProfile(
        name="Household",
        people_count=1,
        family_members=[
            FamilyMember(
                name="Child 1",
                calorie_target=2000,
                protein_target=120,
                fibre_target=30,
            )
        ],
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True

    handler.handle_conversational(_profile_text(text))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    member = saved.family_members[0]
    assert member.calorie_target == 2000
    assert member.protein_target == expected_protein
    assert member.fibre_target == expected_fibre
    handler.repo.save_profile_and_transition_state.assert_called_once()


@pytest.mark.parametrize(
    ("operation", "text", "message_part"),
    [
        (
            ProfileEditOperation.CHANGE_CALORIES,
            "Unknown 1 1800",
            "find",
        ),
        (
            ProfileEditOperation.CHANGE_PROTEIN,
            "Unknown 1 100",
            "find",
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            "Child 1 100 extra",
            "format",
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            "Child 1 100 20",
            "format",
        ),
        (
            ProfileEditOperation.CHANGE_CALORIES,
            "Child 1 none",
            "format",
        ),
        (
            ProfileEditOperation.CHANGE_CALORIES,
            "Child 1 10001",
            "format",
        ),
        (
            ProfileEditOperation.CHANGE_PROTEIN,
            "Child 1 1001",
            "format",
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            "Child 1 0",
            "format",
        ),
    ],
)
def test_numeric_member_name_amendment_errors_do_not_write(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
    message_part: str,
) -> None:
    """Unknown, malformed, and out-of-range numeric edits are controlled."""
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    profile = UserProfile(
        name="Household",
        people_count=1,
        family_members=[
            FamilyMember(
                name="Child 1",
                calorie_target=2000,
                protein_target=120,
                fibre_target=30,
            )
        ],
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile

    handler.handle_conversational(_profile_text(text))

    handler.repo.save_profile_and_transition_state.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert message_part in message
    handler.llm_client.chat_sync.assert_not_called()


def test_duplicate_numeric_member_identity_amendment_does_not_write(
    handler: BotHandler,
) -> None:
    """Case-folded duplicate numeric identities remain ambiguous."""
    profile = UserProfile(
        name="Household",
        people_count=2,
        family_members=[
            FamilyMember(name="Child 1", calorie_target=1800),
            FamilyMember(name=" child 1 ", calorie_target=1900),
        ],
    )
    state = _profile_input_state(
        handler,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_PROTEIN,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile

    handler.handle_conversational(_profile_text("CHILD 1 100"))

    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "ambiguous" in message


@pytest.mark.parametrize("text", ["New 1 1800", "New 1 1800 100 30"])
def test_add_member_numeric_name_grammar_remains_rejected(
    handler: BotHandler, text: str
) -> None:
    """Numeric tokens remain forbidden for deterministic member creation."""
    state = _profile_input_state(
        handler, ProfileEditCategory.FAMILY, ProfileEditOperation.ADD
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_conversational(_profile_text(text))

    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "format" in message


def test_family_removal_and_addition_preserve_other_members(
    handler: BotHandler,
) -> None:
    """Removing or adding a member leaves existing member data unchanged."""
    profile = make_profile(with_nutrient_targets=True)
    for operation, text in [
        (ProfileEditOperation.REMOVE, "Sam"),
        (ProfileEditOperation.ADD, "John Smith 1900 1 1000"),
    ]:
        state = _profile_input_state(
            handler, ProfileEditCategory.FAMILY, operation
        )
        handler.repo.reset_mock()
        handler.telegram_api.reset_mock()
        handler.repo.get_conversation_state.return_value = state
        handler.repo.get_profile.return_value = profile
        handler.repo.save_profile_and_transition_state.return_value = True

        handler.handle_conversational(_profile_text(text))

        saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
        alex = saved.family_members[0]
        assert alex.model_dump() == profile.family_members[0].model_dump()


def test_family_addition_with_full_targets_persists_all_values(
    handler: BotHandler,
) -> None:
    """The four-field add form stores calorie and nutrient targets."""
    state = _profile_input_state(
        handler, ProfileEditCategory.FAMILY, ProfileEditOperation.ADD
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.save_profile_and_transition_state.return_value = True

    handler.handle_conversational(_profile_text("John Smith 2000 120 30"))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    member = saved.family_members[-1]
    assert member.name == "John Smith"
    assert member.calorie_target == 2000
    assert member.protein_target == 120
    assert member.fibre_target == 30


def test_ambiguous_legacy_member_target_edit_does_not_write(
    handler: BotHandler,
) -> None:
    """Case-folded duplicate legacy names remain an explicit error."""
    profile = make_profile().model_copy(
        update={
            "family_members": [
                FamilyMember(name="Sam", calorie_target=1800),
                FamilyMember(name=" sam ", calorie_target=1900),
            ]
        }
    )
    state = _profile_input_state(
        handler,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_PROTEIN,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile

    handler.handle_conversational(_profile_text("Sam 100"))

    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "ambiguous" in message


def test_family_nutrient_operation_in_invalid_category_does_not_write(
    handler: BotHandler,
) -> None:
    """Family-only nutrient operations cannot mutate item categories."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.CHANGE_PROTEIN,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_conversational(_profile_text("Sam 100"))

    handler.repo.save_profile_and_transition_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "not available" in message


@pytest.mark.parametrize(
    "member_names",
    [
        ["Nick", "NICK"],
        ["Nick", " nick "],
        [" NICK ", "nick"],
    ],
)
def test_profile_onboarding_rejects_duplicate_member_identities(
    handler: BotHandler, member_names: list[str]
) -> None:
    """Onboarding rejects names equal after stripping and case-folding."""
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "name": "Household",
            "people_count": 2,
            "family_members": [
                {"name": member_name, "calorie_target": 1800}
                for member_name in member_names
            ],
            "dietary_constraints": [],
            "dietary_preferences": [],
            "goals": [],
        },
        None,
    )

    assert not result.success
    assert result.message and "unique" in result.message.lower()
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.delete_profile_draft.assert_not_called()


def test_profile_replacement_rejects_duplicate_member_identities(
    handler: BotHandler,
) -> None:
    """A family replacement cannot introduce duplicate member identities."""
    existing = make_profile()
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "people_count": 2,
            "family_members": [
                {"name": "Nick", "calorie_target": 1800},
                {"name": " nick ", "calorie_target": 2000},
            ],
        },
        existing,
    )

    assert not result.success
    assert result.message and "unique" in result.message.lower()
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.delete_profile_draft.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "text"),
    [
        (ProfileEditOperation.REMOVE, "nick"),
        (ProfileEditOperation.CHANGE_CALORIES, "NICK 2100"),
    ],
)
def test_legacy_duplicate_member_amendments_are_controlled(
    handler: BotHandler,
    operation: ProfileEditOperation,
    text: str,
) -> None:
    """Name edits do not choose arbitrarily in a legacy duplicate profile."""
    profile = make_profile().model_copy(
        update={
            "family_members": [
                FamilyMember(name="Nick", calorie_target=1800),
                FamilyMember(name="NICK", calorie_target=2000),
            ],
            "people_count": 2,
        }
    )
    state = _profile_input_state(handler, ProfileEditCategory.FAMILY, operation)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile

    handler.handle_conversational(_profile_text(text))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "ambiguous" in message
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()


def test_unique_member_match_is_case_insensitive_and_preserves_display_name(
    handler: BotHandler,
) -> None:
    """A unique match ignores case while retaining the stored spelling."""
    profile = UserProfile(
        name="Household",
        people_count=1,
        family_members=[FamilyMember(name="Nick", calorie_target=1800)],
    )

    updated, message = handler._apply_profile_amendment(
        profile,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
        " nIcK 2100 ",
    )

    assert updated is not None
    assert updated.family_members[0].name == "Nick"
    assert updated.family_members[0].calorie_target == 2100
    assert "Nick" in message


def test_family_addition_preserves_display_spelling(
    handler: BotHandler,
) -> None:
    """A new member keeps the display spelling supplied by the user."""
    profile = make_profile()

    updated, _ = handler._apply_profile_amendment(
        profile,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.ADD,
        "  nIcKy 1600 ",
    )

    assert updated is not None
    assert updated.family_members[-1].name == "nIcKy"


@pytest.mark.parametrize(
    ("category", "text"),
    [
        (ProfileEditCategory.DIETARY_CONSTRAINTS, "dairy"),
        (ProfileEditCategory.DIETARY_PREFERENCES, "Mediterranean"),
    ],
)
def test_unrelated_amendment_is_allowed_on_legacy_duplicate_profile(
    handler: BotHandler,
    category: ProfileEditCategory,
    text: str,
) -> None:
    """Legacy duplicate names do not block unrelated profile amendments."""
    profile = make_profile().model_copy(
        update={
            "family_members": [
                FamilyMember(name="Nick", calorie_target=1800),
                FamilyMember(name="NICK", calorie_target=2000),
            ],
            "people_count": 2,
        }
    )
    state = _profile_input_state(
        handler,
        category,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = json.loads(
        json.dumps(
            {
                "mode": (
                    "constraint"
                    if category is ProfileEditCategory.DIETARY_CONSTRAINTS
                    else "stored_preference"
                ),
                "requirements": []
                if category is ProfileEditCategory.DIETARY_CONSTRAINTS
                else [
                    {
                        "id": "preference-1",
                        "source_text": text,
                        "foods_any_of": [text],
                        "meal_type": None,
                        "count": 1,
                        "operator": "exactly",
                        "strength": "strict",
                    }
                ],
                "exclusions": [
                    {
                        "id": "constraint-1",
                        "source_text": text,
                        "forbidden_terms": [text],
                    }
                ]
                if category is ProfileEditCategory.DIETARY_CONSTRAINTS
                else [],
                "clarification": None,
                "unparsed_text": [],
            }
        )
    )

    handler.handle_conversational(_profile_text(text))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.last_update_id is not None
    assert saved.last_update_id.startswith("profile-pending:")
    handler.repo.save_profile_and_transition_state.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_called_once()


@pytest.mark.parametrize(
    ("category", "operation", "text"),
    [
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            ProfileEditOperation.ADD,
            "none",
        ),
        (
            ProfileEditCategory.DIETARY_PREFERENCES,
            ProfileEditOperation.REMOVE,
            "missing",
        ),
    ],
)
def test_item_profile_amendment_errors_do_not_write(
    handler: BotHandler,
    category: ProfileEditCategory,
    operation: ProfileEditOperation,
    text: str,
) -> None:
    """Item grammar rejects duplicate, missing, and empty values safely."""
    state = _profile_input_state(handler, category, operation)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_conversational(_profile_text(text))

    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    assert handler.telegram_api.send_message.call_count == 1
    handler.llm_client.chat_sync.assert_not_called()


def test_profile_amendment_rejects_malformed_calories_and_last_member(
    handler: BotHandler,
) -> None:
    """Malformed calorie input and removing the final member are safe."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()

    handler.handle_conversational(_profile_text("Sam many"))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "format" in message
    handler.repo.save_profile.assert_not_called()

    handler.telegram_api.send_message.reset_mock()
    one_member = make_profile().model_copy(
        update={
            "family_members": [make_profile().family_members[0]],
            "people_count": 1,
        }
    )
    handler.repo.get_profile.return_value = one_member
    remove_state = _profile_input_state(
        handler, ProfileEditCategory.FAMILY, ProfileEditOperation.REMOVE
    )
    handler.repo.get_conversation_state.return_value = remove_state

    handler.handle_conversational(_profile_text("Alex"))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "at least one" in message
    handler.repo.save_profile.assert_not_called()


def test_profile_amendment_uses_atomic_transaction_path(
    handler: BotHandler,
) -> None:
    """Profile amendments use the atomic transaction path."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.ADD,
    )
    profile = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("dairy")
    )

    handler.handle_conversational(_profile_text("dairy"))

    handler.repo.transition_conversation_state.assert_called_once()
    handler.repo.save_profile.assert_not_called()


def test_confirmed_batch_rule_is_saved_and_reused_by_later_plan(
    handler: BotHandler,
) -> None:
    """A confirmed batch rule survives reload and reaches /plan typed."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    profile = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_json_strict_sync.return_value = {
        "mode": "stored_preference",
        "requirements": [],
        "batch_rules": [
            {
                "id": "provider-batch",
                "source_text": "cook chicken for two dinners",
                "foods_any_of": ["chicken"],
                "preparation_meal_types": ["dinner"],
                "reuse_meal_types": ["dinner"],
                "total_yield": 2,
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    handler.handle_conversational(_profile_text("cook chicken for two dinners"))

    pending = handler.repo.transition_conversation_state.call_args.args[1]
    assert pending.last_update_id is not None
    token = json.loads(pending.last_update_id.split("profile-pending:", 1)[1])[
        "token"
    ]
    handler.repo.get_conversation_state.return_value = pending
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.handle_callback(_profile_callback(f"profile:confirm:{token}"))

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    assert len(saved.batch_rules) == 1
    assert saved.batch_rules[0].total_yield == 2
    assert saved.batch_rules[0].source_text == ("cook chicken for two dinners")

    handler.repo.get_profile.return_value = saved
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.handle_conversational(_plan_route("no preference", 7001))

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["batch_rules"] == [
        saved.batch_rules[0].model_dump(mode="json")
    ]
    assert handler.llm_client.chat_json_strict_sync.call_count == 1


def test_mixed_profile_confirmation_saves_food_and_batch_rules_atomically(
    handler: BotHandler,
) -> None:
    """One confirmation commits all validated preference rule types."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.ADD,
    )
    profile = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = profile
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = {
        "mode": "stored_preference",
        "requirements": [
            {
                "id": "provider-preference",
                "source_text": "eggs for breakfast",
                "foods_any_of": ["eggs"],
                "meal_type": "breakfast",
                "weekdays": [],
                "operator": "at_least",
                "count": 1,
                "strength": "strict",
            }
        ],
        "batch_rules": [
            {
                "id": "provider-batch",
                "source_text": "cook chicken for two dinners",
                "foods_any_of": ["chicken"],
                "preparation_meal_types": ["dinner"],
                "reuse_meal_types": ["dinner"],
                "total_yield": 2,
            }
        ],
        "exclusions": [],
        "clarification": None,
        "unparsed_text": [],
    }

    handler.handle_conversational(
        _profile_text("eggs for breakfast and cook chicken for two dinners")
    )
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending
    _confirm_pending_profile_rule(handler, pending)

    saved = handler.repo.save_profile_and_transition_state.call_args.args[1]
    assert len(saved.dietary_preferences) == 1
    assert len(saved.batch_rules) == 1
    assert saved.dietary_preferences[0].rule.foods_any_of == ["eggs"]
    assert saved.batch_rules[0].foods_any_of == ["chicken"]


def test_profile_amendments_preserve_and_remove_batch_rules(
    handler: BotHandler,
) -> None:
    """Unrelated edits preserve a batch rule and its removal is explicit."""
    rule = make_batch_rule()
    profile = make_profile().model_copy(update={"batch_rules": [rule]})

    updated, _ = handler._apply_profile_amendment(
        profile,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
        "Alex 2100",
    )
    assert updated is not None
    assert updated.batch_rules == [rule]

    removed, message = handler._apply_profile_amendment(
        updated,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.REMOVE,
        rule.source_text,
    )
    assert removed is not None
    assert removed.batch_rules == []
    assert rule.source_text in message


def test_profile_amendment_requests_consistent_profile_read(
    handler: BotHandler,
) -> None:
    """Profile amendments derive replacements from a consistent read."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.save_profile_and_transition_state.return_value = True
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("dairy")
    )

    handler.handle_conversational(_profile_text("dairy"))

    handler.repo.get_profile.assert_not_called()


def test_profile_amendment_conflict_does_not_claim_profile_changed(
    handler: BotHandler,
) -> None:
    """A stale edit sends no success message or category rendering."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.save_profile_and_transition_state.return_value = False
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("dairy")
    )

    handler.handle_conversational(_profile_text("dairy"))

    handler.telegram_api.send_profile_rule_review.assert_called_once()
    handler.telegram_api.send_profile_category.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_called_once()


def test_profile_amendment_unexpected_save_failure_is_not_partial_success(
    handler: BotHandler,
) -> None:
    """An unexpected transaction error uses a generic save failure message."""
    state = _profile_input_state(
        handler,
        ProfileEditCategory.DIETARY_CONSTRAINTS,
        ProfileEditOperation.ADD,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.save_profile_and_transition_state.side_effect = RuntimeError(
        "dynamodb unavailable"
    )
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("dairy")
    )

    handler.handle_conversational(_profile_text("dairy"))

    handler.telegram_api.send_profile_rule_review.assert_called_once()
    handler.telegram_api.send_profile_category.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.transition_conversation_state.assert_called_once()


def test_sequential_profile_amendments_preserve_prior_changes(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """A strongly consistent amendment read preserves the prior amendment."""
    handler, repo = real_profile_handler
    initial_profile = make_profile()
    repo.save_profile("user", initial_profile, expected_revision=None)
    transaction = mocker.spy(repo.table.meta.client, "transact_write_items")
    amendment_transaction = mocker.spy(
        repo, "save_profile_and_transition_state"
    )

    handler.handle_command(_command("profile"))
    handler.handle_callback(
        _profile_callback("profile:category:dietary_constraints")
    )
    handler.handle_callback(
        _profile_callback("profile:operation:dietary_constraints:add")
    )
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("peanuts")
    )
    handler.handle_conversational(_profile_text("peanuts"))
    pending_state = repo.get_conversation_state("user", consistent_read=True)
    assert pending_state is not None
    _confirm_pending_profile_rule(handler, pending_state)

    after_amendment_a = repo.get_profile("user", consistent_read=True)
    assert after_amendment_a is not None
    assert [
        entry.source_text for entry in after_amendment_a.dietary_constraints
    ] == ["peanuts"]
    state_after_amendment_a = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert state_after_amendment_a is not None
    assert state_after_amendment_a.step is ConversationWorkflowStep.PROFILE_MENU

    final_profile = repo.get_profile("user", consistent_read=True)
    assert final_profile is not None
    assert final_profile.name == initial_profile.name
    assert final_profile.people_count == initial_profile.people_count
    assert final_profile.family_members == initial_profile.family_members
    assert final_profile.dietary_preferences == (
        canonicalize_profile_rule_ids(initial_profile).dietary_preferences
    )
    assert [
        entry.source_text for entry in final_profile.dietary_constraints
    ] == ["peanuts"]
    final_state = repo.get_conversation_state("user", consistent_read=True)
    assert final_state is not None
    assert final_state.step is ConversationWorkflowStep.PROFILE_MENU
    assert final_state.profile_category is None
    assert final_state.profile_operation is None
    assert amendment_transaction.call_count == 1
    profile_puts = [
        item
        for call in transaction.call_args_list
        for item in call.kwargs["TransactItems"]
        if item.get("Put", {}).get("Item", {}).get("SK") == "PROFILE"
    ]
    assert len(profile_puts) == 1
    handler.llm_client.chat_json_strict_sync.assert_called_once()


def test_profile_amendment_full_repository_flow_is_deterministic(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """The complete profile flow writes once and never invokes the LLM."""
    handler, repo = real_profile_handler
    profile = make_profile()
    repo.save_profile("user", profile, expected_revision=None)
    transaction = mocker.spy(repo.table.meta.client, "transact_write_items")

    handler.handle_command(_command("profile"))
    assert repo.get_conversation_state("user").step is (
        ConversationWorkflowStep.PROFILE_MENU
    )
    handler.handle_callback(
        _profile_callback("profile:category:dietary_constraints")
    )
    handler.handle_callback(
        _profile_callback("profile:operation:dietary_constraints:add")
    )
    handler.llm_client.chat_json_strict_sync.return_value = (
        _strict_constraint_interpretation("peanuts")
    )
    handler.handle_conversational(_profile_text("peanuts"))
    pending_state = repo.get_conversation_state("user", consistent_read=True)
    assert pending_state is not None
    _confirm_pending_profile_rule(handler, pending_state)

    updated = repo.get_profile("user")
    assert updated is not None
    assert [entry.source_text for entry in updated.dietary_constraints] == [
        "peanuts"
    ]
    menu_state = repo.get_conversation_state("user")
    assert menu_state is not None
    assert menu_state.step is ConversationWorkflowStep.PROFILE_MENU
    assert menu_state.profile_category is None
    assert menu_state.profile_operation is None
    assert (
        len(
            [
                item
                for call in transaction.call_args_list
                for item in call.kwargs["TransactItems"]
                if item.get("Put", {}).get("Item", {}).get("SK") == "PROFILE"
            ]
        )
        == 1
    )
    handler.handle_callback(_profile_callback("profile:done"))
    assert repo.get_conversation_state("user") is None


def test_plan_command_collects_preference_before_generation(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = make_profile()
    handler.handle_command(_command("plan"))
    handler.lambda_client.invoke.assert_not_called()
    state = handler.repo.save_conversation_state.call_args.args[1]
    assert state.step.value == "awaiting_preference"
    assert "preference" in handler.telegram_api.send_message.call_args.args[1]


def test_submit_meals_starts_guided_logging_without_active_plan(
    handler: BotHandler,
) -> None:
    """Meal logging starts with history and one structured input prompt."""
    handler.repo.get_conversation_state.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command("submit_meals"))

    handler.repo.get_active_plan.assert_not_called()
    handler.repo.get_meal_history.assert_called_once_with(
        "user", days=2, on_date=date.today()
    )
    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.workflow_kind is ConversationWorkflowKind.MEAL_LOG
    assert saved.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT
    assert saved.meal_draft == MealLogDraft()
    assert saved.request_id
    assert handler.telegram_api.send_message.call_count == 2
    messages = [
        call.args[1]
        for call in handler.telegram_api.send_message.call_args_list
    ]
    assert "Today" in messages[0]
    assert "No meals submitted." in messages[0]
    assert "Yesterday" in messages[0]
    assert "YYYY-MM-DD" in messages[1]
    assert "today" in messages[1].lower()
    assert "yesterday" in messages[1].lower()
    assert "breakfast" in messages[1].lower()
    assert "lunch" in messages[1].lower()
    assert "snack" in messages[1].lower()
    assert "dinner" in messages[1].lower()


def test_submit_meals_groups_history_in_recent_first_order(
    handler: BotHandler,
) -> None:
    reference = date(2026, 8, 22)
    handler.repo.get_conversation_state.return_value = None
    handler.repo.save_conversation_state.return_value = True
    handler.repo.get_meal_history.return_value = [
        MealLogEntry(
            date=reference - timedelta(days=1),
            meal_type=MealType.DINNER,
            description="Yesterday dinner",
            created_at=datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
        ),
        MealLogEntry(
            date=reference,
            meal_type=MealType.BREAKFAST,
            description="Today breakfast",
            created_at=datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
        ),
        MealLogEntry(
            date=reference,
            meal_type=MealType.LUNCH,
            description="Today lunch",
            created_at=datetime(2026, 8, 22, 3, tzinfo=timezone.utc),
        ),
    ]
    command = RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command="submit_meals",
        raw_update={"message": {"date": 1787356800}},
    )

    handler.handle_command(command)

    history = handler.telegram_api.send_message.call_args_list[0].args[1]
    assert history.index("Today") < history.index("Yesterday")
    assert history.index("Today lunch") < history.index("Today breakfast")
    assert "Today dinner" not in history
    assert "Yesterday dinner" in history
    handler.repo.get_meal_history.assert_called_once_with(
        "user", days=2, on_date=reference
    )


def test_submit_meals_uses_utc_date_at_midnight(
    handler: BotHandler,
) -> None:
    handler.repo.get_conversation_state.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.repo.save_conversation_state.return_value = True
    reference = datetime(2026, 8, 23, tzinfo=timezone.utc)
    command = RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command="submit_meals",
        raw_update={"message": {"date": int(reference.timestamp())}},
    )

    handler.handle_command(command)

    handler.repo.get_meal_history.assert_called_once_with(
        "user", days=2, on_date=date(2026, 8, 23)
    )


@pytest.mark.parametrize(
    "raw_update",
    [
        {"message": {}},
        {"message": {"date": "not-a-timestamp"}},
        {"message": {"date": None}},
    ],
)
def test_submit_meals_uses_utc_processing_date_for_missing_or_invalid_timestamp(
    handler: BotHandler, raw_update: dict[str, Any], mocker: Any
) -> None:
    handler.repo.get_conversation_state.return_value = None
    handler.repo.get_meal_history.return_value = []
    handler.repo.save_conversation_state.return_value = True
    current = datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc)
    handler_datetime = mocker.patch("meal_planner.bot_handler.datetime")
    handler_datetime.now.return_value = current
    handler_datetime.side_effect = datetime
    command = RouteResult(
        route_type=RouteType.COMMAND,
        chat_id=1,
        user_id="user",
        command="submit_meals",
        raw_update=raw_update,
    )

    handler.handle_command(command)

    handler.repo.get_meal_history.assert_called_once_with(
        "user", days=2, on_date=date(2026, 8, 22)
    )


def test_submit_meals_reports_replaced_workflow(
    handler: BotHandler,
) -> None:
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.repo.get_meal_history.return_value = []
    handler.repo.save_conversation_state.return_value = True

    handler.handle_command(_command("submit_meals"))

    messages = [
        call.args[1]
        for call in handler.telegram_api.send_message.call_args_list
    ]
    assert len(messages) == 2
    assert any("replaced" in message.lower() for message in messages)


def test_submit_meals_reports_state_contention_without_sending_prompt(
    handler: BotHandler,
) -> None:
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.repo.save_conversation_state.return_value = False

    handler.handle_command(_command("submit_meals"))

    handler.repo.get_meal_history.assert_not_called()
    handler.telegram_api.send_message.assert_called_once()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "changed" in message.lower()
    assert "try again" in message.lower()


@pytest.mark.parametrize(
    ("submitted", "expected_date", "expected_type"),
    [
        ("today, breakfast, oatmeal", date(2026, 8, 22), MealType.BREAKFAST),
        ("YESTERDAY, Lunch, soup", date(2026, 8, 21), MealType.LUNCH),
        (
            "2026-08-16, SNACK, fruit, with yogurt",
            date(2026, 8, 16),
            MealType.SNACK,
        ),
        ("2026-08-22, dinner, pasta", date(2026, 8, 22), MealType.DINNER),
    ],
)
def test_structured_meal_input_reviews_without_persisting(
    handler: BotHandler,
    submitted: str,
    expected_date: date,
    expected_type: MealType,
) -> None:
    state = handler._new_submission_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    reference = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=submitted,
        raw_update={"message": {"date": int(reference.timestamp())}},
    )

    handler.handle_conversational(route)

    candidate = handler.repo.transition_conversation_state.call_args.args[1]
    assert candidate.step is ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
    assert candidate.meal_draft is not None
    assert candidate.meal_draft.date == expected_date
    assert candidate.meal_draft.meal_type is expected_type
    assert (
        candidate.meal_draft.description == submitted.split(",", 2)[2].strip()
    )
    handler.repo.transition_conversation_state.assert_called_once_with(
        "user", candidate, expected_revision=state.revision
    )
    handler.telegram_api.send_meal_review.assert_called_once_with(
        1, submitted, state.request_id
    )
    handler.repo.log_meal.assert_not_called()
    handler.repo.log_meal_and_transition.assert_not_called()
    handler.repo.get_profile.assert_not_called()
    handler.repo.get_profile_draft.assert_not_called()
    handler.repo.get_latest_plan.assert_not_called()
    handler.repo.get_meal_history.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()


def test_structured_batch_meal_input_stages_one_explicit_role_confirmation(
    handler: BotHandler,
) -> None:
    """A planned batch match asks for the application-owned role explicitly."""
    state = handler._new_submission_state()
    link = PlannedBatchLink(
        batch_id="batch-1",
        role="preparation",
        total_yield=2,
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_planned_batch_link.return_value = link
    handler.repo.transition_conversation_state.return_value = True
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="2026-08-22, dinner, roast chicken",
        raw_update={
            "message": {
                "date": int(
                    datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp()
                )
            }
        },
    )

    handler.handle_conversational(route)

    candidate = handler.repo.transition_conversation_state.call_args.args[1]
    assert candidate.pending_batch_link == link
    handler.telegram_api.send_meal_review.assert_called_once_with(
        1,
        "2026-08-22, dinner, roast chicken",
        state.request_id,
        batch_link=link,
    )


def test_structured_unrelated_meal_keeps_pending_batch_link_empty(
    handler: BotHandler,
) -> None:
    """An unrelated meal follows the ordinary review workflow unchanged."""
    state = handler._new_submission_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.get_planned_batch_link.return_value = None
    handler.repo.transition_conversation_state.return_value = True
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="2026-08-22, breakfast, oatmeal",
        raw_update={
            "message": {
                "date": int(
                    datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp()
                )
            }
        },
    )

    handler.handle_conversational(route)

    candidate = handler.repo.transition_conversation_state.call_args.args[1]
    assert candidate.pending_batch_link is None
    handler.telegram_api.send_meal_review.assert_called_once_with(
        1, "2026-08-22, breakfast, oatmeal", state.request_id
    )


@pytest.mark.parametrize(
    ("submitted", "explanation"),
    [
        ("today, lunch", "two commas"),
        ("2026-08-23, lunch, soup", "future"),
        ("2026-08-15, lunch, soup", "last 7 days"),
        ("2026-99-01, lunch, soup", "real calendar date"),
        ("today, brunch, soup", "meal type"),
        ("today, lunch, ", "description is required"),
    ],
)
def test_invalid_structured_meal_input_explains_error_and_repeats_prompt(
    handler: BotHandler,
    submitted: str,
    explanation: str,
) -> None:
    state = handler._new_submission_state()
    handler.repo.get_conversation_state.return_value = state
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=submitted,
        raw_update={
            "message": {
                "date": int(
                    datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp()
                )
            }
        },
    )

    handler.handle_conversational(route)

    message = handler.telegram_api.send_message.call_args.args[1]
    assert explanation.lower() in message.lower()
    assert "Submit one meal using this format:" in message
    assert "today or yesterday" in message
    assert "breakfast, lunch, snack, or dinner" in message
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.log_meal.assert_not_called()
    assert handler.repo.get_profile.call_count == 0
    handler.repo.get_profile_draft.assert_not_called()
    handler.repo.get_latest_plan.assert_not_called()
    handler.repo.get_meal_history.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()


def test_overlong_structured_meal_input_reports_specific_error_and_prompt(
    handler: BotHandler,
) -> None:
    state = handler._new_submission_state()
    handler.repo.get_conversation_state.return_value = state
    submitted = f"today, lunch, {'x' * 501}"
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=submitted,
        raw_update={
            "message": {
                "date": int(
                    datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp()
                )
            }
        },
    )

    handler.handle_conversational(route)

    message = handler.telegram_api.send_message.call_args.args[1]
    error, prompt = message.split("\n\n", maxsplit=1)
    assert error == "description must be 500 characters or fewer"
    assert prompt == MEAL_INPUT_PROMPT
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.log_meal.assert_not_called()
    handler.repo.log_meal_and_transition.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()


def test_structured_input_uses_message_timestamp_for_utc_date_reference(
    handler: BotHandler,
) -> None:
    state = handler._new_submission_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    reference = datetime(2026, 8, 22, 23, 59, tzinfo=timezone.utc)
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="2026-08-16, lunch, soup",
        raw_update={"message": {"date": int(reference.timestamp())}},
    )

    handler.handle_conversational(route)

    candidate = handler.repo.transition_conversation_state.call_args.args[1]
    assert candidate.meal_draft is not None
    assert candidate.meal_draft.date == date(2026, 8, 16)


def test_legacy_meal_state_is_conditionally_cleared_and_requires_restart(
    handler: BotHandler,
) -> None:
    state = handler._new_meal_state()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="lunch, soup",
    )

    handler.handle_conversational(route)

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user", expected_revision=state.revision
    )
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "/submit_meals" in message
    assert "restart" in message.lower()
    handler.repo.get_profile.assert_not_called()
    handler.repo.get_meal_history.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()


def test_guided_meal_workflow_revalidates_draft_across_separate_updates(
    handler: BotHandler,
) -> None:
    """Date, type, and description replies produce one valid meal write."""
    state = handler._new_meal_state()
    today = date.today().isoformat()

    assert (
        "breakfast"
        in handler._handle_meal_workflow(
            1,
            "user",
            today,
            state,
            {"date": today},
            source_update_id="1",
        ).lower()
    )
    state = handler.repo.transition_conversation_state.call_args.args[1]

    assert (
        "describe"
        in handler._handle_meal_workflow(
            1,
            "user",
            "lunch",
            state,
            {"meal_type": "lunch"},
            source_update_id="2",
        ).lower()
    )
    state = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.log_meal_and_transition.return_value = True

    reply = handler._handle_meal_workflow(
        1,
        "user",
        "Soup",
        state,
        {"description": "Soup"},
        source_update_id="3",
    )

    assert reply.startswith("Meal logged.")
    handler.repo.log_meal_and_transition.assert_called_once()
    entry = handler.repo.log_meal_and_transition.call_args.args[1]
    assert entry.date == date.today()
    assert entry.meal_type.value == "lunch"
    assert entry.description == "Soup"
    handler.repo.log_meal.assert_not_called()


def test_guided_meal_workflow_invalid_type_preserves_saved_draft(
    handler: BotHandler,
) -> None:
    """An invalid replacement field does not corrupt earlier draft fields."""
    draft = MealLogDraft(date=date.today())
    state = handler._new_meal_state().model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_TYPE,
            "meal_draft": draft,
        }
    )

    reply = handler._handle_meal_workflow(
        1,
        "user",
        "brunch",
        state,
        {"meal_type": "brunch"},
        source_update_id="1",
    )

    assert "didn't recognize" in reply
    assert state.meal_draft == draft
    handler.repo.transition_conversation_state.assert_not_called()


@pytest.mark.parametrize("revision", [0, 4])
def test_replacing_conversation_state_increments_revision(
    handler: BotHandler, revision: int
) -> None:
    previous = handler._new_meal_state().model_copy(
        update={"revision": revision}
    )
    replacement = handler._new_plan_state()
    handler.repo.save_conversation_state.return_value = True

    assert handler._replace_conversation_state("user", replacement, previous)

    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.revision == revision + 1
    assert (
        handler.repo.save_conversation_state.call_args.kwargs[
            "expected_revision"
        ]
        == revision
    )


def test_competing_completed_drafts_have_one_revision_winner(
    handler: BotHandler,
) -> None:
    """Only the atomic state winner can persist a completed draft."""
    state = handler._new_meal_state().model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_DESCRIPTION,
            "meal_draft": MealLogDraft(date=date.today(), meal_type="lunch"),
        }
    )
    handler.repo.log_meal_and_transition.side_effect = [True, False]
    entities = {"description": "Soup"}

    first = handler._handle_meal_workflow(
        1, "user", "Soup", state, entities, source_update_id="1"
    )
    second = handler._handle_meal_workflow(
        1,
        "user",
        "Salad",
        state,
        {"description": "Salad"},
        source_update_id="2",
    )

    assert first.startswith("Meal logged.")
    assert "workflow changed" in second
    assert handler.repo.log_meal_and_transition.call_count == 2
    handler.repo.log_meal.assert_not_called()


def test_plan_preference_is_invoked_once_with_request_context(
    handler: BotHandler,
) -> None:
    """The preference reply creates one planner event with stable context."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "Indian and pasta",
                    "foods_any_of": ["Indian", "pasta"],
                    "meal_type": None,
                    "exact_count": 1,
                }
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = None
    handler.handle_command(_command("plan"))
    state = handler.repo.save_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = state
    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="Indian and pasta",
            raw_update={"update_id": 55},
        )
    )
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["preference"] == "Indian and pasta"
    assert payload["requirements"] == []
    assert len(payload["effective_rules"]) == 1
    assert payload["effective_rules"][0]["foods_any_of"] == [
        "Indian",
        "pasta",
    ]
    assert payload["effective_rules"][0]["count"] == 1
    assert payload["effective_rules"][0]["id"].startswith("r-current-")
    assert payload["request_id"] == state.request_id
    assert payload["state_revision"] == 1
    assert payload["attempt"] == 1
    assert payload["repair_feedback"] is None


def test_no_preference_projects_typed_rules_and_submitted_evidence(
    handler: BotHandler,
) -> None:
    """No-preference planning uses typed rules and bounded meal evidence."""
    rule = DietaryRule(
        id="saved-eggs-rule",
        source_text="eggs three breakfasts",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        operator=RuleOperator.AT_LEAST,
        count=3,
    )
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="saved-eggs",
                    source_text=rule.source_text,
                    rule=rule,
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.get_submitted_meals.return_value = [
        MealLogEntry(
            date=date.today() - timedelta(days=2),
            meal_type=MealType.BREAKFAST,
            description="eggs and toast",
            created_at=datetime.now(timezone.utc),
        )
    ]

    handler.handle_conversational(_plan_route("1, no preference", 6100))

    handler.llm_client.chat_sync.assert_not_called()
    handler.repo.get_submitted_meals.assert_called_once()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_start == date.today()
    assert len(saved.obligations) == 1
    assert saved.obligations[0].count == 1
    assert saved.obligations[0].evidence_ids
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["obligations"] == [
        obligation.model_dump(mode="json") for obligation in saved.obligations
    ]


def test_no_preference_crossing_sunday_projects_independent_week_obligations(
    handler: BotHandler,
) -> None:
    """A Sunday-to-Monday request never merges ISO-week obligations."""
    rule = DietaryRule(
        id="weekly-eggs",
        source_text="eggs weekly",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        operator=RuleOperator.AT_LEAST,
        count=1,
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="weekly-eggs-entry",
                    source_text=rule.source_text,
                    rule=rule,
                )
            ]
        }
    )
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.get_submitted_meals.return_value = []

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="2, no preference",
            raw_update={
                "update_id": 6103,
                "message": {
                    "date": int(
                        datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp()
                    )
                },
            },
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert [item.iso_week for item in saved.obligations] == [
        "2026-W35",
        "2026-W36",
    ]
    assert all(
        item.horizon_start <= item.horizon_end for item in saved.obligations
    )


def test_fresh_plan_recalculates_obligations_after_submitted_meal(
    handler: BotHandler,
) -> None:
    """A new request sees newly persisted evidence while retry does not."""
    rule = DietaryRule(
        id="weekly-eggs",
        source_text="eggs three breakfasts",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        operator=RuleOperator.AT_LEAST,
        count=3,
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="weekly-eggs-entry",
                    source_text=rule.source_text,
                    rule=rule,
                )
            ]
        }
    )
    first_state = BotHandler._new_plan_state()
    second_state = BotHandler._new_plan_state()
    handler.repo.get_conversation_state.side_effect = [
        first_state,
        second_state,
    ]
    handler.repo.get_submitted_meals.side_effect = [
        [],
        [
            MealLogEntry(
                date=date.today() - timedelta(days=2),
                meal_type=MealType.BREAKFAST,
                description="eggs on toast",
                created_at=datetime.now(timezone.utc),
            ),
            MealLogEntry(
                date=date.today() - timedelta(days=1),
                meal_type=MealType.BREAKFAST,
                description="eggs and fruit",
                created_at=datetime.now(timezone.utc),
            ),
        ],
    ]

    handler.handle_conversational(_plan_route("1, no preference", 6104))
    first_saved = handler.repo.transition_conversation_state.call_args.args[1]
    handler.handle_conversational(_plan_route("1, no preference", 6105))
    second_saved = handler.repo.transition_conversation_state.call_args.args[1]

    assert first_saved.obligations[0].count == 1
    assert second_saved.obligations == []
    assert handler.repo.get_submitted_meals.call_count == 2


def test_constraints_remain_independent_from_projected_obligations(
    handler: BotHandler,
) -> None:
    """Constraints are forwarded unchanged and never become quota entries."""
    constraint = ConstraintEntry(
        id="no-mushrooms",
        source_text="no mushrooms",
        forbidden_terms=["mushroom"],
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={"dietary_constraints": [constraint]}
    )
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.get_submitted_meals.return_value = []

    handler.handle_conversational(_plan_route("1, no preference", 6106))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert saved.constraint_rules[0].forbidden_terms == ["mushroom"]
    assert saved.obligations == []
    assert payload["constraint_rules"] == [
        saved.constraint_rules[0].model_dump(mode="json")
    ]
    assert payload["obligations"] == []


def test_duplicate_plan_update_does_not_recalculate_or_invoke(
    handler: BotHandler,
) -> None:
    """A duplicate Telegram update has no planner or history side effects."""
    state = BotHandler._new_plan_state().model_copy(
        update={"last_update_id": "6101"}
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(_plan_route("1, no preference", 6101))

    handler.repo.get_profile.assert_not_called()
    handler.repo.get_submitted_meals.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    assert "continue" in handler.telegram_api.send_message.call_args.args[1]


def test_plan_state_revision_race_suppresses_planner_invocation(
    handler: BotHandler,
) -> None:
    """A losing state transition cannot start asynchronous plan work."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = False

    handler.handle_conversational(_plan_route("1, no preference", 6107))

    handler.lambda_client.invoke.assert_not_called()
    assert "changed" in handler.telegram_api.send_message.call_args.args[1]


def test_plan_retry_reuses_week_evidence_and_obligation_snapshot(
    handler: BotHandler,
) -> None:
    """Retry dispatch is immutable even if new submitted history exists."""
    obligation = DietaryObligation(
        id="saved-eggs-rule-2026-W35-2026-08-26",
        source_rule_id="saved-eggs-rule",
        iso_week="2026-W35",
        horizon_start=date(2026, 8, 26),
        horizon_end=date(2026, 8, 26),
        eligible_dates=[date(2026, 8, 26)],
        operator=RuleOperator.AT_LEAST,
        count=1,
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        evidence_ids=["submitted-meal-1"],
    )
    state = BotHandler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "duration_collected": True,
            "plan_days": 1,
            "plan_start": date(2026, 8, 26),
            "obligations": [obligation],
            "revision": 4,
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_command(_command("plan"))

    handler.repo.get_submitted_meals.assert_not_called()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["week_start"] == "2026-08-26"
    assert payload["obligations"] == [obligation.model_dump(mode="json")]


def test_malformed_stored_typed_rule_fails_closed_before_planner_dispatch(
    handler: BotHandler,
) -> None:
    """Malformed persisted rules cannot be silently reinterpreted."""
    profile = make_profile().model_copy(
        update={"dietary_preferences": [object()]}
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )

    handler.handle_conversational(_plan_route("1, no preference", 6102))

    handler.repo.get_submitted_meals.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    assert "safely" in handler.telegram_api.send_message.call_args.args[1]


def test_plan_preference_resolves_current_rules_over_stored_rules(
    handler: BotHandler,
) -> None:
    """Current rules override only overlapping stored preference rules."""
    stored_eggs = DietaryRule(
        id="stored-eggs",
        source_text="eggs three times for breakfast",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=3,
    )
    stored_tofu = DietaryRule(
        id="stored-tofu",
        source_text="tofu once for dinner",
        foods_any_of=["tofu"],
        meal_type=MealType.DINNER,
        count=1,
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_profile.return_value.dietary_preferences = [
        DietaryPreferenceEntry(
            id="stored-eggs",
            source_text=stored_eggs.source_text,
            rule=stored_eggs,
        ),
        DietaryPreferenceEntry(
            id="stored-tofu",
            source_text=stored_tofu.source_text,
            rule=stored_tofu,
        ),
    ]
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-eggs",
                    "source_text": "eggs at most twice for breakfast",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "operator": "at_most",
                    "count": 2,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs at most twice for breakfast",
            raw_update={"update_id": 56},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    stored_by_source = {
        rule.source_text: rule.id for rule in saved.stored_rules
    }
    current_by_source = {
        rule.source_text: rule.id for rule in saved.current_rules
    }
    effective_by_source = {
        rule.source_text: rule.id for rule in saved.effective_rules
    }
    assert set(stored_by_source) == {
        "eggs three times for breakfast",
        "tofu once for dinner",
    }
    assert set(current_by_source) == {"eggs at most twice for breakfast"}
    assert set(effective_by_source) == {
        "eggs at most twice for breakfast",
        "tofu once for dinner",
    }
    assert all(
        identifier.startswith("r-stored-")
        for identifier in stored_by_source.values()
    )
    assert all(
        identifier.startswith("r-current-")
        for identifier in current_by_source.values()
    )
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert {rule["source_text"] for rule in payload["effective_rules"]} == set(
        effective_by_source
    )
    assert {rule["id"] for rule in payload["effective_rules"]} == set(
        effective_by_source.values()
    )
    assert payload["constraint_rules"] == []


def test_horizon_feasibility_checks_resolved_current_override(
    handler: BotHandler, mocker: Any
) -> None:
    """Horizon capacity is checked after current rules cap stored rules."""
    stored_rule = DietaryRule(
        id="stored-eggs",
        source_text="eggs three times for breakfast",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        count=3,
    )
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id=stored_rule.id,
                    source_text=stored_rule.source_text,
                    rule=stored_rule,
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-eggs-max",
                    "source_text": "eggs at most once for breakfast",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "operator": "at_most",
                    "count": 1,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state().model_copy(update={"plan_days": 1})
    handler.repo.get_conversation_state.return_value = state
    feasibility = mocker.patch(
        "meal_planner.bot_handler.validate_horizon_feasibility",
        wraps=validate_horizon_feasibility,
    )

    handler.handle_conversational(
        _plan_route("eggs at most once for breakfast", update_id=6501)
    )

    feasibility.assert_called_once()
    checked_rules = feasibility.call_args.args[0]
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert len(checked_rules) == 1
    assert checked_rules[0].id == saved.effective_rules[0].id
    assert checked_rules[0].source_text == ("eggs at most once for breakfast")
    assert checked_rules[0].count == 1
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.plan_days == 1
    assert saved.duration_collected
    assert saved.current_rules[0].id.startswith("r-current-")
    handler.lambda_client.invoke.assert_called_once()


def test_horizon_feasibility_waits_until_after_constraint_conflict_check(
    handler: BotHandler, mocker: Any
) -> None:
    """A constraint conflict blocks before horizon feasibility is needed."""
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": [
                ConstraintEntry(
                    id="constraint-peanuts",
                    source_text="no peanuts",
                    forbidden_terms=["peanuts"],
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-peanuts",
                    "source_text": "peanuts once",
                    "foods_any_of": ["peanuts"],
                    "operator": "exactly",
                    "count": 1,
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state().model_copy(update={"plan_days": 1})
    handler.repo.get_conversation_state.return_value = state
    feasibility = mocker.patch(
        "meal_planner.bot_handler.validate_horizon_feasibility"
    )

    handler.handle_conversational(_plan_route("peanuts once", update_id=6502))

    feasibility.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.plan_days == 1
    assert saved.duration_collected


def test_infeasible_effective_rule_retains_duration_for_clarification(
    handler: BotHandler,
) -> None:
    """An impossible effective rule pauses without invoking the planner."""
    handler.repo.get_profile.return_value = make_profile()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-eggs",
                    "source_text": "eggs twice for breakfast",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "operator": "exactly",
                    "count": 2,
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state().model_copy(update={"plan_days": 1})
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        _plan_route("eggs twice for breakfast", update_id=6503)
    )

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.plan_days == 1
    assert saved.duration_collected
    assert saved.preference == "eggs twice for breakfast"
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "cannot fit" in message
    assert "eggs twice for breakfast" not in message


@pytest.mark.parametrize("operator_text", ["exactly", "at least"])
def test_infeasible_best_effort_rule_still_invokes_planner(
    handler: BotHandler,
    operator_text: str,
) -> None:
    """Best-effort horizon shortfalls remain prompt and summary guidance."""
    preference_text = f"eggs {operator_text} twice for breakfast if convenient"
    handler.repo.get_profile.return_value = make_profile()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "best-effort-eggs",
                    "source_text": preference_text,
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "operator": operator_text.replace(" ", "_"),
                    "count": 2,
                    "strength": "best_effort",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state().model_copy(update={"plan_days": 1})
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(_plan_route(preference_text, update_id=6505))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.plan_days == 1
    assert saved.effective_rules[0].strength is RuleStrength.BEST_EFFORT
    handler.lambda_client.invoke.assert_called_once()
    assert not any(
        "cannot fit" in call.args[1]
        for call in handler.telegram_api.send_message.call_args_list
    )
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == 1
    assert payload["effective_rules"][0]["strength"] == "best_effort"


def test_absent_weekday_at_most_keeps_priority_tiers_and_generates(
    handler: BotHandler, mocker: Any
) -> None:
    """An absent weekday upper bound remains feasible after resolution."""
    absent_weekday = Weekday((date.today().isoweekday() % 7) + 1)
    handler.repo.get_profile.return_value = make_profile()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-eggs-max",
                    "source_text": "eggs at most twice on another day",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "weekdays": [absent_weekday.value],
                    "operator": "at_most",
                    "count": 2,
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state().model_copy(update={"plan_days": 1})
    handler.repo.get_conversation_state.return_value = state
    feasibility = mocker.patch(
        "meal_planner.bot_handler.validate_horizon_feasibility",
        wraps=validate_horizon_feasibility,
    )

    handler.handle_conversational(
        _plan_route("eggs at most twice on another day", update_id=6504)
    )

    feasibility.assert_called_once()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert len(saved.current_rules) == 1
    assert saved.current_rules[0].id.startswith("r-current-")
    assert [rule.id for rule in saved.effective_rules] == [
        saved.current_rules[0].id
    ]
    result = validate_horizon_feasibility(
        saved.effective_rules,
        start_date=date.today(),
        end_date=date.today(),
    )
    assert result.is_feasible
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["effective_rules"][0]["operator"] == "at_most"


def test_partial_scope_maximum_preserves_ids_through_retry_boundary(
    handler: BotHandler,
) -> None:
    """A retained maximum keeps both identities across every boundary."""
    stored_rule = DietaryRule(
        id="stored-egg-weekdays",
        source_text="eggs on weekdays for breakfast",
        foods_any_of=["egg"],
        meal_type=MealType.BREAKFAST,
        weekdays=[
            Weekday.MONDAY,
            Weekday.TUESDAY,
            Weekday.WEDNESDAY,
            Weekday.THURSDAY,
            Weekday.FRIDAY,
        ],
        count=1,
    )
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="stored-egg-weekdays",
                    source_text=stored_rule.source_text,
                    rule=stored_rule,
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "current-wednesday-maximum",
                    "source_text": "eggs at most once on Wednesday for "
                    "breakfast",
                    "foods_any_of": ["egg"],
                    "meal_type": "breakfast",
                    "weekdays": ["wednesday"],
                    "operator": "at_most",
                    "count": 1,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs at most once on Wednesday for breakfast",
            raw_update={"update_id": 560},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    first_payload = json.loads(
        handler.lambda_client.invoke.call_args_list[0].kwargs["Payload"]
    )
    first_context = PlanGenerationContext.model_validate(first_payload)
    expected_ids = [rule.id for rule in saved.effective_rules]
    assert len(expected_ids) == 2
    assert len(expected_ids) == len(set(expected_ids))
    assert expected_ids == [rule.id for rule in first_context.effective_rules]
    assert expected_ids == sorted(expected_ids)
    assert any(
        rule.operator is RuleOperator.AT_MOST
        and rule.weekdays == [Weekday.WEDNESDAY]
        for rule in saved.effective_rules
    )
    assert any(
        rule.operator is RuleOperator.EXACTLY
        and rule.weekdays
        == [
            Weekday.MONDAY,
            Weekday.TUESDAY,
            Weekday.WEDNESDAY,
            Weekday.THURSDAY,
            Weekday.FRIDAY,
        ]
        for rule in saved.effective_rules
    )
    assert [rule["id"] for rule in first_payload["effective_rules"]] == (
        expected_ids
    )
    assert len(first_payload["effective_rules"]) == 2
    assert (
        "Working on your 7-day meal plan."
        in (handler.telegram_api.send_message.call_args.args[1])
    )

    retry_state = saved.model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "revision": saved.revision + 1,
        }
    )
    handler.repo.get_conversation_state.return_value = retry_state
    handler.handle_command(_command("plan"))

    retry_payload = json.loads(
        handler.lambda_client.invoke.call_args_list[1].kwargs["Payload"]
    )
    retry_context = PlanGenerationContext.model_validate(retry_payload)
    assert [rule.id for rule in retry_context.effective_rules] == expected_ids
    assert retry_payload["effective_rules"] == first_payload["effective_rules"]


def test_snapshot_transfers_ownership_only_when_maximum_is_absorbed(
    handler: BotHandler,
) -> None:
    """Capping ownership changes only when the current rule disappears."""
    stored = DietaryRule(
        id="stored-eggs",
        source_text="eggs every day",
        foods_any_of=["egg"],
        count=7,
    )
    absorbed_current = DietaryRule(
        id="current-maximum",
        source_text="eggs at most three times",
        foods_any_of=["egg"],
        operator=RuleOperator.AT_MOST,
        count=3,
    )
    capped = stored.model_copy(update={"count": 3})

    absorbed = handler._snapshot_effective_rules(
        [capped], [stored], [absorbed_current]
    )
    assert [(rule.id, rule.count) for rule in absorbed] == [
        (absorbed_current.id, 3)
    ]

    unrelated_current = absorbed_current.model_copy(
        update={"id": "current-tofu", "foods_any_of": ["tofu"]}
    )
    unrelated = handler._snapshot_effective_rules(
        [stored, unrelated_current], [stored], [unrelated_current]
    )
    assert [rule.id for rule in unrelated] == [
        unrelated_current.id,
        stored.id,
    ]


def test_duplicate_effective_ids_are_clarified_before_dispatch(
    handler: BotHandler, mocker: Any
) -> None:
    """An impossible duplicate snapshot cannot enter generation."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "current-eggs",
                    "source_text": "eggs once",
                    "foods_any_of": ["egg"],
                    "operator": "exactly",
                    "count": 1,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    duplicate = DietaryRule(
        id="duplicate-rule",
        source_text="eggs once",
        foods_any_of=["egg"],
        count=1,
    )
    mocker.patch.object(
        handler,
        "_snapshot_effective_rules",
        return_value=[duplicate, duplicate.model_copy(update={"count": 1})],
    )

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs once",
            raw_update={"update_id": 561},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.effective_rules == []
    handler.lambda_client.invoke.assert_not_called()
    assert "combine" in handler.telegram_api.send_message.call_args.args[1]


def test_structured_maximum_stays_out_of_legacy_requirements(
    handler: BotHandler,
) -> None:
    """A generalized maximum is dispatched only in the structured channel."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs at most twice",
                    "foods_any_of": ["eggs"],
                    "operator": "at_most",
                    "count": 2,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs at most twice",
            raw_update={"update_id": 601},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.requirements == []
    assert len(saved.effective_rules) == 1
    assert saved.effective_rules[0].operator is RuleOperator.AT_MOST
    assert saved.effective_rules[0].count == 2
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["requirements"] == []
    assert payload["effective_rules"][0]["operator"] == "at_most"
    assert payload["effective_rules"][0]["count"] == 2


def test_stored_structured_rule_dispatches_without_current_preference(
    handler: BotHandler,
) -> None:
    """Stored structured rules remain active for a no-preference request."""
    stored_rule = DietaryRule(
        id="stored-eggs",
        source_text="eggs at most twice",
        foods_any_of=["eggs"],
        operator=RuleOperator.AT_MOST,
        count=2,
        strength=RuleStrength.STRICT,
    )
    profile = make_profile()
    profile.dietary_preferences = [
        DietaryPreferenceEntry(
            id="stored-eggs",
            source_text=stored_rule.source_text,
            rule=stored_rule,
        )
    ]
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 602},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.preference is None
    assert saved.requirements == []
    assert [rule.operator for rule in saved.effective_rules] == [
        RuleOperator.AT_MOST
    ]
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["preference"] is None
    assert payload["requirements"] == []
    assert payload["effective_rules"][0]["id"].startswith("r-stored-")


def test_confirmed_stored_preference_is_used_without_reinterpretation(
    handler: BotHandler,
) -> None:
    """Saved typed rules are used directly by /plan."""
    raw_preference = make_preference(
        source_text="eggs for breakfast", identifier="saved-eggs"
    )
    profile = make_profile().model_copy(
        update={"dietary_preferences": [raw_preference]}
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 603},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert len(saved.stored_rules) == 1
    assert saved.stored_rules[0].source_text == raw_preference.source_text
    assert saved.stored_rules[0].id.startswith("r-stored-")
    assert saved.effective_rules == saved.stored_rules
    handler.lambda_client.invoke.assert_called_once()
    handler.llm_client.chat_sync.assert_not_called()


def test_one_day_no_preference_dispatches_confirmed_preference(
    handler: BotHandler,
) -> None:
    """A one-day plan receives a confirmed typed stored rule."""
    raw_preference = make_preference(
        source_text="eggs for breakfast", identifier="saved-eggs"
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={"dietary_preferences": [raw_preference]}
    )
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )

    handler.handle_conversational(
        _plan_route("1, no preference", update_id=607)
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.plan_days == 1
    assert saved.requirements == []
    assert len(saved.effective_rules) == 1
    rule = saved.effective_rules[0]
    assert rule.operator is RuleOperator.EXACTLY
    assert rule.count == 1
    assert rule.meal_type is MealType.BREAKFAST
    assert rule.strength is RuleStrength.STRICT

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == 1
    assert payload["requirements"] == []
    assert len(payload["effective_rules"]) == 1
    assert payload["effective_rules"][0]["operator"] == "exactly"
    assert payload["effective_rules"][0]["count"] == 1
    assert payload["effective_rules"][0]["meal_type"] == "breakfast"
    handler.llm_client.chat_sync.assert_not_called()


def test_one_day_current_bare_preferences_dispatch_with_meal_scopes(
    handler: BotHandler,
) -> None:
    """Three bare current clauses become strict minimum-one rules."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "provider-eggs",
                    "source_text": "eggs for breakfast",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "weekdays": [],
                },
                {
                    "id": "provider-bean-soup",
                    "source_text": "bean soup for lunch",
                    "foods_any_of": ["bean soup"],
                    "meal_type": "lunch",
                    "weekdays": [],
                },
                {
                    "id": "provider-halloumi",
                    "source_text": "halloumi for dinner",
                    "foods_any_of": ["halloumi"],
                    "meal_type": "dinner",
                    "weekdays": [],
                },
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )

    handler.handle_conversational(
        _plan_route(
            "1, eggs for breakfast, bean soup for lunch, halloumi for dinner",
            update_id=608,
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.plan_days == 1
    assert saved.stored_rules == []
    assert saved.current_rules
    assert saved.requirements == []
    saved_by_food = {
        rule.foods_any_of[0]: rule for rule in saved.effective_rules
    }
    assert set(saved_by_food) == {"eggs", "bean soup", "halloumi"}
    for rule in saved_by_food.values():
        assert rule.operator is RuleOperator.AT_LEAST
        assert rule.count == 1
        assert rule.strength is RuleStrength.STRICT
    assert saved_by_food["eggs"].meal_type is MealType.BREAKFAST
    assert saved_by_food["bean soup"].meal_type is MealType.LUNCH
    assert saved_by_food["halloumi"].meal_type is MealType.DINNER

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == 1
    assert payload["requirements"] == []
    payload_by_food = {
        rule["foods_any_of"][0]: rule for rule in payload["effective_rules"]
    }
    assert set(payload_by_food) == set(saved_by_food)
    assert all(
        rule["operator"] == "at_least" and rule["count"] == 1
        for rule in payload_by_food.values()
    )


def test_malformed_typed_preference_is_rejected_before_plan_dispatch(
    handler: BotHandler,
) -> None:
    """An invalid stored object cannot enter the planner snapshot."""
    handler.repo.get_profile.return_value = object()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )

    handler.handle_conversational(
        _plan_route("1, no preference", update_id=609)
    )

    handler.repo.transition_conversation_state.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "couldn't" in message or "could not" in message


def test_onboarding_raw_preference_is_rejected_before_profile_save(
    handler: BotHandler,
) -> None:
    """Generic profile updates cannot bypass the review workflow."""
    result = handler._update_profile(
        "user",
        {
            "name": "Alex",
            "people_count": 2,
            "family_members": [
                {"name": "Alex", "calorie_target": 2000},
                {"name": "Sam", "calorie_target": 1800},
            ],
            "dietary_constraints": [],
            "dietary_preferences": ["eggs for breakfast"],
        },
        None,
    )
    assert not result.success
    assert result.message and "through /profile" in result.message
    handler.repo.save_profile.assert_not_called()


def test_confirmed_typed_preference_never_reaches_saved_text_interpreter(
    handler: BotHandler,
) -> None:
    """Stored source wording is display metadata, not an input to an LLM."""
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                make_preference(
                    source_text="make meals healthy",
                    identifier="saved-healthy",
                    rule=DietaryRule(
                        id="saved-healthy-rule",
                        source_text="make meals healthy",
                        foods_any_of=["vegetables"],
                        meal_type="dinner",
                        count=1,
                    ),
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.handle_conversational(_plan_route("1, no preference", 604))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    handler.lambda_client.invoke.assert_called_once()
    handler.llm_client.chat_sync.assert_not_called()


def test_stored_interpretation_is_reused_for_plan_retry(
    handler: BotHandler,
) -> None:
    """A retry reuses the typed stored snapshot without reinterpretation."""
    raw_preference = make_preference(
        source_text="eggs for breakfast", identifier="saved-eggs"
    )
    handler.repo.get_profile.return_value = make_profile().model_copy(
        update={"dietary_preferences": [raw_preference]}
    )
    initial_state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = initial_state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 605},
        )
    )
    pending = handler.repo.transition_conversation_state.call_args.args[1]
    retry_state = pending.model_copy(
        update={"step": ConversationWorkflowStep.RETRY_READY}
    )
    handler.repo.get_conversation_state.return_value = retry_state

    handler.handle_command(_command("plan"))

    handler.llm_client.chat_sync.assert_not_called()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["stored_rules"][0]["id"] == pending.stored_rules[0].id
    assert payload["effective_rules"][0]["id"] == pending.effective_rules[0].id


def test_current_plan_rule_conflicting_with_constraint_is_rejected(
    handler: BotHandler,
) -> None:
    """A conflicting current rule never reaches planner dispatch."""
    profile = make_profile()
    profile.dietary_constraints = [
        ConstraintEntry(
            id="c1", source_text="no peanuts", forbidden_terms=["peanuts"]
        )
    ]
    handler.repo.get_profile.return_value = profile
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "mode": "current_plan_preference",
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "peanut butter once",
                    "foods_any_of": ["peanut butter"],
                    "operator": "exactly",
                    "count": 1,
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="peanut butter once",
            raw_update={"update_id": 57},
        )
    )

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.current_rules == []
    assert "constraint" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


@pytest.mark.parametrize(
    "constraint_text",
    ["gluten-free"],
)
def test_legacy_semantic_constraint_dispatches_canonical_terms(
    handler: BotHandler, constraint_text: str
) -> None:
    """Recognized legacy phrases reach the planner as canonical terms."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_constraints"] = [constraint_text]
    profile = UserProfile.model_validate(profile_data)
    handler.repo.get_profile.return_value = profile
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 58},
        )
    )

    handler.lambda_client.invoke.assert_called_once()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["constraint_rules"][0]["forbidden_terms"]
    assert (
        constraint_text not in payload["constraint_rules"][0]["forbidden_terms"]
    )


@pytest.mark.parametrize(
    "animal_derived_food",
    ["cheese", "butter", "shellfish", "gelatin", "honey"],
)
def test_vegan_legacy_constraint_clarifies_before_generation(
    handler: BotHandler, animal_derived_food: str
) -> None:
    """Incomplete vegan semantics never dispatch or publish a plan."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_constraints"] = ["vegan"]
    profile = UserProfile.model_validate(profile_data)
    handler.repo.get_profile.return_value = profile
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 61},
        )
    )

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.constraint_rules[0].uninterpretable
    assert "safely matched" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_unknown_legacy_constraint_clarifies_without_planner_dispatch(
    handler: BotHandler,
) -> None:
    """Unknown saved safety prose stops planning with bounded clarification."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_constraints"] = [
        "I react badly to mystery foods",
    ]
    profile = UserProfile.model_validate(profile_data)
    handler.repo.get_profile.return_value = profile
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 59},
        )
    )

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert "safely matched" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


@pytest.mark.parametrize(
    "constraint_text",
    ["vegetarian", "halal", "kosher", "low sodium"],
)
def test_unknown_short_legacy_constraint_clarifies_before_generation(
    handler: BotHandler, constraint_text: str
) -> None:
    """Unregistered short labels never reach the planner."""
    profile_data = make_profile().model_dump(mode="json")
    profile_data["dietary_constraints"] = [constraint_text]
    profile = UserProfile.model_validate(profile_data)
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = handler._new_plan_state()

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 60},
        )
    )

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.constraint_rules[0].uninterpretable
    assert "safely matched" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_plan_retry_reuses_effective_rule_snapshot_without_reinterpretation(
    handler: BotHandler,
) -> None:
    """Retry events reuse the saved effective snapshot and raw wording."""
    stored = DietaryRule(
        id="stored-1",
        source_text="eggs once",
        foods_any_of=["eggs"],
        count=1,
    )
    effective = DietaryRule(
        id="current-1",
        source_text="eggs twice",
        foods_any_of=["eggs"],
        count=2,
    )
    constraint = ConstraintEntry(
        id="constraint-1", source_text="no peanuts", forbidden_terms=["peanuts"]
    )
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "preference": "eggs twice",
            "stored_rules": [stored],
            "current_rules": [effective],
            "effective_rules": [effective],
            "constraint_rules": [constraint],
            "revision": 4,
        }
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_command(_command("plan"))

    handler.llm_client.chat_sync.assert_not_called()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["preference"] == "eggs twice"
    assert payload["stored_rules"][0]["id"] == "stored-1"
    assert payload["current_rules"][0]["id"] == "current-1"
    assert payload["effective_rules"][0]["id"] == "current-1"
    assert payload["constraint_rules"][0]["id"] == "constraint-1"


def test_plan_preference_persists_interpreted_requirements_for_retry(
    handler: BotHandler,
) -> None:
    """Requirements survive state transitions and enter the planner event."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs three times",
                    "foods_any_of": ["eggs"],
                    "meal_type": None,
                    "exact_count": 3,
                }
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs three times",
            raw_update={"update_id": 109},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.requirements == []
    assert saved.effective_rules[0].id.startswith("r-current-")
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["requirements"] == []
    assert payload["effective_rules"][0]["count"] == 3


def test_no_preference_plan_dispatches_without_interpretation_rules(
    handler: BotHandler,
) -> None:
    """No-preference requests retain the legacy planner path."""
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="no preference",
            raw_update={"update_id": 110},
        )
    )

    handler.llm_client.chat_sync.assert_not_called()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["preference"] is None
    assert payload["requirements"] == []


def test_plan_retry_reuses_requirements_without_reinterpretation(
    handler: BotHandler,
) -> None:
    """Manual retries carry saved rules and skip the interpreter."""
    requirement = PreferenceRequirement(
        id="r1",
        source_text="eggs three times",
        foods_any_of=["eggs"],
        exact_count=3,
    )
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "preference": "eggs three times",
            "requirements": [requirement],
            "revision": 4,
        }
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_command(_command("plan"))

    handler.llm_client.chat_sync.assert_not_called()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["requirements"][0]["id"] == "r1"


def test_plan_preference_complete_interpretation_precedes_generation(
    handler: BotHandler,
) -> None:
    """A complete interpretation transitions and invokes the planner once."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs three times for breakfast",
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "exact_count": 3,
                }
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs three times for breakfast",
            raw_update={"update_id": 101},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.preference == "eggs three times for breakfast"
    assert handler.lambda_client.invoke.call_count == 1
    handler.llm_client.chat_sync.assert_called_once()


@pytest.mark.parametrize("requirement_count", [20, 21])
def test_plan_preference_requirement_count_boundary(
    handler: BotHandler,
    requirement_count: int,
) -> None:
    """The Bot durably handles the parser's exact requirement boundary."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": f"requirement-{index}",
                    "source_text": f"food {index} once",
                    "foods_any_of": [f"food-{index}"],
                    "meal_type": None,
                    "exact_count": 1,
                }
                for index in range(requirement_count)
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="many food rules",
            raw_update={"update_id": 115 + requirement_count},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.request_id == state.request_id
    assert saved.preference == "many food rules"
    if requirement_count == 20:
        assert saved.step is ConversationWorkflowStep.GENERATING
        assert saved.requirements == []
        assert len(saved.effective_rules) == 20
        handler.lambda_client.invoke.assert_called_once()
        assert handler.telegram_api.send_message.call_args.args[1] == (
            "Working on your 7-day meal plan."
        )
    else:
        assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
        assert saved.requirements == []
        assert saved.last_update_id == str(115 + requirement_count)
        handler.lambda_client.invoke.assert_not_called()
        message = handler.telegram_api.send_message.call_args.args[1]
        assert "combine" in message.lower()
        assert "prioritize" in message.lower()
        assert "500" not in message


def test_plan_preference_saves_focused_clarification_without_generation(
    handler: BotHandler,
) -> None:
    """An ambiguous request remains recoverable and does not invoke Lambda."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "How many times should healthy meals occur?",
            "unparsed_text": ["make it healthy"],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="make it healthy",
            raw_update={"update_id": 102},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.preference == "make it healthy"
    assert saved.revision == 1
    assert saved.last_update_id == "102"
    handler.lambda_client.invoke.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "How many times" in message


@pytest.mark.parametrize("operator", ["exactly", "at_least"])
def test_impossible_strict_weekday_rule_clarifies_before_generation(
    handler: BotHandler,
    operator: str,
) -> None:
    """Impossible per-day counts remain retryable and never dispatch."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "r1",
                    "source_text": (
                        "eggs twice for breakfast on Monday and Wednesday"
                    ),
                    "foods_any_of": ["eggs"],
                    "meal_type": "breakfast",
                    "weekdays": ["monday", "wednesday"],
                    "operator": operator,
                    "count": 2,
                    "strength": "strict",
                }
            ],
            "exclusions": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs twice for breakfast on Monday and Wednesday",
            raw_update={"update_id": 116 + (operator == "at_least")},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.preference is not None
    assert handler.lambda_client.invoke.call_count == 0
    assert handler.telegram_api.send_message.call_count == 1
    assert "cannot fit" in handler.telegram_api.send_message.call_args.args[1]


def test_oversized_interpretation_stays_bounded_before_telegram(
    handler: BotHandler,
) -> None:
    """Oversized provider clarification remains one recoverable message."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "x" * 501,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="make it healthy",
            raw_update={"update_id": 114},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.preference == "make it healthy"
    handler.lambda_client.invoke.assert_not_called()
    handler.telegram_api.send_message.assert_called_once()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert len(message) <= 500
    assert len(split_text(message)) == 1
    assert "rephrase" in message.lower()


def test_vacuous_preference_interpretation_stays_awaiting_preference(
    handler: BotHandler,
) -> None:
    """An empty successful interpretation cannot dispatch a planner."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="make it healthy",
            raw_update={"update_id": 111},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.preference == "make it healthy"
    assert saved.requirements == []
    handler.lambda_client.invoke.assert_not_called()
    assert (
        handler.telegram_api.send_message.call_args.args[1]
        == "Please provide a measurable meal preference."
    )


def test_conflicting_preference_interpretation_stays_awaiting_preference(
    handler: BotHandler,
) -> None:
    """Directly conflicting rules cannot dispatch a planner."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [
                {
                    "id": "r1",
                    "source_text": "eggs once for dinner",
                    "foods_any_of": ["eggs"],
                    "meal_type": "dinner",
                    "exact_count": 1,
                },
                {
                    "id": "r2",
                    "source_text": "egg twice for dinner",
                    "foods_any_of": ["egg"],
                    "meal_type": "dinner",
                    "exact_count": 2,
                },
            ],
            "clarification": None,
            "unparsed_text": [],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs for dinner",
            raw_update={"update_id": 112},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.requirements == []
    handler.lambda_client.invoke.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "conflict" in message.lower()


def test_plan_preference_combines_clarification_reply_before_interpreting(
    handler: BotHandler,
) -> None:
    """A clarification answer is interpreted with the saved raw wording."""
    handler.llm_client.chat_sync.side_effect = [
        json.dumps(
            {
                "requirements": [],
                "clarification": "How many times?",
                "unparsed_text": ["eggs"],
            }
        ),
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "r1",
                        "source_text": "eggs three times",
                        "foods_any_of": ["eggs"],
                        "meal_type": None,
                        "exact_count": 3,
                    }
                ],
                "clarification": None,
                "unparsed_text": [],
            }
        ),
    ]
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    first_route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="eggs",
        raw_update={"update_id": 103},
    )
    handler.handle_conversational(first_route)
    clarification_state = (
        handler.repo.transition_conversation_state.call_args.args[1]
    )
    handler.repo.get_conversation_state.return_value = clarification_state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="three times",
            raw_update={"update_id": 104},
        )
    )

    prompt, combined = handler.llm_client.chat_sync.call_args_list[1].args
    assert "eggs" in prompt
    assert combined == "eggs; three times"
    final_state = handler.repo.transition_conversation_state.call_args.args[1]
    assert final_state.step is ConversationWorkflowStep.GENERATING
    assert final_state.preference == "eggs; three times"
    assert handler.lambda_client.invoke.call_count == 1


@pytest.mark.parametrize("initial_length", [498, 499, 500])
def test_clarification_overflow_rejects_answer_without_side_effects(
    handler: BotHandler, initial_length: int
) -> None:
    """A clarification answer cannot overflow the saved preference cap."""
    handler.llm_client.chat_sync.side_effect = [
        json.dumps(
            {
                "requirements": [],
                "clarification": "How many times?",
                "unparsed_text": ["request"],
            }
        ),
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "r1",
                        "source_text": "eggs once",
                        "foods_any_of": ["eggs"],
                        "meal_type": None,
                        "exact_count": 1,
                    }
                ],
                "clarification": None,
                "unparsed_text": [],
            }
        ),
    ]
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="x" * initial_length,
            raw_update={"update_id": 2000 + initial_length},
        )
    )
    saved_state = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = saved_state
    handler.repo.transition_conversation_state.reset_mock()
    handler.telegram_api.send_message.reset_mock()

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="y",
            raw_update={"update_id": 3000 + initial_length},
        )
    )

    assert handler.llm_client.chat_sync.call_count == 1
    handler.lambda_client.invoke.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    assert saved_state.preference == "x" * initial_length
    assert saved_state.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    message = handler.telegram_api.send_message.call_args.args[1]
    assert len(message) <= 500
    assert len(split_text(message)) == 1
    assert "not appended" in message.lower()
    assert "/plan" in message
    assert "complete preference" in message.lower()
    assert "500" in message


def test_clarification_overflow_can_reset_with_plan_command(
    handler: BotHandler,
) -> None:
    """The explicit /plan path replaces an overflowed pending request."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "How many times?",
            "unparsed_text": ["request"],
        }
    )
    initial_state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = initial_state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="x" * 500,
            raw_update={"update_id": 4000},
        )
    )
    pending_state = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending_state
    handler.repo.save_conversation_state.return_value = True
    handler.repo.save_conversation_state.reset_mock()

    handler.handle_command(_command("plan"))

    replacement = handler.repo.save_conversation_state.call_args.args[1]
    assert replacement.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert replacement.preference is None
    assert replacement.request_id != pending_state.request_id
    assert (
        handler.repo.save_conversation_state.call_args.kwargs[
            "expected_revision"
        ]
        == pending_state.revision
    )
    assert "/plan" not in handler.telegram_api.send_message.call_args.args[1]


def test_exactly_500_character_combined_preference_is_interpreted(
    handler: BotHandler,
) -> None:
    """A combined preference at the cap still follows the normal path."""
    handler.llm_client.chat_sync.side_effect = [
        json.dumps(
            {
                "requirements": [],
                "clarification": "How many times?",
                "unparsed_text": ["request"],
            }
        ),
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "r1",
                        "source_text": "eggs once",
                        "foods_any_of": ["eggs"],
                        "meal_type": None,
                        "exact_count": 1,
                    }
                ],
                "clarification": None,
                "unparsed_text": [],
            }
        ),
    ]
    initial_state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = initial_state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="x" * 497,
            raw_update={"update_id": 5000},
        )
    )
    pending_state = handler.repo.transition_conversation_state.call_args.args[1]
    handler.repo.get_conversation_state.return_value = pending_state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="y",
            raw_update={"update_id": 5001},
        )
    )

    assert handler.llm_client.chat_sync.call_count == 2
    assert handler.llm_client.chat_sync.call_args.args[1] == ("x" * 497 + "; y")
    final_state = handler.repo.transition_conversation_state.call_args.args[1]
    assert final_state.preference == "x" * 497 + "; y"
    assert final_state.step is ConversationWorkflowStep.GENERATING
    handler.lambda_client.invoke.assert_called_once()


def test_plan_preference_interpreter_failure_keeps_retryable_state(
    handler: BotHandler,
) -> None:
    """Interpreter transport failures retain wording without planner work."""
    handler.llm_client.chat_sync.side_effect = RuntimeError("provider down")
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs three times",
            raw_update={"update_id": 105},
        )
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.preference == "eggs three times"
    handler.lambda_client.invoke.assert_not_called()
    assert "try again" in handler.telegram_api.send_message.call_args.args[1]


def test_duplicate_plan_preference_update_does_not_reinterpret(
    handler: BotHandler,
) -> None:
    """A redelivered update is idempotent while clarification is pending."""
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_PREFERENCE,
            "preference": "eggs",
            "revision": 1,
            "last_update_id": "106",
        }
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs",
            raw_update={"update_id": 106},
        )
    )

    handler.llm_client.chat_sync.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    assert (
        handler.telegram_api.send_message.call_args.args[1]
        == "Please continue with your preference or try again."
    )


def test_duplicate_interpreter_failure_update_prompts_for_retry(
    handler: BotHandler,
) -> None:
    """A duplicate after interpreter failure does not claim generation."""
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state
    handler.llm_client.chat_sync.side_effect = RuntimeError("provider down")
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="eggs three times",
        raw_update={"update_id": 105},
    )

    handler.handle_conversational(route)
    retryable_state = handler.repo.transition_conversation_state.call_args.args[
        1
    ]
    handler.repo.get_conversation_state.return_value = retryable_state
    handler.handle_conversational(route)

    assert handler.llm_client.chat_sync.call_count == 1
    assert handler.repo.transition_conversation_state.call_count == 1
    handler.lambda_client.invoke.assert_not_called()
    assert (
        handler.telegram_api.send_message.call_args.args[1]
        == "Please continue with your preference or try again."
    )


def test_duplicate_generating_plan_update_keeps_working_reply(
    handler: BotHandler,
) -> None:
    """A duplicate for generation in progress retains its working reply."""
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.GENERATING,
            "revision": 1,
            "last_update_id": "113",
        }
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs three times",
            raw_update={"update_id": 113},
        )
    )

    handler.llm_client.chat_sync.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    assert (
        handler.telegram_api.send_message.call_args.args[1]
        == "Working on your 7-day meal plan."
    )


def test_plan_preference_race_does_not_invoke_after_state_conflict(
    handler: BotHandler,
) -> None:
    """A lost conditional update cannot dispatch a stale planner request."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "How many?",
            "unparsed_text": ["eggs"],
        }
    )
    handler.repo.transition_conversation_state.return_value = False
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="eggs",
            raw_update={"update_id": 107},
        )
    )

    handler.lambda_client.invoke.assert_not_called()
    assert "changed" in handler.telegram_api.send_message.call_args.args[1]


@pytest.mark.parametrize("length", [499, 500, 501])
def test_plan_preference_length_boundary(
    handler: BotHandler, length: int
) -> None:
    """Preferences accept 500 characters and reject the next character."""
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "Please provide a measurable count.",
            "unparsed_text": ["request"],
        }
    )
    state = handler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(
        RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=1,
            user_id="user",
            text="x" * length,
            raw_update={"update_id": str(108 + length)},
        )
    )

    if length <= 500:
        handler.llm_client.chat_sync.assert_called_once()
        assert handler.repo.transition_conversation_state.call_count == 1
    else:
        handler.llm_client.chat_sync.assert_not_called()
        handler.repo.transition_conversation_state.assert_not_called()
        assert "too long" in handler.telegram_api.send_message.call_args.args[1]


def test_plan_retry_preserves_preference_without_reinterpreting(
    handler: BotHandler,
) -> None:
    """Manual retry reuses saved wording and does not call the interpreter."""
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "preference": "eggs three times",
            "revision": 4,
        }
    )
    handler.repo.get_conversation_state.return_value = state

    handler.handle_command(_command("plan"))

    handler.llm_client.chat_sync.assert_not_called()
    handler.lambda_client.invoke.assert_called_once()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["preference"] == "eggs three times"


@pytest.mark.parametrize("plan_days", [3, 7])
def test_plan_retry_dispatches_retained_duration(
    handler: BotHandler, plan_days: int
) -> None:
    """Manual retry serializes the retained duration to the planner."""
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "plan_days": plan_days,
            "duration_collected": True,
            "revision": 4,
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_command(_command("plan"))

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == plan_days
    assert payload["week_start"] == date.today().isoformat()


def test_retry_revalidates_rules_after_utc_date_change(
    handler: BotHandler,
) -> None:
    """A shifted strict weekday horizon returns to bounded clarification."""
    accepted_date = date.today() - timedelta(days=1)
    retry_date = date.today()
    rule = DietaryRule(
        id="monday-eggs",
        source_text="eggs once for breakfast on the prior weekday",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        weekdays=[Weekday(accepted_date.isoweekday())],
        operator=RuleOperator.EXACTLY,
        count=1,
        strength=RuleStrength.STRICT,
    )
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "plan_days": 1,
            "duration_collected": True,
            "preference": "eggs once for breakfast",
            "effective_rules": [rule],
            "revision": 4,
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_command(_plan_command_at(retry_date))

    handler.lambda_client.invoke.assert_not_called()
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.plan_days == 1
    assert saved.duration_collected
    assert saved.request_id == state.request_id
    assert saved.effective_rules == [rule]
    assert saved.revision == state.revision + 1
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "cannot fit" in message
    assert rule.source_text not in message


def test_retry_feasible_date_shift_uses_one_start_date(
    handler: BotHandler,
) -> None:
    """A feasible retry transitions and dispatches its captured UTC date."""
    retry_date = date.today()
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "plan_days": 3,
            "duration_collected": True,
            "revision": 4,
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_command(_plan_command_at(retry_date))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.GENERATING
    assert saved.revision == state.revision + 1
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["week_start"] == retry_date.isoformat()
    assert payload["plan_days"] == state.plan_days
    assert handler.repo.transition_conversation_state.call_args.kwargs == {
        "expected_revision": state.revision
    }


def test_retry_best_effort_shortfall_remains_non_blocking(
    handler: BotHandler,
) -> None:
    """A best-effort retry still dispatches when capacity is insufficient."""
    rule = DietaryRule(
        id="best-effort-eggs",
        source_text="eggs twice for breakfast if convenient",
        foods_any_of=["eggs"],
        meal_type=MealType.BREAKFAST,
        operator=RuleOperator.EXACTLY,
        count=2,
        strength=RuleStrength.BEST_EFFORT,
    )
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "plan_days": 1,
            "duration_collected": True,
            "effective_rules": [rule],
            "revision": 4,
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_command(_plan_command_at(date.today()))

    handler.lambda_client.invoke.assert_called_once()
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == 1
    assert payload["effective_rules"][0]["id"] == rule.id


def test_retry_date_revalidation_does_not_dispatch_after_state_loss(
    handler: BotHandler,
) -> None:
    """A lost retry transition never creates a planner side effect."""
    state = handler._new_plan_state().model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "plan_days": 3,
            "duration_collected": True,
            "revision": 4,
        }
    )
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = False

    handler.handle_command(_plan_command_at(date.today()))

    handler.lambda_client.invoke.assert_not_called()
    handler.repo.transition_conversation_state.assert_called_once()
    assert "changed" in handler.telegram_api.send_message.call_args.args[1]


def _plan_route(text: str, update_id: int = 9000) -> RouteResult:
    """Build a conversational route for focused plan-phase tests."""
    return RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text=text,
        raw_update={"update_id": update_id},
    )


def test_new_plan_phase_requires_duration_and_preference_pair(
    handler: BotHandler,
) -> None:
    """A new uncollected request rejects preference-only input."""
    state = BotHandler._new_plan_state()
    assert not state.duration_collected
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(_plan_route("fish for dinner"))

    handler.llm_client.chat_sync.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    assert (
        "3, fish for dinner"
        in (handler.telegram_api.send_message.call_args.args[1])
    )


@pytest.mark.parametrize("plan_days", [1, 3])
def test_plan_request_dispatches_selected_duration(
    handler: BotHandler, plan_days: int
) -> None:
    """Initial planner events carry the accepted duration and start date."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_conversational(
        _plan_route(f"{plan_days}, no preference", update_id=9020 + plan_days)
    )

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["plan_days"] == plan_days
    assert payload["week_start"] == date.today().isoformat()


def test_legacy_plan_phase_accepts_preference_only_as_seven_days(
    handler: BotHandler,
) -> None:
    """A persisted state without phase fields remains a seven-day request."""
    legacy_payload = handler._new_plan_state().model_dump()
    legacy_payload.pop("plan_days")
    legacy_payload.pop("duration_collected")
    legacy_state = ConversationState.model_validate(legacy_payload)
    handler.repo.get_conversation_state.return_value = legacy_state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {"requirements": [], "clarification": "How many?", "unparsed_text": []}
    )

    handler.handle_conversational(_plan_route("fish", update_id=9001))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 7
    assert saved.duration_collected
    assert saved.preference == "fish"


def test_initial_duration_splits_only_at_first_comma(
    handler: BotHandler,
) -> None:
    """The initial response retains commas in the preference wording."""
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {"requirements": [], "clarification": "How many?", "unparsed_text": []}
    )

    handler.handle_conversational(
        _plan_route("3, fish for dinner, twice", update_id=9002)
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 3
    assert saved.duration_collected
    assert saved.preference == "fish for dinner, twice"
    assert handler.llm_client.chat_sync.call_args.args[1] == (
        "fish for dinner, twice"
    )


@pytest.mark.parametrize(
    "text, expected_days, expected_preference",
    [
        ("1, no preference", 1, None),
        (" 7 ,  fish for dinner  ", 7, "fish for dinner"),
        ("3, fish, pasta, and salad", 3, "fish, pasta, and salad"),
        ("2, anything", 2, None),
        ("4, NO PREFERENCE", 4, None),
        ("5, no preferences!", 5, None),
        ("6, none", 6, None),
        ("7, whatever.", 7, None),
    ],
)
def test_initial_plan_input_matrix_preserves_duration_and_preference(
    handler: BotHandler,
    text: str,
    expected_days: int,
    expected_preference: str | None,
) -> None:
    """Accepted initial forms retain duration and normalized preference."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "Which count?",
            "unparsed_text": [],
        }
    )

    handler.handle_conversational(_plan_route(text, 9050 + expected_days))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == expected_days
    assert saved.duration_collected
    assert saved.preference == expected_preference
    if expected_preference is None:
        handler.llm_client.chat_sync.assert_not_called()
        handler.lambda_client.invoke.assert_called_once()
    else:
        assert handler.llm_client.chat_sync.call_args.args[1] == (
            expected_preference
        )
        handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "1",
        ", fish",
        "1,   ",
        "True, fish",
        "false, fish",
        "three, fish",
        "1.5, fish",
        "0, fish",
        "8, fish",
    ],
)
def test_invalid_initial_plan_input_is_side_effect_free(
    handler: BotHandler, text: str
) -> None:
    """Malformed initial input cannot transition or start plan work."""
    handler.repo.get_profile.return_value = make_profile()
    state = BotHandler._new_plan_state()
    state_snapshot = state.model_dump(mode="json")
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(_plan_route(text, 9100))

    assert state.model_dump(mode="json") == state_snapshot
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.mark_conversation_retry_ready.assert_not_called()
    handler.repo.save_conversation_state.assert_not_called()
    handler.repo.log_meal_and_transition.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    handler.telegram_api.send_plan.assert_not_called()
    messages = [
        call_args.args[1]
        for call_args in handler.telegram_api.send_message.call_args_list
    ]
    assert len(messages) == 1
    assert "Please reply in the form" in messages[0]


def test_no_preference_initial_response_keeps_saved_rule_phase(
    handler: BotHandler,
) -> None:
    """Saved-rule clarification retains duration and preference-only input."""
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                DietaryPreferenceEntry(
                    id="saved-1",
                    source_text="fish on Mondays",
                    rule=DietaryRule(
                        id="saved-1-rule",
                        source_text="fish on Mondays",
                        foods_any_of=["fish"],
                        meal_type="dinner",
                        weekdays=[Weekday.MONDAY],
                        operator=RuleOperator.AT_LEAST,
                        count=1,
                    ),
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_conversational(_plan_route("3, no preference", 9003))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 3
    assert saved.duration_collected
    assert saved.preference is None
    handler.lambda_client.invoke.assert_called_once()
    handler.llm_client.chat_sync.assert_not_called()


@pytest.mark.parametrize("earlier_egg_count", [0, 1, 2])
def test_wednesday_no_preference_projects_exact_profile_without_interpretation(
    handler: BotHandler, earlier_egg_count: int
) -> None:
    """A Wednesday one-day request uses typed rules and submitted evidence."""
    profile, batch_rule = _wednesday_profile_fixture()
    assert batch_rule.total_yield == 2
    assert batch_rule.preparation_meal_types == [
        MealType.LUNCH,
        MealType.DINNER,
    ]
    assert batch_rule.reuse_meal_types == [MealType.LUNCH, MealType.DINNER]
    assert [entry.source_text for entry in profile.dietary_preferences] == [
        "eggs three breakfasts weekly",
        "pancakes or crepes Saturday",
        "fish at least one dinner weekly",
    ]
    assert profile.dietary_constraints[0].forbidden_terms == ["mushroom"]

    handler._new_plan_state = BotHandler._new_plan_state
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True
    monday = date(2026, 8, 24)
    handler.repo.get_submitted_meals.return_value = [
        MealLogEntry(
            date=monday + timedelta(days=index),
            meal_type=MealType.BREAKFAST,
            description="Egg toast",
            created_at=datetime.combine(
                monday + timedelta(days=index),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
        )
        for index in range(earlier_egg_count)
    ]

    handler.handle_conversational(
        _route_on(date(2026, 8, 26), "1, no preference", 12000)
    )

    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    obligations = {
        tuple(item["foods_any_of"]): item for item in payload["obligations"]
    }
    expected_foods = {("fish",)}
    if earlier_egg_count < 2:
        expected_foods.add(("egg",))
    assert set(obligations) == expected_foods
    assert obligations[("fish",)]["count"] == 1
    assert obligations[("fish",)]["eligible_dates"] == ["2026-08-26"]
    if earlier_egg_count < 2:
        assert obligations[("egg",)]["count"] == 1
        assert obligations[("egg",)]["eligible_dates"] == ["2026-08-26"]
        assert len(obligations[("egg",)]["evidence_ids"]) == (earlier_egg_count)
    assert "pancake" not in obligations
    assert "crepe" not in obligations
    assert payload["constraint_rules"][0]["forbidden_terms"] == ["mushroom"]
    handler.repo.get_submitted_meals.assert_called_once_with(
        "user", start_date=monday, end_date=date(2026, 8, 26)
    )
    handler.llm_client.chat_sync.assert_not_called()
    handler.llm_client.chat_json_strict_sync.assert_not_called()


def test_malformed_typed_profile_blocks_wednesday_plan_without_weakening_rules(
    handler: BotHandler,
) -> None:
    """A malformed saved typed profile cannot become an unconstrained plan."""
    profile, _ = _wednesday_profile_fixture()
    malformed = profile.model_copy(update={"dietary_preferences": ["eggs"]})
    handler._new_plan_state = BotHandler._new_plan_state
    handler.repo.get_profile.return_value = malformed
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_conversational(
        _route_on(date(2026, 8, 26), "1, no preference", 12001)
    )

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.AWAITING_PREFERENCE
    assert saved.obligations == []
    assert saved.effective_rules == []
    handler.repo.get_submitted_meals.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    handler.llm_client.chat_sync.assert_not_called()
    handler.llm_client.chat_json_strict_sync.assert_not_called()
    assert "saved dietary rules" in (
        handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_wednesday_retry_keeps_snapshot_and_fresh_plan_recalculates_evidence(
    handler: BotHandler,
) -> None:
    """Retries reuse obligations while a fresh request sees new submissions."""
    profile, _ = _wednesday_profile_fixture()
    handler._new_plan_state = BotHandler._new_plan_state
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True
    handler.repo.get_submitted_meals.return_value = []
    wednesday = date(2026, 8, 26)

    handler.handle_conversational(
        _route_on(wednesday, "1, no preference", 14000)
    )
    first_payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    first_obligations = first_payload["obligations"]
    generating_state = (
        handler.repo.transition_conversation_state.call_args.args[1]
    )
    retry_state = generating_state.model_copy(
        update={
            "step": ConversationWorkflowStep.RETRY_READY,
            "revision": generating_state.revision + 1,
        }
    )
    handler.repo.get_conversation_state.return_value = retry_state
    handler.lambda_client.invoke.reset_mock()

    handler.handle_command(_plan_command_at(wednesday))

    retry_payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert retry_payload["obligations"] == first_obligations
    assert retry_payload["week_start"] == wednesday.isoformat()
    assert retry_payload["plan_days"] == 1

    monday = date(2026, 8, 24)
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.get_submitted_meals.return_value = [
        MealLogEntry(
            date=monday + timedelta(days=index),
            meal_type=MealType.BREAKFAST,
            description="Egg toast",
            created_at=datetime.combine(
                monday + timedelta(days=index),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
        )
        for index in range(2)
    ]
    handler.lambda_client.invoke.reset_mock()

    handler.handle_conversational(
        _route_on(wednesday, "1, no preference", 14001)
    )

    fresh_payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert not any(
        item["foods_any_of"] == ["egg"] for item in fresh_payload["obligations"]
    )
    assert any(
        item["foods_any_of"] == ["fish"]
        for item in fresh_payload["obligations"]
    )


def test_no_preference_initial_response_keeps_constraint_phase(
    handler: BotHandler,
) -> None:
    """Unsafe saved constraints clarify without losing the accepted duration."""
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": [
                ConstraintEntry(
                    id="constraint-1",
                    source_text="an unknown restriction",
                    uninterpretable=True,
                )
            ]
        }
    )
    handler.repo.get_profile.return_value = profile
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_conversational(_plan_route("3, no preference", 9005))

    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 3
    assert saved.duration_collected
    assert saved.preference is None
    handler.llm_client.chat_sync.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    assert (
        "constraint"
        in handler.telegram_api.send_message.call_args.args[1].lower()
    )


def test_clarification_text_with_comma_is_not_reparsed(
    handler: BotHandler,
) -> None:
    """Collected clarification preserves comma-containing text."""
    state = handler._new_plan_state().model_copy(
        update={
            "plan_days": 3,
            "duration_collected": True,
            "preference": "fish",
            "revision": 1,
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "Please provide a count.",
            "unparsed_text": ["fish"],
        }
    )

    handler.handle_conversational(_plan_route("twice, on Mondays", 9004))

    assert handler.llm_client.chat_sync.call_args.args[1] == (
        "fish; twice, on Mondays"
    )
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 3
    assert saved.duration_collected


def test_horizon_clarification_reply_remains_preference_only(
    handler: BotHandler,
) -> None:
    """A horizon clarification reply cannot replace the retained duration."""
    state = BotHandler._new_plan_state().model_copy(
        update={
            "plan_days": 3,
            "duration_collected": True,
            "effective_rules": [
                DietaryRule(
                    id="horizon-rule",
                    source_text="fish twice on Mondays",
                    foods_any_of=["fish"],
                    weekdays=[Weekday.MONDAY],
                    count=2,
                )
            ],
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True
    handler.llm_client.chat_sync.return_value = json.dumps(
        {
            "requirements": [],
            "clarification": "How many fish meals fit this horizon?",
            "unparsed_text": ["fish"],
        }
    )

    handler.handle_conversational(_plan_route("twice, on Mondays", 9007))

    assert handler.llm_client.chat_sync.call_args.args[1] == (
        "twice, on Mondays"
    )
    saved = handler.repo.transition_conversation_state.call_args.args[1]
    assert saved.plan_days == 3
    assert saved.duration_collected


def test_plan_command_reset_starts_uncollected_duration_phase(
    handler: BotHandler,
) -> None:
    """A fresh /plan replaces retained duration and preference state."""
    handler.repo.get_profile.return_value = make_profile()
    previous = handler._new_plan_state().model_copy(
        update={
            "plan_days": 3,
            "duration_collected": True,
            "preference": "fish",
            "revision": 4,
        }
    )
    handler.repo.get_conversation_state.return_value = previous
    handler.repo.save_conversation_state.return_value = True
    handler._new_plan_state = BotHandler._new_plan_state

    handler.handle_command(_command("plan"))

    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.plan_days == 7
    assert not saved.duration_collected
    assert saved.preference is None


def test_initial_duration_is_retained_when_planner_dispatch_fails(
    handler: BotHandler,
) -> None:
    """Generation and retry-ready transitions keep the collected phase."""
    handler.repo.get_profile.return_value = make_profile()
    handler.repo.get_conversation_state.return_value = (
        BotHandler._new_plan_state()
    )
    handler.repo.transition_conversation_state.return_value = True
    handler.lambda_client.invoke.side_effect = RuntimeError("planner down")

    handler.handle_conversational(_plan_route("3, no preference", 9006))

    generating = handler.repo.transition_conversation_state.call_args.args[1]
    retry_ready = handler.repo.mark_conversation_retry_ready.call_args.args[1]
    assert generating.plan_days == 3
    assert generating.duration_collected
    assert retry_ready.plan_days == 3
    assert retry_ready.duration_collected
    assert retry_ready.step is ConversationWorkflowStep.RETRY_READY


@pytest.mark.parametrize("text", ["fish", "0, fish", "8, fish", "three, fish"])
def test_invalid_initial_plan_syntax_has_no_interpreter_or_planner_side_effects(
    handler: BotHandler, text: str
) -> None:
    """Invalid initial syntax leaves the uncollected state untouched."""
    state = BotHandler._new_plan_state()
    handler.repo.get_conversation_state.return_value = state

    handler.handle_conversational(_plan_route(text, 9010))

    handler.llm_client.chat_sync.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()


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
    assert partial.message and "household member" in partial.message
    assert "family_members" not in partial.message
    handler.repo.save_profile_draft.assert_called_once()

    complete_entities = {
        "name": "Alex",
        "people_count": 2,
        "family_members": [
            {"name": "Alex", "calorie_target": 2000},
            {"name": "Sam", "calorie_target": 1800},
        ],
        "dietary_constraints": [],
        "dietary_preferences": [],
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
    assert handler.repo.save_profile_draft.call_count == 1
    handler.repo.delete_profile_draft.assert_called_once_with("user")


def test_existing_profile_update_carries_revision_through_real_repository(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
) -> None:
    """An ordinary update keeps the observed revision outside its draft."""
    handler, repo = real_profile_handler
    initial = make_profile()
    assert repo.save_profile("user", initial, expected_revision=None)
    observed = repo.get_profile("user", consistent_read=True)
    assert observed is not None
    assert repo.save_profile(
        "user",
        observed.model_copy(update={"name": "Before"}),
        expected_revision=observed.profile_revision,
    )
    observed = repo.get_profile("user", consistent_read=True)
    assert observed is not None
    assert observed.profile_revision == 1

    result = handler._update_profile(
        "user",
        {"name": "After"},
        observed,
    )

    assert result.success
    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name == "After"
    assert saved.family_members == observed.family_members
    assert saved.profile_revision == 2


def test_persisted_profile_draft_completion_carries_revision(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
) -> None:
    """Completing a persisted draft does not reset profile concurrency."""
    handler, repo = real_profile_handler
    initial = make_profile()
    assert repo.save_profile("user", initial, expected_revision=None)
    observed = repo.get_profile("user", consistent_read=True)
    assert observed is not None
    assert repo.save_profile(
        "user",
        observed.model_copy(update={"name": "Before"}),
        expected_revision=observed.profile_revision,
    )
    observed = repo.get_profile("user", consistent_read=True)
    assert observed is not None
    draft = ProfileUpdateEntities(
        name="After",
        people_count=observed.people_count,
        family_members=observed.family_members,
    )
    repo.save_profile_draft("user", draft)

    result = handler._update_profile(
        "user",
        {
            "dietary_constraints": [],
            "dietary_preferences": [],
        },
        observed,
    )

    assert result.success
    saved = repo.get_profile("user", consistent_read=True)
    assert saved is not None
    assert saved.name == "After"
    assert saved.profile_revision == 2


def test_profile_onboarding_two_turn_no_value_answers_complete_profile(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    first_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "people_count": 3,
            "family_members": [
                {"name": "Nick", "calorie_target": 2200},
                {"name": "Val", "calorie_target": 1800},
                {"name": "Mike", "calorie_target": 2000},
            ],
            "dietary_constraints": "none",
            "dietary_preferences": "no preferences",
            "goals": ["eat well"],
        },
        None,
    )

    assert first_turn.success
    assert first_turn.message and "family name" in first_turn.message
    assert "dietary constraints" not in first_turn.message
    assert "restrictions" not in first_turn.message
    handler.repo.delete_profile_draft.assert_not_called()

    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    handler.repo.get_profile_draft.return_value = saved_draft
    second_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Nick", "dietary_constraints": "none"},
        None,
    )

    assert second_turn.success
    saved_profile = handler.repo.save_profile.call_args.args[1]
    assert saved_profile.name == "Nick"
    assert [member.name for member in saved_profile.family_members] == [
        "Nick",
        "Val",
        "Mike",
    ]
    assert [
        member.calorie_target for member in saved_profile.family_members
    ] == [2200, 1800, 2000]
    assert saved_profile.dietary_constraints == []
    assert saved_profile.dietary_preferences == []
    assert saved_profile.dietary_preferences == []
    handler.repo.delete_profile_draft.assert_called_once_with("user")


def test_profile_onboarding_preserves_mixed_legacy_and_extended_members(
    handler: BotHandler,
) -> None:
    """Generic draft merging retains every explicitly supplied member field."""
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    first_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "people_count": 3,
            "family_members": [
                {"name": "Alex", "calorie_target": 2000},
                {
                    "name": "Sam",
                    "calorie_target": 1800,
                    "protein_target": 120,
                },
                {
                    "name": "Lee",
                    "calorie_target": 1600,
                    "protein_target": 100,
                    "fibre_target": 30,
                },
            ],
            "dietary_constraints": [],
            "dietary_preferences": [],
            "goals": [],
        },
        None,
    )

    assert first_turn.success
    assert first_turn.message and "family name" in first_turn.message
    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    handler.repo.get_profile_draft.return_value = saved_draft

    second_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Alex"},
        None,
    )

    assert second_turn.success
    saved_profile = handler.repo.save_profile.call_args.args[1]
    assert [member.name for member in saved_profile.family_members] == [
        "Alex",
        "Sam",
        "Lee",
    ]
    assert saved_profile.family_members[0].protein_target is None
    assert saved_profile.family_members[0].fibre_target is None
    assert saved_profile.family_members[1].protein_target == 120
    assert saved_profile.family_members[1].fibre_target is None
    assert saved_profile.family_members[2].protein_target == 100
    assert saved_profile.family_members[2].fibre_target == 30
    handler.repo.delete_profile_draft.assert_called_once_with("user")


def test_profile_onboarding_rejects_ambiguous_scalar_without_draft_mutation(
    handler: BotHandler,
) -> None:
    draft = ProfileUpdateEntities(
        name="Nick",
        people_count=3,
        dietary_constraints=[],
    )
    handler.repo.get_profile_draft.return_value = draft

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"dietary_constraints": "no peanuts"},
        None,
    )

    assert not result.success
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.save_profile.assert_not_called()
    handler.repo.delete_profile_draft.assert_not_called()


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
            "dietary_constraints": [],
            "dietary_preferences": [],
            "goals": [],
        },
        None,
    )
    assert result.success
    assert result.message and "household member name" in result.message
    handler.repo.save_profile_draft.assert_called_once()
    handler.repo.save_profile.assert_not_called()


def test_profile_save_conflict_preserves_retryable_onboarding_draft(
    handler: BotHandler,
) -> None:
    """A stale ordinary save leaves the completed draft available to retry."""
    handler.repo.get_profile_draft.return_value = None
    handler.repo.save_profile.return_value = False
    entities = {
        "name": "Alex",
        "people_count": 1,
        "family_members": [
            {"name": "Alex", "calorie_target": 2000},
        ],
        "dietary_constraints": [],
        "dietary_preferences": [],
        "goals": [],
    }

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        entities,
        None,
    )

    assert not result.success
    assert result.message == (
        "That profile is stale. Your latest profile was kept; please try again."
    )
    handler.repo.save_profile.assert_called_once()
    handler.repo.save_profile_draft.assert_called_once()
    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    assert saved_draft.name == "Alex"
    assert saved_draft.people_count == 1
    assert saved_draft.family_members is not None
    assert saved_draft.model_dump().get("profile_revision") is None
    handler.repo.delete_profile_draft.assert_not_called()


def test_profile_save_conflict_replaces_existing_draft_with_latest_merge(
    handler: BotHandler,
) -> None:
    """A conflict stores the complete draft produced by the latest turn."""
    existing = make_profile().model_copy(update={"profile_revision": 3})
    persisted_draft = ProfileUpdateEntities(
        name="Earlier household",
        people_count=2,
        family_members=[
            FamilyMember(name="Alex", calorie_target=1900),
            FamilyMember(name="Sam", calorie_target=1700),
        ],
    )
    handler.repo.get_profile_draft.return_value = persisted_draft
    handler.repo.save_profile.return_value = False

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "name": "Latest household",
            "family_members": [
                {"name": "Alex", "calorie_target": 2100},
                {"name": "Sam", "calorie_target": 1800},
            ],
            "dietary_constraints": [],
            "dietary_preferences": [],
        },
        existing,
    )

    assert not result.success
    assert result.message == (
        "That profile is stale. Your latest profile was kept; please try again."
    )
    handler.repo.save_profile.assert_called_once()
    handler.repo.save_profile_draft.assert_called_once()
    assert handler.repo.save_profile_draft.call_args.args[0] == "user"
    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    assert saved_draft.name == "Latest household"
    assert saved_draft.people_count == 2
    assert saved_draft.family_members == [
        FamilyMember(name="Alex", calorie_target=2100),
        FamilyMember(name="Sam", calorie_target=1800),
    ]
    assert saved_draft.dietary_constraints == []
    assert saved_draft.dietary_preferences == []
    assert "profile_revision" not in saved_draft.model_dump()
    handler.repo.delete_profile_draft.assert_not_called()


def test_incomplete_merged_profile_remains_a_draft(
    handler: BotHandler,
) -> None:
    """An incomplete latest merge keeps accumulated editable fields."""
    persisted_draft = ProfileUpdateEntities(
        name="Earlier household",
        people_count=2,
        family_members=[
            FamilyMember(name="Alex", calorie_target=1900),
        ],
    )
    handler.repo.get_profile_draft.return_value = persisted_draft

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Latest household"},
        None,
    )

    assert result.success
    assert result.message and "dietary constraints" in result.message
    handler.repo.save_profile.assert_not_called()
    handler.repo.save_profile_draft.assert_called_once()
    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    assert saved_draft.name == "Latest household"
    assert saved_draft.people_count == 2
    assert saved_draft.family_members == persisted_draft.family_members
    assert saved_draft.dietary_constraints is None
    assert saved_draft.dietary_preferences is None


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


def test_profile_member_replacement_preserves_omitted_nutrient_targets(
    handler: BotHandler,
) -> None:
    """Calorie-only replacements retain saved optional nutrient targets."""
    existing = make_profile(with_nutrient_targets=True)
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {"name": "Alex", "calorie_target": 2100},
                {"name": "Sam", "calorie_target": 1900},
            ]
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    assert [member.calorie_target for member in saved.family_members] == [
        2100,
        1900,
    ]
    assert [member.protein_target for member in saved.family_members] == [
        120,
        100,
    ]
    assert [member.fibre_target for member in saved.family_members] == [
        30,
        None,
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected_protein", "expected_fibre"),
    [
        ("protein_target", 150, 150, 30),
        ("protein_target", None, None, 30),
        ("fibre_target", 35, 120, 35),
        ("fibre_target", None, 120, None),
    ],
)
def test_profile_member_replacement_respects_target_field_intent(
    handler: BotHandler,
    field: str,
    value: int | None,
    expected_protein: int | None,
    expected_fibre: int | None,
) -> None:
    """Explicit optional target values replace or clear saved values."""
    existing = make_profile(with_nutrient_targets=True)
    handler.repo.get_profile_draft.return_value = None
    member = {
        "name": "Alex",
        "calorie_target": 2100,
        field: value,
    }
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                member,
                {"name": "Sam", "calorie_target": 1900},
            ]
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    alex = saved.family_members[0]
    assert alex.protein_target == expected_protein
    assert alex.fibre_target == expected_fibre


def test_profile_member_replacement_prefers_persisted_draft_targets(
    handler: BotHandler,
) -> None:
    """A populated draft is the newest source for omitted target fields."""
    existing = make_profile(with_nutrient_targets=True)
    draft = ProfileUpdateEntities(
        name="Draft",
        people_count=2,
        family_members=[
            FamilyMember(
                name="Alex",
                calorie_target=2050,
                protein_target=90,
                fibre_target=20,
            ),
            FamilyMember(name="Sam", calorie_target=1750),
        ],
        dietary_constraints=[],
        dietary_preferences=[],
        goals=[],
    )
    handler.repo.get_profile_draft.return_value = draft
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {"name": "Alex", "calorie_target": 2100},
                {"name": "Sam", "calorie_target": 1900},
            ]
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    alex, sam = saved.family_members
    assert (alex.protein_target, alex.fibre_target) == (90, 20)
    assert (sam.protein_target, sam.fibre_target) == (None, None)


def test_profile_member_replacement_new_member_gets_no_inferred_targets(
    handler: BotHandler,
) -> None:
    """Unmatched replacement members retain absent optional targets."""
    existing = make_profile(with_nutrient_targets=True)
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "people_count": 3,
            "family_members": [
                {"name": "Alex", "calorie_target": 2100},
                {"name": "Sam", "calorie_target": 1900},
                {"name": "Taylor", "calorie_target": 1700},
            ],
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    taylor = saved.family_members[-1]
    assert taylor.name == "Taylor"
    assert (taylor.protein_target, taylor.fibre_target) == (None, None)


def test_profile_size_change_falls_back_to_saved_targets_after_empty_draft(
    handler: BotHandler,
) -> None:
    """Replacement members inherit targets after a size-only draft turn."""
    existing = make_profile(with_nutrient_targets=True)
    handler.repo.get_profile_draft.return_value = None
    first_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"people_count": 3},
        existing,
    )

    assert first_turn.success
    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    assert saved_draft.family_members is None
    handler.repo.get_profile_draft.return_value = saved_draft

    second_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {"name": "Alex", "calorie_target": 2100},
                {"name": "Sam", "calorie_target": 1900},
                {"name": "Taylor", "calorie_target": 1700},
            ]
        },
        existing,
    )

    assert second_turn.success
    saved = handler.repo.save_profile.call_args.args[1]
    alex, sam, taylor = saved.family_members
    assert (alex.protein_target, alex.fibre_target) == (120, 30)
    assert (sam.protein_target, sam.fibre_target) == (100, None)
    assert (taylor.protein_target, taylor.fibre_target) == (None, None)


def test_profile_member_replacement_ambiguous_source_does_not_write(
    handler: BotHandler,
) -> None:
    """Ambiguous legacy sources cannot provide inherited target values."""
    existing = make_profile().model_copy(
        update={
            "family_members": [
                FamilyMember(
                    name="Sam", calorie_target=1800, protein_target=100
                ),
                FamilyMember(
                    name=" sam ", calorie_target=1900, fibre_target=25
                ),
            ]
        }
    )
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {"name": "Sam", "calorie_target": 2000},
                {"name": "Alex", "calorie_target": 2100},
            ]
        },
        existing,
    )

    assert not result.success
    handler.repo.save_profile.assert_not_called()
    handler.repo.save_profile_draft.assert_not_called()


@pytest.mark.parametrize(
    ("protein_target", "fibre_target"),
    [(150, 35), (None, None), (None, 35), (150, None)],
)
def test_profile_replacement_explicit_targets_bypass_ambiguous_saved_source(
    handler: BotHandler,
    protein_target: int | None,
    fibre_target: int | None,
) -> None:
    """Explicit targets do not require resolving duplicate saved members."""
    existing = make_profile(with_nutrient_targets=True).model_copy(
        update={
            "family_members": [
                FamilyMember(
                    name="Sam",
                    calorie_target=1800,
                    protein_target=100,
                ),
                FamilyMember(
                    name=" sam ",
                    calorie_target=1900,
                    fibre_target=25,
                ),
            ]
        }
    )
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {
                    "name": "Sam",
                    "calorie_target": 2100,
                    "protein_target": protein_target,
                    "fibre_target": fibre_target,
                },
                {
                    "name": "Taylor",
                    "calorie_target": 1900,
                    "protein_target": 90,
                    "fibre_target": 20,
                },
            ]
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    sam, taylor = saved.family_members
    assert (sam.name, sam.calorie_target) == ("Sam", 2100)
    assert (sam.protein_target, sam.fibre_target) == (
        protein_target,
        fibre_target,
    )
    assert (taylor.protein_target, taylor.fibre_target) == (90, 20)
    handler.repo.save_profile.assert_called_once()
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.delete_profile_draft.assert_called_once_with("user")


@pytest.mark.parametrize(
    ("protein_target", "fibre_target"),
    [(150, 35), (None, None), (None, 35), (150, None)],
)
def test_profile_replacement_explicit_targets_bypass_ambiguous_draft_source(
    handler: BotHandler,
    protein_target: int | None,
    fibre_target: int | None,
) -> None:
    """Explicit targets do not require resolving duplicate draft members."""
    existing = make_profile(with_nutrient_targets=True)
    draft = ProfileUpdateEntities(
        name="Draft",
        people_count=2,
        family_members=[
            FamilyMember(name="Sam", calorie_target=1800),
            FamilyMember(name=" sam ", calorie_target=1900),
        ],
        dietary_constraints=[],
        dietary_preferences=[],
        goals=[],
    )
    handler.repo.get_profile_draft.return_value = draft
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                {
                    "name": "Sam",
                    "calorie_target": 2100,
                    "protein_target": protein_target,
                    "fibre_target": fibre_target,
                },
                {
                    "name": "Taylor",
                    "calorie_target": 1900,
                    "protein_target": 90,
                    "fibre_target": 20,
                },
            ]
        },
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    sam, taylor = saved.family_members
    assert (sam.name, sam.calorie_target) == ("Sam", 2100)
    assert (sam.protein_target, sam.fibre_target) == (
        protein_target,
        fibre_target,
    )
    assert (taylor.protein_target, taylor.fibre_target) == (90, 20)
    handler.repo.save_profile.assert_called_once()
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.delete_profile_draft.assert_called_once_with("user")


@pytest.mark.parametrize(
    ("source", "omitted_field"),
    [
        ("saved", "protein_target"),
        ("saved", "fibre_target"),
        ("draft", "protein_target"),
        ("draft", "fibre_target"),
    ],
)
def test_profile_replacement_ambiguous_required_source_does_not_write(
    handler: BotHandler,
    source: str,
    omitted_field: str,
) -> None:
    """Omitted targets still require an unambiguous source member."""
    existing = make_profile(with_nutrient_targets=True)
    if source == "saved":
        existing = existing.model_copy(
            update={
                "family_members": [
                    FamilyMember(
                        name="Sam", calorie_target=1800, protein_target=100
                    ),
                    FamilyMember(
                        name=" sam ", calorie_target=1900, fibre_target=25
                    ),
                ]
            }
        )
        handler.repo.get_profile_draft.return_value = None
    else:
        handler.repo.get_profile_draft.return_value = ProfileUpdateEntities(
            name="Draft",
            people_count=2,
            family_members=[
                FamilyMember(name="Sam", calorie_target=1800),
                FamilyMember(name=" sam ", calorie_target=1900),
            ],
            dietary_constraints=[],
            dietary_preferences=[],
            goals=[],
        )

    sam = {
        "name": "Sam",
        "calorie_target": 2100,
        "protein_target": 120,
        "fibre_target": 30,
    }
    del sam[omitted_field]
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "family_members": [
                sam,
                {
                    "name": "Taylor",
                    "calorie_target": 1900,
                    "protein_target": 90,
                    "fibre_target": 20,
                },
            ]
        },
        existing,
    )

    assert not result.success
    assert result.message == (
        "Family member names must be unique, ignoring capitalization and "
        "surrounding spaces."
    )
    handler.repo.save_profile.assert_not_called()
    handler.repo.save_profile_draft.assert_not_called()
    handler.repo.delete_profile_draft.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.save_profile_and_transition_state.assert_not_called()


def test_existing_profile_same_size_update_preserves_members(
    handler: BotHandler,
) -> None:
    existing = make_profile()
    handler.repo.get_profile_draft.return_value = None
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"people_count": 2},
        existing,
    )

    assert result.success
    saved = handler.repo.save_profile.call_args.args[1]
    assert [member.name for member in saved.family_members] == [
        "Alex",
        "Sam",
    ]
    assert saved.dietary_constraints == []


def test_profile_onboarding_rejects_incomplete_vegan_constraint(
    handler: BotHandler,
) -> None:
    """Onboarding does not save an incompletely represented vegan rule."""
    handler.repo.get_profile_draft.return_value = ProfileUpdateEntities()
    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {
            "name": "Alex",
            "people_count": 1,
            "family_members": [{"name": "Alex", "calorie_target": 2000}],
            "allergies": ["Peanuts", "no allergies"],
            "dietary_preferences": [],
            "restrictions": ["Vegan", "no restrictions"],
            "goals": [],
        },
        None,
    )

    assert not result.success
    assert result.message is not None
    assert "through /profile" in result.message
    handler.repo.save_profile.assert_not_called()


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


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_edit_accepts_last_day_and_rejects_next_day(
    handler: BotHandler, plan_days: int
) -> None:
    """Edits can address every persisted day but cannot create a new one."""
    plan = make_plan(plan_days=plan_days)
    handler.repo.get_latest_plan.return_value = plan
    handler.repo.update_meal.return_value = True

    accepted = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {"day": plan_days, "meal_type": "lunch", "name": "New lunch"},
        None,
    )
    assert accepted.success
    handler.repo.update_meal.assert_called_once()

    handler.repo.update_meal.reset_mock()
    rejected = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.EDIT_PLAN,
        {
            "day": plan_days + 1,
            "meal_type": "lunch",
            "name": "Out of range",
        },
        None,
    )
    assert not rejected.success
    assert rejected.message == "That day or meal does not exist."
    handler.repo.update_meal.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_confirmation_starts_grocery_generation(
    handler: BotHandler, plan_days: int
) -> None:
    """Confirmation publishes a short plan and starts its grocery worker."""
    plan = make_plan(plan_days=plan_days)
    handler.repo.get_latest_plan.return_value = plan
    handler.repo.confirm_plan.return_value = True

    result = handler._apply_intent_metadata(
        "user", 1, ConversationIntent.CONFIRM_PLAN, {}, None
    )

    assert result.success
    handler.repo.confirm_plan.assert_called_once_with(
        "user", plan.week_start_date, plan.revision
    )
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["action"] == "finalize_grocery"
    assert payload["week_start"] == plan.week_start_date


@pytest.mark.parametrize("plan_days", [1, 3])
def test_today_displays_the_current_day_of_a_short_plan(
    handler: BotHandler, plan_days: int
) -> None:
    """/today renders a current day that may be the short plan's last day."""
    plan = make_plan(
        week_start=date.today() - timedelta(days=plan_days - 1),
        plan_days=plan_days,
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_active_plan.return_value = plan

    handler._cmd_today(1, "user")

    message = handler.telegram_api.send_message.call_args.args[1]
    assert f"Day {plan_days}" in message
    assert f"Lunch {plan_days}" in message
    assert f"Day {plan_days + 1}" not in message


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_checkin_and_outcome_use_actual_last_day(
    handler: BotHandler, plan_days: int
) -> None:
    """Check-in buttons and outcomes remain bounded by persisted plan days."""
    plan = make_plan(
        week_start=date.today() - timedelta(days=plan_days - 1),
        plan_days=plan_days,
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_active_plan.return_value = plan
    handler._cmd_checkin(1, "user")
    handler.telegram_api.send_meal_checkin.assert_called_once_with(
        1,
        plan.days[-1].meals,
        week_start=plan.week_start_date,
        day=plan_days,
    )

    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=2
    )
    handler.repo.update_meal_outcome.return_value = True
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=(
            f"checkin:{plan.week_start_date}:{plan_days}:lunch:cooked"
        ),
    )
    handler.handle_callback(route)

    handler.repo.update_meal_outcome.assert_called_once_with(
        "user",
        plan.week_start_date,
        plan_days,
        "lunch",
        MealOutcome.COOKED,
        expected_epoch=2,
    )


@pytest.mark.parametrize("plan_days", [1, 3])
def test_short_plan_callback_rejects_day_after_persisted_end(
    handler: BotHandler, plan_days: int
) -> None:
    """Forged callbacks cannot target a day absent from the active plan."""
    plan = make_plan(
        week_start=date.today() - timedelta(days=plan_days - 1),
        plan_days=plan_days,
        status=PlanStatus.CONFIRMED,
    )
    handler.repo.get_active_plan_snapshot.return_value = ActivePlanSnapshot(
        plan=plan, active_epoch=2
    )
    route = RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="query",
        callback_data=(
            f"checkin:{plan.week_start_date}:{plan_days + 1}:lunch:cooked"
        ),
    )

    handler.handle_callback(route)

    handler.repo.update_meal_outcome.assert_not_called()
    assert handler.telegram_api.send_message.call_args_list == [
        call(1, "That check-in button is invalid or outdated.")
    ]
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "query", "Invalid check-in"
    )


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


def _meal_callback_route(action: str, submission_id: str) -> RouteResult:
    return RouteResult(
        route_type=RouteType.CALLBACK,
        chat_id=1,
        user_id="user",
        callback_query_id="meal-query",
        callback_data=f"meal:{action}:{submission_id}",
    )


def _meal_confirmation_state(
    handler: BotHandler,
    *,
    step: ConversationWorkflowStep = (
        ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
    ),
) -> ConversationState:
    state = handler._new_submission_state()
    return state.model_copy(
        update={
            "step": step,
            "meal_draft": MealLogDraft(
                date=date(2026, 8, 22),
                meal_type=MealType.LUNCH,
                description="vegetable soup",
            ),
        }
    )


def test_meal_confirm_saves_once_and_sends_continuation_keyboard(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(handler)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.confirm_meal_and_transition.return_value = True

    handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    confirm_call = handler.repo.confirm_meal_and_transition.call_args
    assert confirm_call is not None
    assert confirm_call.args[0] == "user"
    assert confirm_call.kwargs == {
        "expected_revision": state.revision,
        "submission_id": state.request_id,
        "processing_date": date.today(),
    }
    saved_state = confirm_call.args[2]
    assert (
        saved_state.step is ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
    )
    assert saved_state.revision == state.revision + 1
    handler.telegram_api.send_meal_saved.assert_called_once_with(
        1, "vegetable soup", state.request_id
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Meal saved"
    )


def test_batch_meal_confirm_persists_the_confirmed_role_and_clears_pending(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(handler).model_copy(
        update={
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-1", role="preparation", total_yield=2
            )
        }
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.confirm_meal_and_transition.return_value = True

    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="meal-query",
            callback_data=(f"meal:confirm:{state.request_id}:preparation"),
        )
    )

    confirm_call = handler.repo.confirm_meal_and_transition.call_args
    assert confirm_call is not None
    assert confirm_call.args[1].batch_link is not None
    assert confirm_call.args[1].batch_link.batch_id == "batch-1"
    assert confirm_call.args[1].batch_link.role.value == "preparation"
    assert confirm_call.args[2].pending_batch_link is None


def test_late_batch_confirmation_keeps_review_and_inventory_unchanged(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
) -> None:
    """A late confirmation cannot partially persist any workflow state."""
    original_handler, repo = real_profile_handler
    preparation_date = date(2026, 8, 7)
    processing_datetime = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    state = _meal_confirmation_state(original_handler).model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=preparation_date,
                meal_type=MealType.DINNER,
                description="roast chicken",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="batch-late", role=BatchMealRole.PREPARATION
            ),
        }
    )
    source = make_batch_ledger_entry("batch-late").model_copy(
        update={
            "source_plan_id": "plan-2026-08-03",
            "preparation_date": preparation_date,
            "preparation_meal_type": MealType.DINNER,
            "week_end": date(2026, 8, 9),
        }
    )
    assert repo.save_conversation_state("user", state)
    repo.put_weekly_batch_ledger(
        "user", WeeklyBatchLedger(iso_week="2026-W32", entries=[source])
    )
    late_handler = BotHandler(
        repo,
        original_handler.telegram_api,
        access_policy=TelegramAccessPolicy(frozenset({"1"})),
        processing_date=processing_datetime.date(),
    )

    late_handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    assert repo.get_conversation_state("user") == state
    assert (
        repo.get_submitted_meals(
            "user", start_date=preparation_date, end_date=preparation_date
        )
        == []
    )
    assert (
        repo.table.get_item(
            Key={"PK": "USER#user", "SK": f"MEAL_UPDATE#{state.request_id}"}
        ).get("Item")
        is None
    )
    saved = repo.get_weekly_batch_ledger("user", "2026-W32").entries[0]
    assert saved.state is BatchLedgerState.PROVISIONAL
    assert saved.remaining_portions == 1


def test_raced_expiry_removes_batch_before_handler_submission(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """A conflicted inventory read cannot offer or accept expired stock."""
    handler, repo = real_profile_handler
    processing_date = date(2026, 8, 7)
    handler._processing_date = processing_date
    source = make_batch_ledger_entry("raced-expiry").model_copy(
        update={
            "preparation_date": date(2026, 8, 6),
            "preparation_meal_type": MealType.LUNCH,
            "week_end": date(2026, 8, 9),
            "state": BatchLedgerState.PROVISIONAL,
        }
    )
    original = WeeklyBatchLedger(iso_week="2026-W32", entries=[source])
    winner = original.model_copy(
        update={
            "revision": 1,
            "entries": [
                source.model_copy(
                    update={
                        "state": BatchLedgerState.EXPIRED,
                        "remaining_portions": 0,
                    }
                )
            ],
        }
    )
    repo.put_weekly_batch_ledger("user", original)
    original_put = repo._put_weekly_batch_ledger_conditionally
    put_attempts = 0

    def race_then_fail(*args: Any, **kwargs: Any) -> None:
        nonlocal put_attempts
        put_attempts += 1
        if put_attempts == 1:
            repo.put_weekly_batch_ledger("user", winner)
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )
        original_put(*args, **kwargs)

    mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=race_then_fail,
    )

    assert repo.get_available_batch_portions("user", processing_date) == []
    assert put_attempts == 1

    state = _meal_confirmation_state(handler).model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=processing_date,
                meal_type=MealType.LUNCH,
                description="raced leftover",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="raced-expiry",
                role=BatchMealRole.LEFTOVER,
                source_date=date(2026, 8, 6),
                source_meal_type=MealType.LUNCH,
                portion=2,
            ),
        }
    )
    assert repo.save_conversation_state("user", state)

    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="raced-expiry-query",
            callback_data=(f"meal:confirm:{state.request_id}:leftover"),
        )
    )

    assert repo.get_conversation_state("user", consistent_read=True) == state
    assert (
        repo.get_submitted_meals(
            "user", start_date=processing_date, end_date=processing_date
        )
        == []
    )
    assert (
        repo.table.get_item(
            Key={
                "PK": "USER#user",
                "SK": f"MEAL_UPDATE#{state.request_id}",
            }
        ).get("Item")
        is None
    )
    assert (
        "stale or outdated"
        in (handler.telegram_api.send_message.call_args.args[1])
    )


def test_final_raced_expiry_winner_is_not_offered_or_submitted(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """A final-conflict expiry winner is current at both handler boundaries."""
    handler, repo = real_profile_handler
    processing_date = date(2026, 8, 7)
    handler._processing_date = processing_date
    source = make_batch_ledger_entry("final-raced-expiry").model_copy(
        update={
            "preparation_date": date(2026, 8, 6),
            "preparation_meal_type": MealType.LUNCH,
            "week_end": date(2026, 8, 9),
            "state": BatchLedgerState.PROVISIONAL,
        }
    )
    original = WeeklyBatchLedger(iso_week="2026-W32", entries=[source])
    winner = original.model_copy(
        update={
            "revision": 9,
            "entries": [
                source.model_copy(
                    update={
                        "state": BatchLedgerState.EXPIRED,
                        "remaining_portions": 0,
                    }
                )
            ],
        }
    )
    repo.put_weekly_batch_ledger("user", original)
    put_attempts = 0

    def conflict_then_final_winner(*args: Any, **kwargs: Any) -> None:
        nonlocal put_attempts
        put_attempts += 1
        if put_attempts == 3:
            repo.put_weekly_batch_ledger("user", winner)
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )

    mocker.patch.object(
        repo,
        "_put_weekly_batch_ledger_conditionally",
        side_effect=conflict_then_final_winner,
    )

    assert repo.get_available_batch_portions("user", processing_date) == []
    assert put_attempts == 3

    state = _meal_confirmation_state(handler).model_copy(
        update={
            "meal_draft": MealLogDraft(
                date=processing_date,
                meal_type=MealType.LUNCH,
                description="final raced leftover",
            ),
            "pending_batch_link": PlannedBatchLink(
                batch_id="final-raced-expiry",
                role=BatchMealRole.LEFTOVER,
                source_date=date(2026, 8, 6),
                source_meal_type=MealType.LUNCH,
                portion=2,
            ),
        }
    )
    assert repo.save_conversation_state("user", state)

    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="final-raced-expiry-query",
            callback_data=(f"meal:confirm:{state.request_id}:leftover"),
        )
    )

    assert repo.get_conversation_state("user", consistent_read=True) == state
    assert repo.get_meal_history("user", days=7) == []
    assert (
        repo.table.get_item(
            Key={
                "PK": "USER#user",
                "SK": f"MEAL_UPDATE#{state.request_id}",
            }
        ).get("Item")
        is None
    )
    assert (
        "stale or outdated"
        in (handler.telegram_api.send_message.call_args.args[1])
    )


def test_provider_reversed_batch_plan_is_not_published_for_submission(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """The real planner boundary rejects an unfulfillable provider order."""
    handler, repo = real_profile_handler
    week = date(2026, 8, 24)
    profile, batch_rule = _wednesday_profile_fixture(total_yield=3)
    assert repo.save_profile("user", profile, expected_revision=None)

    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _three_portion_provider_payload(
        week, reversed_ordinals=True
    )
    planner = PlannerHandler(repo, handler.telegram_api, llm_client=llm)

    planner.generate_plan(
        "user",
        1,
        week_start=week,
        batch_rules=[batch_rule],
        available_batches=[],
    )

    assert repo.get_plan("user", week) is None
    handler.telegram_api.send_plan.assert_not_called()


def test_published_canonical_batch_plan_submits_portions_in_date_order(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
    mocker: Any,
) -> None:
    """A canonical generated batch supports preparation, 2, then 3."""
    handler, repo = real_profile_handler
    week = date(2026, 8, 24)
    profile, batch_rule = _wednesday_profile_fixture(total_yield=3)
    assert repo.save_profile("user", profile, expected_revision=None)

    llm = mocker.MagicMock()
    llm.chat_json_sync.return_value = _three_portion_provider_payload(
        week, reversed_ordinals=False
    )
    planner = PlannerHandler(repo, handler.telegram_api, llm_client=llm)
    planner.generate_plan(
        "user",
        1,
        week_start=week,
        batch_rules=[batch_rule],
        available_batches=[],
    )
    assert repo.get_plan("user", week) is not None

    handler._processing_date = week
    handler.handle_command(_command("submit_meals"))
    handler.handle_conversational(
        _route_on(week, "2026-08-24, lunch, chicken preparation", 14000)
    )
    preparation_review = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert preparation_review is not None
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="preparation-query",
            callback_data=(
                f"meal:confirm:{preparation_review.request_id}:preparation"
            ),
        )
    )

    handler._processing_date = week + timedelta(days=1)
    handler.handle_callback(
        _meal_callback_route("add", preparation_review.request_id or "")
    )
    handler.handle_conversational(
        _route_on(
            week + timedelta(days=1),
            "2026-08-25, lunch, chicken leftover",
            14001,
        )
    )
    portion_two_review = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert portion_two_review is not None
    assert portion_two_review.pending_batch_link is not None
    assert portion_two_review.pending_batch_link.portion == 2
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="portion-two-query",
            callback_data=(
                f"meal:confirm:{portion_two_review.request_id}:leftover"
            ),
        )
    )

    handler._processing_date = week + timedelta(days=2)
    continuation = repo.get_conversation_state("user", consistent_read=True)
    assert continuation is not None
    handler.handle_callback(
        _meal_callback_route("add", continuation.request_id or "")
    )
    handler.handle_conversational(
        _route_on(
            week + timedelta(days=2),
            "2026-08-26, lunch, chicken leftover",
            14002,
        )
    )
    portion_three_review = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert portion_three_review is not None
    assert portion_three_review.pending_batch_link is not None
    assert portion_three_review.pending_batch_link.portion == 3
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="portion-three-query",
            callback_data=(
                f"meal:confirm:{portion_three_review.request_id}:leftover"
            ),
        )
    )

    history = repo.get_meal_history("user", days=7)
    portions = sorted(
        entry.batch_link.portion
        for entry in history
        if entry.batch_link is not None
    )
    assert portions == [1, 2, 3]


def test_wednesday_batch_preparation_then_leftover_consumption_is_atomic(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
) -> None:
    """A three-meal batch is consumed across separate fresh plans."""
    handler, repo = real_profile_handler
    handler._processing_date = date(2026, 8, 25)
    profile, batch_rule = _wednesday_profile_fixture(total_yield=3)
    assert batch_rule.total_yield == 3
    assert repo.save_profile("user", profile, expected_revision=None)

    preparation_date = date(2026, 8, 25)
    later_date = date(2026, 8, 26)
    preparation_plan = make_plan(
        week_start=preparation_date,
        plan_days=1,
    )
    preparation_plan.days[0].meals[0].batch_link = PlannedBatchLink(
        batch_id="batch-chicken",
        role=BatchMealRole.PREPARATION,
        total_yield=3,
    )
    provisional = make_batch_ledger_entry("batch-chicken").model_copy(
        update={
            "source_plan_id": "plan-2026-08-25",
            "preparation_date": preparation_date,
            "preparation_meal_type": MealType.LUNCH,
            "week_end": date(2026, 8, 30),
            "state": BatchLedgerState.PROVISIONAL,
            "total_portions": 3,
            "remaining_portions": 2,
        }
    )
    assert repo.save_generated_draft(
        "user",
        preparation_plan,
        expected_revision=None,
        batch_entries=[provisional],
    )

    handler.handle_command(_command("submit_meals"))
    handler.handle_conversational(
        _route_on(
            preparation_date,
            "2026-08-25, lunch, chicken batch lunch",
            13000,
        )
    )
    preparation_state = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert preparation_state is not None
    assert preparation_state.pending_batch_link is not None
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="preparation-query",
            callback_data=(
                f"meal:confirm:{preparation_state.request_id}:preparation"
            ),
        )
    )
    activated = repo.get_weekly_batch_ledger("user", "2026-W35")
    assert activated.entries[0].state is BatchLedgerState.AVAILABLE
    assert activated.entries[0].remaining_portions == 2

    final_date = date(2026, 8, 27)
    final_plan = make_plan(week_start=final_date, plan_days=1)
    final_plan.days[0].meals[0].batch_link = PlannedBatchLink(
        batch_id="batch-chicken",
        role=BatchMealRole.LEFTOVER,
        source_date=preparation_date,
        source_meal_type=MealType.LUNCH,
        portion=3,
    )
    assert repo.save_generated_draft(
        "user", final_plan, expected_revision=None, batch_entries=[]
    )

    # A separate guided workflow must not accept the high ordinal early.
    handler.handle_callback(
        _meal_callback_route("add", preparation_state.request_id or "")
    )
    handler.handle_conversational(
        _route_on(final_date, "2026-08-27, lunch, chicken leftover", 13001)
    )
    early_review = repo.get_conversation_state("user", consistent_read=True)
    assert early_review is not None
    assert early_review.pending_batch_link is not None
    assert early_review.pending_batch_link.portion == 3
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="early-confirm-query",
            callback_data=f"meal:confirm:{early_review.request_id}:leftover",
        )
    )
    early_message = handler.telegram_api.send_message.call_args.args[1]
    assert "stale or outdated" in early_message
    assert "Nothing was changed" in early_message
    assert repo.get_conversation_state("user", consistent_read=True) == (
        early_review
    )
    assert (
        len(
            repo.get_submitted_meals(
                "user", start_date=preparation_date, end_date=final_date
            )
        )
        == 1
    )
    assert (
        repo.table.get_item(
            Key={
                "PK": "USER#user",
                "SK": f"MEAL_UPDATE#{early_review.request_id}",
            }
        ).get("Item")
        is None
    )
    unchanged = repo.get_weekly_batch_ledger("user", "2026-W35")
    assert unchanged.revision == activated.revision
    assert unchanged.entries[0].remaining_portions == 2
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="early-cancel-query",
            callback_data=f"meal:cancel:{early_review.request_id}",
        )
    )
    assert repo.get_conversation_state("user", consistent_read=True) is None

    leftover_plan = make_plan(week_start=later_date, plan_days=1)
    leftover_plan.days[0].meals[0].batch_link = PlannedBatchLink(
        batch_id="batch-chicken",
        role=BatchMealRole.LEFTOVER,
        source_date=preparation_date,
        source_meal_type=MealType.LUNCH,
        portion=2,
    )
    assert repo.save_generated_draft(
        "user", leftover_plan, expected_revision=None, batch_entries=[]
    )
    assert repo.get_weekly_batch_ledger("user", "2026-W35").entries[
        0
    ].state is (BatchLedgerState.AVAILABLE)

    handler.handle_command(_command("submit_meals"))
    preparation_state = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert preparation_state is not None
    handler.handle_callback(
        _meal_callback_route("add", preparation_state.request_id or "")
    )
    leftover_state = repo.get_conversation_state("user", consistent_read=True)
    assert leftover_state is not None
    handler.handle_conversational(
        _route_on(later_date, "2026-08-26, lunch, chicken leftover", 13001)
    )
    review = repo.get_conversation_state("user", consistent_read=True)
    assert review is not None
    assert review.pending_batch_link is not None
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="leftover-query",
            callback_data=f"meal:confirm:{review.request_id}:leftover",
        )
    )

    consumed = repo.get_weekly_batch_ledger("user", "2026-W35")
    assert consumed.entries[0].state is BatchLedgerState.AVAILABLE
    assert consumed.entries[0].remaining_portions == 1

    continuation = repo.get_conversation_state("user", consistent_read=True)
    assert continuation is not None
    handler.handle_callback(
        _meal_callback_route("add", continuation.request_id or "")
    )
    handler.handle_conversational(
        _route_on(final_date, "2026-08-27, lunch, chicken leftover", 13002)
    )
    final_review = repo.get_conversation_state("user", consistent_read=True)
    assert final_review is not None
    assert final_review.pending_batch_link is not None
    assert final_review.pending_batch_link.portion == 3
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="final-leftover-query",
            callback_data=(f"meal:confirm:{final_review.request_id}:leftover"),
        )
    )

    exhausted = repo.get_weekly_batch_ledger("user", "2026-W35")
    assert exhausted.entries[0].state is BatchLedgerState.EXHAUSTED
    assert exhausted.entries[0].remaining_portions == 0
    history = repo.get_meal_history("user", days=7, on_date=later_date)
    assert [entry.batch_link.role for entry in history if entry.batch_link] == [
        BatchMealRole.LEFTOVER,
        BatchMealRole.PREPARATION,
    ]
    assert repo.get_meal_history("user", days=7, on_date=final_date)

    # A later guided workflow replaying portion 3 is rejected after it was
    # already made unavailable by the ordered portion-2 submission.
    continuation = repo.get_conversation_state("user", consistent_read=True)
    assert continuation is not None
    handler.handle_callback(
        _meal_callback_route("add", continuation.request_id or "")
    )
    handler.handle_conversational(
        _route_on(final_date, "2026-08-27, lunch, chicken leftover", 13003)
    )
    replay_review = repo.get_conversation_state("user", consistent_read=True)
    assert replay_review is not None
    assert replay_review.pending_batch_link is not None
    assert replay_review.pending_batch_link.portion == 3
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="replay-confirm-query",
            callback_data=f"meal:confirm:{replay_review.request_id}:leftover",
        )
    )
    replay_message = handler.telegram_api.send_message.call_args.args[1]
    assert "stale or outdated" in replay_message
    assert "Nothing was changed" in replay_message
    assert repo.get_conversation_state("user", consistent_read=True) == (
        replay_review
    )
    assert (
        len(
            repo.get_submitted_meals(
                "user", start_date=preparation_date, end_date=final_date
            )
        )
        == 3
    )
    replay_ledger = repo.get_weekly_batch_ledger("user", "2026-W35")
    assert replay_ledger.revision == exhausted.revision
    assert replay_ledger.entries[0].remaining_portions == 0
    assert replay_ledger.entries[0].state is BatchLedgerState.EXHAUSTED
    assert (
        repo.table.get_item(
            Key={
                "PK": "USER#user",
                "SK": f"MEAL_UPDATE#{replay_review.request_id}",
            }
        ).get("Item")
        is None
    )


def test_backdated_batch_submission_uses_preceding_covering_plan(
    real_profile_handler: tuple[BotHandler, DynamoRepository],
) -> None:
    """A newer plan cannot hide links for an earlier guided submission."""
    handler, repo = real_profile_handler
    handler._processing_date = date(2026, 8, 20)
    preparation_date = date(2026, 8, 20)
    leftover_date = date(2026, 8, 21)
    covering_plan = make_plan(
        week_start=date(2026, 8, 17),
        plan_days=7,
    )
    covering_plan.days[3].meals[0].batch_link = PlannedBatchLink(
        batch_id="backdated-batch",
        role=BatchMealRole.PREPARATION,
        total_yield=2,
    )
    covering_plan.days[4].meals[0].batch_link = PlannedBatchLink(
        batch_id="backdated-batch",
        role=BatchMealRole.LEFTOVER,
        source_date=preparation_date,
        source_meal_type=MealType.LUNCH,
        portion=2,
    )
    source = make_batch_ledger_entry("backdated-batch").model_copy(
        update={
            "source_plan_id": "plan-2026-08-17",
            "preparation_date": preparation_date,
            "preparation_meal_type": MealType.LUNCH,
            "week_end": date(2026, 8, 23),
        }
    )
    assert repo.save_generated_draft(
        "user", covering_plan, expected_revision=None, batch_entries=[source]
    )
    assert repo.save_generated_draft(
        "user",
        make_plan(week_start=date(2026, 8, 24), plan_days=3),
        expected_revision=None,
        batch_entries=[],
    )

    handler.handle_command(_command("submit_meals"))
    handler.handle_conversational(
        _route_on(
            preparation_date,
            "2026-08-20, lunch, backdated chicken preparation",
            14000,
        )
    )
    preparation_review = repo.get_conversation_state(
        "user", consistent_read=True
    )
    assert preparation_review is not None
    assert preparation_review.pending_batch_link is not None
    assert preparation_review.pending_batch_link.batch_id == "backdated-batch"
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="backdated-preparation-query",
            callback_data=(
                f"meal:confirm:{preparation_review.request_id}:preparation"
            ),
        )
    )

    continuation = repo.get_conversation_state("user", consistent_read=True)
    assert continuation is not None
    handler.handle_callback(
        _meal_callback_route("add", continuation.request_id or "")
    )
    handler.handle_conversational(
        _route_on(
            leftover_date,
            "2026-08-21, lunch, backdated chicken leftover",
            14001,
        )
    )
    leftover_review = repo.get_conversation_state("user", consistent_read=True)
    assert leftover_review is not None
    assert leftover_review.pending_batch_link is not None
    assert leftover_review.pending_batch_link.batch_id == "backdated-batch"
    handler.handle_callback(
        RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=1,
            user_id="user",
            callback_query_id="backdated-leftover-query",
            callback_data=(
                f"meal:confirm:{leftover_review.request_id}:leftover"
            ),
        )
    )

    ledger = repo.get_weekly_batch_ledger("user", "2026-W34")
    assert ledger.entries[0].state is BatchLedgerState.EXHAUSTED
    history = repo.get_submitted_meals(
        "user", start_date=preparation_date, end_date=leftover_date
    )
    assert [
        entry.batch_link.portion for entry in history if entry.batch_link
    ] == [2, 1]
    assert all(
        entry.batch_link is not None
        and entry.batch_link.batch_id == "backdated-batch"
        for entry in history
    )


def test_meal_confirm_delivery_failure_keeps_saved_result_and_acknowledges(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(handler)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.confirm_meal_and_transition.return_value = True
    handler.telegram_api.send_meal_saved.side_effect = TelegramAPIError(
        "delivery failed"
    )

    handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    handler.repo.confirm_meal_and_transition.assert_called_once()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Meal saved"
    )


def test_repeated_confirm_retries_continuation_after_delivery_failure(
    handler: BotHandler,
) -> None:
    """A repeated Confirm restores controls without saving a second meal."""
    state = _meal_confirmation_state(handler)
    saved = state.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "revision": state.revision + 1,
        }
    )
    handler.repo.get_conversation_state.side_effect = [state, saved]
    handler.repo.confirm_meal_and_transition.return_value = True
    handler.telegram_api.send_meal_saved.side_effect = TelegramAPIError(
        "delivery failed"
    )

    callback = _meal_callback_route("confirm", state.request_id or "")
    handler.handle_callback(callback)
    handler.handle_callback(callback)

    handler.repo.confirm_meal_and_transition.assert_called_once()
    assert handler.telegram_api.send_meal_saved.call_count == 2
    handler.telegram_api.send_meal_saved.assert_any_call(
        1, "vegetable soup", state.request_id
    )
    assert handler.telegram_api.answer_callback_query.call_args_list == [
        call("meal-query", "Meal saved"),
        call("meal-query", "Already saved"),
    ]


def test_conditional_confirm_loss_reemits_saved_continuation(
    handler: BotHandler,
) -> None:
    """A matching state reloaded after contention gets its controls back."""
    state = _meal_confirmation_state(handler)
    saved = state.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "revision": state.revision + 1,
        }
    )
    handler.repo.get_conversation_state.side_effect = [state, saved]
    handler.repo.confirm_meal_and_transition.return_value = False

    handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    handler.repo.confirm_meal_and_transition.assert_called_once()
    handler.telegram_api.send_meal_saved.assert_called_once_with(
        1, "vegetable soup", state.request_id
    )
    handler.telegram_api.send_message.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Already saved"
    )


def test_repeated_meal_confirm_reports_already_saved_without_duplicate_write(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(handler)
    saved = state.model_copy(
        update={
            "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "revision": state.revision + 1,
        }
    )
    handler.repo.get_conversation_state.side_effect = [state, saved]
    handler.repo.confirm_meal_and_transition.return_value = False

    handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    handler.repo.confirm_meal_and_transition.assert_called_once()
    handler.telegram_api.send_meal_saved.assert_called_once_with(
        1, "vegetable soup", state.request_id
    )
    handler.telegram_api.send_message.assert_not_called()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Already saved"
    )


def test_meal_confirm_persistence_failure_keeps_review_retryable(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(handler)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.confirm_meal_and_transition.side_effect = RuntimeError(
        "database unavailable"
    )

    handler.handle_callback(
        _meal_callback_route("confirm", state.request_id or "")
    )

    message = handler.telegram_api.send_message.call_args.args[1]
    assert "try again" in message.lower()
    assert "review" in message.lower()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Unable to save meal"
    )


def test_meal_cancel_discards_unconfirmed_draft(handler: BotHandler) -> None:
    state = _meal_confirmation_state(handler)
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True

    handler.handle_callback(
        _meal_callback_route("cancel", state.request_id or "")
    )

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user",
        expected_revision=state.revision,
        expected_request_id=state.request_id,
        expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
    )
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "not saved" in message.lower()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Meal cancelled"
    )


@pytest.mark.parametrize(
    ("action", "step", "mutation"),
    [
        (
            "cancel",
            ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
            "delete_conversation_state",
        ),
        (
            "add",
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "transition_conversation_state",
        ),
        (
            "done",
            ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
            "delete_conversation_state",
        ),
    ],
)
def test_meal_callbacks_preserve_same_revision_replacement(
    handler: BotHandler,
    action: str,
    step: ConversationWorkflowStep,
    mutation: str,
) -> None:
    """A callback cannot mutate a replacement read at the same revision."""
    original = _meal_confirmation_state(handler, step=step)
    replacement = _meal_confirmation_state(handler, step=step).model_copy(
        update={"revision": original.revision}
    )
    current: dict[str, ConversationState | None] = {"state": original}
    handler.repo.get_conversation_state.return_value = original

    def race(*args: Any, **kwargs: Any) -> bool:
        del args
        current["state"] = replacement
        expected = {
            "expected_revision": original.revision,
            "expected_request_id": original.request_id,
            "expected_step": step,
        }
        if kwargs == expected:
            return False
        current["state"] = None
        return True

    getattr(handler.repo, mutation).side_effect = race

    handler.handle_callback(
        _meal_callback_route(action, original.request_id or "")
    )

    assert current["state"] == replacement
    handler.telegram_api.send_message.assert_called_once()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "stale" in message.lower() or "outdated" in message.lower()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Stale meal action"
    )


def test_meal_add_more_creates_fresh_empty_submission_and_prompt(
    handler: BotHandler,
) -> None:
    state = _meal_confirmation_state(
        handler, step=ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.transition_conversation_state.return_value = True

    handler.handle_callback(_meal_callback_route("add", state.request_id or ""))

    transition = handler.repo.transition_conversation_state.call_args
    assert transition is not None
    next_state = transition.args[1]
    assert next_state.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT
    assert next_state.meal_draft == MealLogDraft()
    assert next_state.request_id != state.request_id
    assert next_state.revision == state.revision + 1
    assert transition.kwargs == {
        "expected_revision": state.revision,
        "expected_request_id": state.request_id,
        "expected_step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
    }
    handler.telegram_api.send_message.assert_called_once_with(
        1, MEAL_INPUT_PROMPT
    )
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Add more"
    )


def test_meal_done_clears_completed_state(handler: BotHandler) -> None:
    state = _meal_confirmation_state(
        handler, step=ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
    )
    handler.repo.get_conversation_state.return_value = state
    handler.repo.delete_conversation_state.return_value = True

    handler.handle_callback(
        _meal_callback_route("done", state.request_id or "")
    )

    handler.repo.delete_conversation_state.assert_called_once_with(
        "user",
        expected_revision=state.revision,
        expected_request_id=state.request_id,
        expected_step=ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
    )
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "complete" in message.lower()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Meal logging complete"
    )


@pytest.mark.parametrize(
    ("action", "step"),
    [
        ("confirm", ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION),
        ("cancel", ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION),
        ("add", ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION),
        ("done", ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION),
    ],
)
def test_stale_meal_buttons_cannot_mutate_current_workflow(
    handler: BotHandler,
    action: str,
    step: ConversationWorkflowStep,
) -> None:
    state = _meal_confirmation_state(handler, step=step)
    handler.repo.get_conversation_state.return_value = state
    old_id = "00000000-0000-4000-8000-000000000000"

    handler.handle_callback(_meal_callback_route(action, old_id))

    handler.repo.confirm_meal_and_transition.assert_not_called()
    handler.repo.transition_conversation_state.assert_not_called()
    handler.repo.delete_conversation_state.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert any(
        phrase in message.lower()
        for phrase in ("already saved", "outdated", "stale")
    )
    handler.telegram_api.answer_callback_query.assert_called_once()


def test_malformed_meal_callback_is_acknowledged_before_checkin_validation(
    handler: BotHandler,
) -> None:
    route = _meal_callback_route(
        "unknown", "00000000-0000-4000-8000-000000000000"
    )

    handler.handle_callback(route)

    handler.repo.get_active_plan_snapshot.assert_not_called()
    message = handler.telegram_api.send_message.call_args.args[1]
    assert "invalid" in message.lower()
    handler.telegram_api.answer_callback_query.assert_called_once_with(
        "meal-query", "Invalid meal action"
    )


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


def test_draft_revision_starts_one_async_exact_snapshot(
    handler: BotHandler,
) -> None:
    plan = make_plan(week_start=date.today(), revision=4)
    handler.repo.get_latest_plan.return_value = plan

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.REVISE_PLAN,
        {"amendment": "Avoid cauliflower"},
        make_profile(),
    )

    assert result.success
    assert result.message == "I'm revising your draft now."
    state = handler.repo.save_conversation_state.call_args.args[1]
    assert state.workflow_kind is ConversationWorkflowKind.PLAN_REVISION
    assert state.expected_plan_revision == 4
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload == {
        "action": "revise_plan",
        "user_id": "user",
        "chat_id": 1,
        "week_start": plan.week_start_date,
        "amendment": "Avoid cauliflower",
        "request_id": state.request_id,
        "state_revision": state.revision,
        "expected_plan_revision": 4,
    }


def test_draft_revision_passes_normalized_source_update_id_to_repository(
    handler: BotHandler,
) -> None:
    plan = make_plan(week_start=date.today(), revision=4)
    handler.repo.get_latest_plan.return_value = plan
    handler.repo.has_plan_revision_update_marker.return_value = False
    handler.repo.start_plan_revision.return_value = True

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.REVISE_PLAN,
        {"amendment": "Avoid cauliflower"},
        make_profile(),
        source_update_id="42",
    )

    assert result.success
    state = handler.repo.start_plan_revision.call_args.args[1]
    assert state.last_update_id == "42"
    assert handler.repo.start_plan_revision.call_args.kwargs == {
        "source_update_id": "42"
    }
    handler.repo.save_conversation_state.assert_not_called()


def test_duplicate_revision_update_does_not_invoke_planner(
    handler: BotHandler,
) -> None:
    handler.repo.has_plan_revision_update_marker.return_value = True

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.REVISE_PLAN,
        {"amendment": "Avoid cauliflower"},
        make_profile(),
        source_update_id="42",
    )

    assert result.success
    assert result.message == "I'm revising your draft now."
    handler.repo.get_latest_plan.assert_not_called()
    handler.repo.start_plan_revision.assert_not_called()
    handler.lambda_client.invoke.assert_not_called()


def test_revision_workflow_blocks_confirmation_and_new_amendments(
    handler: BotHandler,
) -> None:
    now = datetime.now(timezone.utc)
    state = ConversationState(
        workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
        step=ConversationWorkflowStep.GENERATING,
        amendment="Avoid cauliflower",
        target_week=date.today(),
        expected_plan_revision=0,
        request_id="revision-1",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    handler.repo.get_conversation_state.return_value = state
    route = RouteResult(
        route_type=RouteType.CONVERSATIONAL,
        chat_id=1,
        user_id="user",
        text="confirm it",
    )

    handler.handle_conversational(route)

    assert (
        "still being generated"
        in (handler.telegram_api.send_message.call_args.args[1])
    )
    handler.llm_client.chat_sync.assert_not_called()


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
        '{"date":"'
        + date.today().isoformat()
        + '","meal_type":"lunch","description":"Soup"}}\n```'
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
        '{"date":"'
        + date.today().isoformat()
        + '","meal_type":"lunch","description":"Soup"}}\n```'
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
    assert "Family Name: Alex" in prompt
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


def test_unauthorized_private_update_is_silent_and_has_no_side_effects(
    handler: BotHandler,
) -> None:
    """Unknown private users cannot reach any Bot Lambda action."""
    result = handler.handle_update(
        {
            "message": {
                "from": {"id": 999},
                "chat": {"id": 999, "type": "private"},
                "text": "/start do-not-log-this",
            }
        }
    )

    assert result == {"statusCode": 200, "body": "ok"}
    handler.repo.assert_not_called()
    handler.telegram_api.assert_not_called()
    handler.lambda_client.assert_not_called()
    handler.llm_client.assert_not_called()


def test_allowlisted_group_update_is_silent_and_has_no_side_effects(
    handler: BotHandler,
) -> None:
    """An authorized sender is still denied outside a private chat."""
    result = handler.handle_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "/start",
            }
        }
    )

    assert result == {"statusCode": 200, "body": "ok"}
    handler.repo.assert_not_called()
    handler.telegram_api.assert_not_called()
    handler.lambda_client.assert_not_called()
    handler.llm_client.assert_not_called()


def test_allowlisted_update_without_chat_id_has_no_side_effects(
    handler: BotHandler,
) -> None:
    """Malformed private updates must not inherit the sender ID as chat ID."""
    result = handler.handle_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"type": "private"},
                "text": "/start",
            }
        }
    )

    assert result == {"statusCode": 200, "body": "ok"}
    handler.repo.assert_not_called()
    handler.telegram_api.assert_not_called()
    handler.lambda_client.assert_not_called()
    handler.llm_client.assert_not_called()


def test_denied_callback_does_not_acknowledge_or_mutate(
    handler: BotHandler,
) -> None:
    """Denied callbacks do not answer Telegram or invoke persistence."""
    result = handler.handle_update(
        {
            "callback_query": {
                "id": "query-secret",
                "from": {"id": 1},
                "message": {
                    "chat": {"id": -1001, "type": "group"},
                },
                "data": "checkin:2026-08-10:1:lunch:cooked-secret",
            }
        }
    )

    assert result["statusCode"] == 200
    handler.repo.assert_not_called()
    handler.telegram_api.assert_not_called()
    handler.lambda_client.assert_not_called()
    handler.llm_client.assert_not_called()


def test_denial_log_omits_update_contents(
    handler: BotHandler, caplog: pytest.LogCaptureFixture
) -> None:
    """Operational denial logs contain identity context only."""
    caplog.set_level("INFO")
    text = "private message that must not appear"
    callback_data = "callback payload that must not appear"

    handler.handle_update(
        {
            "message": {
                "from": {"id": 999},
                "chat": {"id": 999, "type": "private"},
                "text": text,
            }
        }
    )
    handler.handle_update(
        {
            "callback_query": {
                "id": "query",
                "from": {"id": 1},
                "message": {"chat": {"id": -1001, "type": "group"}},
                "data": callback_data,
            }
        }
    )

    log_text = caplog.text
    assert "user_id=999" in log_text
    assert "chat_type=private" in log_text
    assert text not in log_text
    assert callback_data not in log_text


def test_allowlisted_private_command_preserves_current_behavior(
    handler: BotHandler,
) -> None:
    """Authorized private commands still reach the normal handler."""
    handler.repo.get_profile.return_value = None

    result = handler.handle_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/start",
            }
        }
    )

    assert result["statusCode"] == 200
    handler.repo.get_profile.assert_called_once_with("1")
    handler.telegram_api.send_message.assert_called_once()


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


@pytest.mark.parametrize("allowed_user_ids", [None, "invalid"])
def test_lambda_handler_ignores_invalid_bot_configuration(
    mocker: Any,
    mock_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    allowed_user_ids: str | None,
) -> None:
    """Bad allowlist settings are silent and do not initialize dependencies."""
    if allowed_user_ids is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowed_user_ids)
    dynamodb_resource = mocker.patch("boto3.resource")
    lambda_client = mocker.patch("boto3.client")
    llm_client = mocker.patch("meal_planner.bot_handler.LLMClient")
    handler_class = mocker.patch("meal_planner.bot_handler.BotHandler")
    caplog.set_level("ERROR")

    result = lambda_handler(
        {
            "headers": {
                "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"
            },
            "body": json.dumps(
                {
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            ),
        },
        None,
    )

    assert result == {"statusCode": 200, "body": "ok"}
    dynamodb_resource.assert_not_called()
    lambda_client.assert_not_called()
    llm_client.assert_not_called()
    handler_class.assert_not_called()
    assert "Bot configuration is invalid" in caplog.text
    assert "test-api-key" not in caplog.text
    assert "test-webhook-secret" not in caplog.text
