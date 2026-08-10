"""Telegram update message router."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RouteType(str, Enum):
    """Types of update routes."""

    COMMAND = "command"
    CALLBACK = "callback"
    CONVERSATIONAL = "conversational"
    UNKNOWN = "unknown"


class RouteResult(BaseModel):
    """Routing decision result from parsing a Telegram update."""

    route_type: RouteType
    chat_id: int | str | None = None
    user_id: str | None = None
    command: str | None = None
    args: str | None = None
    text: str | None = None
    callback_data: str | None = None
    callback_query_id: str | None = None
    raw_update: dict[str, Any] = Field(default_factory=dict)


SUPPORTED_COMMANDS = {
    "start",
    "profile",
    "plan",
    "grocery",
    "today",
    "submit_meals",
}


def route_update(update: dict[str, Any]) -> RouteResult:
    """Parse a Telegram update dictionary and determine routing decision."""
    if not isinstance(update, dict):
        return RouteResult(route_type=RouteType.UNKNOWN, raw_update={})

    if "message" in update and isinstance(update["message"], dict):
        msg = update["message"]
        from_user = msg.get("from", {})
        chat = msg.get("chat", {})

        user_id = str(from_user.get("id")) if "id" in from_user else None
        chat_id = chat.get("id") if "id" in chat else user_id

        text = msg.get("text")
        if not text or not isinstance(text, str):
            return RouteResult(
                route_type=RouteType.UNKNOWN,
                chat_id=chat_id,
                user_id=user_id,
                raw_update=update,
            )

        text = text.strip()
        if text.startswith("/"):
            parts = text[1:].split(maxsplit=1)
            cmd_part = parts[0].split("@")[0].lower()
            args_part = parts[1].strip() if len(parts) > 1 else ""

            return RouteResult(
                route_type=RouteType.COMMAND,
                chat_id=chat_id,
                user_id=user_id,
                command=cmd_part,
                args=args_part,
                text=text,
                raw_update=update,
            )

        return RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            raw_update=update,
        )

    if "callback_query" in update and isinstance(
        update["callback_query"], dict
    ):
        cb = update["callback_query"]
        from_user = cb.get("from", {})
        msg = cb.get("message", {})
        chat = msg.get("chat", {}) if isinstance(msg, dict) else {}

        user_id = str(from_user.get("id")) if "id" in from_user else None
        chat_id = chat.get("id") if "id" in chat else user_id

        cb_data = cb.get("data")
        cb_id = cb.get("id")

        return RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=chat_id,
            user_id=user_id,
            callback_data=str(cb_data) if cb_data is not None else None,
            callback_query_id=str(cb_id) if cb_id is not None else None,
            raw_update=update,
        )

    return RouteResult(route_type=RouteType.UNKNOWN, raw_update=update)
