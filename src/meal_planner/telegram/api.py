"""Telegram Bot API helper for sending formatted messages and keyboards."""

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from meal_planner.models.schemas import (
    GrocerySection,
    PlannedMeal,
    WeeklyPlan,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks not exceeding max_length."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    lines = text.split("\n")
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        if len(line) > max_length:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
            continue

        added_len = len(line) + (1 if current_chunk else 0)
        if current_len + added_len > max_length:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += added_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


class TelegramAPI:
    """Helper client for Telegram Bot API HTTP endpoints."""

    def __init__(self, bot_token: str) -> None:
        """Initialize TelegramAPI with bot token."""
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform HTTP POST request to Telegram Bot API."""
        url = f"{self.base_url}/{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp_data = resp.read().decode("utf-8")
                return json.loads(resp_data)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8")
            logger.error(
                "Telegram API HTTPError: %s, Body: %s", err.code, err_body
            )
            try:
                return json.loads(err_body)  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "error_code": err.code,
                    "description": str(err),
                }
        except urllib.error.URLError as err:
            logger.error("Telegram API URLError: %s", err.reason)
            return {"ok": False, "description": str(err.reason)}

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Send message text to specified chat, splitting if >4096 chars."""
        chunks = split_text(text)
        results: list[dict[str, Any]] = []

        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
            }
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup

            res = self._post("sendMessage", payload)
            results.append(res)

        return results

    def send_plan(
        self, chat_id: int | str, plan: WeeklyPlan
    ) -> list[dict[str, Any]]:
        """Format and send weekly plan to Telegram user."""
        lines = [
            f"*Weekly Meal Plan (Week of {plan.week_start})*",
            f"Status: _{plan.status}_",
            "",
        ]
        for day_plan in plan.days:
            lines.append(f"*Day {day_plan.day}*")
            if not day_plan.meals:
                lines.append("  No meals planned.")
            for meal in day_plan.meals:
                cooked = " [Cooked]" if meal.was_cooked else ""
                lines.append(
                    f"• *{meal.meal_type.capitalize()}*: {meal.name} "
                    f"({meal.est_calories} kcal){cooked}"
                )
                if meal.ingredients:
                    ing_str = ", ".join(
                        f"{ing.item} ({ing.amount})" if ing.amount else ing.item
                        for ing in meal.ingredients
                    )
                    lines.append(f"  _Ingredients:_ {ing_str}")
            lines.append("")

        formatted_text = "\n".join(lines).strip()
        return self.send_message(chat_id, formatted_text)

    def send_grocery_list(
        self, chat_id: int | str, sections: list[GrocerySection]
    ) -> list[dict[str, Any]]:
        """Format and send grocery list to Telegram user."""
        if not sections:
            return self.send_message(chat_id, "Your grocery list is empty.")

        lines = ["🛒 *Grocery List*", ""]
        for sec in sections:
            lines.append(f"*{sec.name}*")
            if not sec.items:
                lines.append("  (No items)")
            for item in sec.items:
                lines.append(f"• {item}")
            lines.append("")

        formatted_text = "\n".join(lines).strip()
        return self.send_message(chat_id, formatted_text)

    def send_meal_checkin(
        self,
        chat_id: int | str,
        meals_today: list[PlannedMeal],
        day: int = 1,
    ) -> list[dict[str, Any]]:
        """Send daily meal check-in message with inline keyboard buttons."""
        if not meals_today:
            return self.send_message(
                chat_id, "No meals planned for today to check in on."
            )

        lines = ["📋 *Daily Meal Check-in*", "How did today's meals go?", ""]
        keyboard_inline: list[list[dict[str, str]]] = []

        for meal in meals_today:
            m_type = meal.meal_type
            cooked_str = " (Cooked)" if meal.was_cooked else ""
            lines.append(f"• *{m_type.capitalize()}*: {meal.name}{cooked_str}")

            row = [
                {
                    "text": f"✅ {m_type.capitalize()}",
                    "callback_data": f"checkin:{day}:{m_type}:cooked",
                },
                {
                    "text": f"❌ Skip {m_type.capitalize()}",
                    "callback_data": f"checkin:{day}:{m_type}:skipped",
                },
                {
                    "text": f"🔄 Swap {m_type.capitalize()}",
                    "callback_data": f"checkin:{day}:{m_type}:swapped",
                },
            ]
            keyboard_inline.append(row)

        reply_markup = {"inline_keyboard": keyboard_inline}
        formatted_text = "\n".join(lines).strip()
        return self.send_message(
            chat_id, formatted_text, reply_markup=reply_markup
        )
