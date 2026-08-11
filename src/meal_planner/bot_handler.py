"""AWS Lambda entry point for Telegram webhook commands and routing."""

import base64
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import get_settings, get_webhook_secret
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient
from meal_planner.llm.parser import parse_conversational_response
from meal_planner.llm.prompts import build_conversational_prompt
from meal_planner.models.schemas import (
    ConversationIntent,
    GroceryStatus,
    Ingredient,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlannedMeal,
    PlanStatus,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)
from meal_planner.planner_handler import FINALIZE_GROCERY, GENERATE_PLAN
from meal_planner.router import (
    RouteResult,
    RouteType,
    parse_checkin_callback,
    route_update,
)
from meal_planner.telegram.api import TelegramAPI, TelegramAPIError

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"


@dataclass(frozen=True)
class MutationResult:
    """Outcome of applying structured conversational metadata."""

    success: bool
    message: str | None = None


def _webhook_secret_is_valid(event: dict[str, Any]) -> bool:
    try:
        expected_secret = get_webhook_secret()
    except ValidationError:
        return False
    headers = event.get("headers")
    if not expected_secret or not isinstance(headers, dict):
        return False
    for name, value in headers.items():
        if (
            isinstance(name, str)
            and name.lower() == WEBHOOK_SECRET_HEADER
            and isinstance(value, str)
        ):
            try:
                return hmac.compare_digest(value, expected_secret)
            except TypeError:
                return False
    return False


