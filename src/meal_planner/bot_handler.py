"""AWS Lambda entry point for Telegram webhook commands and routing."""

import base64
import hmac
import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from meal_planner.config import (
    BotConfigurationError,
    get_settings,
    get_webhook_secret,
)
from meal_planner.db.dynamo import DynamoRepository
from meal_planner.models.schemas import (
    ConversationState,
    ConversationWorkflowKind,
    ConversationWorkflowStep,
    FamilyMember,
    MealLogDraft,
    MealLogEntry,
    PlanChatAction,
    PlanChatEvent,
    ProfileDraft,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
)
from meal_planner.router import (
    MealCallback,
    PlanChatCallback,
    ProfileCallback,
    ProfileCallbackAction,
    RouteResult,
    RouteType,
    parse_meal_callback,
    parse_meal_input,
    parse_plan_chat_callback,
    parse_profile_callback,
    route_update,
)
from meal_planner.telegram.access import TelegramAccessPolicy
from meal_planner.telegram.api import (
    TelegramAPI,
    TelegramAPIError,
    profile_presentation_items,
)
from meal_planner.telegram.commands import render_help

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"
MEAL_INPUT_PROMPT = (
    "Submit one meal using this format:\n"
    "when, meal type, what you ate\n\n"
    "Use today or yesterday, or a strict YYYY-MM-DD date. Dates are "
    "interpreted in UTC and must be from UTC today through the previous "
    "seven dates, inclusive (eight calendar dates). Meal type must be "
    "breakfast, lunch, snack, or "
    "dinner. Keep the description after the second comma.\n\n"
    "Example: today, lunch, vegetable soup"
)

PLAN_CHAT_PROMPT = "Tell me what kind of meal plan draft would help today."
PLAN_CHAT_PROGRESS_MESSAGE = "I'm still working on your meal-plan draft."
PLAN_CHAT_START_FAILURE_MESSAGE = (
    "I couldn't start a draft right now. Please send a new request."
)

_COMMAND_REFERENCE_DATE: ContextVar[date | None] = ContextVar(
    "command_reference_date", default=None
)


class ProfileLoadValidationError(RuntimeError):
    """A persisted profile could not satisfy the current profile schema."""


PROFILE_LOAD_RECOVERY_MESSAGE = (
    "I couldn't safely load your profile. Please try again or edit it "
    "through /profile."
)

