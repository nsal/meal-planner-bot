"""User-level access policy for Telegram updates."""

from dataclasses import dataclass
from enum import Enum

from meal_planner.router import RouteResult


class AccessReason(str, Enum):
    """Stable reasons for allowing or denying an update."""

    ALLOWED = "allowed"
    MISSING_USER_ID = "missing_user_id"
    USER_NOT_ALLOWED = "user_not_allowed"
    MISSING_CHAT_TYPE = "missing_chat_type"
    NON_PRIVATE_CHAT = "non_private_chat"
    MISSING_CHAT_ID = "missing_chat_id"
    CHAT_USER_MISMATCH = "chat_user_mismatch"


@dataclass(frozen=True)
class AccessDecision:
    """Result of evaluating one routed Telegram update."""

    allowed: bool
    reason: AccessReason


@dataclass(frozen=True)
class TelegramAccessPolicy:
    """Authorize configured users only in their matching private chats."""

    allowed_user_ids: frozenset[str]

    def __post_init__(self) -> None:
        """Copy any input collection into an immutable set."""
        object.__setattr__(
            self, "allowed_user_ids", frozenset(self.allowed_user_ids)
        )

    def evaluate(self, route: RouteResult) -> AccessDecision:
        """Return a stable authorization decision for a routed update."""
        if not route.user_id:
            return AccessDecision(False, AccessReason.MISSING_USER_ID)
        if route.user_id not in self.allowed_user_ids:
            return AccessDecision(False, AccessReason.USER_NOT_ALLOWED)
        if route.chat_type is None:
            return AccessDecision(False, AccessReason.MISSING_CHAT_TYPE)
        if route.chat_type != "private":
            return AccessDecision(False, AccessReason.NON_PRIVATE_CHAT)
        if route.chat_id is None:
            return AccessDecision(False, AccessReason.MISSING_CHAT_ID)
        if str(route.chat_id) != route.user_id:
            return AccessDecision(False, AccessReason.CHAT_USER_MISMATCH)
        return AccessDecision(True, AccessReason.ALLOWED)
