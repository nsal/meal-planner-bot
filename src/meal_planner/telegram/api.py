"""Telegram Bot API client with safe plain-text formatting."""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from meal_planner.models.schemas import (
    GrocerySection,
    MealOutcome,
    PlannedMeal,
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

    def send_plan(
        self, chat_id: int | str, plan: WeeklyPlan
    ) -> list[dict[str, Any]]:
        lines = [
            f"Weekly Meal Plan (week of {plan.week_start_date})",
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
                lines.append(
                    f"• {meal.meal_type.value.capitalize()}: {meal.name} "
                    f"({meal.est_calories} kcal){outcome}"
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
