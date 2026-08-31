"""Tests for the asynchronous conversational plan-chat worker."""

from datetime import date, datetime, timezone
from typing import Any

import pytest
from pytest_mock import MockerFixture

from meal_planner.llm.client import (
    LLMPermanentError,
    LLMTextResponseError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.llm.prompts import MAX_MEAL_HISTORY_CHARACTERS
from meal_planner.models.schemas import ConversationWorkflowStep, MealLogEntry
from meal_planner.plan_chat_handler import PlanChatHandler, lambda_handler
from tests.factories import (
    make_plan_chat_event,
    make_plan_chat_state,
    make_profile,
)


def _handler(mocker: MockerFixture) -> tuple[PlanChatHandler, Any, Any, Any]:
    """Return a worker with independently inspectable collaborators."""
    repo = mocker.MagicMock()
    telegram = mocker.MagicMock()
    client = mocker.MagicMock()
    return PlanChatHandler(repo, telegram, client), repo, telegram, client


def test_worker_generates_one_unchanged_draft_from_current_context(
    mocker: MockerFixture,
) -> None:
    """A valid event loads current context and delivers one raw text draft."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=4,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    profile = make_profile()
    meal_history = [
        mocker.MagicMock(
            date=date(2026, 8, 28),
            meal_type=mocker.MagicMock(value="dinner"),
            description="Pasta",
        )
    ]
    repo.get_conversation_state.side_effect = [state, state]
    repo.get_profile.return_value = profile
    repo.get_meal_history.return_value = meal_history
    client.chat_text_strict_sync.return_value = "Draft heading\n- Pasta"
    repo.transition_conversation_state.return_value = True

    assert handler.handle_event(event.model_dump(mode="json"))

    assert repo.get_conversation_state.call_args_list[0].kwargs == {
        "consistent_read": True
    }
    repo.get_profile.assert_called_once_with("12345", consistent_read=True)
    repo.get_meal_history.assert_called_once_with(
        "12345", days=21, on_date=date(2026, 8, 28)
    )
    client.chat_text_strict_sync.assert_called_once()
    _, prompt = client.chat_text_strict_sync.call_args.args
    assert "Plan three family dinners" in prompt
    assert "Pasta" in prompt
    telegram.send_plan_chat.assert_called_once_with(
        12345,
        "Draft heading\n- Pasta",
        state.session_id,
    )
    saved = repo.transition_conversation_state.call_args.args[1]
    assert saved.step is ConversationWorkflowStep.PLAN_CHAT_READY
    assert saved.pending_message == "Draft heading\n- Pasta"
    assert saved.latest_response == "Draft heading\n- Pasta"
    assert saved.revision == 5
    assert repo.transition_conversation_state.call_args.kwargs == {
        "expected_revision": 4,
        "expected_request_id": state.request_id,
        "expected_step": ConversationWorkflowStep.PLAN_CHAT_GENERATING,
    }


def test_worker_includes_legacy_meal_in_prompt_history(
    mocker: MockerFixture,
) -> None:
    """Legacy stored meals reach the provider as current meal evidence."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=4,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    legacy_meal = MealLogEntry.model_validate(
        {
            "date": "2026-08-28",
            "meal_type": "dinner",
            "description": "Legacy batch dinner",
            "created_at": datetime(
                2026, 8, 28, 12, tzinfo=timezone.utc
            ).isoformat(),
            "batch_link": {
                "batch_id": "batch-2026-08-28",
                "role": "preparation",
                "portion": 1,
            },
        }
    )
    repo.get_conversation_state.side_effect = [state, state]
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = [legacy_meal]
    client.chat_text_strict_sync.return_value = "Draft"
    repo.transition_conversation_state.return_value = True

    assert handler.handle_event(event.model_dump(mode="json"))

    _, prompt = client.chat_text_strict_sync.call_args.args
    assert "Legacy batch dinner" in prompt


