"""Asynchronous Lambda workflows for plans and grocery finalization."""

import logging
from datetime import date
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]

from meal_planner.config import get_settings
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient
from meal_planner.llm.parser import parse_grocery_response, parse_plan_response
from meal_planner.llm.prompts import build_grocery_prompt, build_plan_prompt
from meal_planner.models.schemas import (
    GroceryStatus,
    MealOutcome,
    PlanStatus,
)
from meal_planner.telegram.api import TelegramAPI, TelegramAPIError

logger = logging.getLogger(__name__)

GENERATE_PLAN = "generate_plan"
FINALIZE_GROCERY = "finalize_grocery"


class PlannerHandler:
    """Manage asynchronous plan generation and grocery finalization."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.llm_client = llm_client

    def generate_plan(
        self,
        user_id: str,
        chat_id: int | str,
        *,
        week_start: date | None = None,
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
            client = self.llm_client or LLMClient()
            prompt = build_plan_prompt(
                profile=profile,
                meal_history=self.repo.get_meal_history(user_id, days=14),
                previous_plan=self.repo.get_latest_plan(user_id),
                week_start=target_week.isoformat(),
            )
            plan = parse_plan_response(
                client.chat_json_sync(prompt, "Generate weekly meal plan")
            )
            if not plan or plan.week_start != target_week:
                self.telegram_api.send_message(
                    chat_id,
                    "Sorry, I couldn't generate a valid meal plan. Try again.",
                )
                return
            plan.status = PlanStatus.DRAFT
            plan.revision = 0
            plan.grocery_status = GroceryStatus.NOT_REQUESTED
            plan.grocery_list = []
            for plan_day in plan.days:
                for meal in plan_day.meals:
                    meal.outcome = MealOutcome.UNREPORTED
            if not self.repo.save_generated_draft(user_id, plan):
                self.telegram_api.send_message(
                    chat_id,
                    "That week's plan was already confirmed, so I kept it "
                    "unchanged.",
                )
                return
            self.telegram_api.send_plan(chat_id, plan)
            self.telegram_api.send_message(
                chat_id,
                "Review this draft, request edits, then tell me to confirm it.",
            )
        except Exception as exc:
            logger.error("Plan generation failed for user %s: %s", user_id, exc)
            self._notify_failure(
                chat_id,
                "Sorry, an error occurred while generating your plan.",
            )

    def finalize_grocery(
        self, user_id: str, chat_id: int | str, week_start: str
    ) -> None:
        """Generate groceries for one exact confirmed week."""
        plan = self.repo.get_plan(user_id, week_start)
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
        if not user_id or chat_id is None:
            return False
        if action == GENERATE_PLAN:
            requested_week = event.get("week_start")
            try:
                week = (
                    date.fromisoformat(requested_week)
                    if requested_week
                    else None
                )
            except TypeError, ValueError:
                return False
            self.generate_plan(user_id, chat_id, week_start=week)
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
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for asynchronous planner events."""
    settings = get_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    repo = DynamoRepository(dynamodb.Table(settings.dynamodb_table_name))
    telegram_api = TelegramAPI(
        settings.telegram_bot_token,
        request_timeout=settings.planner_telegram_request_timeout_seconds,
    )
    llm_client = LLMClient(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        max_retries=settings.planner_llm_max_retries,
        initial_backoff=settings.planner_llm_initial_backoff_seconds,
        request_timeout=settings.planner_llm_request_timeout_seconds,
    )
    planner = PlannerHandler(repo, telegram_api, llm_client)
    if not planner.handle_event(event):
        logger.error("Invalid planner event")
        return {"statusCode": 400, "body": "invalid event"}
    return {"statusCode": 200, "body": "ok"}
