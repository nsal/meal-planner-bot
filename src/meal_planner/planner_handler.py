"""AWS Lambda entry point for asynchronous weekly plan generation."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]

from meal_planner.config import get_settings
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient
from meal_planner.llm.parser import parse_grocery_response, parse_plan_response
from meal_planner.llm.prompts import build_grocery_prompt, build_plan_prompt
from meal_planner.telegram.api import TelegramAPI

logger = logging.getLogger(__name__)


class PlannerHandler:
    """Handler managing asynchronous 7-day meal plan generation."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.llm_client = llm_client

    def generate_plan(self, user_id: str, chat_id: int | str) -> None:
        """Generate weekly plan and grocery list, save to DB, send to user."""
        try:
            profile = self.repo.get_profile(user_id)
            if not profile:
                self.telegram_api.send_message(
                    chat_id,
                    "No profile found. Please set up your profile with /start!",
                )
                return

            meal_history = self.repo.get_meal_history(user_id, days=14)
            previous_plan = self.repo.get_current_plan(user_id)
            week_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            plan_prompt = build_plan_prompt(
                profile=profile,
                meal_history=meal_history,
                previous_plan=previous_plan,
                week_start=week_start,
            )

            client = self.llm_client or LLMClient()
            raw_plan_dict = client.chat_json_sync(
                plan_prompt, "Generate weekly meal plan"
            )
            plan = parse_plan_response(raw_plan_dict)

            if not plan or not plan.days:
                self.telegram_api.send_message(
                    chat_id,
                    "Sorry, I couldn't generate a valid meal plan right now. "
                    "Please try again.",
                )
                return

            grocery_prompt = build_grocery_prompt(
                plan, people_count=profile.people_count
            )
            raw_grocery_dict = client.chat_json_sync(
                grocery_prompt, "Generate grocery list"
            )
            sections = parse_grocery_response(raw_grocery_dict)
            plan.grocery_list = sections

            self.repo.save_plan(user_id, plan)
            self.telegram_api.send_plan(chat_id, plan)
            self.telegram_api.send_message(
                chat_id,
                "Here is your 7-day meal plan! 🛒 Use /grocery to view "
                "the grocery list, or tell me anything you'd like to change.",
            )
        except Exception as exc:
            logger.error(
                "Failed to generate plan for user %s: %s", user_id, exc
            )
            self.telegram_api.send_message(
                chat_id,
                "Sorry, an error occurred while generating your plan. "
                "Please try again later.",
            )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for async invocation from Bot Lambda."""
    user_id = str(event.get("user_id", ""))
    chat_id = event.get("chat_id")

    if not user_id or chat_id is None:
        logger.error("Invalid event payload in planner lambda: %s", event)
        return {"statusCode": 400, "body": "missing user_id or chat_id"}

    settings = get_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
    table = dynamodb.Table(settings.DYNAMODB_TABLE_NAME)
    repo = DynamoRepository(table)
    telegram_api = TelegramAPI(settings.TELEGRAM_BOT_TOKEN)
    llm_client = LLMClient(
        model=settings.PLANNER_LLM_MODEL,
        api_key=settings.LLM_API_KEY,
        reasoning_effort=settings.PLANNER_LLM_REASONING_EFFORT,
    )

    planner = PlannerHandler(
        repo=repo, telegram_api=telegram_api, llm_client=llm_client
    )
    planner.generate_plan(user_id, chat_id)

    return {"statusCode": 200, "body": "ok"}