def test_worker_bounds_oversized_meal_history_in_provider_prompt(
    mocker: MockerFixture,
) -> None:
    """An oversized repository result cannot create an unbounded prompt."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=4,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    history = [
        MealLogEntry(
            date=date(2026, 8, 8 + index % 21),
            meal_type="dinner",
            description=f"history-{index:02d} " + "x" * 488,
            created_at=datetime(
                2026, 8, 28, 12, index % 60, tzinfo=timezone.utc
            ),
        )
        for index in range(60)
    ]
    repo.get_conversation_state.side_effect = [state, state]
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = history
    client.chat_text_strict_sync.return_value = "Draft"
    repo.transition_conversation_state.return_value = True

    assert handler.handle_event(event.model_dump(mode="json"))

    _, prompt = client.chat_text_strict_sync.call_args.args
    history_section = prompt.split("--- BEGIN SUBMITTED MEALS ---", maxsplit=1)[
        1
    ].split("--- END SUBMITTED MEALS ---", maxsplit=1)[0]
    assert len(history_section) <= MAX_MEAL_HISTORY_CHARACTERS


@pytest.mark.parametrize(
    "state",
    [
        None,
        make_plan_chat_state(
            step=ConversationWorkflowStep.AWAITING_PLAN_REQUEST
        ),
        make_plan_chat_state(step=ConversationWorkflowStep.PLAN_CHAT_READY),
    ],
)
def test_worker_discards_non_owned_state_without_request_or_delivery(
    mocker: MockerFixture,
    state: Any,
) -> None:
    """Missing, cancelled, and wrong-step sessions cannot produce output."""
    handler, repo, telegram, client = _handler(mocker)
    event = make_plan_chat_event()
    repo.get_conversation_state.return_value = state

    assert handler.handle_event(event.model_dump(mode="json"))

    client.chat_text_strict_sync.assert_not_called()
    repo.transition_conversation_state.assert_not_called()
    telegram.send_plan_chat.assert_not_called()


@pytest.mark.parametrize("attribute", ["session_id", "request_id", "revision"])
def test_worker_discards_replaced_state_without_delivery(
    mocker: MockerFixture, attribute: str
) -> None:
    """A replacement session, request, or revision loses worker ownership."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=3,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    if attribute == "session_id":
        event = event.model_copy(
            update={"session_id": make_plan_chat_event().session_id}
        )
    elif attribute == "request_id":
        event = event.model_copy(
            update={"request_id": make_plan_chat_event().request_id}
        )
    else:
        event = event.model_copy(update={"state_revision": state.revision + 1})
    repo.get_conversation_state.return_value = state

    assert handler.handle_event(event.model_dump(mode="json"))

    client.chat_text_strict_sync.assert_not_called()
    repo.transition_conversation_state.assert_not_called()
    telegram.send_plan_chat.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        LLMTimeoutError("timeout"),
        LLMTransientError("unavailable"),
        LLMPermanentError("invalid key"),
        LLMTextResponseError("blank response"),
    ],
)
def test_worker_restores_first_request_after_provider_failure(
    mocker: MockerFixture, failure: Exception
) -> None:
    """Provider failures guide retries after successful state recovery."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=8,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.return_value = state
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.side_effect = failure
    repo.transition_conversation_state.return_value = True

    assert handler.handle_event(event.model_dump(mode="json"))

    restored = repo.transition_conversation_state.call_args.args[1]
    assert restored.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST
    assert restored.session_id == state.session_id
    assert restored.initial_request is None
    assert restored.pending_message is None
    assert restored.latest_response is None
    repo.transition_conversation_state.assert_called_once()
    telegram.send_plan_chat.assert_called_once_with(
        12345,
        "I couldn't generate a draft right now. Please try again.",
        state.session_id,
    )


def test_worker_restores_the_previous_draft_after_follow_up_failure(
    mocker: MockerFixture,
) -> None:
    """A failed follow-up preserves the last useful draft for another try."""
    handler, repo, telegram, client = _handler(mocker)
    previous_response = "Previous draft"
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        pending_message="Make it vegetarian",
        latest_response=previous_response,
        revision=8,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.return_value = state
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.side_effect = LLMTransientError("unavailable")
    repo.transition_conversation_state.return_value = True

    assert handler.handle_event(event.model_dump(mode="json"))

    restored = repo.transition_conversation_state.call_args.args[1]
    assert restored.step is ConversationWorkflowStep.PLAN_CHAT_READY
    assert restored.pending_message == previous_response
    assert restored.latest_response == previous_response
    telegram.send_plan_chat.assert_called_once_with(
        12345,
        "I couldn't generate a draft right now. Please try again.",
        state.session_id,
    )


def test_worker_suppresses_retry_message_after_persistence_failure(
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery failures can be silent while bounded metadata is logged."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.return_value = state
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.side_effect = LLMTimeoutError(
        "private timeout"
    )
    repo.transition_conversation_state.side_effect = RuntimeError("private db")

    with caplog.at_level("ERROR", logger="meal_planner.plan_chat_handler"):
        assert handler.handle_event(event.model_dump(mode="json"))

    repo.transition_conversation_state.assert_called_once()
    telegram.send_plan_chat.assert_not_called()
    assert "category=persistence" in caplog.text


def test_worker_suppresses_output_when_ready_transition_is_stale(
    mocker: MockerFixture,
) -> None:
    """A cancelled or replaced state during generation never gets delivered."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        revision=2,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.side_effect = [state, None]
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.return_value = "draft"

    assert handler.handle_event(event.model_dump(mode="json"))

    repo.transition_conversation_state.assert_not_called()
    telegram.send_plan_chat.assert_not_called()


