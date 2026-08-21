"""AWS Lambda entry point for Telegram webhook commands and routing."""

import base64
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import (
    BotConfigurationError,
    get_settings,
    get_webhook_secret,
)
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.llm.client import LLMClient
from meal_planner.llm.parser import (
    parse_conversational_response,
    parse_preference_interpretation,
)
from meal_planner.llm.prompts import (
    build_conversational_prompt,
    build_preference_interpretation_prompt,
)
from meal_planner.models.schemas import (
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    GroceryStatus,
    Ingredient,
    MealLogDraft,
    MealLogEntry,
    MealOutcome,
    MealType,
    PlannedMeal,
    PlanStatus,
    PreferenceRequirement,
    ProfileEditCategory,
    ProfileEditOperation,
    ProfileUpdateEntities,
    UserProfile,
    WeeklyPlan,
)
from meal_planner.planner_handler import (
    FINALIZE_GROCERY,
    GENERATE_PLAN,
    REVISE_PLAN,
)
from meal_planner.router import (
    ProfileCallback,
    ProfileCallbackAction,
    RouteResult,
    RouteType,
    parse_checkin_callback,
    parse_profile_callback,
    route_update,
)
from meal_planner.telegram.access import TelegramAccessPolicy
from meal_planner.telegram.api import TelegramAPI, TelegramAPIError
from meal_planner.telegram.commands import render_help

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
        access_policy: TelegramAccessPolicy | None = None,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.lambda_client = lambda_client
        self.planner_function_name = planner_function_name
        self.llm_client = llm_client
        self.access_policy = access_policy or TelegramAccessPolicy(frozenset())

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        route = route_update(update)
        if route.route_type is RouteType.UNKNOWN:
            return {"statusCode": 200, "body": "ok"}
        decision = self.access_policy.evaluate(route)
        if not decision.allowed:
            logger.info(
                "Telegram update denied: user_id=%s chat_type=%s reason=%s",
                route.user_id,
                route.chat_type,
                decision.reason.value,
            )
            return {"statusCode": 200, "body": "ok"}
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
            "help": self._cmd_help,
            "profile": self._cmd_profile,
            "plan": self._cmd_plan,
            "grocery": self._cmd_grocery,
            "today": self._cmd_today,
            "submit_meals": self._cmd_submit_meals,
            "checkin": self._cmd_checkin,
            "cancel": self._cmd_cancel,
        }
        handler = handlers.get(route.command or "")
        if handler:
            handler(route.chat_id, route.user_id)
        else:
            self.telegram_api.send_message(
                route.chat_id,
                f"Unknown command: /{route.command}. Type /help for options.",
            )

    def _cmd_help(self, chat_id: int | str, user_id: str) -> None:
        """Send the stateless command reference."""
        self.telegram_api.send_message(chat_id, render_help())

    def _cmd_start(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if profile and profile.is_complete:
            message = (
                f"Welcome back, {profile.name} family! Use /plan to "
                "generate a plan "
                "or /profile to review your details. Use /submit_meals to "
                "log actual meals, /checkin for planned meal outcomes, and "
                "/cancel to stop an unfinished workflow."
            )
        else:
            message = (
                "Welcome to Meal Planner Bot! Tell me your family name, "
                "household size, each household member's name and calorie "
                "target, dietary constraints, dietary preferences, and "
                "goals. "
                "After setup, use /plan, /submit_meals, /checkin, or "
                "/cancel."
            )
        self.telegram_api.send_message(chat_id, message)

    def _cmd_profile(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile:
            self.telegram_api.send_message(
                chat_id, "No complete profile found. Use /start to begin."
            )
            return
        previous = self._get_conversation_state(user_id)
        profile_state = self._new_profile_state()
        if not self._replace_conversation_state(
            user_id, profile_state, previous
        ):
            self.telegram_api.send_message(
                chat_id,
                "That workflow changed while I was opening /profile. "
                "Please try again.",
            )
            return
        self.telegram_api.send_profile(chat_id, profile)

    def _cmd_plan(self, chat_id: int | str, user_id: str) -> None:
        profile = self.repo.get_profile(user_id)
        if not profile or not profile.is_complete:
            self.telegram_api.send_message(
                chat_id, "Complete your profile before generating a plan."
            )
            return
        state = self._get_conversation_state(user_id)
        if (
            state
            and state.workflow_kind is ConversationWorkflowKind.PLAN_REQUEST
            and state.step is ConversationWorkflowStep.RETRY_READY
        ):
            self._retry_plan_request(user_id, chat_id, state)
            return
        replaced = state is not None
        request_state = self._new_plan_state()
        if not self._replace_conversation_state(user_id, request_state, state):
            self.telegram_api.send_message(
                chat_id,
                "That workflow changed while I was starting /plan. "
                "Please try again.",
            )
            return
        prefix = "I replaced your unfinished workflow. " if replaced else ""
        self.telegram_api.send_message(
            chat_id,
            prefix + "Do you have any preferences for the next plan? "
            "Reply with a preference, or say 'no preference'.",
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
        state = self._get_conversation_state(user_id)
        meal_state = self._new_meal_state()
        if not self._replace_conversation_state(user_id, meal_state, state):
            self.telegram_api.send_message(
                chat_id,
                "That workflow changed while I was starting meal logging. "
                "Please try again.",
            )
            return
        prefix = "I replaced your unfinished workflow. " if state else ""
        self.telegram_api.send_message(
            chat_id,
            prefix
            + "What date was the meal? Use YYYY-MM-DD, from today through "
            "the previous seven days.",
        )

    def _cmd_checkin(self, chat_id: int | str, user_id: str) -> None:
        """Show planned-meal outcome buttons for today's active plan."""
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

    def _cmd_cancel(self, chat_id: int | str, user_id: str) -> None:
        state = self._get_conversation_state(user_id)
        if state is None:
            self.telegram_api.send_message(
                chat_id, "There is nothing to cancel."
            )
            return
        if not self.repo.delete_conversation_state(
            user_id, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id, "That workflow changed. Please use /cancel again."
            )
            return
        self.telegram_api.send_message(
            chat_id, "Cancelled the unfinished workflow."
        )

    def _get_conversation_state(self, user_id: str) -> ConversationState | None:
        state = self.repo.get_conversation_state(user_id, consistent_read=True)
        return state if isinstance(state, ConversationState) else None

    @staticmethod
    def _new_meal_state() -> ConversationState:
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_DATE,
            meal_draft=MealLogDraft(),
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
        )

    @staticmethod
    def _new_plan_state() -> ConversationState:
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.PLAN_REQUEST,
            step=ConversationWorkflowStep.AWAITING_PREFERENCE,
            request_id=str(uuid4()),
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
        )

    @staticmethod
    def _new_profile_state() -> ConversationState:
        """Create an empty durable profile amendment menu state."""
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.PROFILE_EDIT,
            step=ConversationWorkflowStep.PROFILE_MENU,
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
        )

    def _replace_conversation_state(
        self,
        user_id: str,
        state: ConversationState,
        previous: ConversationState | None,
    ) -> bool:
        expected_revision = previous.revision if previous else None
        if previous is not None:
            state = state.model_copy(update={"revision": previous.revision + 1})
        return self.repo.save_conversation_state(
            user_id, state, expected_revision=expected_revision
        )

    @staticmethod
    def _get_todays_plan_day(plan: WeeklyPlan) -> Any:
        offset = (date.today() - plan.week_start).days + 1
        if not 1 <= offset <= 7:
            return None
        return next((item for item in plan.days if item.day == offset), None)

    def handle_callback(self, route: RouteResult) -> None:
        if route.callback_data:
            profile_callback = parse_profile_callback(route.callback_data)
            if profile_callback is not None:
                self._handle_profile_callback(route, profile_callback)
                return
        acknowledgement = "Unable to update meal"
        try:
            if (
                route.chat_id is None
                or not route.user_id
                or not route.callback_data
            ):
                return
            callback = parse_checkin_callback(route.callback_data)
            if callback is None:
                acknowledgement = "Invalid check-in"
                self.telegram_api.send_message(
                    route.chat_id,
                    "That check-in button is invalid or outdated.",
                )
                return

            snapshot = self.repo.get_active_plan_snapshot(route.user_id)
            today = date.today()
            if (
                snapshot is None
                or snapshot.plan.status is not PlanStatus.CONFIRMED
                or not snapshot.plan.week_start
                <= today
                <= snapshot.plan.week_end
                or snapshot.plan.week_start_date != callback.week_start
            ):
                acknowledgement = "Inactive plan"
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
                expected_epoch=snapshot.active_epoch,
            )
            if not updated:
                acknowledgement = "Meal changed"
                self.telegram_api.send_message(
                    route.chat_id,
                    "That meal changed before it could be updated. "
                    "Please try again.",
                )
                return
            acknowledgement = "Meal updated"
        except Exception as exc:
            logger.exception("Error handling callback: %s", exc)
            if route.chat_id is not None:
                self.telegram_api.send_message(
                    route.chat_id,
                    "Sorry, I couldn't update that meal. Please try again.",
                )
        else:
            try:
                self.telegram_api.send_message(
                    route.chat_id,
                    f"Marked {callback.meal_type.value} as "
                    f"{callback.outcome.value}.",
                )
            except TelegramAPIError as exc:
                logger.error("Callback success delivery failed: %s", exc)
        finally:
            if route.callback_query_id:
                try:
                    self.telegram_api.answer_callback_query(
                        route.callback_query_id, acknowledgement
                    )
                except TelegramAPIError:
                    logger.error("Failed to acknowledge callback query")

    def _handle_profile_callback(
        self, route: RouteResult, callback: ProfileCallback
    ) -> None:
        """Navigate the profile editor without changing the profile."""
        if (
            route.chat_id is None
            or not route.user_id
            or not route.callback_query_id
        ):
            return

        try:
            self.telegram_api.answer_callback_query(
                route.callback_query_id, "Processing profile action"
            )
        except TelegramAPIError:
            logger.error("Failed to acknowledge profile callback")

        try:
            state = self._get_conversation_state(route.user_id)
            if (
                state is None
                or state.workflow_kind
                is not ConversationWorkflowKind.PROFILE_EDIT
            ):
                self.telegram_api.send_message(
                    route.chat_id,
                    "That profile menu is no longer active. Use /profile "
                    "to open it again.",
                )
                return

            if callback.action is ProfileCallbackAction.ROOT:
                if state.step is not ConversationWorkflowStep.PROFILE_MENU:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "Finish or go back from the current profile edit "
                        "first.",
                    )
                    return
                self.telegram_api.send_profile_root(route.chat_id)
                return

            if callback.action is ProfileCallbackAction.CATEGORY:
                assert callback.category is not None
                if state.step is not ConversationWorkflowStep.PROFILE_MENU:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "Finish or go back from the current profile edit "
                        "first.",
                    )
                    return
                self.telegram_api.send_profile_category(
                    route.chat_id, callback.category
                )
                return

            if callback.action is ProfileCallbackAction.OPERATION:
                assert callback.category is not None
                assert callback.operation is not None
                if state.step is not ConversationWorkflowStep.PROFILE_MENU:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "Finish or go back from the current profile edit "
                        "first.",
                    )
                    return
                now = datetime.now(timezone.utc)
                candidate = state.model_copy(
                    update={
                        "step": ConversationWorkflowStep.AWAITING_PROFILE_INPUT,
                        "profile_category": callback.category,
                        "profile_operation": callback.operation,
                        "revision": state.revision + 1,
                        "updated_at": now,
                    }
                )
                if not self.repo.transition_conversation_state(
                    route.user_id,
                    candidate,
                    expected_revision=state.revision,
                ):
                    self.telegram_api.send_message(
                        route.chat_id,
                        "That profile menu changed. Please use /profile again.",
                    )
                    return
                self.telegram_api.send_profile_operation(
                    route.chat_id, callback.category, callback.operation
                )
                return

            if callback.action is ProfileCallbackAction.BACK:
                if (
                    state.step
                    is ConversationWorkflowStep.AWAITING_PROFILE_INPUT
                ):
                    now = datetime.now(timezone.utc)
                    candidate = state.model_copy(
                        update={
                            "step": ConversationWorkflowStep.PROFILE_MENU,
                            "profile_category": None,
                            "profile_operation": None,
                            "revision": state.revision + 1,
                            "updated_at": now,
                        }
                    )
                    if not self.repo.transition_conversation_state(
                        route.user_id,
                        candidate,
                        expected_revision=state.revision,
                    ):
                        self.telegram_api.send_message(
                            route.chat_id,
                            "That profile menu changed. Please try again.",
                        )
                        return
                self.telegram_api.send_profile_root(route.chat_id)
                return

            if callback.action in {
                ProfileCallbackAction.DONE,
                ProfileCallbackAction.CLOSE,
            }:
                if not self.repo.delete_conversation_state(
                    route.user_id, expected_revision=state.revision
                ):
                    self.telegram_api.send_message(
                        route.chat_id,
                        "That profile menu changed. Please use /profile again.",
                    )
                    return
                message = (
                    "Profile amendments saved."
                    if callback.action is ProfileCallbackAction.DONE
                    else "Closed profile amendments."
                )
                self.telegram_api.send_message(route.chat_id, message)
        except Exception:
            logger.exception("Profile callback handling failed")
            if route.chat_id is not None:
                try:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "Sorry, I couldn't open that profile menu. Please try "
                        "again.",
                    )
                except TelegramAPIError:
                    logger.error(
                        "Failed to deliver profile callback error message"
                    )

    def handle_conversational(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id or not route.text:
            return
        try:
            source_update_id = self._get_source_update_id(route)
            state = self._get_conversation_state(route.user_id)
            if (
                state
                and state.workflow_kind is ConversationWorkflowKind.PLAN_REQUEST
            ):
                self._handle_plan_preference(
                    route.chat_id,
                    route.user_id,
                    route.text,
                    state,
                    source_update_id=source_update_id,
                )
                return
            if (
                state
                and state.workflow_kind
                is ConversationWorkflowKind.PLAN_REVISION
            ):
                self._handle_plan_revision_state(
                    route.chat_id,
                    route.user_id,
                    route.text,
                    state,
                )
                return
            if (
                state
                and state.workflow_kind is ConversationWorkflowKind.PROFILE_EDIT
            ):
                message = self._handle_profile_edit_input(
                    route.user_id, route.chat_id, route.text, state
                )
                if message is not None:
                    self.telegram_api.send_message(route.chat_id, message)
                return
            profile = self.repo.get_profile(route.user_id)
            profile_draft = self.repo.get_profile_draft(route.user_id)
            prompt = build_conversational_prompt(
                profile=profile,
                profile_draft=(
                    profile_draft
                    if isinstance(profile_draft, ProfileUpdateEntities)
                    else None
                ),
                current_plan=self.repo.get_latest_plan(route.user_id),
                recent_meals=self.repo.get_meal_history(route.user_id, days=14),
                conversation_state=state,
                current_date=date.today(),
            )
            client = self.llm_client or LLMClient()
            reply, metadata = parse_conversational_response(
                client.chat_sync(prompt, route.text)
            )
            if (
                state
                and state.workflow_kind is ConversationWorkflowKind.MEAL_LOG
            ):
                reply = self._handle_meal_workflow(
                    route.chat_id,
                    route.user_id,
                    route.text,
                    state,
                    metadata.entities,
                    source_update_id=source_update_id,
                )
                self.telegram_api.send_message(route.chat_id, reply)
                return
            result = self._apply_intent_metadata(
                route.user_id,
                route.chat_id,
                metadata.intent,
                metadata.entities,
                profile,
                source_update_id=source_update_id,
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

    @staticmethod
    def _parse_profile_name_calories(
        text: str,
    ) -> tuple[str, int] | None:
        """Parse one member name followed by a positive calorie target."""
        if "\n" in text or "\r" in text:
            return None
        match = re.fullmatch(r"(.+?)\s+([0-9]+)", text.strip())
        if match is None:
            return None
        name = match.group(1).strip()
        calories = int(match.group(2))
        if not name or not 1 <= calories <= 10_000:
            return None
        return name, calories

    @staticmethod
    def _parse_profile_member_name(text: str) -> str | None:
        """Parse one exact, non-empty family member name."""
        if "\n" in text or "\r" in text:
            return None
        name = text.strip()
        return name or None

    @staticmethod
    def _member_identity(name: str) -> str:
        """Return the stable identity key for a family member name."""
        return name.strip().casefold()

    @staticmethod
    def _has_duplicate_member_identities(
        members: list[FamilyMember],
    ) -> bool:
        """Return whether members contain a stripped, case-folded duplicate."""
        identities: set[str] = set()
        for member in members:
            identity = BotHandler._member_identity(member.name)
            if identity in identities:
                return True
            identities.add(identity)
        return False

    @staticmethod
    def _parse_profile_item(text: str) -> str | None:
        """Parse one non-empty profile list item."""
        if "\n" in text or "\r" in text:
            return None
        item = text.strip()
        if not item or item.casefold() in {
            "none",
            "no",
            "nothing",
            "n/a",
            "not applicable",
        }:
            return None
        return item

    @staticmethod
    def _profile_with_updates(
        profile: UserProfile, updates: dict[str, Any]
    ) -> UserProfile:
        """Create a validated profile copy without mutating the original."""
        data = profile.model_dump(mode="json")
        data.update(updates)
        return UserProfile.model_validate(data)

    @staticmethod
    def _profile_items(
        profile: UserProfile, category: ProfileEditCategory
    ) -> list[str]:
        """Return a copied list for a non-family profile category."""
        if category is ProfileEditCategory.DIETARY_CONSTRAINTS:
            return list(profile.dietary_constraints)
        if category is ProfileEditCategory.DIETARY_PREFERENCES:
            return list(profile.dietary_preferences)
        if category is ProfileEditCategory.GOALS:
            return list(profile.goals)
        raise ValueError("family is not an item category")

    def _handle_profile_edit_input(
        self,
        user_id: str,
        chat_id: int | str,
        text: str,
        state: ConversationState,
    ) -> str | None:
        """Apply one deterministic profile amendment and return its result."""
        if state.step is not ConversationWorkflowStep.AWAITING_PROFILE_INPUT:
            return "Please choose a profile amendment from the buttons."
        category = state.profile_category
        operation = state.profile_operation
        if category is None or operation is None:
            return "That profile edit is invalid. Please use /profile again."

        profile = self.repo.get_profile(user_id)
        if profile is None:
            return "No complete profile found. Use /start to begin."

        try:
            updated, message = self._apply_profile_amendment(
                profile, category, operation, text
            )
        except ValidationError:
            return "That profile change is invalid. Please use a shorter value."
        except ValueError as exc:
            return str(exc)
        if updated is None:
            return message

        now = datetime.now(timezone.utc)
        menu_state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.PROFILE_MENU,
                "profile_category": None,
                "profile_operation": None,
                "revision": state.revision + 1,
                "updated_at": now,
            }
        )
        try:
            committed = self.repo.save_profile_and_transition_state(
                user_id, updated, menu_state, state
            )
        except Exception:
            logger.exception("Profile amendment transaction failed")
            return "I couldn't save that profile change. Please try again."
        if not committed:
            return "That profile edit is stale. Please open /profile again."
        self.telegram_api.send_message(chat_id, message)
        self.telegram_api.send_profile_category(chat_id, category)
        return None

    def _apply_profile_amendment(
        self,
        profile: UserProfile,
        category: ProfileEditCategory,
        operation: ProfileEditOperation,
        text: str,
    ) -> tuple[UserProfile | None, str]:
        """Validate and apply one immutable profile amendment."""
        if category is ProfileEditCategory.FAMILY:
            return self._apply_family_amendment(profile, operation, text)
        return self._apply_item_amendment(profile, category, operation, text)

    @staticmethod
    def _apply_family_amendment(
        profile: UserProfile,
        operation: ProfileEditOperation,
        text: str,
    ) -> tuple[UserProfile | None, str]:
        """Apply a family member add, remove, or calorie change."""
        members = list(profile.family_members)
        if operation is ProfileEditOperation.REMOVE:
            name = BotHandler._parse_profile_member_name(text)
            if name is None:
                return None, "Send one exact member name to remove."
            if len(members) == 1:
                return None, "You must keep at least one family member."
            matching_indices = [
                index
                for index, member in enumerate(members)
                if BotHandler._member_identity(member.name)
                == BotHandler._member_identity(name)
            ]
            if len(matching_indices) > 1:
                return (
                    None,
                    "That family member name is ambiguous in this legacy "
                    "profile. Please update the names before trying again.",
                )
            if not matching_indices:
                return None, "I couldn't find that family member."
            index = matching_indices[0]
            removed = members.pop(index)
            return (
                BotHandler._profile_with_updates(
                    profile,
                    {
                        "family_members": [
                            member.model_dump(mode="json") for member in members
                        ],
                        "people_count": len(members),
                    },
                ),
                f"Removed {removed.name}.",
            )

        parsed = BotHandler._parse_profile_name_calories(text)
        if parsed is None:
            return None, "Use the format: name calories, for example John 1500."
        name, calories = parsed
        matching_indices = [
            index
            for index, member in enumerate(members)
            if BotHandler._member_identity(member.name)
            == BotHandler._member_identity(name)
        ]
        if operation is ProfileEditOperation.ADD:
            if len(matching_indices) > 1:
                return (
                    None,
                    "That family member name is ambiguous in this legacy "
                    "profile. Please update the names before trying again.",
                )
            if matching_indices:
                return None, "That family member already exists."
            if len(members) >= 20:
                return None, "A profile can have at most 20 family members."
            member = FamilyMember(name=name, calorie_target=calories)
            members.append(member)
            return (
                BotHandler._profile_with_updates(
                    profile,
                    {
                        "family_members": [
                            item.model_dump(mode="json") for item in members
                        ],
                        "people_count": len(members),
                    },
                ),
                f"Added {name}.",
            )
        if len(matching_indices) > 1:
            return (
                None,
                "That family member name is ambiguous in this legacy profile. "
                "Please update the names before trying again.",
            )
        if not matching_indices:
            return None, "I couldn't find that family member."
        index = matching_indices[0]
        members[index] = FamilyMember(
            name=members[index].name, calorie_target=calories
        )
        return (
            BotHandler._profile_with_updates(
                profile,
                {
                    "family_members": [
                        item.model_dump(mode="json") for item in members
                    ],
                    "people_count": len(members),
                },
            ),
            f"Updated {members[index].name}'s calorie target.",
        )

    @staticmethod
    def _apply_item_amendment(
        profile: UserProfile,
        category: ProfileEditCategory,
        operation: ProfileEditOperation,
        text: str,
    ) -> tuple[UserProfile | None, str]:
        """Apply one item-list addition or removal."""
        item = BotHandler._parse_profile_item(text)
        if item is None:
            return None, "Send one non-empty item."
        items = BotHandler._profile_items(profile, category)
        matching_index = next(
            (
                index
                for index, existing in enumerate(items)
                if existing.casefold() == item.casefold()
            ),
            None,
        )
        label = category.value.replace("_", " ")
        if operation is ProfileEditOperation.ADD:
            if matching_index is not None:
                return None, f"That {label[:-1]} already exists."
            items.append(item)
            return (
                BotHandler._profile_with_updates(
                    profile, {category.value: items}
                ),
                f"Added {item}.",
            )
        if matching_index is None:
            return None, f"I couldn't find that {label[:-1]}."
        removed = items.pop(matching_index)
        return (
            BotHandler._profile_with_updates(profile, {category.value: items}),
            f"Removed {removed}.",
        )

    def _handle_meal_workflow(
        self,
        chat_id: int | str,
        user_id: str,
        text: str,
        state: ConversationState,
        entities: dict[str, Any],
        *,
        source_update_id: str | None,
    ) -> str:
        """Merge explicit meal fields and advance one durable workflow."""
        if state.step is ConversationWorkflowStep.AWAITING_ANOTHER_MEAL:
            normalized = text.strip().casefold().rstrip(".!?,;:")
            if source_update_id and state.last_update_id == source_update_id:
                return "Would you like to log another meal? Reply yes or no."
            if normalized in {"yes", "y", "sure", "ok", "okay", "another"}:
                now = datetime.now(timezone.utc)
                next_state = state.model_copy(
                    update={
                        "step": ConversationWorkflowStep.AWAITING_DATE,
                        "meal_draft": MealLogDraft(),
                        "revision": state.revision + 1,
                        "updated_at": now,
                        "last_update_id": source_update_id,
                    }
                )
                if not self.repo.transition_conversation_state(
                    user_id, next_state, expected_revision=state.revision
                ):
                    return (
                        "That meal workflow changed. Please use /submit_meals."
                    )
                return (
                    "Okay. What date was the next meal? Use YYYY-MM-DD, "
                    "from today through the previous seven days."
                )
            if normalized in {"no", "n", "nope", "done", "stop"}:
                if not self.repo.delete_conversation_state(
                    user_id, expected_revision=state.revision
                ):
                    return "That workflow changed. Please try again."
                return "Done — your meal was logged."
            return "Would you like to log another meal? Reply yes or no."

        draft = state.meal_draft or MealLogDraft()
        values = draft.model_dump(mode="json")
        for field in ("date", "meal_type", "description"):
            if field not in entities or entities[field] in (None, ""):
                continue
            values[field] = entities[field]
        try:
            if values.get("date") is not None:
                parsed_date = date.fromisoformat(str(values["date"]))
                earliest = date.today() - timedelta(days=7)
                if not earliest <= parsed_date <= date.today():
                    return (
                        "That date must be today or within the previous seven "
                        "days. What date was the meal?"
                    )
                values["date"] = parsed_date
            if values.get("meal_type") is not None:
                values["meal_type"] = MealType(
                    str(values["meal_type"]).strip().casefold()
                )
            if values.get("description") is not None:
                description = str(values["description"]).strip()
                if not description:
                    raise ValueError("description must not be empty")
                values["description"] = description
            new_draft = MealLogDraft.model_validate(values)
        except TypeError, ValueError, ValidationError:
            if "meal_type" in entities:
                return (
                    "I didn't recognize that meal type. Please use "
                    "breakfast, lunch, dinner, or snack."
                )
            if "description" in entities:
                return "Please provide a non-empty description of the meal."
            return "I couldn't understand that meal detail. Please try again."

        missing = (
            ("date", "What date was the meal? Use YYYY-MM-DD.")
            if new_draft.date is None
            else ("meal_type", "Was it breakfast, lunch, dinner, or snack?")
            if new_draft.meal_type is None
            else ("description", "What did you eat? Please describe the meal.")
            if new_draft.description is None
            else None
        )
        now = datetime.now(timezone.utc)
        if missing:
            next_state = state.model_copy(
                update={
                    "step": {
                        "date": ConversationWorkflowStep.AWAITING_DATE,
                        "meal_type": (
                            ConversationWorkflowStep.AWAITING_MEAL_TYPE
                        ),
                        "description": (
                            ConversationWorkflowStep.AWAITING_DESCRIPTION
                        ),
                    }[missing[0]],
                    "meal_draft": new_draft,
                    "revision": state.revision + 1,
                    "updated_at": now,
                }
            )
            if not self.repo.transition_conversation_state(
                user_id, next_state, expected_revision=state.revision
            ):
                return "That meal workflow changed. Please use /submit_meals."
            return missing[1]

        assert new_draft.date is not None
        assert new_draft.meal_type is not None
        assert new_draft.description is not None
        entry = MealLogEntry(
            date=new_draft.date,
            meal_type=new_draft.meal_type,
            description=new_draft.description,
            created_at=now,
        )
        next_state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
                "meal_draft": new_draft,
                "revision": state.revision + 1,
                "updated_at": now,
                "last_update_id": source_update_id,
            }
        )
        try:
            persisted = self.repo.log_meal_and_transition(
                user_id,
                entry,
                next_state,
                expected_revision=state.revision,
                source_update_id=source_update_id,
            )
        except Exception:
            logger.exception("Meal persistence failed for user %s", user_id)
            return "I couldn't save that meal. Please try again."
        if not persisted:
            return "That meal workflow changed. Please use /submit_meals."
        return (
            "Meal logged. Would you like to log another meal? Reply yes or no."
        )

    def _handle_plan_preference(
        self,
        chat_id: int | str,
        user_id: str,
        text: str,
        state: ConversationState,
        *,
        source_update_id: str | None,
    ) -> None:
        if source_update_id and state.last_update_id == source_update_id:
            if state.step is ConversationWorkflowStep.AWAITING_PREFERENCE:
                self.telegram_api.send_message(
                    chat_id,
                    "Please continue with your preference or try again.",
                )
                return
            elif state.step is ConversationWorkflowStep.GENERATING:
                self.telegram_api.send_message(
                    chat_id, "Working on your weekly meal plan."
                )
                return
        if state.step is not ConversationWorkflowStep.AWAITING_PREFERENCE:
            self.telegram_api.send_message(
                chat_id,
                "Plan generation is already in progress. Use /plan to retry "
                "if it fails.",
            )
            return
        normalized = text.strip().casefold().rstrip(".!?,;:")
        preference = (
            None
            if normalized
            in {
                "anything",
                "no preference",
                "no preferences",
                "none",
                "whatever",
            }
            else text.strip()
        )

        if preference is None:
            interpretation: tuple[list[PreferenceRequirement], str | None] = (
                [],
                None,
            )
        else:
            combined_preference = (
                f"{state.preference}; {preference}"
                if state.preference
                else preference
            )
            if state.preference and len(combined_preference) > 500:
                self.telegram_api.send_message(
                    chat_id,
                    "Your answer was not appended because the combined "
                    "preference exceeds 500 characters. Use /plan to reset, "
                    "then provide one complete preference of 500 characters "
                    "or fewer.",
                )
                return
            preference = combined_preference
            if len(preference) > 500:
                self.telegram_api.send_message(
                    chat_id,
                    "That preference is too long. Please keep it to 500 "
                    "characters or fewer.",
                )
                return
            try:
                client = self.llm_client or LLMClient()
                interpretation = parse_preference_interpretation(
                    client.chat_sync(
                        build_preference_interpretation_prompt(preference),
                        preference,
                    )
                )
            except Exception:
                logger.error("Plan preference interpretation failed")
                interpretation = (
                    [],
                    "I couldn't interpret that preference yet. Please try "
                    "again or rephrase it.",
                )

        requirements, clarification = interpretation
        next_step = (
            ConversationWorkflowStep.AWAITING_PREFERENCE
            if clarification
            else ConversationWorkflowStep.GENERATING
        )
        try:
            candidate = ConversationState.model_validate(
                {
                    **state.model_dump(),
                    "step": next_step,
                    "preference": preference,
                    "requirements": requirements,
                    "revision": state.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                    "last_update_id": source_update_id,
                }
            )
        except ValidationError:
            self.telegram_api.send_message(
                chat_id,
                "That preference is too long. Please keep it under 500 "
                "characters.",
            )
            return
        if not self.repo.transition_conversation_state(
            user_id, candidate, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id, "That plan request changed. Please use /plan again."
            )
            return
        if clarification:
            self.telegram_api.send_message(chat_id, clarification)
            return
        invoked = self._invoke_planner(
            user_id,
            chat_id,
            GENERATE_PLAN,
            week_start=date.today().isoformat(),
            preference=preference,
            requirements=requirements,
            request_id=candidate.request_id,
            state_revision=candidate.revision,
        )
        if not invoked:
            retry_state = candidate.model_copy(
                update={
                    "step": ConversationWorkflowStep.RETRY_READY,
                    "revision": candidate.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.repo.mark_conversation_retry_ready(
                user_id, retry_state, expected_revision=candidate.revision
            )
            self.telegram_api.send_message(
                chat_id,
                "I couldn't start plan generation. Your preference was saved; "
                "use /plan to retry.",
            )
            return
        self.telegram_api.send_message(
            chat_id, "Working on your weekly meal plan."
        )

    def _retry_plan_request(
        self, user_id: str, chat_id: int | str, state: ConversationState
    ) -> None:
        candidate = state.model_copy(
            update={
                "step": ConversationWorkflowStep.GENERATING,
                "revision": state.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        if not self.repo.transition_conversation_state(
            user_id, candidate, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id, "That plan request changed. Please try /plan again."
            )
            return
        if not self._invoke_planner(
            user_id,
            chat_id,
            GENERATE_PLAN,
            week_start=date.today().isoformat(),
            preference=candidate.preference,
            requirements=candidate.requirements,
            request_id=candidate.request_id,
            state_revision=candidate.revision,
        ):
            retry_state = candidate.model_copy(
                update={
                    "step": ConversationWorkflowStep.RETRY_READY,
                    "revision": candidate.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.repo.mark_conversation_retry_ready(
                user_id, retry_state, expected_revision=candidate.revision
            )
            self.telegram_api.send_message(
                chat_id,
                "I couldn't start plan generation. Please use /plan to retry.",
            )
            return
        self.telegram_api.send_message(
            chat_id, "Working on your weekly meal plan."
        )

    @staticmethod
    def _get_source_update_id(route: RouteResult) -> str | None:
        """Return a normalized Telegram update ID for conversational writes."""
        update_id = route.raw_update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            return str(update_id)
        return None

    def _apply_intent_metadata(
        self,
        user_id: str,
        chat_id: int | str,
        intent: ConversationIntent,
        entities: dict[str, Any],
        existing_profile: Optional[UserProfile],
        *,
        source_update_id: str | None = None,
    ) -> MutationResult:
        try:
            if intent is ConversationIntent.LOG_MEAL:
                entry = MealLogEntry(
                    date=entities["date"],
                    meal_type=entities["meal_type"],
                    description=entities["description"],
                    created_at=datetime.now(timezone.utc),
                )
                self.repo.log_meal(
                    user_id, entry, source_update_id=source_update_id
                )
                return MutationResult(True)
            if intent is ConversationIntent.UPDATE_PROFILE:
                return self._update_profile(user_id, entities, existing_profile)
            if intent is ConversationIntent.CONFIRM_PLAN:
                return self._confirm_plan(user_id, chat_id)
            if intent is ConversationIntent.EDIT_PLAN:
                return self._edit_plan(user_id, chat_id, entities)
            if intent is ConversationIntent.REVISE_PLAN:
                return self._start_plan_revision(
                    user_id,
                    chat_id,
                    entities,
                    source_update_id=source_update_id,
                )
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
        persisted_draft = self.repo.get_profile_draft(user_id)
        if isinstance(persisted_draft, ProfileUpdateEntities):
            data = persisted_draft.model_dump(mode="json")
        elif existing:
            data = existing.model_dump(mode="json")
        else:
            data = {}
        if (
            "people_count" in update.model_fields_set
            and "family_members" not in update.model_fields_set
            and update.people_count != data.get("people_count")
        ):
            data["family_members"] = None
        for field in update.model_fields_set:
            data[field] = getattr(update, field)
        draft = ProfileUpdateEntities.model_validate(data)
        if (
            "family_members" in update.model_fields_set
            and draft.family_members is not None
            and self._has_duplicate_member_identities(draft.family_members)
        ):
            return MutationResult(
                False,
                "Family member names must be unique, ignoring capitalization "
                "and surrounding spaces.",
            )
        required = (
            "name",
            "people_count",
            "family_members",
            "dietary_constraints",
            "dietary_preferences",
            "goals",
        )
        missing = [field for field in required if getattr(draft, field) is None]
        if missing:
            missing_labels = {
                "name": "family name",
                "people_count": "household size",
                "family_members": (
                    "each household member's name and calorie target"
                ),
                "dietary_constraints": "dietary constraints",
                "dietary_preferences": "dietary preferences",
                "goals": "goals",
            }
            self.repo.save_profile_draft(user_id, draft)
            return MutationResult(
                True,
                "I still need: "
                + ", ".join(missing_labels[field] for field in missing)
                + ".",
            )
        if (
            draft.family_members is None
            or len(draft.family_members) != draft.people_count
        ):
            self.repo.save_profile_draft(user_id, draft)
            return MutationResult(
                True,
                "Please provide one household member name and calorie "
                "target for each person.",
            )
        profile = UserProfile.model_validate(draft.model_dump())
        self.repo.save_profile(user_id, profile)
        self.repo.delete_profile_draft(user_id)
        return MutationResult(True, "Your profile has been saved.")

    @staticmethod
    def _is_eligible_draft(plan: WeeklyPlan | None) -> bool:
        return bool(
            plan
            and plan.status is PlanStatus.DRAFT
            and plan.week_end >= date.today()
        )

    @staticmethod
    def _is_active_confirmed_plan(plan: WeeklyPlan | None) -> bool:
        today = date.today()
        return bool(
            plan
            and plan.status is PlanStatus.CONFIRMED
            and plan.week_start <= today <= plan.week_end
        )

    def _confirm_plan(self, user_id: str, chat_id: int | str) -> MutationResult:
        latest_plan = self.repo.get_latest_plan(user_id)
        plan = latest_plan if self._is_eligible_draft(latest_plan) else None
        if plan is None:
            active_plan = self.repo.get_active_plan(user_id)
            if self._is_active_confirmed_plan(active_plan):
                plan = active_plan
        if not plan:
            if latest_plan and latest_plan.status is PlanStatus.DRAFT:
                return MutationResult(
                    False,
                    "That draft has expired and cannot be confirmed.",
                )
            return MutationResult(False, "There is no current plan to confirm.")
        if plan.status is PlanStatus.DRAFT:
            transitioned = self.repo.confirm_plan(
                user_id, plan.week_start_date, plan.revision
            )
            response = "Plan confirmed. Groceries are being prepared."
        elif (
            plan.status is PlanStatus.CONFIRMED
            and plan.grocery_status is GroceryStatus.ERROR
        ):
            transitioned = self.repo.retry_grocery(
                user_id, plan.week_start_date, plan.revision
            )
            response = "Retrying grocery generation for your confirmed plan."
        else:
            return MutationResult(
                False,
                "Only a draft or a failed grocery request can be confirmed "
                "again.",
            )
        if not transitioned:
            return MutationResult(
                False,
                "That plan changed while I was saving it. Please try again.",
            )
        plan.status = PlanStatus.CONFIRMED
        plan.grocery_status = GroceryStatus.PENDING
        plan.grocery_list = []
        if not self._invoke_planner(
            user_id,
            chat_id,
            FINALIZE_GROCERY,
            week_start=plan.week_start_date,
        ):
            self.repo.fail_grocery(user_id, plan.week_start_date, plan.revision)
            return MutationResult(
                False,
                "The plan is saved, but grocery generation failed to "
                "start. Please confirm again to retry.",
            )
        return MutationResult(True, response)

    def _edit_plan(
        self, user_id: str, chat_id: int | str, entities: dict[str, Any]
    ) -> MutationResult:
        latest_plan = self.repo.get_latest_plan(user_id)
        plan = (
            latest_plan
            if self._is_eligible_draft(latest_plan)
            else self.repo.get_active_plan(user_id)
        )
        if not self._is_eligible_draft(
            plan
        ) and not self._is_active_confirmed_plan(plan):
            if latest_plan and latest_plan.status is PlanStatus.DRAFT:
                return MutationResult(
                    False, "That draft has expired and cannot be edited."
                )
            if latest_plan and latest_plan.status is PlanStatus.CONFIRMED:
                return MutationResult(
                    False,
                    "That confirmed plan is inactive and cannot be edited.",
                )
            return MutationResult(False, "There is no plan to edit.")
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
        refresh = plan.status is PlanStatus.CONFIRMED
        if not self.repo.update_meal(
            user_id,
            plan.week_start_date,
            day_number,
            meal_type.value,
            updated,
            plan.revision,
            expected_status=plan.status,
        ):
            return MutationResult(
                False, "That plan changed while I was saving it. Please retry."
            )
        plan_day.meals[plan_day.meals.index(meal)] = updated
        plan.revision += 1
        if refresh:
            plan.grocery_status = GroceryStatus.PENDING
            plan.grocery_list = []
        if refresh and not self._invoke_planner(
            user_id,
            chat_id,
            FINALIZE_GROCERY,
            week_start=plan.week_start_date,
        ):
            self.repo.fail_grocery(user_id, plan.week_start_date, plan.revision)
            return MutationResult(
                False, "The meal changed, but grocery refresh failed."
            )
        return MutationResult(True, "The meal plan was updated.")

    def _handle_plan_revision_state(
        self,
        chat_id: int | str,
        user_id: str,
        text: str,
        state: ConversationState,
    ) -> None:
        """Route messages while a whole-draft revision owns the workflow."""
        if state.step is ConversationWorkflowStep.GENERATING:
            self.telegram_api.send_message(
                chat_id,
                "Your draft revision is still being generated. Please wait "
                "before confirming or requesting another amendment.",
            )
            return
        if text.strip().casefold() == "retry":
            self._retry_plan_revision(user_id, chat_id, state)
            return
        self.telegram_api.send_message(
            chat_id,
            "The draft revision failed, but your original draft is unchanged. "
            "Reply retry to try again or use /cancel.",
        )

    def _start_plan_revision(
        self,
        user_id: str,
        chat_id: int | str,
        entities: dict[str, Any],
        *,
        source_update_id: str | None = None,
    ) -> MutationResult:
        """Start one asynchronous replacement of the eligible draft."""
        if (
            source_update_id is not None
            and self.repo.has_plan_revision_update_marker(
                user_id, source_update_id
            )
        ):
            return MutationResult(True, "I'm revising your draft now.")
        amendment = entities.get("amendment")
        if not isinstance(amendment, str) or not amendment.strip():
            return MutationResult(
                False, "Please describe the desired plan change again."
            )
        if len(amendment.strip()) > 500:
            return MutationResult(
                False,
                "Please describe the desired plan change in under 500 "
                "characters.",
            )
        plan = self.repo.get_latest_plan(user_id)
        if not self._is_eligible_draft(plan):
            return MutationResult(
                False, "There is no current draft to revise. Use /plan first."
            )
        assert plan is not None
        if len(plan.planning_instructions) >= 20:
            return MutationResult(
                False,
                "This draft has reached its limit for saved amendments. "
                "Use /plan to create a fresh draft.",
            )
        now = datetime.now(timezone.utc)
        state = ConversationState(
            workflow_kind=ConversationWorkflowKind.PLAN_REVISION,
            step=ConversationWorkflowStep.GENERATING,
            amendment=amendment.strip(),
            target_week=plan.week_start,
            expected_plan_revision=plan.revision,
            request_id=str(uuid4()),
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
            last_update_id=source_update_id,
        )
        if source_update_id is None:
            started = self.repo.save_conversation_state(user_id, state)
            duplicate = False
        else:
            started = self.repo.start_plan_revision(
                user_id, state, source_update_id=source_update_id
            )
            duplicate = (
                not started
                and self.repo.has_plan_revision_update_marker(
                    user_id, source_update_id
                )
            )
        if duplicate:
            return MutationResult(True, "I'm revising your draft now.")
        if not started:
            return MutationResult(
                False,
                "A draft revision is already in progress. Please wait for "
                "it to finish.",
            )
        if not self._invoke_planner(
            user_id,
            chat_id,
            REVISE_PLAN,
            week_start=plan.week_start_date,
            amendment=state.amendment,
            request_id=state.request_id,
            state_revision=state.revision,
            expected_plan_revision=plan.revision,
        ):
            retry_state = state.model_copy(
                update={
                    "step": ConversationWorkflowStep.RETRY_READY,
                    "revision": state.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.repo.mark_conversation_retry_ready(
                user_id, retry_state, expected_revision=state.revision
            )
            return MutationResult(
                False,
                "I couldn't start the revision. Your original draft is "
                "unchanged; reply retry or use /cancel.",
            )
        return MutationResult(True, "I'm revising your draft now.")

    def _retry_plan_revision(
        self, user_id: str, chat_id: int | str, state: ConversationState
    ) -> None:
        """Retry a failed revision using its persisted request snapshot."""
        candidate = state.model_copy(
            update={
                "step": ConversationWorkflowStep.GENERATING,
                "revision": state.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        if not self.repo.transition_conversation_state(
            user_id, candidate, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id, "That revision changed. Please use /plan again."
            )
            return
        assert candidate.target_week is not None
        if not self._invoke_planner(
            user_id,
            chat_id,
            REVISE_PLAN,
            week_start=candidate.target_week.isoformat(),
            amendment=candidate.amendment,
            request_id=candidate.request_id,
            state_revision=candidate.revision,
            expected_plan_revision=candidate.expected_plan_revision,
        ):
            retry_state = candidate.model_copy(
                update={
                    "step": ConversationWorkflowStep.RETRY_READY,
                    "revision": candidate.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.repo.mark_conversation_retry_ready(
                user_id, retry_state, expected_revision=candidate.revision
            )
            self.telegram_api.send_message(
                chat_id,
                "I couldn't start the revision. Your original draft is "
                "unchanged; reply retry or use /cancel.",
            )
            return
        self.telegram_api.send_message(chat_id, "I'm revising your draft now.")

    def _invoke_planner(
        self,
        user_id: str,
        chat_id: int | str,
        action: str,
        *,
        week_start: str,
        preference: str | None = None,
        requirements: list[PreferenceRequirement] | None = None,
        attempt: int = 1,
        repair_feedback: str | None = None,
        amendment: str | None = None,
        request_id: str | None = None,
        state_revision: int | None = None,
        expected_plan_revision: int | None = None,
    ) -> bool:
        if not self.lambda_client or not self.planner_function_name:
            return False
        try:
            payload: dict[str, Any] = {
                "action": action,
                "user_id": user_id,
                "chat_id": chat_id,
                "week_start": week_start,
            }
            if action == GENERATE_PLAN:
                payload.update(
                    {
                        "preference": preference,
                        "requirements": [
                            requirement.model_dump(mode="json")
                            for requirement in (requirements or [])
                        ],
                        "attempt": attempt,
                        "repair_feedback": repair_feedback,
                        "request_id": request_id,
                        "state_revision": state_revision,
                    }
                )
            elif action == REVISE_PLAN:
                payload.update(
                    {
                        "amendment": amendment,
                        "request_id": request_id,
                        "state_revision": state_revision,
                        "expected_plan_revision": expected_plan_revision,
                    }
                )
            self.lambda_client.invoke(
                FunctionName=self.planner_function_name,
                InvocationType="Event",
                Payload=json.dumps(payload),
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
    try:
        settings = get_settings()
    except BotConfigurationError:
        logger.error(
            "Ignored Telegram update because Bot configuration is invalid"
        )
        return {"statusCode": 200, "body": "ok"}
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    repo = DynamoRepository(dynamodb.Table(settings.dynamodb_table_name))
    telegram_api = TelegramAPI(
        settings.telegram_bot_token,
        request_timeout=settings.bot_telegram_request_timeout_seconds,
    )
    llm_client = LLMClient(
        model=settings.conversational_llm_model,
        api_key=settings.llm_api_key,
        reasoning_effort=settings.conversational_llm_reasoning_effort,
        max_retries=settings.bot_llm_max_retries,
        initial_backoff=settings.bot_llm_initial_backoff_seconds,
        request_timeout=settings.bot_llm_request_timeout_seconds,
    )
    handler = BotHandler(
        repo,
        telegram_api,
        lambda_client=boto3.client("lambda", region_name=settings.aws_region),
        planner_function_name=os.getenv(
            "PLANNER_FUNCTION_NAME", "meal-planner-planner"
        ),
        llm_client=llm_client,
        access_policy=TelegramAccessPolicy(settings.telegram_allowed_user_ids),
    )
    return handler.handle_update(update)
