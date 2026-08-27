"""Telegram Bot API client with safe plain-text formatting."""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from meal_planner.models.schemas import (
    BatchMealRole,
    BatchRule,
    GrocerySection,
    MealCallbackAction,
    MealOutcome,
    PlannedBatchLink,
    PlannedMeal,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
    WeeklyPlan,
)
from meal_planner.telegram.commands import (
    BOT_COMMANDS,
    TelegramCommand,
    validate_commands,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


class TelegramAPIError(RuntimeError):
    """A Telegram request failed or returned an unsuccessful response."""


InlineKeyboard = dict[str, list[list[dict[str, str]]]]


def _format_nutrient_target(label: str, target: int | None) -> str:
    """Format an optional daily nutrient target for profile display."""
    if target is None:
        return f"{label}: not set"
    return f"{label}: {target} g/day"


def _meal_callback_data(
    action: MealCallbackAction,
    submission_id: UUID | str,
    batch_role: BatchMealRole | None = None,
) -> str:
    """Build a canonical, Telegram-safe callback for a meal submission."""
    try:
        canonical_id = str(UUID(str(submission_id)))
    except (AttributeError, ValueError) as exc:
        raise ValueError("submission_id must be a valid UUID") from exc
    role_suffix = f":{batch_role.value}" if batch_role is not None else ""
    callback_data = f"meal:{action.value}:{canonical_id}{role_suffix}"
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("meal callback data exceeds Telegram's byte limit")
    return callback_data


def meal_review_keyboard(
    submission_id: UUID | str,
    batch_link: PlannedBatchLink | None = None,
) -> InlineKeyboard:
    """Return one-row Confirm and Cancel buttons for a staged meal."""
    confirm_text = "✅ Confirm"
    confirm_role: BatchMealRole | None = None
    if batch_link is not None:
        confirm_role = batch_link.role
        confirm_text = f"✅ Confirm {batch_link.role.value}"
    return {
        "inline_keyboard": [
            [
                {
                    "text": confirm_text,
                    "callback_data": _meal_callback_data(
                        MealCallbackAction.CONFIRM,
                        submission_id,
                        confirm_role,
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
        constraints = (
            ", ".join(item.source_text for item in profile.dietary_constraints)
            or "None"
        )
        preferences = (
            ", ".join(item.source_text for item in profile.dietary_preferences)
            or "None"
        )
        batch_rule_lines: list[str] = []
        for rule in profile.batch_rules:
            preparation = ", ".join(
                item.value for item in rule.preparation_meal_types
            )
            reuse = ", ".join(item.value for item in rule.reuse_meal_types)
            batch_rule_lines.append(
                f"{rule.source_text} (prepare {preparation}; "
                f"reuse {reuse}; {rule.total_yield} meals)"
            )
        batch_rules = "; ".join(batch_rule_lines) or "None"
        lines = [
            f"Family name: {profile.name}",
            f"People count: {profile.people_count}",
            "Family members:",
            *(
                f"- {member.name} ({member.calorie_target} kcal/day, "
                f"{_format_nutrient_target('protein', member.protein_target)}, "
                f"{_format_nutrient_target('fibre', member.fibre_target)})"
                for member in profile.family_members
            ),
            f"Dietary constraints: {constraints}",
            f"Dietary preferences: {preferences}",
            f"Batch rules: {batch_rules}",
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
    ) -> list[dict[str, Any]]:
        """Render one guided input prompt and its navigation controls."""
        if not operation.is_valid_for(category):
            raise ValueError("operation is invalid for its category")
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
        keyboard = {
            "inline_keyboard": [
                [{"text": "Back", "callback_data": "profile:back"}],
                [{"text": "Done", "callback_data": "profile:done"}],
                [{"text": "Close", "callback_data": "profile:close"}],
            ]
        }
        return self.send_message(chat_id, prompt, reply_markup=keyboard)

    def send_profile_rule_review(
        self,
        chat_id: int | str,
        category: ProfileEditCategory,
        source_text: str,
        rules: list[Any],
        token: str | None,
    ) -> list[dict[str, Any]]:
        """Show an interpreted dietary rule before durable profile write."""
        if not token:
            raise ValueError(
                "rule review requires a supported category and token"
            )
        lines = [
            "Review this dietary profile change:",
            source_text,
            "",
            "Meaning:",
        ]
        for rule in rules:
            if isinstance(rule, BatchRule):
                preparation = ", ".join(
                    meal_type.value for meal_type in rule.preparation_meal_types
                )
                reuse = ", ".join(
                    meal_type.value for meal_type in rule.reuse_meal_types
                )
                lines.append(
                    f"- Batch: prepare for {preparation}; reuse for {reuse}; "
                    f"{rule.total_yield} meals"
                )
            elif hasattr(rule, "forbidden_terms"):
                lines.append("- Exclude: " + ", ".join(rule.forbidden_terms))
            else:
                scope = (
                    f" for {rule.meal_type.value}"
                    if rule.meal_type is not None
                    else ""
                )
                lines.append(
                    f"- {rule.operator.value} {rule.count} of "
                    f"{' or '.join(rule.foods_any_of)}{scope} "
                    f"({rule.strength.value})"
                )
        return self.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm",
                            "callback_data": f"profile:confirm:{token}",
                        },
                        {
                            "text": "Cancel",
                            "callback_data": f"profile:cancel:{token}",
                        },
                    ],
                    [{"text": "Back", "callback_data": "profile:back"}],
                ]
            },
        )

    def send_meal_review(
        self,
        chat_id: int | str,
        submitted_text: str,
        submission_id: UUID | str,
        *,
        batch_link: PlannedBatchLink | None = None,
    ) -> list[dict[str, Any]]:
        """Echo a submitted meal and ask whether it should be saved."""
        batch_prompt = ""
        if batch_link is not None:
            batch_prompt = (
                "\n\nThis matches a planned batch "
                f"{batch_link.role.value}. Confirm it as the "
                f"batch {batch_link.role.value} or cancel."
            )
        text = (
            "Review this meal submission:\n"
            f"{submitted_text}\n\n"
            "Confirm to save it or cancel."
            f"{batch_prompt}"
        )
        return self.send_message(
            chat_id,
            text,
            reply_markup=meal_review_keyboard(submission_id, batch_link),
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

    def send_plan(
        self, chat_id: int | str, plan: WeeklyPlan
    ) -> list[dict[str, Any]]:
        batch_yields: dict[str, int] = {}
        for plan_day in plan.days:
            for meal in plan_day.meals:
                if meal.batch_link is None:
                    continue
                link = meal.batch_link
                inferred_yield = link.total_yield or link.portion
                batch_yields[link.batch_id] = max(
                    2, batch_yields.get(link.batch_id, 0), inferred_yield
                )
        lines = [
            f"Meal Plan (starting {plan.week_start_date})",
            f"Status: {plan.status.value}",
            "",
        ]
        for plan_day in plan.days:
            lines.append(f"Day {plan_day.day}")
            for meal in plan_day.meals:
                outcome = (
                    ""
                    if meal.outcome is MealOutcome.UNREPORTED
                    else f" [{meal.outcome.value}]"
                )
                batch_label = ""
                if meal.batch_link is not None:
                    if meal.batch_link.role is BatchMealRole.PREPARATION:
                        batch_label = (
                            f" [Batch preparation; makes "
                            f"{batch_yields[meal.batch_link.batch_id]} meals]"
                        )
                    else:
                        batch_label = " [Batch leftover]"
                lines.append(
                    f"• {meal.meal_type.value.capitalize()}: {meal.name} "
                    f"({meal.est_calories} kcal){batch_label}{outcome}"
                )
            lines.append("")
        return self.send_message(chat_id, "\n".join(lines).strip())

    def send_grocery_list(
        self, chat_id: int | str, sections: list[GrocerySection]
    ) -> list[dict[str, Any]]:
        if not sections:
            return self.send_message(chat_id, "Your grocery list is empty.")
        lines = ["🛒 Grocery List", ""]
        for section in sections:
            lines.append(section.name)
            lines.extend(f"• {item}" for item in section.items)
            lines.append("")
        return self.send_message(chat_id, "\n".join(lines).strip())

    def send_meal_checkin(
        self,
        chat_id: int | str,
        meals_today: list[PlannedMeal],
        *,
        week_start: str,
        day: int,
    ) -> list[dict[str, Any]]:
        if not meals_today:
            return self.send_message(
                chat_id, "No meals planned for today to check in on."
            )
        lines = ["📋 Daily Meal Check-in", "How did today's meals go?", ""]
        keyboard: list[list[dict[str, str]]] = []
        for meal in meals_today:
            meal_type = meal.meal_type.value
            lines.append(f"• {meal_type.capitalize()}: {meal.name}")
            keyboard.append(
                [
                    {
                        "text": f"✅ {meal_type.capitalize()}",
                        "callback_data": (
                            f"checkin:{week_start}:{day}:{meal_type}:cooked"
                        ),
                    },
                    {
                        "text": f"❌ Skip {meal_type.capitalize()}",
                        "callback_data": (
                            f"checkin:{week_start}:{day}:{meal_type}:skipped"
                        ),
                    },
                    {
                        "text": f"🔄 Swap {meal_type.capitalize()}",
                        "callback_data": (
                            f"checkin:{week_start}:{day}:{meal_type}:swapped"
                        ),
                    },
                ]
            )
        return self.send_message(
            chat_id,
            "\n".join(lines).strip(),
            reply_markup={"inline_keyboard": keyboard},
        )
