"""Asynchronous worker for temporary conversational meal-plan drafts."""

import logging
from datetime import datetime, timezone
from typing import Any

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import get_plan_chat_settings
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient, LLMFailure, LLMTextResponseError
from meal_planner.llm.prompts import build_plan_chat_prompt
from meal_planner.models.schemas import (
    MAX_PLAN_CHAT_RESPONSE_LENGTH,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    PlanChatEvent,
)
from meal_planner.telegram.api import TelegramAPI

logger = logging.getLogger(__name__)

_PLAN_CHAT_SYSTEM_PROMPT = "Generate one helpful meal-planning draft."
_RETRY_MESSAGE = "I couldn't generate a draft right now. Please try again."


class PlanChatHandler:
    """Generate and deliver a draft only while its session still owns work."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        llm_client: LLMClient,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.llm_client = llm_client

    @staticmethod
    def _owns_request(
        state: ConversationState | None,
        event: PlanChatEvent,
    ) -> bool:
        """Return whether one live generating state belongs to this event."""
        return (
            state is not None
            and state.workflow_kind is ConversationWorkflowKind.PLAN_CHAT
            and state.step is ConversationWorkflowStep.PLAN_CHAT_GENERATING
            and state.session_id == event.session_id
            and state.request_id == event.request_id
            and state.revision == event.state_revision
        )

    @staticmethod
    def _next_state(
        state: ConversationState,
        **updates: Any,
    ) -> ConversationState:
        """Return a validated revision of a state with a fresh timestamp."""
        data = state.model_dump()
        data.update(updates)
        data["revision"] = state.revision + 1
        data["updated_at"] = datetime.now(timezone.utc)
        return ConversationState.model_validate(data)

    def _restore_after_failure(
        self, event: PlanChatEvent, state: ConversationState
    ) -> None:
        """Return a still-owned request to a user-retryable state."""
        if state.latest_response is None:
            restored = self._next_state(
                state,
                step=ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
                request_id=None,
                initial_request=None,
                pending_message=None,
                latest_response=None,
                context_date=None,
            )
        else:
            restored = self._next_state(
                state,
                step=ConversationWorkflowStep.PLAN_CHAT_READY,
                pending_message=state.latest_response,
            )
        try:
            transitioned = self.repo.transition_conversation_state(
                event.user_id,
                restored,
                expected_revision=state.revision,
                expected_request_id=event.request_id,
                expected_step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
            )
        except Exception:
            logger.error("Plan chat state recovery failed category=persistence")
            return
        if not transitioned:
            logger.info("Plan chat state recovery discarded category=stale")
            return
        try:
            self.telegram_api.send_plan_chat(
                event.chat_id,
                _RETRY_MESSAGE,
                event.session_id,
            )
        except Exception:
            logger.error("Plan chat retry delivery failed category=delivery")

    def _handle_failure(
        self,
        event: PlanChatEvent,
        state: ConversationState,
        failure: Exception,
    ) -> None:
        """Log a bounded failure category and expose a retryable message."""
        if isinstance(failure, LLMFailure):
            category = "provider"
        else:
            category = "internal"
        logger.warning("Plan chat generation failed category=%s", category)
        self._restore_after_failure(event, state)

    def handle_event(self, payload: dict[str, Any]) -> bool:
        """Handle one typed plan-chat event without logging private content."""
        try:
            event = PlanChatEvent.model_validate(payload)
        except TypeError, ValidationError:
            logger.warning("Rejected plan chat event category=invalid_event")
            return False

        try:
            state = self.repo.get_conversation_state(
                event.user_id,
                consistent_read=True,
            )
        except Exception:
            logger.error("Plan chat state load failed category=persistence")
            return True
        if not self._owns_request(state, event):
            logger.info("Plan chat request discarded category=stale")
            return True
        assert state is not None
        assert state.context_date is not None
        try:
            profile = self.repo.get_profile(event.user_id, consistent_read=True)
            history = self.repo.get_meal_history(
                event.user_id,
                days=21,
                on_date=state.context_date,
            )
            prompt = build_plan_chat_prompt(
                profile=profile,
                meal_history=history,
                initial_request=state.initial_request or "",
                latest_response=state.latest_response,
                pending_message=state.pending_message or "",
                context_date=state.context_date,
            )
            response = self.llm_client.chat_text_strict_sync(
                _PLAN_CHAT_SYSTEM_PROMPT,
                prompt,
            )
            if len(response) > MAX_PLAN_CHAT_RESPONSE_LENGTH:
                raise LLMTextResponseError("provider text exceeded state bound")
        except Exception as exc:
            self._handle_failure(event, state, exc)
            return True

        try:
            current_state = self.repo.get_conversation_state(
                event.user_id,
                consistent_read=True,
            )
        except Exception:
            logger.error(
                "Plan chat ownership recheck failed category=persistence"
            )
            return True
        if not self._owns_request(current_state, event):
            logger.info("Plan chat result discarded category=stale")
            return True
        assert current_state is not None
        ready = self._next_state(
            current_state,
            step=ConversationWorkflowStep.PLAN_CHAT_READY,
            pending_message=response,
            latest_response=response,
        )
        try:
            transitioned = self.repo.transition_conversation_state(
                event.user_id,
                ready,
                expected_revision=current_state.revision,
                expected_request_id=event.request_id,
                expected_step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
            )
        except Exception:
            logger.error(
                "Plan chat ready transition failed category=persistence"
            )
            return True
        if not transitioned:
            logger.info("Plan chat ready transition discarded category=stale")
            return True
        try:
            self.telegram_api.send_plan_chat(
                event.chat_id,
                response,
                event.session_id,
            )
        except Exception:
            logger.error("Plan chat delivery failed category=delivery")
        return True


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """AWS Lambda entry point for identifier-only plan-chat events."""
    del context
    settings = get_plan_chat_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    handler = PlanChatHandler(
        DynamoRepository(dynamodb.Table(settings.dynamodb_table_name)),
        TelegramAPI(
            settings.telegram_bot_token,
            request_timeout=settings.plan_chat_telegram_request_timeout_seconds,
        ),
        LLMClient(
            model=settings.plan_chat_llm_model,
            api_key=settings.llm_api_key,
            reasoning_effort=settings.plan_chat_llm_reasoning_effort,
            request_timeout=settings.plan_chat_llm_request_timeout_seconds,
        ),
    )
    status_code = 200 if handler.handle_event(event) else 400
    return {"statusCode": status_code}