def test_worker_logs_delivery_failure_without_persisted_retry(
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Delivery failures are logged without a second send or retry message."""
    handler, repo, telegram, client = _handler(mocker)
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.side_effect = [state, state]
    repo.get_profile.return_value = make_profile()
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.return_value = "draft"
    repo.transition_conversation_state.return_value = True
    telegram.send_plan_chat.side_effect = RuntimeError("telegram unavailable")

    with caplog.at_level("ERROR", logger="meal_planner.plan_chat_handler"):
        assert handler.handle_event(event.model_dump(mode="json"))

    repo.transition_conversation_state.assert_called_once()
    telegram.send_plan_chat.assert_called_once_with(
        12345, "draft", state.session_id
    )
    assert "category=delivery" in caplog.text


def test_worker_logs_no_prompt_or_generated_content(
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operational records contain categories, never conversation content."""
    handler, repo, telegram, client = _handler(mocker)
    secret_request = "private request"
    secret_draft = "private generated draft"
    state = make_plan_chat_state(
        step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
        initial_request=secret_request,
    )
    event = make_plan_chat_event(
        session_id=state.session_id,
        request_id=state.request_id,
        state_revision=state.revision,
    )
    repo.get_conversation_state.side_effect = [state, state]
    repo.get_profile.return_value = make_profile().model_copy(
        update={"name": "private profile"}
    )
    repo.get_meal_history.return_value = []
    client.chat_text_strict_sync.return_value = secret_draft
    repo.transition_conversation_state.return_value = True
    telegram.send_plan_chat.side_effect = RuntimeError("private telegram")

    with caplog.at_level("INFO", logger="meal_planner.plan_chat_handler"):
        assert handler.handle_event(event.model_dump(mode="json"))

    assert secret_request not in caplog.text
    assert secret_draft not in caplog.text
    assert "private profile" not in caplog.text
    assert state.session_id not in caplog.text
    assert state.request_id not in caplog.text


def test_lambda_does_not_configure_application_retries(
    mocker: MockerFixture,
) -> None:
    """The Lambda constructs its plan-chat client with one application try."""
    settings = mocker.MagicMock(
        aws_region="us-east-1",
        dynamodb_table_name="meal-planner",
        telegram_bot_token="token",
        plan_chat_telegram_request_timeout_seconds=10.0,
        plan_chat_llm_model="gpt-5.6-terra",
        llm_api_key="key",
        plan_chat_llm_reasoning_effort="high",
        plan_chat_llm_request_timeout_seconds=240.0,
    )
    mocker.patch(
        "meal_planner.plan_chat_handler.get_plan_chat_settings",
        return_value=settings,
    )
    mocker.patch("meal_planner.plan_chat_handler.boto3.resource")
    llm_client = mocker.patch("meal_planner.plan_chat_handler.LLMClient")
    handler_class = mocker.patch(
        "meal_planner.plan_chat_handler.PlanChatHandler"
    )
    handler_class.return_value.handle_event.return_value = True

    assert lambda_handler({}, None) == {"statusCode": 200}

    assert "max_retries" not in llm_client.call_args.kwargs


def test_worker_rejects_invalid_event_without_loading_context(
    mocker: MockerFixture,
) -> None:
    """Invalid worker payloads do not reach persistence or external services."""
    handler, repo, telegram, client = _handler(mocker)

    assert not handler.handle_event({"action": "generate_plan_chat"})

    repo.get_conversation_state.assert_not_called()
    client.chat_text_strict_sync.assert_not_called()
    telegram.send_plan_chat.assert_not_called()
