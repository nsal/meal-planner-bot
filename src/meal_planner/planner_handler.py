"""Asynchronous Lambda workflows for plans and grocery finalization."""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import get_planner_settings
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import (
    LLMClient,
    LLMFailure,
    LLMPermanentError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from meal_planner.llm.parser import (
    parse_grocery_response,
    parse_plan_response_with_feedback,
)
from meal_planner.llm.prompts import (
    build_grocery_prompt,
    build_plan_prompt,
    build_plan_revision_prompt,
)
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    GroceryStatus,
    MealOutcome,
    PlanGenerationContext,
    PlanRevisionContext,
    PlanStatus,
    WeeklyPlan,
)
from meal_planner.telegram.api import TelegramAPI, TelegramAPIError

logger = logging.getLogger(__name__)

GENERATE_PLAN = "generate_plan"
FINALIZE_GROCERY = "finalize_grocery"
REVISE_PLAN = "revise_plan"


class PlannerHandler:
    """Manage asynchronous plan generation and grocery finalization."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        llm_client: Optional[LLMClient] = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.repo = repo
        self.telegram_api = telegram_api
        self.llm_client = llm_client
        self.max_attempts = max_attempts

    def generate_plan(
        self,
        user_id: str,
        chat_id: int | str,
        *,
        week_start: date | None = None,
        preference: str | None = None,
        request_id: str | None = None,
        state_revision: int | None = None,
    ) -> None:
        """Generate and persist a draft plan without a grocery list."""
        try:
            profile = self.repo.get_profile(user_id)
            if not profile or not profile.is_complete:
                self.telegram_api.send_message(
                    chat_id,
                    "Complete your profile before generating a meal plan.",
                )
                return
            target_week = week_start or date.today()
            current_plan = self.repo.get_plan(
                user_id, target_week, consistent_read=True
            )
            if current_plan and current_plan.status is PlanStatus.CONFIRMED:
                if request_id and state_revision is not None:
                    self.repo.clear_conversation_state_if_matches(
                        user_id,
                        request_id=request_id,
                        expected_revision=state_revision,
                    )
                self.telegram_api.send_message(
                    chat_id,
                    "That week's plan is already confirmed, so I kept it "
                    "unchanged.",
                )
                return
            client = self.llm_client or LLMClient()
            prompt = build_plan_prompt(
                profile=profile,
                meal_history=self.repo.get_meal_history(user_id, days=14),
                previous_plan=self.repo.get_latest_plan(user_id),
                week_start=target_week.isoformat(),
                preference=preference,
            )
            plan = self._generate_with_bounded_repair(
                client, prompt, target_week, chat_id
            )
            if plan is None:
                self._retain_retry_state(
                    user_id,
                    request_id=request_id,
                    state_revision=state_revision,
                )
                return
            if request_id and state_revision is not None:
                current_state = self.repo.get_conversation_state(user_id)
                if not self._request_matches(
                    current_state, request_id, state_revision
                ):
                    logger.info(
                        "Discarded stale planner request %s", request_id
                    )
                    return
            plan.status = PlanStatus.DRAFT
            plan.revision = (
                0 if current_plan is None else current_plan.revision + 1
            )
            plan.grocery_status = GroceryStatus.NOT_REQUESTED
            plan.grocery_list = []
            plan.planning_instructions = [preference] if preference else []
            for plan_day in plan.days:
                for meal in plan_day.meals:
                    meal.outcome = MealOutcome.UNREPORTED
            expected_revision = (
                None if current_plan is None else current_plan.revision
            )
            if not self.repo.save_generated_draft(
                user_id, plan, expected_revision=expected_revision
            ):
                logger.info(
                    "Discarded stale generated plan for user %s week %s",
                    user_id,
                    target_week,
                )
                self.telegram_api.send_message(
                    chat_id,
                    "That week's plan changed while I was generating it, so "
                    "I discarded the stale result.",
                )
                return
            if request_id and state_revision is not None:
                self.repo.clear_conversation_state_if_matches(
                    user_id,
                    request_id=request_id,
                    expected_revision=state_revision,
                )
        except Exception as exc:
            logger.error("Plan generation failed for user %s: %s", user_id, exc)
            self._notify_failure(
                chat_id,
                "Sorry, an error occurred while generating your plan.",
            )
            return
        try:
            self.telegram_api.send_plan(chat_id, plan)
            self.telegram_api.send_message(
                chat_id,
                "Review this draft, request edits, then tell me to confirm it.",
            )
        except Exception as exc:
            logger.error(
                "Generated plan delivery failed for user %s week %s: %s",
                user_id,
                target_week,
                exc,
            )

    def _generate_with_bounded_repair(
        self,
        client: LLMClient,
        prompt: str,
        target_week: date,
        chat_id: int | str,
        failure_mode: str = "initial",
    ) -> WeeklyPlan | None:
        """Use the configured number of provider calls for plan generation."""
        repair_feedback: str | None = None
        for attempt in range(self.max_attempts):
            is_last_attempt = attempt == self.max_attempts - 1
            user_message = "Generate weekly meal plan"
            if repair_feedback:
                user_message += (
                    "\nRepair the previous response using these validation "
                    f"errors: {repair_feedback}"
                )
            try:
                raw = self._strict_json_call(client, prompt, user_message)
            except LLMTimeoutError:
                if is_last_attempt:
                    self._notify_failure(
                        chat_id,
                        self._generation_failure_message(
                            failure_mode, "timed out"
                        ),
                    )
                    return None
                continue
            except LLMTransientError:
                if is_last_attempt:
                    self._notify_failure(
                        chat_id,
                        self._generation_failure_message(
                            failure_mode, "temporarily unavailable"
                        ),
                    )
                    return None
                continue
            except LLMPermanentError:
                self._notify_failure(
                    chat_id,
                    self._generation_failure_message(
                        failure_mode, "rejected the request"
                    ),
                )
                return None
            except LLMResponseFormatError, LLMFailure:
                if is_last_attempt:
                    self._notify_failure(
                        chat_id,
                        self._generation_failure_message(
                            failure_mode, self._invalid_plan_message()
                        ),
                    )
                    return None
                repair_feedback = "return one complete JSON plan object"
                continue
            plan, feedback = parse_plan_response_with_feedback(raw)
            if plan and plan.week_start == target_week:
                return plan
            if is_last_attempt:
                self._notify_failure(
                    chat_id,
                    self._generation_failure_message(
                        failure_mode, self._invalid_plan_message()
                    ),
                )
                return None
            repair_feedback = (
                feedback or "week_start_date must match the request"
            )
        return None

    @staticmethod
    def _generation_failure_message(mode: str, reason: str) -> str:
        """Return a user-facing failure message for one planner workflow."""
        if mode == "revision":
            if reason == "timed out":
                return (
                    "Draft revision timed out. Your original draft is "
                    "unchanged; reply retry or use /cancel."
                )
            if reason == "temporarily unavailable":
                return (
                    "Draft revision is temporarily unavailable. Your original "
                    "draft is unchanged; reply retry or use /cancel."
                )
            if reason == "rejected the request":
                return (
                    "The revision service rejected the request. Your original "
                    "draft is unchanged; reply retry or use /cancel."
                )
            return (
                f"{reason} Your original draft is unchanged; reply retry or "
                "use /cancel."
            )
        if reason == "timed out":
            return (
                "Plan generation timed out. Your preference was saved; use "
                "/plan to retry."
            )
        if reason == "temporarily unavailable":
            return (
                "The meal-planning service is temporarily unavailable. Your "
                "preference was saved; use /plan to retry."
            )
        if reason == "rejected the request":
            return (
                "The meal-planning service rejected the request. Your "
                "preference was saved; use /plan to retry."
            )
        return f"{reason} Your preference was saved; use /plan to retry."

    def _invalid_plan_message(self) -> str:
        """Return the terminal message for invalid planner output."""
        if self.max_attempts == 1:
            return "The AI returned an invalid meal plan."
        if self.max_attempts == 2:
            return "The AI returned an invalid meal plan twice."
        return (
            f"The AI returned an invalid meal plan {self.max_attempts} times."
        )

    @staticmethod
    def _strict_json_call(
        client: LLMClient, prompt: str, user_message: str
    ) -> dict[str, Any]:
        strict_method = getattr(client, "chat_json_strict_sync", None)
        if callable(strict_method):
            raw = strict_method(prompt, user_message)
            if isinstance(raw, dict):
                return raw
        raw = client.chat_json_sync(prompt, user_message)
        if not isinstance(raw, dict):
            raise LLMResponseFormatError("LLM response was not a JSON object")
        return raw

    @staticmethod
    def _request_matches(
        state: ConversationState | None,
        request_id: str,
        revision: int,
    ) -> bool:
        return bool(
            state
            and state.request_id == request_id
            and state.revision == revision
            and state.step is ConversationWorkflowStep.GENERATING
        )

    def _retain_retry_state(
        self,
        user_id: str,
        *,
        request_id: str | None,
        state_revision: int | None,
    ) -> None:
        if not request_id or state_revision is None:
            return
        state = self.repo.get_conversation_state(user_id)
        if not self._request_matches(state, request_id, state_revision):
            return
        assert state is not None
        retry_state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.RETRY_READY,
                "revision": state.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.repo.mark_conversation_retry_ready(
            user_id, retry_state, expected_revision=state_revision
        )

    def finalize_grocery(
        self, user_id: str, chat_id: int | str, week_start: str
    ) -> None:
        """Generate groceries for one exact confirmed week."""
        plan = self.repo.get_plan(user_id, week_start, consistent_read=True)
        if not plan or plan.week_start_date != week_start:
            self._notify_failure(
                chat_id, "That meal-plan week no longer exists."
            )
            return
        if plan.status is not PlanStatus.CONFIRMED:
            self._notify_failure(chat_id, "Confirm the plan before groceries.")
            return
        if plan.grocery_status is not GroceryStatus.PENDING:
            logger.info(
                "Ignoring stale grocery event for user %s week %s in state %s",
                user_id,
                week_start,
                plan.grocery_status.value,
            )
            return
        revision = plan.revision
        try:
            profile = self.repo.get_profile(user_id)
            if not profile:
                raise ValueError("profile missing")
            client = self.llm_client or LLMClient()
            sections = parse_grocery_response(
                client.chat_json_sync(
                    build_grocery_prompt(plan, profile.people_count),
                    "Generate grocery list",
                )
            )
            if not sections:
                raise ValueError("grocery response contained no valid sections")
            if not self.repo.complete_grocery(
                user_id, week_start, revision, sections
            ):
                logger.info(
                    "Discarded stale grocery result for user %s week %s",
                    user_id,
                    week_start,
                )
                return
        except Exception as exc:
            logger.error(
                "Grocery finalization failed for user %s week %s: %s",
                user_id,
                week_start,
                exc,
            )
            if self.repo.fail_grocery(user_id, week_start, revision):
                self._notify_failure(
                    chat_id,
                    "I couldn't generate groceries for that plan. Please "
                    "retry.",
                )
            else:
                logger.info(
                    "Suppressed stale grocery failure for user %s week %s",
                    user_id,
                    week_start,
                )
            return
        try:
            self.telegram_api.send_message(
                chat_id, "Your grocery list is ready. Use /grocery to view it."
            )
        except Exception as exc:
            logger.error(
                "Grocery-ready notification failed for user %s week %s: %s",
                user_id,
                week_start,
                exc,
            )

    def revise_plan(
        self,
        user_id: str,
        chat_id: int | str,
        context: PlanRevisionContext,
    ) -> None:
        """Generate and atomically publish a complete draft replacement."""
        try:
            profile = self.repo.get_profile(user_id)
            plan = self.repo.get_plan(
                user_id, context.week_start, consistent_read=True
            )
            state = self.repo.get_conversation_state(user_id)
            if not self._revision_state_matches(state, context):
                logger.info(
                    "Discarded stale plan revision %s", context.request_id
                )
                return
            if not profile:
                self._notify_failure(
                    chat_id,
                    "I couldn't revise the draft because your profile is "
                    "missing. Use /plan to create a new draft.",
                )
                return
            if not plan:
                self._notify_failure(
                    chat_id,
                    "That draft is no longer available. Use /plan to create a "
                    "new draft.",
                )
                self.repo.clear_conversation_state_if_matches(
                    user_id,
                    request_id=context.request_id,
                    expected_revision=context.state_revision,
                )
                return
            if not self._revision_request_matches(state, context, plan):
                logger.info(
                    "Discarded stale plan revision %s", context.request_id
                )
                return
            if (
                plan.status is not PlanStatus.DRAFT
                or plan.week_end < date.today()
            ):
                self._notify_failure(
                    chat_id,
                    "That draft is no longer eligible for revision. Use /plan "
                    "to create a new draft.",
                )
                self.repo.clear_conversation_state_if_matches(
                    user_id,
                    request_id=context.request_id,
                    expected_revision=context.state_revision,
                )
                return
            client = self.llm_client or LLMClient()
            revised = self._generate_with_bounded_repair(
                client,
                build_plan_revision_prompt(
                    profile,
                    plan,
                    context.amendment,
                    week_start=context.week_start.isoformat(),
                ),
                context.week_start,
                chat_id,
                failure_mode="revision",
            )
            if revised is None:
                self._retain_retry_state(
                    user_id,
                    request_id=context.request_id,
                    state_revision=context.state_revision,
                )
                return
            revised.status = PlanStatus.DRAFT
            revised.revision = context.expected_plan_revision + 1
            revised.week_start = context.week_start
            revised.grocery_status = GroceryStatus.NOT_REQUESTED
            revised.grocery_list = []
            revised.planning_instructions = [
                *plan.planning_instructions,
                context.amendment,
            ]
            for plan_day in revised.days:
                for meal in plan_day.meals:
                    meal.outcome = MealOutcome.UNREPORTED
            published = self.repo.replace_draft_and_clear_revision_state(
                user_id,
                revised,
                expected_plan_revision=context.expected_plan_revision,
                request_id=context.request_id,
                expected_state_revision=context.state_revision,
            )
            if not published:
                self._notify_failure(
                    chat_id,
                    "The draft changed while I was revising it, so I discarded "
                    "the stale result.",
                )
                return
        except Exception as exc:
            logger.error(
                "Plan revision failed for user %s week %s: %s",
                user_id,
                context.week_start,
                exc,
            )
            self._notify_failure(
                chat_id,
                "I couldn't revise the draft. Your original draft is "
                "unchanged; reply retry or use /cancel.",
            )
            return
        try:
            self.telegram_api.send_plan(chat_id, revised)
            self.telegram_api.send_message(
                chat_id,
                "Review this revised draft, request more edits, or tell me to "
                "confirm it.",
            )
        except Exception as exc:
            logger.error(
                "Revised plan delivery failed for user %s week %s: %s",
                user_id,
                context.week_start,
                exc,
            )

    @staticmethod
    def _revision_request_matches(
        state: ConversationState | None,
        context: PlanRevisionContext,
        plan: WeeklyPlan,
    ) -> bool:
        """Check the durable state and exact plan snapshot for an event."""
        return bool(
            PlannerHandler._revision_state_matches(state, context)
            and plan.week_start == context.week_start
            and plan.revision == context.expected_plan_revision
        )

    @staticmethod
    def _revision_state_matches(
        state: ConversationState | None,
        context: PlanRevisionContext,
    ) -> bool:
        """Check only the durable request snapshot for a revision event."""
        return bool(
            state
            and state.workflow_kind is ConversationWorkflowKind.PLAN_REVISION
            and state.step is ConversationWorkflowStep.GENERATING
            and state.request_id == context.request_id
            and state.revision == context.state_revision
            and state.target_week == context.week_start
            and state.expected_plan_revision == context.expected_plan_revision
        )

    def _notify_failure(self, chat_id: int | str, message: str) -> None:
        try:
            self.telegram_api.send_message(chat_id, message)
        except TelegramAPIError:
            logger.error("Could not deliver planner failure notification")

    def handle_event(self, event: dict[str, Any]) -> bool:
        """Dispatch a validated asynchronous planner event."""
        user_id = str(event.get("user_id", ""))
        chat_id = event.get("chat_id")
        action = event.get("action", GENERATE_PLAN)
        raw_user_id = event.get("user_id")
        if (
            not isinstance(raw_user_id, str)
            or not raw_user_id.strip()
            or chat_id is None
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, (int, str))
            or not isinstance(action, str)
        ):
            return False
        user_id = raw_user_id.strip()
        if action == GENERATE_PLAN:
            requested_week = event.get("week_start")
            try:
                week = (
                    date.fromisoformat(requested_week)
                    if requested_week
                    else None
                )
                context = PlanGenerationContext.model_validate(
                    {
                        "preference": event.get("preference"),
                        "request_id": event.get("request_id"),
                        "state_revision": event.get("state_revision"),
                    }
                )
            except TypeError, ValueError, ValidationError:
                return False
            self.generate_plan(
                user_id,
                chat_id,
                week_start=week,
                preference=context.preference,
                request_id=context.request_id,
                state_revision=context.state_revision,
            )
            return True
        if action == FINALIZE_GROCERY:
            week_start = event.get("week_start")
            if not isinstance(week_start, str):
                return False
            try:
                date.fromisoformat(week_start)
            except ValueError:
                return False
            self.finalize_grocery(user_id, chat_id, week_start)
            return True
        if action == REVISE_PLAN:
            try:
                revision_context = PlanRevisionContext.model_validate(event)
            except TypeError, ValueError, ValidationError:
                return False
            self.revise_plan(user_id, chat_id, revision_context)
            return True
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for asynchronous planner events."""
    settings = get_planner_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    repo = DynamoRepository(dynamodb.Table(settings.dynamodb_table_name))
    telegram_api = TelegramAPI(
        settings.telegram_bot_token,
        request_timeout=settings.planner_telegram_request_timeout_seconds,
    )
    llm_client = LLMClient(
        model=settings.planner_llm_model,
        api_key=settings.llm_api_key,
        reasoning_effort=settings.planner_llm_reasoning_effort,
        max_retries=settings.planner_llm_max_retries,
        initial_backoff=settings.planner_llm_initial_backoff_seconds,
        request_timeout=settings.planner_llm_request_timeout_seconds,
    )
    planner = PlannerHandler(
        repo,
        telegram_api,
        llm_client,
        max_attempts=settings.planner_llm_max_retries,
    )
    if not planner.handle_event(event):
        logger.error("Invalid planner event")
        return {"statusCode": 400, "body": "invalid event"}
    return {"statusCode": 200, "body": "ok"}
