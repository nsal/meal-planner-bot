"""AWS Lambda entry point for Telegram webhook commands and routing."""

import base64
import hmac
import json
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import (
    BotConfigurationError,
    get_settings,
    get_webhook_secret,
)
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.dietary_rules import (
    expand_constraint_entry,
    has_constraint_conflict,
    resolve_priority_rules,
    validate_constraints,
)
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
    MAX_PLAN_REQUIREMENTS,
    ConstraintEntry,
    ConversationIntent,
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    DietaryPreferenceEntry,
    DietaryRule,
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
    RuleOperator,
    UserProfile,
    WeeklyPlan,
    canonicalize_constraint_entry,
    canonicalize_dietary_rule,
    canonicalize_profile_rule_ids,
)
from meal_planner.planner_handler import (
    FINALIZE_GROCERY,
    GENERATE_PLAN,
    REVISE_PLAN,
)
from meal_planner.router import (
    MealCallback,
    ProfileCallback,
    ProfileCallbackAction,
    RouteResult,
    RouteType,
    parse_checkin_callback,
    parse_meal_callback,
    parse_meal_input,
    parse_profile_callback,
    route_update,
)
from meal_planner.telegram.access import TelegramAccessPolicy
from meal_planner.telegram.api import TelegramAPI, TelegramAPIError
from meal_planner.telegram.commands import render_help

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"
MEAL_INPUT_PROMPT = (
    "Submit one meal using this format:\n"
    "when, meal type, what you ate\n\n"
    "Use today or yesterday, or a strict YYYY-MM-DD date. Dates are "
    "interpreted in UTC and must be within the last seven calendar days, "
    "including today. Meal type must be breakfast, lunch, snack, or "
    "dinner. Keep the description after the second comma.\n\n"
    "Example: today, lunch, vegetable soup"
)

_COMMAND_REFERENCE_DATE: ContextVar[date | None] = ContextVar(
    "command_reference_date", default=None
)

