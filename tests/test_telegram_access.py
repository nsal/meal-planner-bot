"""Tests for Telegram user and chat authorization."""

import pytest

from meal_planner.router import RouteResult, RouteType
from meal_planner.telegram.access import (
    AccessReason,
    TelegramAccessPolicy,
)


@pytest.fixture
def policy() -> TelegramAccessPolicy:
    """Return a policy with one authorized Telegram user."""
    return TelegramAccessPolicy(frozenset({"123"}))


def _route(**values: object) -> RouteResult:
    defaults: dict[str, object] = {
        "route_type": RouteType.COMMAND,
        "user_id": "123",
        "chat_id": 123,
        "chat_type": "private",
    }
    defaults.update(values)
    return RouteResult.model_validate(defaults)


def test_policy_allows_matching_private_user(
    policy: TelegramAccessPolicy,
) -> None:
    """An allowlisted user can act in the matching private chat."""
    decision = policy.evaluate(_route())

    assert decision.allowed
    assert decision.reason is AccessReason.ALLOWED


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"user_id": "999"}, AccessReason.USER_NOT_ALLOWED),
        ({"chat_type": "group"}, AccessReason.NON_PRIVATE_CHAT),
        ({"chat_type": "supergroup"}, AccessReason.NON_PRIVATE_CHAT),
        ({"chat_type": "channel"}, AccessReason.NON_PRIVATE_CHAT),
        ({"chat_id": "999"}, AccessReason.CHAT_USER_MISMATCH),
        ({"user_id": None}, AccessReason.MISSING_USER_ID),
        ({"chat_type": None}, AccessReason.MISSING_CHAT_TYPE),
        ({"chat_id": None}, AccessReason.MISSING_CHAT_ID),
    ],
)
def test_policy_denies_invalid_identity(
    policy: TelegramAccessPolicy,
    values: dict[str, object],
    reason: AccessReason,
) -> None:
    """Every failed identity or chat requirement is denied stably."""
    decision = policy.evaluate(_route(**values))

    assert not decision.allowed
    assert decision.reason is reason


def test_policy_normalizes_private_chat_id_to_text(
    policy: TelegramAccessPolicy,
) -> None:
    """Numeric and textual forms of the same private chat ID match."""
    decision = policy.evaluate(_route(chat_id="123"))

    assert decision.allowed


def test_policy_copies_allowlist_immutably() -> None:
    """Policy construction cannot retain a mutable caller-owned set."""
    configured = {"123"}
    policy = TelegramAccessPolicy(configured)  # type: ignore[arg-type]
    configured.add("999")

    assert policy.allowed_user_ids == frozenset({"123"})
