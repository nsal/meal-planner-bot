"""Telegram update message router."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from meal_planner.models.schemas import (
    MealOutcome,
    MealType,
    ProfileEditCategory,
    ProfileEditOperation,
)
from meal_planner.telegram.commands import BOT_COMMANDS

MAX_CALLBACK_DATA_BYTES = 64


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
    chat_type: str | None = None
    user_id: str | None = None
    command: str | None = None
    args: str | None = None
    text: str | None = None
    callback_data: str | None = None
    callback_query_id: str | None = None
    raw_update: dict[str, Any] = Field(default_factory=dict)


SUPPORTED_COMMANDS = frozenset(command.name for command in BOT_COMMANDS)


class CheckinCallback(BaseModel):
    """Validated, plan-specific check-in callback payload."""

    week_start: str
    day: int = Field(ge=1, le=7)
    meal_type: MealType
    outcome: MealOutcome


class ProfileCallbackAction(str, Enum):
    """Navigation actions supported by the profile edit menu."""

    ROOT = "root"
    CATEGORY = "category"
    OPERATION = "operation"
    BACK = "back"
    DONE = "done"
    CLOSE = "close"


class ProfileCallback(BaseModel):
    """Validated callback payload for deterministic profile editing."""

    action: ProfileCallbackAction
    category: ProfileEditCategory | None = None
    operation: ProfileEditOperation | None = None

    @model_validator(mode="after")
    def validate_payload_shape(self) -> "ProfileCallback":
        """Require exactly the fields allowed by each callback action."""
        if self.action is ProfileCallbackAction.CATEGORY:
            if self.category is None or self.operation is not None:
                raise ValueError("category callbacks require only a category")
            return self
        if self.action is ProfileCallbackAction.OPERATION:
            if self.category is None or self.operation is None:
                raise ValueError(
                    "operation callbacks require category and operation"
                )
            if not self.operation.is_valid_for(self.category):
                raise ValueError("operation is invalid for its category")
            return self
        if self.category is not None or self.operation is not None:
            raise ValueError("navigation callbacks cannot contain selections")
        return self


def parse_checkin_callback(data: str) -> CheckinCallback | None:
    """Parse the exact callback format accepted by check-in handlers."""
    if len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        return None
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != "checkin":
        return None
    try:
        from datetime import date

        date.fromisoformat(parts[1])
        return CheckinCallback(
            week_start=parts[1],
            day=int(parts[2]),
            meal_type=MealType(parts[3]),
            outcome=MealOutcome(parts[4]),
        )
    except TypeError, ValueError:
        return None


def parse_profile_callback(data: str) -> ProfileCallback | None:
    """Parse the exact compact callback format for profile editing."""
    if (
        not isinstance(data, str)
        or len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES
    ):
        return None

    parts = data.split(":")
    if not parts or parts[0] != "profile":
        return None

    try:
        action = ProfileCallbackAction(parts[1])
        if action in {
            ProfileCallbackAction.ROOT,
            ProfileCallbackAction.BACK,
            ProfileCallbackAction.DONE,
            ProfileCallbackAction.CLOSE,
        }:
            if len(parts) != 2:
                return None
            return ProfileCallback(action=action)
        if action is ProfileCallbackAction.CATEGORY:
            if len(parts) != 3:
                return None
            return ProfileCallback(
                action=action,
                category=ProfileEditCategory(parts[2]),
            )
        if len(parts) != 4:
            return None
        return ProfileCallback(
            action=action,
            category=ProfileEditCategory(parts[2]),
            operation=ProfileEditOperation(parts[3]),
        )
    except IndexError, TypeError, ValueError, ValidationError:
        return None


def route_update(update: dict[str, Any]) -> RouteResult:
    """Parse a Telegram update dictionary and determine routing decision."""
    if not isinstance(update, dict):
        return RouteResult(route_type=RouteType.UNKNOWN, raw_update={})

    if "message" in update and isinstance(update["message"], dict):
        msg = update["message"]
        from_user = msg.get("from", {})
        chat = msg.get("chat", {})

        user_id = (
            str(from_user.get("id"))
            if isinstance(from_user, dict) and "id" in from_user
            else None
        )
        chat_id = (
            chat.get("id") if isinstance(chat, dict) and "id" in chat else None
        )
        chat_type = (
            chat.get("type")
            if isinstance(chat, dict) and isinstance(chat.get("type"), str)
            else None
        )

        text = msg.get("text")
        if not text or not isinstance(text, str):
            return RouteResult(
                route_type=RouteType.UNKNOWN,
                chat_id=chat_id,
                chat_type=chat_type,
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
                chat_type=chat_type,
                user_id=user_id,
                command=cmd_part,
                args=args_part,
                text=text,
                raw_update=update,
            )

        return RouteResult(
            route_type=RouteType.CONVERSATIONAL,
            chat_id=chat_id,
            chat_type=chat_type,
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

        user_id = (
            str(from_user.get("id"))
            if isinstance(from_user, dict) and "id" in from_user
            else None
        )
        chat_id = (
            chat.get("id") if isinstance(chat, dict) and "id" in chat else None
        )
        chat_type = (
            chat.get("type")
            if isinstance(chat, dict) and isinstance(chat.get("type"), str)
            else None
        )

        cb_data = cb.get("data")
        cb_id = cb.get("id")

        return RouteResult(
            route_type=RouteType.CALLBACK,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            callback_data=str(cb_data) if cb_data is not None else None,
            callback_query_id=str(cb_id) if cb_id is not None else None,
            raw_update=update,
        )

    return RouteResult(route_type=RouteType.UNKNOWN, raw_update=update)