_PROFILE_PENDING_PREFIX = "profile-pending:"
_MAX_PROFILE_PENDING_BYTES = 4_000


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
            token = _COMMAND_REFERENCE_DATE.set(
                self._reference_date_from_route(route)
            )
            try:
                handler(route.chat_id, route.user_id)
            finally:
                _COMMAND_REFERENCE_DATE.reset(token)
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
                "household size, and each household member's name and "
                "required calorie target. You may also provide optional "
                "protein and fibre targets in grams/day for each member; "
                "these are not required to complete setup or generate a "
                "plan. Tell me your dietary constraints, dietary "
                "preferences. "
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
        reference_date = (
            _COMMAND_REFERENCE_DATE.get() or datetime.now(timezone.utc).date()
        )
        state = self._get_conversation_state(user_id)
        meal_state = self._new_submission_state()
        if not self._replace_conversation_state(user_id, meal_state, state):
            self.telegram_api.send_message(
                chat_id,
                "That workflow changed while I was starting meal logging. "
                "Please try again.",
            )
            return
        history = self.repo.get_meal_history(
            user_id, days=2, on_date=reference_date
        )
        prefix = "I replaced your unfinished workflow.\n\n" if state else ""
        self.telegram_api.send_message(
            chat_id,
            prefix + self._format_recent_meal_history(history, reference_date),
        )
        self.telegram_api.send_message(chat_id, MEAL_INPUT_PROMPT)

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
    def _reference_date_from_route(route: RouteResult) -> date:
        """Derive a UTC command date, falling back to processing time."""
        message = route.raw_update.get("message")
        timestamp = message.get("date") if isinstance(message, dict) else None
        if isinstance(timestamp, int) and not isinstance(timestamp, bool):
            try:
                return datetime.fromtimestamp(timestamp, timezone.utc).date()
            except OSError, OverflowError, TypeError, ValueError:
                pass
        return datetime.now(timezone.utc).date()

    @staticmethod
    def _format_recent_meal_history(
        history: list[MealLogEntry], reference_date: date
    ) -> str:
        """Render the two calendar groups shown when meal logging starts."""
        ordered = sorted(
            history,
            key=lambda entry: (
                entry.date,
                (
                    entry.created_at.astimezone(timezone.utc)
                    if entry.created_at.tzinfo is not None
                    and entry.created_at.utcoffset() is not None
                    else entry.created_at.replace(tzinfo=timezone.utc)
                ),
            ),
            reverse=True,
        )
        groups = {
            "Today": [
                entry for entry in ordered if entry.date == reference_date
            ],
            "Yesterday": [
                entry
                for entry in ordered
                if entry.date == reference_date - timedelta(days=1)
            ],
        }
        lines = ["Recent meals", ""]
        for index, (label, entries) in enumerate(groups.items()):
            lines.append(label)
            if entries:
                lines.extend(
                    f"- {entry.meal_type.value.capitalize()}: "
                    f"{entry.description}"
                    for entry in entries
                )
            else:
                lines.append("No meals submitted.")
            if index == 0:
                lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _new_submission_state() -> ConversationState:
        """Create an empty, request-identifiable meal input state."""
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.MEAL_LOG,
            step=ConversationWorkflowStep.AWAITING_MEAL_INPUT,
            meal_draft=MealLogDraft(),
            request_id=str(uuid4()),
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
        )

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

    @staticmethod
    def _encode_pending_profile_rules(
        mode: str,
        source_text: str,
        rules: list[ConstraintEntry | DietaryRule],
    ) -> tuple[str, str]:
        """Serialize one bounded interpretation for durable confirmation."""
        token = uuid4().hex[:12]
        payload = {
            "mode": mode,
            "source_text": source_text,
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "token": token,
        }
        encoded = _PROFILE_PENDING_PREFIX + json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        )
        if len(encoded.encode("utf-8")) > _MAX_PROFILE_PENDING_BYTES:
            raise ValueError("profile interpretation is too large")
        return encoded, token

    @staticmethod
    def _decode_pending_profile_rules(
        value: str | None,
    ) -> tuple[str, str, list[ConstraintEntry | DietaryRule], str] | None:
        """Validate a serialized pending interpretation before use."""
        if value is None or not value.startswith(_PROFILE_PENDING_PREFIX):
            return None
        if len(value.encode("utf-8")) > _MAX_PROFILE_PENDING_BYTES:
            return None
        try:
            payload = json.loads(value[len(_PROFILE_PENDING_PREFIX) :])
        except TypeError, ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        mode = payload.get("mode")
        source_text = payload.get("source_text")
        token = payload.get("token")
        raw_rules = payload.get("rules")
        if (
            mode not in {"constraint", "stored_preference"}
            or not isinstance(source_text, str)
            or not source_text
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", token)
            or not isinstance(raw_rules, list)
            or not 1 <= len(raw_rules) <= 20
        ):
            return None
        rules: list[ConstraintEntry | DietaryRule] = []
        try:
            for raw_rule in raw_rules:
                if not isinstance(raw_rule, dict):
                    return None
                rules.append(
                    ConstraintEntry.model_validate(raw_rule)
                    if mode == "constraint"
                    else DietaryRule.model_validate(raw_rule)
                )
        except ValidationError:
            return None
        return mode, source_text, rules, token

    @staticmethod
    def _profile_menu_state(state: ConversationState) -> ConversationState:
        """Return the next menu state while consuming pending input."""
        now = datetime.now(timezone.utc)
        return state.model_copy(
            update={
                "step": ConversationWorkflowStep.PROFILE_MENU,
                "profile_category": None,
                "profile_operation": None,
                "last_update_id": None,
                "revision": state.revision + 1,
                "updated_at": now,
            }
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
            if route.callback_data.startswith("meal:"):
                meal_callback = parse_meal_callback(route.callback_data)
                if meal_callback is None:
                    acknowledgement = "Invalid meal action"
                    self._send_meal_message(
                        route.chat_id,
                        "That meal button is invalid or outdated.",
                    )
                    return
                acknowledgement = self._handle_meal_callback(
                    route, meal_callback
                )
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

    def _handle_profile_rule_callback(
        self,
        route: RouteResult,
        callback: ProfileCallback,
        state: ConversationState,
    ) -> None:
        """Confirm or cancel a pending interpreted dietary rule."""
        assert route.chat_id is not None
        assert route.user_id is not None
        assert callback.token is not None
        pending = self._decode_pending_profile_rules(state.last_update_id)
        if pending is None or pending[3] != callback.token:
            self.telegram_api.send_message(
                route.chat_id,
                "That profile confirmation is stale or outdated. Nothing was "
                "changed.",
            )
            return
        if callback.action is ProfileCallbackAction.CANCEL:
            next_state = self._profile_menu_state(state)
            if not self.repo.transition_conversation_state(
                route.user_id, next_state, expected_revision=state.revision
            ):
                self.telegram_api.send_message(
                    route.chat_id,
                    "That profile confirmation changed. Please try again.",
                )
                return
            self.telegram_api.send_message(
                route.chat_id, "Cancelled the pending profile change."
            )
            self.telegram_api.send_profile_root(route.chat_id)
            return

        profile = self.repo.get_profile(route.user_id, consistent_read=True)
        if profile is None:
            self.telegram_api.send_message(
                route.chat_id, "No complete profile found. Use /start to begin."
            )
            return
        mode, _, rules, _ = pending
        removed_sources: list[str] = []
        if mode == "constraint":
            constraints = list(profile.dietary_constraints)
            constraints.extend(
                rule for rule in rules if isinstance(rule, ConstraintEntry)
            )
            for preference in profile.dietary_preferences:
                if preference.rule is not None and any(
                    has_constraint_conflict(preference.rule, [constraint])
                    for constraint in constraints
                ):
                    removed_sources.append(preference.source_text)
            updated = self._profile_with_updates(
                profile, {"dietary_constraints": constraints}
            )
        else:
            preferences = list(profile.dietary_preferences)
            for rule in rules:
                if not isinstance(rule, DietaryRule):
                    continue
                if has_constraint_conflict(rule, profile.dietary_constraints):
                    next_state = self._profile_menu_state(state)
                    if self.repo.transition_conversation_state(
                        route.user_id,
                        next_state,
                        expected_revision=state.revision,
                    ):
                        self.telegram_api.send_message(
                            route.chat_id,
                            "That preference conflicts with a dietary "
                            "constraint and was not saved.",
                        )
                    else:
                        self.telegram_api.send_message(
                            route.chat_id,
                            "That profile confirmation changed. Please try "
                            "again.",
                        )
                    return
                preferences.append(
                    DietaryPreferenceEntry(
                        id=rule.id,
                        source_text=rule.source_text,
                        rule=rule,
                    )
                )
            updated = self._profile_with_updates(
                profile, {"dietary_preferences": preferences}
            )
        updated = canonicalize_profile_rule_ids(updated)
        next_state = self._profile_menu_state(state)
        try:
            committed = self.repo.save_profile_and_transition_state(
                route.user_id, updated, next_state, state
            )
        except Exception:
            logger.exception("Profile rule confirmation failed")
            committed = False
        if not committed:
            self.repo.get_profile(route.user_id, consistent_read=True)
            self.telegram_api.send_message(
                route.chat_id,
                "That profile confirmation is stale. Please open /profile "
                "again.",
            )
            return
        message = "Profile change saved."
        if removed_sources:
            message += (
                " Removed conflicting preferences: "
                + ", ".join(removed_sources)
                + "."
            )
        self.telegram_api.send_message(route.chat_id, message)
        self.telegram_api.send_profile_root(route.chat_id)

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

            if callback.action in {
                ProfileCallbackAction.CONFIRM,
                ProfileCallbackAction.CANCEL,
            }:
                self._handle_profile_rule_callback(route, callback, state)
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

    def _handle_meal_callback(
        self, route: RouteResult, callback: MealCallback
    ) -> str:
        """Apply one revision- and submission-checked meal action."""
        if route.chat_id is None or route.user_id is None:
            return "Invalid meal action"

        try:
            state = self._get_conversation_state(route.user_id)
            if not self._meal_callback_matches(state, callback.submission_id):
                self._send_stale_meal_action(route.chat_id)
                return "Stale meal action"
            assert state is not None

            if callback.action.value == "confirm":
                return self._confirm_meal_callback(
                    route.chat_id, route.user_id, state, callback.submission_id
                )
            if callback.action.value == "cancel":
                return self._cancel_meal_callback(
                    route.chat_id,
                    route.user_id,
                    state,
                    callback.submission_id,
                )
            if callback.action.value == "add":
                return self._add_meal_callback(
                    route.chat_id,
                    route.user_id,
                    state,
                    callback.submission_id,
                )
            if callback.action.value == "done":
                return self._done_meal_callback(
                    route.chat_id,
                    route.user_id,
                    state,
                    callback.submission_id,
                )
        except Exception:
            logger.exception("Error handling meal callback")
            self._send_meal_message(
                route.chat_id,
                "Sorry, I couldn't update that meal. Please try again.",
            )
            return "Unable to update meal"
        return "Invalid meal action"

    @staticmethod
    def _meal_callback_matches(
        state: ConversationState | None, submission_id: str
    ) -> bool:
        return (
            state is not None
            and state.workflow_kind is ConversationWorkflowKind.MEAL_LOG
            and state.request_id == submission_id
        )

    def _confirm_meal_callback(
        self,
        chat_id: int | str,
        user_id: str,
        state: ConversationState,
        submission_id: str,
    ) -> str:
        """Atomically save a reviewed meal and show continuation actions."""
        if (
            state.step
            is not ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
        ):
            if (
                state.step
                is ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
            ):
                self._send_saved_meal_continuation(
                    chat_id, state, submission_id
                )
                return "Already saved"
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        draft = state.meal_draft
        if draft is None or not draft.date or not draft.meal_type:
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        if not draft.description:
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"

        now = datetime.now(timezone.utc)
        entry = MealLogEntry(
            date=draft.date,
            meal_type=draft.meal_type,
            description=draft.description,
            created_at=now,
        )
        continuation = state.model_copy(
            update={
                "step": ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION,
                "revision": state.revision + 1,
                "updated_at": now,
            }
        )
        try:
            confirmed = self.repo.confirm_meal_and_transition(
                user_id,
                entry,
                continuation,
                expected_revision=state.revision,
                submission_id=submission_id,
            )
        except Exception:
            logger.exception("Meal confirmation persistence failed")
            self._send_meal_message(
                chat_id,
                "I couldn't save that meal. Your review is still available; "
                "please try again.",
            )
            return "Unable to save meal"

        if confirmed:
            try:
                self.telegram_api.send_meal_saved(
                    chat_id, entry.description, submission_id
                )
            except TelegramAPIError:
                logger.error("Meal saved delivery failed")
            return "Meal saved"

        current = self._get_conversation_state(user_id)
        if (
            current is not None
            and current.workflow_kind is ConversationWorkflowKind.MEAL_LOG
            and current.step
            is ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
            and current.request_id == submission_id
        ):
            self._send_saved_meal_continuation(chat_id, current, submission_id)
            return "Already saved"
        self._send_stale_meal_action(chat_id)
        return "Stale meal action"

    def _send_saved_meal_continuation(
        self,
        chat_id: int | str,
        state: ConversationState,
        submission_id: str,
    ) -> None:
        """Retry saved-meal delivery with its continuation controls."""
        if state.meal_draft is None or state.meal_draft.description is None:
            self._send_stale_meal_action(chat_id)
            return
        try:
            self.telegram_api.send_meal_saved(
                chat_id, state.meal_draft.description, submission_id
            )
        except TelegramAPIError:
            logger.error("Already-saved meal delivery failed")

    def _cancel_meal_callback(
        self,
        chat_id: int | str,
        user_id: str,
        state: ConversationState,
        submission_id: str,
    ) -> str:
        """Discard an unconfirmed meal only at its observed revision."""
        if (
            state.step
            is not ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
        ):
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        try:
            deleted = self.repo.delete_conversation_state(
                user_id,
                expected_revision=state.revision,
                expected_request_id=submission_id,
                expected_step=(
                    ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION
                ),
            )
        except Exception:
            logger.exception("Meal cancellation persistence failed")
            self._send_meal_message(
                chat_id,
                "I couldn't cancel that meal. Please try again.",
            )
            return "Unable to cancel meal"
        if not deleted:
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        self._send_meal_message(chat_id, "Cancelled. This meal was not saved.")
        return "Meal cancelled"

    def _add_meal_callback(
        self,
        chat_id: int | str,
        user_id: str,
        state: ConversationState,
        submission_id: str,
    ) -> str:
        """Start another empty submission after a confirmed meal."""
        if (
            state.step
            is not ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
        ):
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        next_state = self._new_submission_state().model_copy(
            update={
                "revision": state.revision + 1,
            }
        )
        try:
            transitioned = self.repo.transition_conversation_state(
                user_id,
                next_state,
                expected_revision=state.revision,
                expected_request_id=submission_id,
                expected_step=(
                    ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
                ),
            )
        except Exception:
            logger.exception("Starting another meal persistence failed")
            self._send_meal_message(
                chat_id,
                "I couldn't start another meal. Please try again.",
            )
            return "Unable to add meal"
        if not transitioned:
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        self._send_meal_message(chat_id, MEAL_INPUT_PROMPT)
        return "Add more"

    def _done_meal_callback(
        self,
        chat_id: int | str,
        user_id: str,
        state: ConversationState,
        submission_id: str,
    ) -> str:
        """Finish meal logging after the current meal has been saved."""
        if (
            state.step
            is not ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
        ):
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        try:
            deleted = self.repo.delete_conversation_state(
                user_id,
                expected_revision=state.revision,
                expected_request_id=submission_id,
                expected_step=(
                    ConversationWorkflowStep.AWAITING_MEAL_CONTINUATION
                ),
            )
        except Exception:
            logger.exception("Completing meal workflow persistence failed")
            self._send_meal_message(
                chat_id,
                "I couldn't finish meal logging. Please try again.",
            )
            return "Unable to finish meal logging"
        if not deleted:
            self._send_stale_meal_action(chat_id)
            return "Stale meal action"
        self._send_meal_message(chat_id, "Done. Meal logging is complete.")
        return "Meal logging complete"

    def _send_stale_meal_action(self, chat_id: int | str) -> None:
        """Tell the user that a meal button no longer applies."""
        self._send_meal_message(
            chat_id,
            "That meal action is stale or outdated. Nothing was changed.",
        )

    def _send_meal_message(self, chat_id: int | str, text: str) -> None:
        """Deliver a response without losing its callback acknowledgement."""
        try:
            self.telegram_api.send_message(chat_id, text)
        except TelegramAPIError:
            logger.error("Meal callback delivery failed")

    def handle_conversational(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id or not route.text:
            return
        try:
            source_update_id = self._get_source_update_id(route)
            state = self._get_conversation_state(route.user_id)
            if (
                state
                and state.workflow_kind is ConversationWorkflowKind.MEAL_LOG
            ):
                if state.step is ConversationWorkflowStep.AWAITING_MEAL_INPUT:
                    self._handle_structured_meal_input(
                        route.chat_id,
                        route.user_id,
                        route.text,
                        route,
                        state,
                    )
                elif state.step in {
                    ConversationWorkflowStep.AWAITING_DATE,
                    ConversationWorkflowStep.AWAITING_MEAL_TYPE,
                    ConversationWorkflowStep.AWAITING_DESCRIPTION,
                    ConversationWorkflowStep.AWAITING_ANOTHER_MEAL,
                }:
                    self._restart_legacy_meal_workflow(
                        route.chat_id, route.user_id, state
                    )
                return
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
    ) -> tuple[str, int, int | None, int | None] | None:
        """Parse a member name followed by two or four target fields."""
        if "\n" in text or "\r" in text:
            return None
        parts = text.strip().split()
        if len(parts) < 2:
            return None
        if len(parts) >= 4 and all(part.isdecimal() for part in parts[-3:]):
            name_parts = parts[:-3]
            calorie_text, protein_text, fibre_text = parts[-3:]
            if not name_parts or any(part.isdecimal() for part in name_parts):
                return None
            name = " ".join(name_parts)
            calories = int(calorie_text)
            protein = int(protein_text)
            fibre = int(fibre_text)
            if (
                not 1 <= calories <= 10_000
                or not 1 <= protein <= 1_000
                or not 1 <= fibre <= 1_000
            ):
                return None
            return name, calories, protein, fibre
        if not parts[-1].isdecimal():
            return None
        name_parts = parts[:-1]
        if not name_parts or any(part.isdecimal() for part in name_parts):
            return None
        name = " ".join(name_parts)
        calories = int(parts[-1])
        if not 1 <= calories <= 10_000:
            return None
        return name, calories, None, None

    @staticmethod
    def _parse_profile_target_change(
        text: str,
    ) -> tuple[str, int | None] | None:
        """Parse a member name followed by one nutrient target or ``none``."""
        if "\n" in text or "\r" in text:
            return None
        match = re.fullmatch(r"(.+?)\s+(none|[0-9]+)", text.strip(), re.I)
        if match is None:
            return None
        name = match.group(1).strip()
        target_text = match.group(2)
        if not name or any(part.isdecimal() for part in name.split()):
            return None
        if target_text.casefold() == "none":
            return name, None
        target = int(target_text)
        if not 1 <= target <= 1_000:
            return None
        return name, target

    @staticmethod
    def _parse_profile_member_suffix(
        text: str,
        members: list[FamilyMember],
        *,
        allow_none: bool,
        maximum: int,
    ) -> tuple[str, int | None] | None:
        """Parse a final target suffix for an existing member edit.

        Unlike the add-member parser, this parser permits numeric name tokens.
        A stored identity prefix is used to reject extra suffix fields before
        the caller resolves the complete parsed name.
        """
        if "\n" in text or "\r" in text:
            return None
        stripped = text.strip()
        try:
            name, target_text = stripped.rsplit(maxsplit=1)
        except ValueError:
            return None
        target: int | None
        if target_text.casefold() == "none":
            if not allow_none:
                return None
            target = None
        elif target_text.isdecimal():
            target = int(target_text)
            if not 1 <= target <= maximum:
                return None
        else:
            return None

        name_identity = BotHandler._member_identity(name)
        if not name_identity:
            return None
        if any(
            name_identity == BotHandler._member_identity(member.name)
            for member in members
        ):
            return name, target
        for member in members:
            stored_name = member.name.strip()
            if name.casefold().startswith(stored_name.casefold() + " "):
                return None
        return name, target

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
    ) -> list[Any]:
        """Return a copied list for a non-family profile category."""
        if category is ProfileEditCategory.DIETARY_CONSTRAINTS:
            return list(profile.dietary_constraints)
        if category is ProfileEditCategory.DIETARY_PREFERENCES:
            return list(profile.dietary_preferences)
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

        if self._decode_pending_profile_rules(state.last_update_id) is not None:
            return "Please confirm or cancel the pending profile change first."

        if operation is ProfileEditOperation.ADD and category in {
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            ProfileEditCategory.DIETARY_PREFERENCES,
        }:
            if self._parse_profile_item(text) is None:
                return "Send one non-empty item."
            mode: Literal["constraint", "stored_preference"] = (
                "constraint"
                if category is ProfileEditCategory.DIETARY_CONSTRAINTS
                else "stored_preference"
            )
            try:
                client = self.llm_client or LLMClient()
                interpretation = parse_preference_interpretation(
                    client.chat_sync(
                        build_preference_interpretation_prompt(text, mode=mode),
                        text,
                    ),
                    mode=mode,
                )
            except Exception:
                logger.error("Profile rule interpretation failed")
                interpretation = (
                    [],
                    "I couldn't safely interpret that profile change. "
                    "Please rephrase it with specific foods.",
                )
            rules, clarification = interpretation
            if clarification:
                return clarification
            if not rules:
                return "Please provide a specific dietary rule."
            typed_rules = [
                rule
                for rule in rules
                if isinstance(rule, (ConstraintEntry, DietaryRule))
            ]
            if len(typed_rules) != len(rules):
                return "I couldn't safely interpret that profile change."
            pending_rules: list[ConstraintEntry | DietaryRule] = typed_rules
            if mode == "constraint":
                canonical_constraints: list[ConstraintEntry] = []
                for rule in typed_rules:
                    if not isinstance(rule, ConstraintEntry):
                        return (
                            "I couldn't safely interpret that profile change."
                        )
                    expansion = expand_constraint_entry(rule)
                    if not expansion.is_safe:
                        return (
                            "I couldn't safely interpret that constraint. "
                            "Please rephrase it with specific foods."
                        )
                    canonical_constraints.append(
                        canonicalize_constraint_entry(
                            rule.model_copy(
                                update={
                                    "forbidden_terms": list(expansion.terms),
                                    "uninterpretable": False,
                                }
                            ),
                            namespace="profile-constraint",
                        )
                    )
                pending_rules = list(canonical_constraints)
            else:
                pending_rules = [
                    canonicalize_dietary_rule(
                        rule, namespace="profile-preference"
                    )
                    for rule in typed_rules
                    if isinstance(rule, DietaryRule)
                ]
            try:
                pending_value, token = self._encode_pending_profile_rules(
                    mode, text.strip(), pending_rules
                )
            except ValueError:
                return "That profile change is too large. Please shorten it."
            candidate = state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                    "last_update_id": pending_value,
                }
            )
            if not self.repo.transition_conversation_state(
                user_id, candidate, expected_revision=state.revision
            ):
                return "That profile edit is stale. Please open /profile again."
            self.telegram_api.send_profile_rule_review(
                chat_id, category, text.strip(), pending_rules, token
            )
            return None

        profile = self.repo.get_profile(user_id, consistent_read=True)
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
            self.repo.get_profile(user_id, consistent_read=True)
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
        if not operation.is_valid_for(category):
            return None, "That profile operation is not available here."
        if category is ProfileEditCategory.FAMILY:
            return self._apply_family_amendment(profile, operation, text)
        return self._apply_item_amendment(profile, category, operation, text)

    @staticmethod
    def _apply_family_amendment(
        profile: UserProfile,
        operation: ProfileEditOperation,
        text: str,
    ) -> tuple[UserProfile | None, str]:
        """Apply one deterministic family-member amendment."""
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

        if operation in {
            ProfileEditOperation.CHANGE_PROTEIN,
            ProfileEditOperation.CHANGE_FIBRE,
        }:
            parsed_target = BotHandler._parse_profile_member_suffix(
                text, members, allow_none=True, maximum=1_000
            )
            if parsed_target is None:
                return None, "Use the format: name grams, or name none."
            name, target = parsed_target
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
            field_name = (
                "protein_target"
                if operation is ProfileEditOperation.CHANGE_PROTEIN
                else "fibre_target"
            )
            members[index] = members[index].model_copy(
                update={field_name: target}
            )
            label = "protein" if field_name == "protein_target" else "fibre"
            action = "cleared" if target is None else "updated"
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
                f"{action.capitalize()} {members[index].name}'s {label} "
                "target.",
            )

        if operation is ProfileEditOperation.CHANGE_CALORIES:
            parsed_target = BotHandler._parse_profile_member_suffix(
                text, members, allow_none=False, maximum=10_000
            )
            if parsed_target is None or parsed_target[1] is None:
                return (
                    None,
                    "Use the format: name calories, or name calories "
                    "protein fibre.",
                )
            name = parsed_target[0]
            calories = parsed_target[1]
            assert calories is not None
            protein = None
            fibre = None
        else:
            parsed = BotHandler._parse_profile_name_calories(text)
            if parsed is None:
                return (
                    None,
                    "Use the format: name calories, or name calories "
                    "protein fibre.",
                )
            name, calories, protein, fibre = parsed
        if operation is ProfileEditOperation.CHANGE_CALORIES and (
            protein is not None or fibre is not None
        ):
            return None, "Use the format: name calories."
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
            member = FamilyMember(
                name=name,
                calorie_target=calories,
                protein_target=protein,
                fibre_target=fibre,
            )
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
        members[index] = members[index].model_copy(
            update={"calorie_target": calories}
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
                if (
                    existing.casefold()
                    if isinstance(existing, str)
                    else existing.source_text.casefold()
                )
                == item.casefold()
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
        removed_text = (
            removed if isinstance(removed, str) else removed.source_text
        )
        return (
            BotHandler._profile_with_updates(profile, {category.value: items}),
            f"Removed {removed_text}.",
        )

    def _handle_structured_meal_input(
        self,
        chat_id: int | str,
        user_id: str,
        text: str,
        route: RouteResult,
        state: ConversationState,
    ) -> None:
        """Validate one meal locally and stage it for explicit review."""
        result = parse_meal_input(text, self._reference_date_from_route(route))
        if not result.is_valid:
            explanation = "\n".join(result.errors)
            self.telegram_api.send_message(
                chat_id, f"{explanation}\n\n{MEAL_INPUT_PROMPT}"
            )
            return

        assert result.draft is not None
        now = datetime.now(timezone.utc)
        review_state = state.model_copy(
            update={
                "step": ConversationWorkflowStep.AWAITING_MEAL_CONFIRMATION,
                "meal_draft": result.draft,
                "revision": state.revision + 1,
                "updated_at": now,
            }
        )
        if not self.repo.transition_conversation_state(
            user_id, review_state, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id,
                "That meal workflow changed. Please use /submit_meals.",
            )
            return
        if review_state.request_id is None:
            raise ValueError("meal review state requires a request ID")
        self.telegram_api.send_meal_review(
            chat_id, text, review_state.request_id
        )

    def _restart_legacy_meal_workflow(
        self,
        chat_id: int | str,
        user_id: str,
        state: ConversationState,
    ) -> None:
        """Remove an old field-by-field meal workflow before restarting."""
        if not self.repo.delete_conversation_state(
            user_id, expected_revision=state.revision
        ):
            self.telegram_api.send_message(
                chat_id,
                "That meal workflow changed. Please use /submit_meals "
                "to restart.",
            )
            return
        self.telegram_api.send_message(
            chat_id,
            "Your older meal logging workflow was cleared. Please use "
            "/submit_meals to restart.",
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

        profile = self.repo.get_profile(user_id)
        if profile is None:
            self.telegram_api.send_message(
                chat_id, "Complete your profile before generating a plan."
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
            interpretation: tuple[
                list[DietaryRule | ConstraintEntry], str | None
            ] = (
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
        current_rules = [
            canonicalize_dietary_rule(rule, namespace="current")
            for rule in requirements
            if isinstance(rule, DietaryRule)
        ]
        stored_rules: list[DietaryRule] = []
        stored_clarification: str | None = None
        if not clarification:
            stored_rules, stored_clarification = (
                self._prepare_stored_preference_rules(profile)
            )
            if stored_clarification:
                clarification = stored_clarification
        constraint_rules = [
            canonicalize_constraint_entry(constraint, namespace="constraint")
            for constraint in profile.dietary_constraints
        ]
        resolution_message: str | None = None
        if not clarification:
            safety = validate_constraints(constraint_rules)
            if not safety.is_safe:
                resolution_message = (
                    "A saved dietary constraint cannot be safely matched. "
                    "Please edit that constraint before generating a plan."
                )
            elif any(
                has_constraint_conflict(rule, constraint_rules)
                for rule in (*stored_rules, *current_rules)
            ):
                resolution_message = (
                    "That preference conflicts with a dietary constraint and "
                    "was not sent to the planner."
                )
            else:
                resolution = resolve_priority_rules(
                    stored_rules,
                    current_rules,
                    constraints=constraint_rules,
                )
                if resolution.clarification is not None:
                    resolution_message = resolution.clarification.message
                else:
                    effective_rules = self._snapshot_effective_rules(
                        list(resolution.effective_rules),
                        stored_rules,
                        current_rules,
                    )
                    effective_ids = [rule.id for rule in effective_rules]
                    if len(effective_ids) != len(set(effective_ids)):
                        resolution_message = (
                            "I couldn't safely combine your dietary rules. "
                            "Please clarify or remove the conflicting rule."
                        )
        if clarification or resolution_message:
            effective_rules = []
        # Structured interpretations belong exclusively in effective_rules.
        # The requirements field is reserved for genuinely legacy events.
        planner_requirements: list[PreferenceRequirement] = []
        next_step = (
            ConversationWorkflowStep.AWAITING_PREFERENCE
            if clarification or resolution_message
            else ConversationWorkflowStep.GENERATING
        )
        try:
            candidate = ConversationState.model_validate(
                {
                    **state.model_dump(),
                    "step": next_step,
                    "preference": preference,
                    "requirements": planner_requirements,
                    "stored_rules": stored_rules,
                    "current_rules": current_rules
                    if not resolution_message
                    else [],
                    "effective_rules": effective_rules,
                    "constraint_rules": constraint_rules,
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
        if clarification or resolution_message:
            message = clarification or resolution_message
            assert message is not None
            self.telegram_api.send_message(chat_id, message)
            return
        invoked = self._invoke_planner(
            user_id,
            chat_id,
            GENERATE_PLAN,
            week_start=date.today().isoformat(),
            preference=preference,
            requirements=planner_requirements,
            stored_rules=stored_rules,
            current_rules=current_rules,
            effective_rules=effective_rules,
            constraint_rules=constraint_rules,
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

    def _prepare_stored_preference_rules(
        self, profile: UserProfile
    ) -> tuple[list[DietaryRule], str | None]:
        """Interpret legacy stored wording before resolving plan rules."""
        stored_rules: list[DietaryRule] = []
        client = self.llm_client or LLMClient()
        for entry in profile.dietary_preferences:
            if entry.rule is not None:
                stored_rules.append(
                    canonicalize_dietary_rule(entry.rule, namespace="stored")
                )
                continue
            try:
                interpretation = parse_preference_interpretation(
                    client.chat_sync(
                        build_preference_interpretation_prompt(
                            entry.source_text, mode="stored_preference"
                        ),
                        entry.source_text,
                    ),
                    mode="stored_preference",
                )
            except Exception:
                logger.error("Stored preference interpretation failed")
                return [], (
                    "I couldn't safely interpret a saved dietary preference. "
                    "Please rephrase or remove it before generating a plan."
                )
            rules, clarification = interpretation
            if clarification:
                return [], (
                    "I couldn't safely interpret a saved dietary preference. "
                    f"{clarification}"
                )
            if not rules or not all(
                isinstance(rule, DietaryRule) for rule in rules
            ):
                return [], (
                    "I couldn't safely interpret a saved dietary preference. "
                    "Please rephrase or remove it before generating a plan."
                )
            stored_rules.extend(
                canonicalize_dietary_rule(rule, namespace="stored")
                for rule in rules
                if isinstance(rule, DietaryRule)
            )
            if len(stored_rules) > MAX_PLAN_REQUIREMENTS:
                return [], (
                    "Your saved dietary preferences contain too many rules. "
                    "Please remove one before generating a plan."
                )
        rule_ids = [rule.id for rule in stored_rules]
        if len(rule_ids) != len(set(rule_ids)):
            return [], (
                "I couldn't safely combine your saved dietary preferences. "
                "Please remove the duplicate preference and try again."
            )
        return sorted(stored_rules, key=lambda rule: rule.id), None

    @staticmethod
    def _snapshot_effective_rules(
        resolved: list[DietaryRule],
        stored_rules: list[DietaryRule],
        current_rules: list[DietaryRule],
    ) -> list[DietaryRule]:
        """Give capped current maxima stable ownership in the snapshot.

        The resolver retains a capped lower-priority obligation when a broad
        maximum is absorbed.  The dispatch snapshot should still identify
        that obligation as originating from the current request while
        retaining the resolver's preferred scope and exact count.
        """
        snapshot = list(resolved)
        snapshot_ids = {rule.id for rule in snapshot}
        for current in current_rules:
            if current.operator is not RuleOperator.AT_MOST:
                continue
            if current.id in snapshot_ids:
                continue
            overlapping_stored = [
                rule
                for rule in stored_rules
                if rule.id in snapshot_ids
                and rule.foods_any_of == current.foods_any_of
            ]
            if len(overlapping_stored) != 1:
                continue
            stored = overlapping_stored[0]
            capped = next(
                (item for item in snapshot if item.id == stored.id), None
            )
            if capped is None:
                continue
            snapshot[snapshot.index(capped)] = current.model_copy(
                update={
                    "operator": RuleOperator.EXACTLY,
                    "count": capped.count,
                    "weekdays": capped.weekdays,
                    "meal_type": capped.meal_type,
                }
            )
        return sorted(snapshot, key=lambda rule: rule.id)

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
            stored_rules=candidate.stored_rules,
            current_rules=candidate.current_rules,
            effective_rules=candidate.effective_rules,
            constraint_rules=candidate.constraint_rules,
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

    def _merge_omitted_member_targets(
        self,
        incoming_members: list[FamilyMember],
        draft_members: list[FamilyMember] | None,
        saved_members: list[FamilyMember] | None,
    ) -> list[FamilyMember] | None:
        """Inherit only omitted targets from an unambiguous member source."""
        if all(
            {
                "protein_target",
                "fibre_target",
            }.issubset(member.model_fields_set)
            for member in incoming_members
        ):
            return list(incoming_members)

        draft_by_identity: dict[str, list[FamilyMember]] = {}
        for member in draft_members or []:
            identity = self._member_identity(member.name)
            draft_by_identity.setdefault(identity, []).append(member)

        saved_by_identity: dict[str, list[FamilyMember]] = {}
        for member in saved_members or []:
            identity = self._member_identity(member.name)
            saved_by_identity.setdefault(identity, []).append(member)

        merged_members: list[FamilyMember] = []
        for incoming in incoming_members:
            if {
                "protein_target",
                "fibre_target",
            }.issubset(incoming.model_fields_set):
                merged_members.append(incoming)
                continue
            identity = self._member_identity(incoming.name)
            draft_matches = draft_by_identity.get(identity, [])
            source: FamilyMember | None
            if len(draft_matches) > 1:
                return None
            if len(draft_matches) == 1:
                source = draft_matches[0]
            else:
                saved_matches = saved_by_identity.get(identity, [])
                if len(saved_matches) > 1:
                    return None
                source = saved_matches[0] if saved_matches else None

            updates: dict[str, int | None] = {}
            if source is not None:
                if "protein_target" not in incoming.model_fields_set:
                    updates["protein_target"] = source.protein_target
                if "fibre_target" not in incoming.model_fields_set:
                    updates["fibre_target"] = source.fibre_target
            merged_members.append(
                incoming.model_copy(update=updates) if updates else incoming
            )
        return merged_members

    def _update_profile(
        self,
        user_id: str,
        entities: dict[str, Any],
        existing: UserProfile | None,
    ) -> MutationResult:
        update = ProfileUpdateEntities.model_validate(entities)
        expected_revision = (
            existing.profile_revision if existing is not None else None
        )
        persisted_draft = self.repo.get_profile_draft(user_id)
        draft_members = (
            persisted_draft.family_members
            if isinstance(persisted_draft, ProfileUpdateEntities)
            else None
        )
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
            value = getattr(update, field)
            if field == "family_members" and value is not None:
                value = self._merge_omitted_member_targets(
                    value,
                    draft_members,
                    existing.family_members if existing else None,
                )
                if value is None:
                    return MutationResult(
                        False,
                        "Family member names must be unique, ignoring "
                        "capitalization and surrounding spaces.",
                    )
            data[field] = value
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
        profile = UserProfile.model_validate(draft.model_dump()).model_copy(
            update={
                "profile_revision": (
                    expected_revision if expected_revision is not None else 0
                )
            }
        )
        if any(
            constraint.uninterpretable
            for constraint in profile.dietary_constraints
        ):
            return MutationResult(
                False,
                "I couldn't safely interpret that constraint. Please "
                "rephrase it with specific foods.",
            )
        saved = self.repo.save_profile(
            user_id, profile, expected_revision=expected_revision
        )
        if not saved:
            self.repo.save_profile_draft(user_id, draft)
            return MutationResult(
                False,
                "That profile is stale. Your latest profile was kept; "
                "please try again.",
            )
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
        stored_rules: list[DietaryRule] | None = None,
        current_rules: list[DietaryRule] | None = None,
        effective_rules: list[DietaryRule] | None = None,
        constraint_rules: list[ConstraintEntry] | None = None,
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
                        "stored_rules": [
                            rule.model_dump(mode="json")
                            for rule in (stored_rules or [])
                        ],
                        "current_rules": [
                            rule.model_dump(mode="json")
                            for rule in (current_rules or [])
                        ],
                        "effective_rules": [
                            rule.model_dump(mode="json")
                            for rule in (effective_rules or [])
                        ],
                        "constraint_rules": [
                            rule.model_dump(mode="json")
                            for rule in (constraint_rules or [])
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
