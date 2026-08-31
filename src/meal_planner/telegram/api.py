"""Telegram Bot API client with safe plain-text formatting."""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from meal_planner.models.schemas import (
    ConversationWorkflowStep,
    FamilyMember,
    MealCallbackAction,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
)
from meal_planner.telegram.commands import (
    BOT_COMMANDS,
    TelegramCommand,
    validate_commands,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_CALLBACK_DATA_BYTES = 64
PROFILE_NUMBER_BUTTONS_PER_ROW = 5


class TelegramAPIError(RuntimeError):
    """A Telegram request failed or returned an unsuccessful response."""


InlineKeyboard = dict[str, list[list[dict[str, str]]]]


ProfilePresentationValue = FamilyMember | str


@dataclass(frozen=True, slots=True)
class ProfilePresentationItem:
    """One user-visible profile item and its source domain object."""

    label: str
    value: ProfilePresentationValue


def _family_member_label(member: FamilyMember) -> str:
    """Render one family member without storage-only fields."""
    protein = _format_nutrient_target("protein", member.protein_target)
    fibre = _format_nutrient_target("fibre", member.fibre_target)
    return (
        f"{member.name} ({member.calorie_target} kcal/day, {protein}, {fibre})"
    )


def profile_presentation_items(
    profile: UserProfile,
    category: ProfileEditCategory,
    *,
    include_type_labels: bool = True,
) -> list[ProfilePresentationItem]:
    """Return the deterministic, category-relative profile projection.

    Dietary entries are presented in their persisted order.
    """
    if category is ProfileEditCategory.FAMILY:
        return [
            ProfilePresentationItem(
                label=_family_member_label(member),
                value=member,
            )
            for member in profile.family_members
        ]
    if category is ProfileEditCategory.DIETARY_CONSTRAINTS:
        return [
            ProfilePresentationItem(
                label=constraint,
                value=constraint,
            )
            for constraint in profile.dietary_constraints
        ]

    preference_items = [
        ProfilePresentationItem(
            label=(
                f"Dietary preference: {preference}"
                if include_type_labels
                else preference
            ),
            value=preference,
        )
        for preference in profile.dietary_preferences
    ]
    return preference_items


def _numbered_lines(items: Sequence[ProfilePresentationItem]) -> str:
    """Render presentation items as stable one-based numbered lines."""
    if not items:
        return "None"
    return "\n".join(
        f"{index}. {item.label}" for index, item in enumerate(items, start=1)
    )


def _profile_navigation_keyboard() -> InlineKeyboard:
    """Return navigation controls shared by profile input screens."""
    return {
        "inline_keyboard": [
            [{"text": "Back", "callback_data": "profile:back"}],
            [{"text": "Done", "callback_data": "profile:done"}],
            [{"text": "Close", "callback_data": "profile:close"}],
        ]
    }


def _profile_setup_keyboard() -> InlineKeyboard:
    """Return the only control available while setting up a profile."""
    return {
        "inline_keyboard": [
            [{"text": "Close", "callback_data": "profile:close"}]
        ]
    }


def _profile_removal_keyboard(
    category: ProfileEditCategory,
    profile_revision: int,
    item_count: int,
) -> InlineKeyboard:
    """Build wrapped number-only removal buttons and navigation controls."""
    number_rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for index in range(1, item_count + 1):
        callback_data = (
            f"profile:remove:{category.value}:{index}:{profile_revision}"
        )
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError(
                "profile removal callback exceeds Telegram's limit"
            )
        row.append({"text": str(index), "callback_data": callback_data})
        if len(row) == PROFILE_NUMBER_BUTTONS_PER_ROW:
            number_rows.append(row)
            row = []
    if row:
        number_rows.append(row)
    return {
        "inline_keyboard": number_rows
        + _profile_navigation_keyboard()["inline_keyboard"]
    }


def _format_nutrient_target(label: str, target: int | None) -> str:
    """Format an optional daily nutrient target for profile display."""
    if target is None:
        return f"{label}: not set"
    return f"{label}: {target} g/day"


def _meal_callback_data(
    action: MealCallbackAction,
    submission_id: UUID | str,
) -> str:
    """Build a canonical, Telegram-safe meal callback."""
    try:
        canonical_id = str(UUID(str(submission_id)))
    except (AttributeError, ValueError) as exc:
        raise ValueError("submission_id must be a valid UUID") from exc
    callback_data = f"meal:{action.value}:{canonical_id}"
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("meal callback data exceeds Telegram's byte limit")
    return callback_data


def meal_review_keyboard(
    submission_id: UUID | str,
) -> InlineKeyboard:
    """Return one-row Confirm and Cancel buttons for a staged meal."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Confirm",
                    "callback_data": _meal_callback_data(
                        MealCallbackAction.CONFIRM,
                        submission_id,
                    ),
                    "style": "success",
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": _meal_callback_data(
                        MealCallbackAction.CANCEL, submission_id
                    ),
                    "style": "danger",
                },
            ]
        ]
    }


def meal_continuation_keyboard(
    submission_id: UUID | str,
) -> InlineKeyboard:
    """Return one-row Add more and Done buttons after saving a meal."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Add more",
                    "callback_data": _meal_callback_data(
                        MealCallbackAction.ADD, submission_id
                    ),
                    "style": "primary",
                },
                {
                    "text": "✅ Done",
                    "callback_data": _meal_callback_data(
                        MealCallbackAction.DONE, submission_id
                    ),
                    "style": "success",
                },
            ]
        ]
    }