class BotHandler:
    """Route commands, callbacks, and conversational mutations."""

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
        route = route_update(update)
        try:
            if route.route_type is RouteType.COMMAND:
                self.handle_command(route)
            elif route.route_type is RouteType.CALLBACK:
                self.handle_callback(route)
            elif route.route_type is RouteType.CONVERSATIONAL:
                self.handle_conversational(route)
        except TelegramAPIError as exc:
            logger.error("Telegram delivery failed: %s", exc)
        return {"statusCode": 200, "body": "ok"}

    def handle_command(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id:
            return
        handlers = {
            "start": self._cmd_start,
            "profile": self._cmd_profile,
            "plan": self._cmd_plan,
            "grocery": self._cmd_grocery,
            "today": self._cmd_today,
            "submit_meals": self._cmd_submit_meals,
        }
        handler = handlers.get(route.command or "")
        if handler:
            handler(route.chat_id, route.user_id)
        else:
            self.telegram_api.send_message(
                route.chat_id,
                f"Unknown command: /{route.command}. Type /start for options.",
            )

    def _cmd_start(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if profile and profile.is_complete:
            message = (
                f"Welcome back, {profile.name}! Use /plan to generate a plan "
                "or /profile to review your details."
            )
        else:
            message = (
                "Welcome to Meal Planner Bot! Tell me your name, household "
                "size, each person's name and calorie target, allergies, "
                "preferences, restrictions, and goals."
            )
        self.telegram_api.send_message(chat_id, message)

    def _cmd_profile(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile:
            self.telegram_api.send_message(
                chat_id, "No complete profile found. Use /start to begin."
            )
            return
        lines = [
            f"Profile: {profile.name}",
            f"People count: {profile.people_count}",
            "Family members:",
            *(
                f"- {member.name} ({member.calorie_target} kcal/day)"
                for member in profile.family_members
            ),
            f"Allergies: {', '.join(profile.allergies) or 'None'}",
            f"Preferences: {', '.join(profile.dietary_preferences) or 'None'}",
            f"Restrictions: {', '.join(profile.restrictions) or 'None'}",
            f"Goals: {', '.join(profile.goals) or 'None'}",
        ]
        self.telegram_api.send_message(chat_id, "\n".join(lines))

    def _cmd_plan(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile or not profile.is_complete:
            self.telegram_api.send_message(
                chat_id, "Complete your profile before generating a plan."
            )
            return
        if self._invoke_planner(
            user_id, chat_id, GENERATE_PLAN, week_start=date.today().isoformat()
        ):
            self.telegram_api.send_message(
                chat_id, "Working on your weekly meal plan."
            )
        else:
            self.telegram_api.send_message(
                chat_id, "I couldn't start plan generation. Please retry."
            )

    def _cmd_grocery(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_active_plan(user_id)
        if not plan:
            self.telegram_api.send_message(chat_id, "No active confirmed plan.")
        elif plan.grocery_status is GroceryStatus.PENDING:
            self.telegram_api.send_message(
                chat_id, "Your grocery list is pending."
            )
        elif plan.grocery_status is GroceryStatus.ERROR:
            self.telegram_api.send_message(
                chat_id, "Grocery generation failed. Confirm again to retry."
            )
        elif plan.grocery_status is not GroceryStatus.READY:
            self.telegram_api.send_message(
                chat_id, "Confirm your draft before requesting groceries."
            )
        else:
            self.telegram_api.send_grocery_list(chat_id, plan.grocery_list)

    def _cmd_today(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_active_plan(user_id)
        plan_day = self._get_todays_plan_day(plan) if plan else None
        if not plan_day:
            self.telegram_api.send_message(chat_id, "No active plan for today.")
            return
        lines = [f"Today's Planned Meals (Day {plan_day.day})", ""]
        lines.extend(
            f"• {meal.meal_type.value.capitalize()}: {meal.name} "
            f"({meal.est_calories} kcal)"
            for meal in plan_day.meals
        )
        self.telegram_api.send_message(chat_id, "\n".join(lines))

    def _cmd_submit_meals(self, chat_id: int | str, user_id: str) -> None:
        plan = self.repo.get_active_plan(user_id)
        plan_day = self._get_todays_plan_day(plan) if plan else None
        if not plan or not plan_day:
            self.telegram_api.send_message(chat_id, "No active plan for today.")
            return
        self.telegram_api.send_meal_checkin(
            chat_id,
            plan_day.meals,
            week_start=plan.week_start_date,
            day=plan_day.day,
        )

    @staticmethod
    def _get_todays_plan_day(plan: WeeklyPlan) -> Any:
        offset = (date.today() - plan.week_start).days + 1
        if not 1 <= offset <= 7:
            return None
        return next((item for item in plan.days if item.day == offset), None)

    def handle_callback(self, route: RouteResult) -> None:
        acknowledgement = "Unable to update meal"
        try:
            if (
                route.chat_id is None
                or not route.user_id
                or not route.callback_data
            ):
                return
            callback = parse_checkin_callback(route.callback_data)
            if not callback:
                self.telegram_api.send_message(
                    route.chat_id,
                    "That check-in button is invalid or outdated.",
                )
                return
            plan = self.repo.get_plan(route.user_id, callback.week_start)
            today = date.today()
            if (
                not plan
                or plan.status is not PlanStatus.CONFIRMED
                or not plan.week_start <= today <= plan.week_end
            ):
                self.telegram_api.send_message(
                    route.chat_id, "That check-in belongs to an inactive plan."
                )
                return
            updated = self.repo.update_meal_outcome(
                route.user_id,
                callback.week_start,
                callback.day,
                callback.meal_type.value,
                callback.outcome,
            )
            if not updated:
                self.telegram_api.send_message(
                    route.chat_id, "That meal could not be updated."
                )
                return
            acknowledgement = "Meal updated"
            self.telegram_api.send_message(
                route.chat_id,
                f"Marked {callback.meal_type.value} as "
                f"{callback.outcome.value}.",
            )
        finally:
            if route.callback_query_id:
                try:
                    self.telegram_api.answer_callback_query(
                        route.callback_query_id, acknowledgement
                    )
                except TelegramAPIError:
                    logger.error("Failed to acknowledge callback query")

    def handle_conversational(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id or not route.text:
            return
        try:
            profile = self.repo.get_profile(route.user_id)
            prompt = build_conversational_prompt(
                profile=profile,
                current_plan=self.repo.get_latest_plan(route.user_id),
                recent_meals=self.repo.get_meal_history(route.user_id, days=14),
            )
            client = self.llm_client or LLMClient()
            reply, metadata = parse_conversational_response(
                client.chat_sync(prompt, route.text)
            )
            result = self._apply_intent_metadata(
                route.user_id,
                route.chat_id,
                metadata.intent,
                metadata.entities,
                profile,
            )
            if result.message:
                reply = result.message
            elif not result.success:
                reply = "I couldn't save that change. Please try again."
            self.telegram_api.send_message(
                route.chat_id,
                reply or "I got your message. What would you like to do next?",
            )
        except Exception as exc:
            logger.error("Conversational handling failed: %s", exc)
            self.telegram_api.send_message(
                route.chat_id,
                "Sorry, I couldn't process that request. Please try again.",
            )

    def _apply_intent_metadata(
        self,
        user_id: str,
        chat_id: int | str,
        intent: ConversationIntent,
        entities: dict[str, Any],
        existing_profile: Optional[UserProfile],
    ) -> MutationResult:
        try:
            if intent is ConversationIntent.LOG_MEAL:
                entry = MealLogEntry(
                    date=entities.get("date", date.today().isoformat()),
                    meal_type=entities.get("meal_type", MealType.SNACK.value),
                    description=entities.get("description", "Logged meal"),
                    created_at=datetime.now(timezone.utc),
                )
                self.repo.log_meal(user_id, entry)
                return MutationResult(True)
            if intent is ConversationIntent.UPDATE_PROFILE:
                return self._update_profile(user_id, entities, existing_profile)
            if intent is ConversationIntent.CONFIRM_PLAN:
                return self._confirm_plan(user_id, chat_id)
            if intent is ConversationIntent.EDIT_PLAN:
                return self._edit_plan(user_id, chat_id, entities)
            return MutationResult(True)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning("Rejected conversational mutation: %s", exc)
            return MutationResult(False)
        except Exception as exc:
            logger.error("Mutation persistence failed: %s", exc)
            return MutationResult(
                False, "I couldn't save that change. Please try again."
            )

    def _update_profile(
        self,
        user_id: str,
        entities: dict[str, Any],
        existing: UserProfile | None,
    ) -> MutationResult:
        update = ProfileUpdateEntities.model_validate(entities)
        if existing:
            data = existing.model_dump(mode="json")
        else:
            data = self.repo.get_profile_draft(user_id).model_dump(mode="json")
        for field in update.model_fields_set:
            data[field] = getattr(update, field)
        draft = ProfileUpdateEntities.model_validate(data)
        required = (
            "name",
            "people_count",
            "family_members",
            "allergies",
            "dietary_preferences",
            "restrictions",
            "goals",
        )
        missing = [field for field in required if getattr(draft, field) is None]
        if missing:
            self.repo.save_profile_draft(user_id, draft)
            return MutationResult(
                True, "I still need: " + ", ".join(missing) + "."
            )
        profile = UserProfile.model_validate(draft.model_dump())
        if not profile.is_complete:
            return MutationResult(
                False,
                "Please provide one name and calorie target for each person.",
            )
        self.repo.save_profile(user_id, profile)
        self.repo.delete_profile_draft(user_id)
        return MutationResult(True, "Your profile has been saved.")

    def _confirm_plan(self, user_id: str, chat_id: int | str) -> MutationResult:
        plan = self.repo.get_latest_plan(user_id)
        if not plan or plan.status is not PlanStatus.DRAFT:
            return MutationResult(False, "There is no draft plan to confirm.")
        plan.status = PlanStatus.CONFIRMED
        plan.grocery_status = GroceryStatus.PENDING
        plan.grocery_list = []
        self.repo.save_plan(user_id, plan)
        if not self._invoke_planner(
            user_id,
            chat_id,
            FINALIZE_GROCERY,
            week_start=plan.week_start_date,
        ):
            plan.grocery_status = GroceryStatus.ERROR
            self.repo.save_plan(user_id, plan)
            return MutationResult(
                False, "The plan was confirmed, but groceries failed."
            )
        return MutationResult(
            True, "Plan confirmed. Groceries are being prepared."
        )

    def _edit_plan(
        self, user_id: str, chat_id: int | str, entities: dict[str, Any]
    ) -> MutationResult:
        plan = self.repo.get_latest_plan(user_id)
        if not plan:
            return MutationResult(False, "There is no plan to edit.")
        day_number = int(entities["day"])
        meal_type = MealType(str(entities["meal_type"]).lower())
        plan_day = next(
            (item for item in plan.days if item.day == day_number), None
        )
        meal = (
            next(
                (
                    item
                    for item in plan_day.meals
                    if item.meal_type is meal_type
                ),
                None,
            )
            if plan_day
            else None
        )
        if not meal:
            return MutationResult(False, "That day or meal does not exist.")
        assert plan_day is not None
        ingredients = meal.ingredients
        if "ingredients" in entities:
            ingredients = [
                Ingredient.model_validate(item)
                for item in entities["ingredients"]
            ]
        updated = PlannedMeal.model_validate(
            {
                **meal.model_dump(mode="json"),
                "name": entities.get("name", meal.name),
                "est_calories": entities.get("est_calories", meal.est_calories),
                "ingredients": ingredients,
                "outcome": MealOutcome.UNREPORTED,
            }
        )
        plan_day.meals[plan_day.meals.index(meal)] = updated
        refresh = plan.status is PlanStatus.CONFIRMED
        if refresh:
            plan.grocery_status = GroceryStatus.PENDING
            plan.grocery_list = []
        self.repo.save_plan(user_id, plan)
        if refresh and not self._invoke_planner(
            user_id,
            chat_id,
            FINALIZE_GROCERY,
            week_start=plan.week_start_date,
        ):
            plan.grocery_status = GroceryStatus.ERROR
            self.repo.save_plan(user_id, plan)
            return MutationResult(
                False, "The meal changed, but grocery refresh failed."
            )
        return MutationResult(True, "The meal plan was updated.")

    def _invoke_planner(
        self,
        user_id: str,
        chat_id: int | str,
        action: str,
        *,
        week_start: str,
    ) -> bool:
        if not self.lambda_client or not self.planner_function_name:
            return False
        try:
            self.lambda_client.invoke(
                FunctionName=self.planner_function_name,
                InvocationType="Event",
                Payload=json.dumps(
                    {
                        "action": action,
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "week_start": week_start,
                    }
                ),
            )
        except Exception as exc:
            logger.error("Planner invocation failed: %s", exc)
            return False
        return True


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for API Gateway HTTP API events."""
    if not _webhook_secret_is_valid(event):
        return {"statusCode": 403, "body": "forbidden"}
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except ValueError, UnicodeDecodeError:
            return {"statusCode": 200, "body": "ok"}
    try:
        update = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"statusCode": 200, "body": "ok"}
    settings = get_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    repo = DynamoRepository(dynamodb.Table(settings.dynamodb_table_name))
    telegram_api = TelegramAPI(
        settings.telegram_bot_token,
        request_timeout=settings.telegram_request_timeout_seconds,
    )
    llm_client = LLMClient(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        max_retries=settings.llm_max_retries,
        initial_backoff=settings.llm_initial_backoff_seconds,
        request_timeout=settings.llm_request_timeout_seconds,
    )
    handler = BotHandler(
        repo,
        telegram_api,
        lambda_client=boto3.client("lambda", region_name=settings.aws_region),
        planner_function_name=os.getenv(
            "PLANNER_FUNCTION_NAME", "meal-planner-planner"
        ),
        llm_client=llm_client,
    )
    return handler.handle_update(update)
