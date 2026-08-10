"""AWS Lambda entry point for Telegram bot webhook commands and routing."""

import base64
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]

from meal_planner.config import get_settings
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient
from meal_planner.llm.parser import parse_conversational_response
from meal_planner.llm.prompts import build_conversational_prompt
from meal_planner.models.schemas import (
    ConversationIntent,
    Ingredient,
    MealLogEntry,
    PlanDay,
    PlannedMeal,
    UserProfile,
    WeeklyPlan,
)
from meal_planner.router import RouteResult, RouteType, route_update
from meal_planner.telegram.api import TelegramAPI

logger = logging.getLogger(__name__)


class BotHandler:
    """Bot handler managing Telegram update routing and command execution."""

    def __init__(
        self,
        repo: DynamoRepository,
        telegram_api: TelegramAPI,
        lambda_client: Any = None,
        planner_function_name: str = "",
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.lambda_client = lambda_client
        self.planner_function_name = planner_function_name
        self.llm_client = llm_client

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Process incoming Telegram update."""
        route = route_update(update)

        if route.route_type == RouteType.COMMAND:
            self.handle_command(route)
        elif route.route_type == RouteType.CALLBACK:
            self.handle_callback(route)
        elif route.route_type == RouteType.CONVERSATIONAL:
            self.handle_conversational(route)

        return {"statusCode": 200, "body": "ok"}

    def handle_command(self, route: RouteResult) -> None:
        """Handle command update."""
        if not route.chat_id or not route.user_id:
            return

        cmd = route.command
        chat_id = route.chat_id
        user_id = route.user_id

        if cmd == "start":
            self._cmd_start(chat_id, user_id)
        elif cmd == "profile":
            self._cmd_profile(chat_id, user_id)
        elif cmd == "plan":
            self._cmd_plan(chat_id, user_id)
        elif cmd == "grocery":
            self._cmd_grocery(chat_id, user_id)
        elif cmd == "today":
            self._cmd_today(chat_id, user_id)
        elif cmd == "submit_meals":
            self._cmd_submit_meals(chat_id, user_id)
        else:
            self.telegram_api.send_message(
                chat_id,
                f"Unknown command: /{cmd}. Type /start for options.",
            )

    def _cmd_start(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if profile:
            msg = (
                f"Welcome back, {profile.name}! 👋\n"
                "Use /plan to generate a weekly plan or /profile to view info."
            )
        else:
            msg = (
                "Welcome to Meal Planner Bot! 🥗\n"
                "Let's get set up. What is your name and how many people "
                "are in your family?"
            )
        self.telegram_api.send_message(chat_id, msg)

    def _cmd_profile(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile:
            self.telegram_api.send_message(
                chat_id, "No profile found. Type /start to get set up!"
            )
            return

        lines = [
            f"👤 *Profile: {profile.name}*",
            f"• People count: {profile.people_count}",
        ]
        if profile.family_members:
            lines.append("• *Family Members:*")
            for m in profile.family_members:
                lines.append(f"  - {m.name} ({m.calorie_target} kcal/day)")
        if profile.allergies:
            lines.append(f"• *Allergies:* {', '.join(profile.allergies)}")
        if profile.dietary_preferences:
            lines.append(
                f"• *Preferences:* {', '.join(profile.dietary_preferences)}"
            )
        if profile.restrictions:
            lines.append(f"• *Restrictions:* {', '.join(profile.restrictions)}")
        if profile.goals:
            lines.append(f"• *Goals:* {', '.join(profile.goals)}")

        self.telegram_api.send_message(chat_id, "\n".join(lines))

    def _cmd_plan(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile:
            self.telegram_api.send_message(
                chat_id, "Please set up your profile first using /start!"
            )
            return

        self.telegram_api.send_message(
            chat_id,
            "Working on your weekly meal plan... 🧑‍🍳 This may take a minute!",
        )

        if self.lambda_client and self.planner_function_name:
            try:
                payload = json.dumps({"user_id": user_id, "chat_id": chat_id})
                self.lambda_client.invoke(
                    FunctionName=self.planner_function_name,
                    InvocationType="Event",
                    Payload=payload,
                )
            except Exception as err:
                logger.error("Failed to invoke planner lambda: %s", err)
                self.telegram_api.send_message(
                    chat_id, "Error generating plan. Please try again later."
                )

    def _cmd_grocery(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_current_plan(user_id)
        if not plan or not plan.grocery_list:
            self.telegram_api.send_message(
                chat_id, "No active meal plan found. Use /plan to generate one!"
            )
            return
        self.telegram_api.send_grocery_list(chat_id, plan.grocery_list)

    def _cmd_today(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_current_plan(user_id)
        if not plan or not plan.days:
            self.telegram_api.send_message(
                chat_id,
                "No active meal plan found. Use /plan to generate one!",
            )
            return

        today_plan = self._get_todays_plan_day(plan)
        lines = [f"📅 *Today's Planned Meals (Day {today_plan.day})*", ""]
        for meal in today_plan.meals:
            cooked = " [Cooked]" if meal.was_cooked else ""
            lines.append(
                f"• *{meal.meal_type.capitalize()}*: {meal.name} "
                f"({meal.est_calories} kcal){cooked}"
            )
        self.telegram_api.send_message(chat_id, "\n".join(lines))

    def _cmd_submit_meals(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_current_plan(user_id)
        if not plan or not plan.days:
            self.telegram_api.send_message(
                chat_id,
                "No active meal plan found. Use /plan to generate one!",
            )
            return

        today_plan = self._get_todays_plan_day(plan)
        self.telegram_api.send_meal_checkin(
            chat_id, today_plan.meals, day=today_plan.day
        )

    @staticmethod
    def _get_todays_plan_day(plan: WeeklyPlan) -> PlanDay:
        """Return the PlanDay that corresponds to today's date.

        Computes a 1-based offset from plan.week_start to today and returns
        the matching PlanDay. Falls back to the first day in the plan when
        today falls outside the 7-day window (e.g. a stale plan).
        """
        try:
            week_start = date.fromisoformat(plan.week_start)
            day_offset = (date.today() - week_start).days + 1
            if 1 <= day_offset <= 7:
                matched = next(
                    (d for d in plan.days if d.day == day_offset), None
                )
                if matched is not None:
                    return matched
        except ValueError, TypeError:
            pass
        return plan.days[0]

    def handle_callback(self, route: RouteResult) -> None:
        """Handle inline keyboard callback queries."""
        if not route.chat_id or not route.user_id or not route.callback_data:
            return

        data_parts = route.callback_data.split(":")
        if len(data_parts) == 4 and data_parts[0] == "checkin":
            try:
                day = int(data_parts[1])
            except ValueError:
                return
            meal_type = data_parts[2]
            action = data_parts[3]

            plan = self.repo.get_current_plan(route.user_id)
            if not plan:
                self.telegram_api.send_message(
                    route.chat_id, "No active plan found."
                )
                return

            if action == "cooked":
                self.repo.update_meal_status(
                    route.user_id,
                    plan.week_start,
                    day,
                    meal_type,
                    was_cooked=True,
                )
                self.telegram_api.send_message(
                    route.chat_id, f"Marked {meal_type} as cooked! ✅"
                )
            elif action == "skipped":
                self.repo.update_meal_status(
                    route.user_id,
                    plan.week_start,
                    day,
                    meal_type,
                    was_cooked=False,
                )
                self.telegram_api.send_message(
                    route.chat_id, f"Marked {meal_type} as skipped ❌"
                )
            elif action == "swapped":
                self.repo.update_meal_status(
                    route.user_id,
                    plan.week_start,
                    day,
                    meal_type,
                    was_cooked=False,
                )
                self.telegram_api.send_message(
                    route.chat_id,
                    f"Marked {meal_type} as swapped 🔄. "
                    "What did you have instead?",
                )

    def handle_conversational(self, route: RouteResult) -> None:
        """Handle free-form user message using LLM and parse metadata intent."""
        if not route.chat_id or not route.user_id or not route.text:
            return

        try:
            profile = self.repo.get_profile(route.user_id)
            current_plan = self.repo.get_current_plan(route.user_id)
            recent_meals = self.repo.get_meal_history(route.user_id, days=14)

            system_prompt = build_conversational_prompt(
                profile=profile,
                current_plan=current_plan,
                recent_meals=recent_meals,
            )

            client = self.llm_client or LLMClient()
            raw_response = client.chat_sync(system_prompt, route.text)

            reply_text, metadata = parse_conversational_response(raw_response)

            self._apply_intent_metadata(
                route.user_id, metadata.intent, metadata.entities, profile
            )

            final_reply = (
                reply_text
                or "I got your message! Let me know if you need anything else."
            )
            self.telegram_api.send_message(route.chat_id, final_reply)
        except Exception as exc:
            logger.error("Error in conversational handler: %s", exc)
            self.telegram_api.send_message(
                route.chat_id,
                "Sorry, I had trouble understanding that. Please try again.",
            )

    def _apply_intent_metadata(
        self,
        user_id: str,
        intent: ConversationIntent,
        entities: dict[str, Any],
        existing_profile: Optional[UserProfile],
    ) -> None:
        """Persist state changes to DynamoDB based on LLM intent metadata."""
        if intent == ConversationIntent.LOG_MEAL:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry = MealLogEntry(
                date=str(entities.get("date", today_str)),
                meal_type=str(entities.get("meal_type", "meal")),
                description=str(entities.get("description", "Logged meal")),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.repo.log_meal(user_id, entry)

        elif intent == ConversationIntent.EDIT_PLAN:
            current_plan = self.repo.get_current_plan(user_id)
            if current_plan and "day" in entities and "meal_type" in entities:
                try:
                    day_num = int(entities["day"])
                    m_type = str(entities["meal_type"]).lower()
                    new_name = str(entities.get("name", "Updated meal"))
                    est_cals = int(entities.get("est_calories", 500))

                    raw_ings = entities.get("ingredients", [])
                    ings = [
                        Ingredient(
                            item=str(ing.get("item", "")),
                            amount=str(ing.get("amount", "")),
                        )
                        for ing in raw_ings
                        if isinstance(ing, dict)
                    ]

                    for p_day in current_plan.days:
                        if p_day.day == day_num:
                            found = False
                            for meal in p_day.meals:
                                if meal.meal_type.lower() == m_type:
                                    meal.name = new_name
                                    meal.est_calories = est_cals
                                    if ings:
                                        meal.ingredients = ings
                                    found = True
                                    break
                            if not found:
                                p_day.meals.append(
                                    PlannedMeal(
                                        meal_type=m_type,
                                        name=new_name,
                                        ingredients=ings,
                                        est_calories=est_cals,
                                    )
                                )
                    self.repo.save_plan(user_id, current_plan)
                except (ValueError, TypeError) as err:
                    logger.warning("Failed to edit plan from entities: %s", err)

        elif intent == ConversationIntent.UPDATE_PROFILE:
            prof = existing_profile or UserProfile(
                name=str(entities.get("name", "User"))
            )
            data = prof.model_dump()
            for key in [
                "name",
                "people_count",
                "allergies",
                "dietary_preferences",
                "restrictions",
                "goals",
            ]:
                if key in entities and entities[key] is not None:
                    data[key] = entities[key]

            try:
                updated_profile = UserProfile.model_validate(data)
                self.repo.save_profile(user_id, updated_profile)
            except Exception as err:
                logger.warning(
                    "Failed to update profile from entities: %s", err
                )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for API Gateway HTTP API events."""
    body_str = event.get("body", "")
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8")

    try:
        update = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError:
        logger.error("Failed to parse event body as JSON")
        return {"statusCode": 200, "body": "ok"}

    settings = get_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
    table = dynamodb.Table(settings.DYNAMODB_TABLE_NAME)
    repo = DynamoRepository(table)

    telegram_api = TelegramAPI(settings.TELEGRAM_BOT_TOKEN)
    lambda_client = boto3.client("lambda", region_name=settings.AWS_REGION)
    planner_func = os.getenv("PLANNER_FUNCTION_NAME", "meal-planner-planner")
    llm_client = LLMClient(
        model=settings.LLM_MODEL, api_key=settings.LLM_API_KEY
    )

    bot_handler = BotHandler(
        repo=repo,
        telegram_api=telegram_api,
        lambda_client=lambda_client,
        planner_function_name=planner_func,
        llm_client=llm_client,
    )

    return bot_handler.handle_update(update)