def _plan_chat_callback_data(session_id: UUID | str) -> str:
    """Build a canonical, Telegram-safe callback for ending a session."""
    try:
        canonical_id = str(UUID(str(session_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("session_id must be a valid UUID") from exc
    callback_data = f"plan_chat:end:{canonical_id}"
    if len(callback_data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        raise ValueError(
            "plan-chat callback data exceeds Telegram's byte limit"
        )
    return callback_data


def plan_chat_keyboard(session_id: UUID | str) -> InlineKeyboard:
    """Return the session-scoped plan-chat exit control."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "End planning",
                    "callback_data": _plan_chat_callback_data(session_id),
                    "style": "danger",
                }
            ]
        ]
    }


def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split plain text into chunks without losing newline boundaries."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_length:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:max_length])
            line = line[max_length:]
        if len(current) + len(line) > max_length:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or [""]


class TelegramAPI:
    """Small synchronous Telegram Bot API client."""

    def __init__(self, bot_token: str, request_timeout: float = 10.0) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.request_timeout = request_timeout

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.request_timeout
            ) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.error(
                "Telegram endpoint %s returned HTTP %s", endpoint, exc.code
            )
            raise TelegramAPIError(
                f"Telegram {endpoint} failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Telegram endpoint %s was unavailable", endpoint)
            raise TelegramAPIError(
                f"Telegram {endpoint} request failed"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.error("Telegram endpoint %s returned invalid JSON", endpoint)
            raise TelegramAPIError(
                f"Telegram {endpoint} returned invalid JSON"
            ) from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            status = (
                parsed.get("error_code", "unknown")
                if isinstance(parsed, dict)
                else "unknown"
            )
            logger.error(
                "Telegram endpoint %s returned API error %s", endpoint, status
            )
            raise TelegramAPIError(
                f"Telegram {endpoint} returned API error {status}"
            )
        return parsed

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Send plain text, stopping immediately if a chunk fails."""
        chunks = split_text(text)
        results: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            results.append(self._post("sendMessage", payload))
        return results

    def send_plan_chat(
        self,
        chat_id: int | str,
        text: str,
        session_id: UUID | str,
    ) -> list[dict[str, Any]]:
        """Send plan-chat text with its session-scoped exit control."""
        return self.send_message(
            chat_id,
            text,
            reply_markup=plan_chat_keyboard(session_id),
        )

    def set_my_commands(
        self,
        commands: Sequence[TelegramCommand] = BOT_COMMANDS,
    ) -> dict[str, Any]:
        """Register the supplied command menu with Telegram."""
        validated_commands = validate_commands(commands)
        return self._post(
            "setMyCommands",
            {
                "commands": [
                    command.to_payload() for command in validated_commands
                ]
            },
        )

    def send_profile_setup_prompt(
        self,
        chat_id: int | str,
        step: ConversationWorkflowStep,
        *,
        people_count: int | None = None,
        text: str | None = None,
    ) -> list[dict[str, Any]]:
        """Send the deterministic prompt for one profile setup step."""
        prompts = {
            ConversationWorkflowStep.AWAITING_PROFILE_FAMILY_NAME: (
                "What is your family name?"
            ),
            ConversationWorkflowStep.AWAITING_PROFILE_HOUSEHOLD_SIZE: (
                "How many people are in your household? Reply with a number "
                "from 1 to 20."
            ),
            ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS: (
                "Send one line for each household member using: name "
                "calories [protein] [fibre]. Calories are required; protein "
                "and fibre are optional grams/day. For example:\n"
                "Alex 2000 120 30\nSam 1800"
            ),
            ConversationWorkflowStep.AWAITING_PROFILE_CONSTRAINTS: (
                "List dietary constraints, one per line. Reply 'none' if "
                "there are no constraints."
            ),
            ConversationWorkflowStep.AWAITING_PROFILE_PREFERENCES: (
                "List dietary preferences, one per line. Reply 'none' if "
                "there are no preferences."
            ),
        }
        if text is not None:
            prompt = text
        elif step is ConversationWorkflowStep.AWAITING_PROFILE_MEMBERS:
            prompt = prompts[step]
            if people_count is not None:
                prompt = f"There are {people_count} people. " + prompt
        else:
            prompt = prompts.get(
                step,
                "Please continue setting up your profile.",
            )
        return self.send_message(
            chat_id,
            prompt,
            reply_markup=_profile_setup_keyboard(),
        )

    def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        """Configure Telegram to deliver updates to the deployed webhook."""
        if not url.strip():
            raise ValueError("Webhook URL must not be empty")
        if not secret_token.strip():
            raise ValueError("Webhook secret token must not be empty")
        return self._post(
            "setWebhook",
            {"url": url, "secret_token": secret_token},
        )

    def get_webhook_info(self) -> dict[str, Any]:
        """Return Telegram's current webhook status."""
        return self._post("getWebhookInfo", {})

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        """Acknowledge a Telegram callback spinner."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._post("answerCallbackQuery", payload)

    def send_profile(
        self, chat_id: int | str, profile: UserProfile
    ) -> list[dict[str, Any]]:
        """Render a saved profile with controls for deterministic editing."""
        constraints = _numbered_lines(
            profile_presentation_items(
                profile, ProfileEditCategory.DIETARY_CONSTRAINTS
            )
        )
        preferences = _numbered_lines(
            profile_presentation_items(
                profile,
                ProfileEditCategory.DIETARY_PREFERENCES,
                include_type_labels=False,
            )
        )
        family_items = profile_presentation_items(
            profile, ProfileEditCategory.FAMILY
        )
        lines = [
            f"Family name: {profile.name}",
            f"People count: {profile.people_count}",
            "Family members:",
            *(f"- {item.label}" for item in family_items),
            f"Dietary constraints:\n{constraints}",
            f"Dietary preferences:\n{preferences}",
        ]
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Amend profile",
                        "callback_data": "profile:root",
                    }
                ],
                [{"text": "Close", "callback_data": "profile:close"}],
            ]
        }
        return self.send_message(
            chat_id, "\n".join(lines), reply_markup=keyboard
        )

    def send_profile_root(self, chat_id: int | str) -> list[dict[str, Any]]:
        """Render the top-level profile amendment category menu."""
        categories = [
            (ProfileEditCategory.FAMILY, "Family"),
            (ProfileEditCategory.DIETARY_CONSTRAINTS, "Dietary constraints"),
            (ProfileEditCategory.DIETARY_PREFERENCES, "Dietary preferences"),
        ]
        keyboard = [
            [
                {
                    "text": label,
                    "callback_data": f"profile:category:{category.value}",
                }
            ]
            for category, label in categories
        ]
        keyboard.extend(
            [
                [{"text": "Done", "callback_data": "profile:done"}],
                [{"text": "Close", "callback_data": "profile:close"}],
            ]
        )
        return self.send_message(
            chat_id,
            "What would you like to amend?",
            reply_markup={"inline_keyboard": keyboard},
        )

    def send_profile_category(
        self, chat_id: int | str, category: ProfileEditCategory
    ) -> list[dict[str, Any]]:
        """Render operations valid for one profile category."""
        labels = {
            ProfileEditOperation.ADD: {
                ProfileEditCategory.FAMILY: "Add member",
                ProfileEditCategory.DIETARY_CONSTRAINTS: "Add constraint",
                ProfileEditCategory.DIETARY_PREFERENCES: "Add preference",
            },
            ProfileEditOperation.REMOVE: {
                ProfileEditCategory.FAMILY: "Remove member",
                ProfileEditCategory.DIETARY_CONSTRAINTS: "Remove constraint",
                ProfileEditCategory.DIETARY_PREFERENCES: "Remove preference",
            },
            ProfileEditOperation.CHANGE_CALORIES: {
                ProfileEditCategory.FAMILY: "Change calories",
            },
            ProfileEditOperation.CHANGE_PROTEIN: {
                ProfileEditCategory.FAMILY: "Change protein",
            },
            ProfileEditOperation.CHANGE_FIBRE: {
                ProfileEditCategory.FAMILY: "Change fibre",
            },
        }
        operations = [
            operation
            for operation in ProfileEditOperation
            if operation.is_valid_for(category)
        ]
        keyboard = [
            [
                {
                    "text": labels[operation][category],
                    "callback_data": (
                        f"profile:operation:{category.value}:{operation.value}"
                    ),
                }
            ]
            for operation in operations
        ]
        keyboard.extend(
            [
                [{"text": "Back", "callback_data": "profile:back"}],
                [{"text": "Done", "callback_data": "profile:done"}],
            ]
        )
        return self.send_message(
            chat_id,
            f"Choose an operation for {category.value.replace('_', ' ')}:",
            reply_markup={"inline_keyboard": keyboard},
        )

    def send_profile_operation(
        self,
        chat_id: int | str,
        category: ProfileEditCategory,
        operation: ProfileEditOperation,
        profile: UserProfile | None = None,
    ) -> list[dict[str, Any]]:
        """Render one guided input prompt and its navigation controls."""
        if not operation.is_valid_for(category):
            raise ValueError("operation is invalid for its category")
        if operation is ProfileEditOperation.REMOVE and profile is not None:
            items = profile_presentation_items(profile, category)
            if not items:
                item_name = {
                    ProfileEditCategory.FAMILY: "family members",
                    ProfileEditCategory.DIETARY_CONSTRAINTS: (
                        "dietary constraints"
                    ),
                    ProfileEditCategory.DIETARY_PREFERENCES: (
                        "dietary preferences"
                    ),
                }[category]
                return self.send_message(
                    chat_id,
                    f"There are no {item_name} to remove.",
                    reply_markup=_profile_navigation_keyboard(),
                )
            lines = [
                f"Select a {category.value.replace('_', ' ')} to remove:",
                _numbered_lines(items),
            ]
            return self.send_message(
                chat_id,
                "\n".join(lines),
                reply_markup=_profile_removal_keyboard(
                    category, profile.profile_revision, len(items)
                ),
            )
        prompts = {
            (
                ProfileEditCategory.FAMILY,
                ProfileEditOperation.ADD,
            ): "Send the member's name and calorie target. Use "
            "name calories (for example: John 1500) or include both "
            "optional targets as name calories protein fibre (for example: "
            "John 2000 120 30).",
            (
                ProfileEditCategory.FAMILY,
                ProfileEditOperation.REMOVE,
            ): "Send the exact member name to remove.",
            (
                ProfileEditCategory.FAMILY,
                ProfileEditOperation.CHANGE_CALORIES,
            ): "Send the member's name and new calorie target, for example: "
            "John 1500.",
            (
                ProfileEditCategory.FAMILY,
                ProfileEditOperation.CHANGE_PROTEIN,
            ): "Send the member's name and new protein target in grams, or "
            "use 'none' to clear it, for example: John 120 or John none.",
            (
                ProfileEditCategory.FAMILY,
                ProfileEditOperation.CHANGE_FIBRE,
            ): "Send the member's name and new fibre target in grams, or "
            "use 'none' to clear it, for example: John 30 or John none.",
        }
        item_name = {
            ProfileEditCategory.DIETARY_CONSTRAINTS: "constraint",
            ProfileEditCategory.DIETARY_PREFERENCES: "preference",
        }.get(category, "item")
        prompt = prompts.get(
            (category, operation),
            f"Send one non-empty {item_name} to "
            f"{'add' if operation is ProfileEditOperation.ADD else 'remove'}.",
        )
        keyboard = _profile_navigation_keyboard()
        return self.send_message(chat_id, prompt, reply_markup=keyboard)

    def send_meal_review(
        self,
        chat_id: int | str,
        submitted_text: str,
        submission_id: UUID | str,
    ) -> list[dict[str, Any]]:
        """Echo a submitted meal and ask whether it should be saved."""
        text = (
            "Review this meal submission:\n"
            f"{submitted_text}\n\n"
            "Confirm to save it or cancel."
        )
        return self.send_message(
            chat_id,
            text,
            reply_markup=meal_review_keyboard(submission_id),
        )

    def send_meal_saved(
        self,
        chat_id: int | str,
        meal_description: str,
        submission_id: UUID | str,
    ) -> list[dict[str, Any]]:
        """Report a saved meal and offer another submission or completion."""
        return self.send_message(
            chat_id,
            f"✅ Meal saved: {meal_description}",
            reply_markup=meal_continuation_keyboard(submission_id),
        )
