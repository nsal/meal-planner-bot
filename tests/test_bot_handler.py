"""Bot command, mutation, callback, and Lambda boundary tests."""

import base64
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from meal_planner.bot_handler import BotHandler, lambda_handler
from meal_planner.db.dynamo import ActivePlanSnapshot
from meal_planner.models.schemas import (
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    GroceryStatus,
    MealLogDraft,
    MealOutcome,
    PlanStatus,
    PreferenceRequirement,
    ProfileUpdateEntities,
)
from meal_planner.router import RouteResult, RouteType
from meal_planner.telegram.access import TelegramAccessPolicy
from meal_planner.telegram.api import TelegramAPIError, split_text
from meal_planner.telegram.commands import BOT_COMMANDS, render_help
from tests.factories import make_plan, make_profile


@pytest.fixture
def handler(mocker: Any) -> BotHandler:
    return BotHandler(
        mocker.MagicMock(),
        mocker.MagicMock(),
        lambda_client=mocker.MagicMock(),
        planner_function_name="planner",
        llm_client=mocker.MagicMock(),
        access_policy=TelegramAccessPolicy(frozenset({"1"})),
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


def test_start_requests_family_name_separately_from_member_names(
    handler: BotHandler,
) -> None:
    handler.repo.get_profile.return_value = None

    handler.handle_command(_command("start"))

    message = handler.telegram_api.send_message.call_args.args[1].lower()
    assert "family name" in message
    assert "each household member's name" in message
    assert "tell me your name" not in message


def test_help_renders_catalogue_without_repository_interaction(
    handler: BotHandler,
) -> None:
    handler.handle_command(_command("help"))

    handler.telegram_api.send_message.assert_called_once_with(1, render_help())
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

    message = handler.telegram_api.send_message.call_args.args[1]
    assert "Family name: Alex" in message
    assert "Family members:" in message
    assert "- Alex (2000 kcal/day)" in message


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
    """Actual meal logging is independent from planned check-in state."""
    handler.repo.get_conversation_state.return_value = None

    handler.handle_command(_command("submit_meals"))

    handler.repo.get_active_plan.assert_not_called()
    saved = handler.repo.save_conversation_state.call_args.args[1]
    assert saved.workflow_kind is ConversationWorkflowKind.MEAL_LOG
    assert saved.step is ConversationWorkflowStep.AWAITING_DATE
    assert "What date" in handler.telegram_api.send_message.call_args.args[1]


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
    assert payload["requirements"] == [
        {
            "id": "r1",
            "source_text": "Indian and pasta",
            "foods_any_of": ["Indian", "pasta"],
            "meal_type": None,
            "exact_count": 1,
        }
    ]
    assert payload["request_id"] == state.request_id
    assert payload["state_revision"] == 1
    assert payload["attempt"] == 1
    assert payload["repair_feedback"] is None


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
    assert saved.requirements[0].id == "r1"
    payload = json.loads(
        handler.lambda_client.invoke.call_args.kwargs["Payload"]
    )
    assert payload["requirements"][0]["exact_count"] == 3


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
        assert len(saved.requirements) == 20
        handler.lambda_client.invoke.assert_called_once()
        assert handler.telegram_api.send_message.call_args.args[1] == (
            "Working on your weekly meal plan."
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
        == "Working on your weekly meal plan."
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
            "allergies": "none",
            "dietary_preferences": "no preferences",
            "goals": ["eat well"],
        },
        None,
    )

    assert first_turn.success
    assert first_turn.message and "family name" in first_turn.message
    assert "restrictions" in first_turn.message
    handler.repo.delete_profile_draft.assert_not_called()

    saved_draft = handler.repo.save_profile_draft.call_args.args[1]
    handler.repo.get_profile_draft.return_value = saved_draft
    second_turn = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"name": "Nick", "restrictions": "none"},
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
    assert saved_profile.allergies == []
    assert saved_profile.dietary_preferences == []
    assert saved_profile.restrictions == []
    assert saved_profile.goals == ["eat well"]
    handler.repo.delete_profile_draft.assert_called_once_with("user")


def test_profile_onboarding_rejects_ambiguous_scalar_without_draft_mutation(
    handler: BotHandler,
) -> None:
    draft = ProfileUpdateEntities(
        name="Nick",
        people_count=3,
        restrictions=[],
    )
    handler.repo.get_profile_draft.return_value = draft

    result = handler._apply_intent_metadata(
        "user",
        1,
        ConversationIntent.UPDATE_PROFILE,
        {"restrictions": "no peanuts"},
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
            "allergies": [],
            "dietary_preferences": [],
            "restrictions": [],
            "goals": [],
        },
        None,
    )
    assert result.success
    assert result.message and "household member name" in result.message
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