STALE_PROFILE_REMOVAL_MESSAGE = (
    "That profile removal is stale. Nothing was changed. Please reopen "
    "/profile to get current buttons."
)


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
        plan_chat_function_name: str = "",
        llm_client: Any = None,
        access_policy: TelegramAccessPolicy | None = None,
        processing_date: date | None = None,
    ) -> None:
        self.repo = repo
        self.telegram_api = telegram_api
        self.lambda_client = lambda_client
        self.plan_chat_function_name = plan_chat_function_name
        self.llm_client = llm_client
        self.access_policy = access_policy or TelegramAccessPolicy(frozenset())
        self._processing_date = processing_date

    def _load_profile(
        self, user_id: str, *, consistent_read: bool = False
    ) -> UserProfile | None:
        """Load a profile while containing known persisted-schema failures."""
        try:
            if consistent_read:
                return self.repo.get_profile(user_id, consistent_read=True)
            return self.repo.get_profile(user_id)
        except ValidationError:
            logger.warning("Profile load rejected reason_code=validation")
            raise ProfileLoadValidationError from None

    def _send_profile_load_recovery(self, chat_id: int | str) -> None:
        """Tell the user how to recover from an invalid saved profile."""
        self.telegram_api.send_message(chat_id, PROFILE_LOAD_RECOVERY_MESSAGE)

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
        except ProfileLoadValidationError:
            if route.chat_id is not None:
                try:
                    self._send_profile_load_recovery(route.chat_id)
                except TelegramAPIError:
                    logger.error(
                        "Profile load recovery delivery failed "
                        "reason_code=delivery"
                    )
            if route.callback_query_id:
                try:
                    self.telegram_api.answer_callback_query(
                        route.callback_query_id, "Profile unavailable"
                    )
                except TelegramAPIError:
                    logger.error("Failed to acknowledge profile callback")
        except TelegramAPIError:
            logger.error("Telegram delivery failed reason_code=delivery")
        except Exception:
            logger.error("Update handling failed reason_code=unexpected")
            if route.chat_id is not None:
                try:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "Sorry, I couldn't process that request. Please try "
                        "again.",
                    )
                except TelegramAPIError:
                    logger.error(
                        "Update error delivery failed reason_code=delivery"
                    )
            return {"statusCode": 500, "body": "error"}
        return {"statusCode": 200, "body": "ok"}

    def handle_command(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id:
            return
        handlers = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "profile": self._cmd_profile,
            "plan": self._cmd_plan,
            "submit_meals": self._cmd_submit_meals,
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
        try:
            profile = self._load_profile(user_id)
        except ProfileLoadValidationError:
            self._send_profile_load_recovery(chat_id)
            return
        if profile and profile.is_complete:
            self.telegram_api.send_message(
                chat_id,
                f"Welcome back, {profile.name} family! Use /plan to create "
                "a conversational meal-plan draft, or /profile to review "
                "your details. Drafts are suggestions, not confirmed plans. "
                "Use /submit_meals to log actual meals.",
            )
            return

        self.telegram_api.send_message(
            chat_id,
            "Welcome to Meal Planner Bot! We will set up your profile "
            "one step at a time: family name, household size, each "
            "household member's name and required calorie target, "
            "dietary constraints, and dietary preferences. Optional "
            "protein and fibre targets in grams/day are collected per "
            "member and are not required. After setup, use /plan for a "
            "conversational draft or /submit_meals to log actual meals. "
            "Drafts are suggestions, not confirmed plans.",
        )
        draft = self.repo.get_profile_draft(user_id)
        state = self._get_conversation_state(user_id)
        if (
            state is None
            or state.workflow_kind is not ConversationWorkflowKind.PROFILE_SETUP
        ):
            setup_state = self._new_profile_setup_state(
                self._profile_setup_step_for_draft(draft)
            )
            if not self._replace_conversation_state(
                user_id, setup_state, state
            ):
                self.telegram_api.send_message(
                    chat_id,
                    "That setup changed while it was starting. Please "
                    "send /start again.",
                )
                return
        else:
            setup_state = state
            expected_step = self._profile_setup_step_for_draft(draft)
            if setup_state.step is not expected_step:
                reconciled_state = setup_state.model_copy(
                    update={
                        "step": expected_step,
                        "revision": setup_state.revision + 1,
                        "updated_at": datetime.now(timezone.utc),
                        "last_update_id": None,
                    }
                )
                try:
                    reconciled = self.repo.transition_conversation_state(
                        user_id,
                        reconciled_state,
                        expected_revision=setup_state.revision,
                        expected_step=setup_state.step,
                    )
                except Exception:
                    logger.error(
                        "Profile setup resume failed reason_code=persistence"
                    )
                    reconciled = False
                if not reconciled:
                    self.telegram_api.send_message(
                        chat_id,
                        "That setup changed while it was resuming. "
                        "Please send /start again.",
                    )
                    return
                setup_state = reconciled_state
        self._send_profile_setup_prompt(chat_id, setup_state, draft=draft)

    def _cmd_profile(self, chat_id: int | str, user_id: str) -> None:
        try:
            profile = self._load_profile(user_id)
        except ProfileLoadValidationError:
            self._send_profile_load_recovery(chat_id)
            return
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
        try:
            profile = self._load_profile(user_id)
        except ProfileLoadValidationError:
            self._send_profile_load_recovery(chat_id)
            return
        if not profile or not profile.is_complete:
            self.telegram_api.send_message(
                chat_id, "Complete your profile before generating a plan."
            )
            return
        state = self._get_conversation_state(user_id)
        replaced = state is not None
        request_state = self._new_plan_chat_state()
        if not self._replace_conversation_state(user_id, request_state, state):
            self.telegram_api.send_message(
                chat_id,
                "That workflow changed while I was starting /plan. "
                "Please try again.",
            )
            return
        prefix = "I replaced your unfinished workflow. " if replaced else ""
        assert request_state.session_id is not None
        self.telegram_api.send_plan_chat(
            chat_id,
            prefix + PLAN_CHAT_PROMPT,
            request_state.session_id,
        )

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
    def _new_plan_chat_state() -> ConversationState:
        """Create an empty session for a conversational plan draft."""
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.PLAN_CHAT,
            step=ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
            session_id=str(uuid4()),
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
    def _new_profile_setup_state(
        step: ConversationWorkflowStep,
    ) -> ConversationState:
        """Create state for one deterministic profile setup step."""
        now = datetime.now(timezone.utc)
        return ConversationState(
            workflow_kind=ConversationWorkflowKind.PROFILE_SETUP,
            step=step,
            revision=0,
            created_at=now,
            updated_at=now,
            expires_at=int((now + timedelta(hours=24)).timestamp()),
        )

    @staticmethod
    def _profile_setup_step_for_draft(
        draft: ProfileDraft | None,
    ) -> ConversationWorkflowStep:
        """Return the first unanswered deterministic setup step."""
        if draft is None or draft.name is None:
            return ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME
        if draft.people_count is None:
            return ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
        if draft.family_members is None:
            return ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS
        if draft.dietary_constraints is None:
            return ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS
        return ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES

    def _send_profile_setup_prompt(
        self,
        chat_id: int | str,
        state: ConversationState,
        *,
        draft: ProfileDraft | None,
        text: str | None = None,
    ) -> None:
        """Render one setup prompt with its scoped close control."""
        self.telegram_api.send_profile_setup_prompt(
            chat_id,
            state.step,
            people_count=draft.people_count if draft else None,
            text=text,
        )

    @staticmethod
    def _parse_setup_member_line(text: str) -> FamilyMember | None:
        """Parse ``name calories [protein] [fibre]`` for onboarding."""
        if "\n" in text or "\r" in text:
            return None
        parts = text.strip().split()
        if not 2 <= len(parts) <= 5:
            return None

        def parse_target(value: str, maximum: int) -> int | None | bool:
            if value.casefold() == "none":
                return None
            if not value.isdecimal():
                return False
            target = int(value)
            return target if 1 <= target <= maximum else False

        for target_count in (3, 2, 1):
            if len(parts) <= target_count:
                continue
            name_parts = parts[:-target_count]
            target_parts = parts[-target_count:]
            if not name_parts or any(part.isdecimal() for part in name_parts):
                continue
            calories_value = parse_target(target_parts[0], 10_000)
            if calories_value is False or calories_value is None:
                continue
            protein: int | None = None
            fibre: int | None = None
            if len(target_parts) >= 2:
                protein_value = parse_target(target_parts[1], 1_000)
                if protein_value is False:
                    continue
                protein = protein_value
            if len(target_parts) == 3:
                fibre_value = parse_target(target_parts[2], 1_000)
                if fibre_value is False:
                    continue
                fibre = fibre_value
            try:
                return FamilyMember(
                    name=" ".join(name_parts),
                    calorie_target=calories_value,
                    protein_target=protein,
                    fibre_target=fibre,
                )
            except ValidationError:
                continue
        return None

    @staticmethod
    def _parse_setup_member_lines(
        text: str, people_count: int
    ) -> list[FamilyMember] | None:
        """Parse and validate exactly one member line per person."""
        if not 1 <= people_count <= 20:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != people_count:
            return None
        members: list[FamilyMember] = []
        identities: set[str] = set()
        for line in lines:
            member = BotHandler._parse_setup_member_line(line)
            if member is None:
                return None
            identity = BotHandler._member_identity(member.name)
            if identity in identities:
                return None
            identities.add(identity)
            members.append(member)
        return members

    @staticmethod
    def _parse_setup_dietary_text(
        text: str, field_name: str
    ) -> list[str] | None:
        """Parse bounded newline-separated raw dietary text."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or len(lines) > 20:
            return None
        try:
            draft = ProfileDraft.model_validate({field_name: lines})
        except ValidationError:
            return None
        parsed = getattr(draft, field_name)
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            return None
        return parsed

    def _handle_profile_setup_input(
        self,
        user_id: str,
        chat_id: int | str,
        text: str,
        state: ConversationState,
        *,
        source_update_id: str | None,
    ) -> None:
        """Consume one deterministic setup answer and persist progress."""
        if state.step not in {
            ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME,
            ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE,
            ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS,
            ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS,
            ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES,
        }:
            self.telegram_api.send_message(
                chat_id,
                "That profile setup is unavailable. Please send /start again.",
            )
            return
        if source_update_id and state.last_update_id == source_update_id:
            draft = self.repo.get_profile_draft(user_id)
            self._send_profile_setup_prompt(chat_id, state, draft=draft)
            return

        draft = self.repo.get_profile_draft(user_id) or ProfileDraft()
        expected_step = self._profile_setup_step_for_draft(draft)
        if expected_step is not state.step:
            self.telegram_api.send_message(
                chat_id,
                "That profile setup is stale. Please send /start to resume "
                "from the saved step.",
            )
            return

        update: dict[str, Any]
        next_step: ConversationWorkflowStep
        retry_message: str
        if state.step is ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME:
            family_name = text.strip()
            try:
                checked = ProfileDraft.model_validate({"name": family_name})
            except ValidationError:
                checked = None
            if checked is None:
                retry_message = "Please send one family name, up to 100 "
                "characters."
                self._send_profile_setup_prompt(
                    chat_id, state, draft=draft, text=retry_message
                )
                return
            update = {"name": checked.name}
            next_step = ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
            retry_message = ""
        elif (
            state.step
            is ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE
        ):
            count_text = text.strip()
            if not count_text.isdecimal() or not 1 <= int(count_text) <= 20:
                retry_message = "Please reply with a whole household size "
                "from 1 to 20."
                self._send_profile_setup_prompt(
                    chat_id, state, draft=draft, text=retry_message
                )
                return
            update = {"people_count": int(count_text)}
            next_step = ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS
            retry_message = ""
        elif state.step is ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS:
            assert draft.people_count is not None
            members = self._parse_setup_member_lines(text, draft.people_count)
            if members is None:
                retry_message = (
                    f"Please send exactly {draft.people_count} valid member "
                    "lines: name calories [protein] [fibre]. Names must be "
                    "unique."
                )
                self._send_profile_setup_prompt(
                    chat_id, state, draft=draft, text=retry_message
                )
                return
            update = {"family_members": members}
            next_step = ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS
            retry_message = ""
        elif (
            state.step is ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS
        ):
            constraints = self._parse_setup_dietary_text(
                text, "dietary_constraints"
            )
            if constraints is None:
                retry_message = (
                    "Please list dietary constraints one per line, or reply "
                    "'none'. Each item must be at most 500 characters, with "
                    "no more than 20 items."
                )
                self._send_profile_setup_prompt(
                    chat_id, state, draft=draft, text=retry_message
                )
                return
            update = {"dietary_constraints": constraints}
            next_step = ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES
            retry_message = ""
        else:
            preferences = self._parse_setup_dietary_text(
                text, "dietary_preferences"
            )
            if preferences is None:
                retry_message = (
                    "Please list dietary preferences one per line, or reply "
                    "'none'. Each item must be at most 500 characters, with "
                    "no more than 20 items."
                )
                self._send_profile_setup_prompt(
                    chat_id, state, draft=draft, text=retry_message
                )
                return
            update = {"dietary_preferences": preferences}
            next_step = state.step
            retry_message = ""

        updated_data = draft.model_dump(mode="json")
        updated_data.update(update)
        updated_draft = ProfileDraft.model_validate(updated_data)
        now = datetime.now(timezone.utc)
        if state.step is ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES:
            profile = UserProfile(
                name=updated_draft.name or "",
                people_count=updated_draft.people_count or 0,
                family_members=updated_draft.family_members or [],
                dietary_constraints=updated_draft.dietary_constraints or [],
                dietary_preferences=updated_draft.dietary_preferences or [],
            )
            existing = self._load_profile(user_id, consistent_read=True)
            expected_revision = existing.profile_revision if existing else None
            if existing is not None:
                profile = profile.model_copy(
                    update={"profile_revision": existing.profile_revision}
                )
            try:
                saved = self.repo.complete_profile_setup(
                    user_id,
                    profile,
                    state,
                    expected_profile_revision=expected_revision,
                )
            except Exception:
                logger.error(
                    "Profile setup completion failed reason_code=persistence"
                )
                self._send_profile_setup_prompt(
                    chat_id,
                    state,
                    draft=draft,
                    text="I couldn't save your profile. Please try again.",
                )
                return
            if not saved:
                self.telegram_api.send_message(
                    chat_id,
                    "That profile setup changed while it was being saved. "
                    "Please send /start to try again.",
                )
                return
            self.telegram_api.send_message(
                chat_id,
                "Your profile has been saved. Use /plan for a conversational "
                "draft or /submit_meals to log actual meals.",
            )
            return

        next_state = state.model_copy(
            update={
                "step": next_step,
                "revision": state.revision + 1,
                "updated_at": now,
                "last_update_id": source_update_id,
            }
        )
        try:
            transitioned = self.repo.save_profile_draft_and_transition_state(
                user_id,
                updated_draft,
                next_state,
                state,
            )
        except Exception:
            logger.error(
                "Profile setup progress failed reason_code=persistence"
            )
            self._send_profile_setup_prompt(
                chat_id,
                state,
                draft=draft,
                text="I couldn't save that setup answer. Please try again.",
            )
            return
        if not transitioned:
            self.telegram_api.send_message(
                chat_id,
                "That profile setup changed. Please send /start to resume.",
            )
            return
        self._send_profile_setup_prompt(
            chat_id, next_state, draft=updated_draft
        )

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

    def handle_callback(self, route: RouteResult) -> None:
        if route.callback_data:
            plan_chat_callback = parse_plan_chat_callback(route.callback_data)
            if plan_chat_callback is not None:
                self._handle_plan_chat_callback(route, plan_chat_callback)
                return
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
            acknowledgement = "Unsupported action"
            self.telegram_api.send_message(
                route.chat_id,
                "That button is invalid or outdated.",
            )
            return
        except Exception:
            logger.error("Error handling callback reason_code=unexpected")
            if route.chat_id is not None:
                self.telegram_api.send_message(
                    route.chat_id,
                    "Sorry, I couldn't update that meal. Please try again.",
                )
        finally:
            if route.callback_query_id:
                try:
                    self.telegram_api.answer_callback_query(
                        route.callback_query_id, acknowledgement
                    )
                except TelegramAPIError:
                    logger.error("Failed to acknowledge callback query")

    def _handle_plan_chat_callback(
        self,
        route: RouteResult,
        callback: PlanChatCallback,
    ) -> None:
        """End only the active plan-chat session named by its own button."""
        if route.chat_id is None or not route.user_id:
            return

        acknowledgement = "Unable to end planning"
        try:
            try:
                state = self._get_conversation_state(route.user_id)
                if state is None:
                    acknowledgement = "Planning already ended"
                    self._send_plan_chat_callback_message(
                        route.chat_id,
                        "That planning session has already ended.",
                        callback.session_id,
                    )
                    return

                if (
                    state.workflow_kind
                    is not ConversationWorkflowKind.PLAN_CHAT
                    or state.session_id != callback.session_id
                ):
                    acknowledgement = "Stale planning button"
                    self._send_plan_chat_callback_message(
                        route.chat_id,
                        "That planning button is stale or outdated. Nothing "
                        "was changed.",
                        callback.session_id,
                    )
                    return

                deleted = self.repo.delete_conversation_state(
                    route.user_id,
                    expected_revision=state.revision,
                    expected_step=state.step,
                )
            except Exception:
                logger.error(
                    "Plan chat cancellation failed reason_code=persistence"
                )
                self._send_plan_chat_callback_message(
                    route.chat_id,
                    "I couldn't end planning right now. Please try again.",
                    callback.session_id,
                )
                return

            if not deleted:
                acknowledgement = "Planning changed"
                self._send_plan_chat_callback_message(
                    route.chat_id,
                    "That planning session changed. Nothing was ended.",
                    callback.session_id,
                )
                return

            acknowledgement = "Planning ended"
            try:
                self.telegram_api.send_message(
                    route.chat_id,
                    "Planning ended. Use /plan whenever you want a new draft.",
                )
            except Exception:
                logger.error(
                    "Plan chat cancellation success delivery failed "
                    "reason_code=delivery"
                )
        finally:
            if route.callback_query_id:
                try:
                    self.telegram_api.answer_callback_query(
                        route.callback_query_id, acknowledgement
                    )
                except TelegramAPIError:
                    logger.error("Failed to acknowledge plan chat callback")

    def _send_plan_chat_callback_message(
        self,
        chat_id: int | str,
        text: str,
        session_id: str,
    ) -> None:
        """Deliver a stale or failure response with the session's end button."""
        try:
            self.telegram_api.send_plan_chat(chat_id, text, session_id)
        except TelegramAPIError:
            logger.error("Plan chat callback delivery failed")

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
            if state is None or state.workflow_kind not in {
                ConversationWorkflowKind.PROFILE_EDIT,
                ConversationWorkflowKind.PROFILE_SETUP,
            }:
                self.telegram_api.send_message(
                    route.chat_id,
                    "That profile menu is no longer active. Use /profile "
                    "to open it again.",
                )
                return

            if state.workflow_kind is ConversationWorkflowKind.PROFILE_SETUP:
                if callback.action is not ProfileCallbackAction.CLOSE:
                    self.telegram_api.send_message(
                        route.chat_id,
                        "That profile setup control is invalid. Use /start "
                        "to resume setup.",
                    )
                    return
                if not self.repo.delete_conversation_state(
                    route.user_id,
                    expected_revision=state.revision,
                    expected_step=state.step,
                ):
                    self.telegram_api.send_message(
                        route.chat_id,
                        "That profile setup changed. Please use /start to "
                        "resume it.",
                    )
                    return
                self.telegram_api.send_message(
                    route.chat_id,
                    "Profile setup paused. Use /start to resume it.",
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
                profile: UserProfile | None = None
                if callback.operation is ProfileEditOperation.REMOVE:
                    try:
                        profile = self._load_profile(
                            route.user_id, consistent_read=True
                        )
                    except ProfileLoadValidationError:
                        self._send_profile_load_recovery(route.chat_id)
                        return
                    if profile is None:
                        self.telegram_api.send_message(
                            route.chat_id,
                            "No complete profile found. Use /start to begin.",
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
                if profile is None:
                    self.telegram_api.send_profile_operation(
                        route.chat_id, callback.category, callback.operation
                    )
                else:
                    self.telegram_api.send_profile_operation(
                        route.chat_id,
                        callback.category,
                        callback.operation,
                        profile,
                    )
                return

            if callback.action is ProfileCallbackAction.REMOVE_SELECTION:
                self._handle_profile_removal_callback(route, callback, state)
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
            logger.error(
                "Profile callback handling failed reason_code=unexpected"
            )
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

    def _handle_profile_removal_callback(
        self,
        route: RouteResult,
        callback: ProfileCallback,
        state: ConversationState,
    ) -> None:
        """Remove one revision-matched item and refresh its numbered list."""
        assert route.chat_id is not None
        assert route.user_id is not None
        assert callback.category is not None
        assert callback.index is not None
        assert callback.profile_revision is not None
        if (
            state.step is not ConversationWorkflowStep.AWAITING_PROFILE_INPUT
            or state.profile_category is not callback.category
            or state.profile_operation is not ProfileEditOperation.REMOVE
        ):
            self.telegram_api.send_message(
                route.chat_id,
                STALE_PROFILE_REMOVAL_MESSAGE,
            )
            return

        try:
            profile = self._load_profile(route.user_id, consistent_read=True)
        except ProfileLoadValidationError:
            self._send_profile_load_recovery(route.chat_id)
            return
        if profile is None:
            self.telegram_api.send_message(
                route.chat_id, "No complete profile found. Use /start to begin."
            )
            return
        if profile.profile_revision != callback.profile_revision:
            self.telegram_api.send_message(
                route.chat_id, STALE_PROFILE_REMOVAL_MESSAGE
            )
            return

        items = profile_presentation_items(profile, callback.category)
        if callback.index > len(items):
            self.telegram_api.send_message(
                route.chat_id,
                "That profile selection is invalid. Nothing was changed.",
            )
            return
        if (
            callback.category is ProfileEditCategory.FAMILY
            and len(profile.family_members) <= 1
        ):
            self.telegram_api.send_message(
                route.chat_id,
                "You must keep at least one family member.",
            )
            return

        index = callback.index - 1
        if callback.category is ProfileEditCategory.FAMILY:
            members = list(profile.family_members)
            members.pop(index)
            updated = self._profile_with_updates(
                profile,
                {
                    "family_members": [
                        member.model_dump(mode="json") for member in members
                    ],
                    "people_count": len(members),
                },
            )
        elif callback.category is ProfileEditCategory.DIETARY_CONSTRAINTS:
            constraints = list(profile.dietary_constraints)
            constraints.pop(index)
            updated = self._profile_with_updates(
                profile, {"dietary_constraints": constraints}
            )
        else:
            preferences = list(profile.dietary_preferences)
            preferences.pop(index)
            updated = self._profile_with_updates(
                profile, {"dietary_preferences": preferences}
            )

        next_state = state.model_copy(
            update={
                "revision": state.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        try:
            committed = self.repo.remove_profile_item_and_transition_state(
                route.user_id,
                updated,
                next_state,
                state,
                expected_profile_revision=callback.profile_revision,
            )
        except Exception:
            logger.error(
                "Profile removal transaction failed reason_code=persistence"
            )
            self.telegram_api.send_message(
                route.chat_id,
                "I couldn't save that profile change. Please try again.",
            )
            return
        if not committed:
            self.telegram_api.send_message(
                route.chat_id, STALE_PROFILE_REMOVAL_MESSAGE
            )
            return

        self.telegram_api.send_message(route.chat_id, "Profile item removed.")
        self.telegram_api.send_profile_operation(
            route.chat_id,
            callback.category,
            ProfileEditOperation.REMOVE,
            committed,
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
                    route.chat_id,
                    route.user_id,
                    state,
                    callback.submission_id,
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
            logger.error("Error handling meal callback reason_code=unexpected")
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
            logger.error(
                "Meal confirmation persistence failed reason_code=persistence"
            )
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
            logger.error(
                "Meal cancellation persistence failed reason_code=persistence"
            )
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
            logger.error(
                "Starting another meal persistence failed "
                "reason_code=persistence"
            )
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
            logger.error(
                "Completing meal workflow persistence failed "
                "reason_code=persistence"
            )
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

    @staticmethod
    def _next_plan_chat_state(
        state: ConversationState,
        **updates: Any,
    ) -> ConversationState:
        """Return one validated, incremented plan-chat state transition."""
        data = state.model_dump()
        data.update(updates)
        data["revision"] = state.revision + 1
        data["updated_at"] = datetime.now(timezone.utc)
        return ConversationState.model_validate(data)

    def _restore_plan_chat_after_invocation_failure(
        self,
        user_id: str,
        state: ConversationState,
    ) -> bool:
        """Restore an owned session after the asynchronous invoke fails."""
        if state.latest_response is None:
            restored = self._next_plan_chat_state(
                state,
                step=ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
                request_id=None,
                initial_request=None,
                pending_message=None,
                latest_response=None,
                context_date=None,
            )
        else:
            restored = self._next_plan_chat_state(
                state,
                step=ConversationWorkflowStep.PLAN_CHAT_READY,
                pending_message=state.latest_response,
            )
        try:
            return self.repo.transition_conversation_state(
                user_id,
                restored,
                expected_revision=state.revision,
                expected_request_id=state.request_id,
                expected_step=ConversationWorkflowStep.PLAN_CHAT_GENERATING,
            )
        except Exception:
            logger.error(
                "Plan chat invocation recovery failed reason_code=persistence"
            )
            return False

    def _handle_plan_chat_message(
        self,
        chat_id: int | str,
        user_id: str,
        text: str,
        state: ConversationState,
        *,
        source_update_id: str | None,
        context_date: date,
    ) -> None:
        """Claim one plan-chat turn and dispatch its identifier-only event."""
        assert state.session_id is not None
        if state.step is ConversationWorkflowStep.PLAN_CHAT_GENERATING:
            self.telegram_api.send_plan_chat(
                chat_id,
                PLAN_CHAT_PROGRESS_MESSAGE,
                state.session_id,
            )
            return
        if state.step not in {
            ConversationWorkflowStep.AWAITING_PLAN_REQUEST,
            ConversationWorkflowStep.PLAN_CHAT_READY,
        }:
            self.telegram_api.send_message(
                chat_id,
                "That planning session is unavailable. Please use /plan again.",
            )
            return
        if source_update_id and state.last_update_id == source_update_id:
            self.telegram_api.send_plan_chat(
                chat_id,
                "That request was already received. Please send a new request.",
                state.session_id,
            )
            return

        request = text.strip()
        try:
            updates: dict[str, Any] = {
                "step": ConversationWorkflowStep.PLAN_CHAT_GENERATING,
                "request_id": str(uuid4()),
                "pending_message": request,
                "context_date": context_date,
                "last_update_id": source_update_id,
            }
            if state.step is ConversationWorkflowStep.AWAITING_PLAN_REQUEST:
                updates["initial_request"] = request
                updates["latest_response"] = None
            candidate = self._next_plan_chat_state(state, **updates)
        except ValidationError:
            self.telegram_api.send_plan_chat(
                chat_id,
                "Please send a shorter meal-plan request.",
                state.session_id,
            )
            return

        transition_kwargs: dict[str, Any] = {
            "expected_revision": state.revision,
            "expected_step": state.step,
        }
        if state.request_id is not None:
            transition_kwargs["expected_request_id"] = state.request_id
        try:
            transitioned = self.repo.transition_conversation_state(
                user_id,
                candidate,
                **transition_kwargs,
            )
        except Exception:
            logger.error("Plan chat claim failed reason_code=persistence")
            transitioned = False
        if not transitioned:
            self.telegram_api.send_plan_chat(
                chat_id,
                "That planning session changed. Please send your request "
                "again.",
                state.session_id,
            )
            return

        assert candidate.session_id is not None
        assert candidate.request_id is not None
        if not self._invoke_plan_chat(
            user_id,
            chat_id,
            candidate.session_id,
            candidate.request_id,
            candidate.revision,
        ):
            restored = self._restore_plan_chat_after_invocation_failure(
                user_id, candidate
            )
            message = (
                PLAN_CHAT_START_FAILURE_MESSAGE
                if restored
                else "I couldn't start a draft. Please use /plan again."
            )
            self.telegram_api.send_plan_chat(
                chat_id,
                message,
                candidate.session_id,
            )
            return
        self.telegram_api.send_plan_chat(
            chat_id,
            "I'm drafting that for you now.",
            candidate.session_id,
        )

    def handle_conversational(self, route: RouteResult) -> None:
        if route.chat_id is None or not route.user_id or not route.text:
            return
        try:
            source_update_id = self._get_source_update_id(route)
            state = self._get_conversation_state(route.user_id)
            if (
                state
                and state.workflow_kind
                is ConversationWorkflowKind.PROFILE_SETUP
            ):
                self._handle_profile_setup_input(
                    route.user_id,
                    route.chat_id,
                    route.text,
                    state,
                    source_update_id=source_update_id,
                )
                return
            if (
                state
                and state.workflow_kind is ConversationWorkflowKind.PLAN_CHAT
            ):
                self._handle_plan_chat_message(
                    route.chat_id,
                    route.user_id,
                    route.text,
                    state,
                    source_update_id=source_update_id,
                    context_date=self._reference_date_from_route(route),
                )
                return
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
                and state.workflow_kind is ConversationWorkflowKind.PROFILE_EDIT
            ):
                message = self._handle_profile_edit_input(
                    route.user_id, route.chat_id, route.text, state
                )
                if message is not None:
                    self.telegram_api.send_message(route.chat_id, message)
                return
            self.telegram_api.send_message(
                route.chat_id,
                "Please use /plan, /profile, or /submit_meals to get started.",
            )
        except ProfileLoadValidationError:
            self._send_profile_load_recovery(route.chat_id)
        except Exception:
            logger.error(
                "Conversational handling failed reason_code=unexpected"
            )
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
            "no dietary constraints",
            "no allergies",
            "no restrictions",
            "no dietary preferences",
            "no preferences",
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

        if operation is ProfileEditOperation.REMOVE:
            return "Please use the numbered buttons to remove a profile item."

        profile = self._load_profile(user_id, consistent_read=True)
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
            logger.error(
                "Profile amendment transaction failed reason_code=persistence"
            )
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
        assert result.draft.date is not None
        assert result.draft.meal_type is not None
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

    @staticmethod
    def _get_source_update_id(route: RouteResult) -> str | None:
        """Return a normalized Telegram update ID for conversational writes."""
        update_id = route.raw_update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            return str(update_id)
        return None

    def _invoke_plan_chat(
        self,
        user_id: str,
        chat_id: int | str,
        session_id: str,
        request_id: str,
        state_revision: int,
    ) -> bool:
        """Asynchronously invoke Plan Chat with stable identifiers only."""
        if not self.lambda_client or not self.plan_chat_function_name:
            return False
        try:
            event = PlanChatEvent(
                action=PlanChatAction.GENERATE_PLAN_CHAT,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                request_id=request_id,
                state_revision=state_revision,
            )
            self.lambda_client.invoke(
                FunctionName=self.plan_chat_function_name,
                InvocationType="Event",
                Payload=json.dumps(event.model_dump(mode="json")),
            )
        except Exception:
            logger.error("Plan chat invocation failed reason_code=dispatch")
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
    handler = BotHandler(
        repo,
        telegram_api,
        lambda_client=boto3.client("lambda", region_name=settings.aws_region),
        plan_chat_function_name=os.getenv(
            "PLAN_CHAT_FUNCTION_NAME", "meal-planner-plan-chat"
        ),
        access_policy=TelegramAccessPolicy(settings.telegram_allowed_user_ids),
    )
    return handler.handle_update(update)
