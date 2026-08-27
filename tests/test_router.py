"""Telegram update and callback routing tests."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from meal_planner.models.schemas import (
    BatchMealRole,
    MealOutcome,
    MealType,
    ProfileEditCategory,
    ProfileEditOperation,
)
from meal_planner.router import (
    MAX_PROFILE_REMOVAL_INDEX,
    MAX_PROFILE_REVISION,
    MealCallbackAction,
    ProfileCallback,
    ProfileCallbackAction,
    RouteType,
    parse_checkin_callback,
    parse_meal_callback,
    parse_meal_input,
    parse_profile_callback,
    route_update,
)


def test_router_imports_before_any_telegram_modules() -> None:
    """Importing the router first works in a clean Python interpreter."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "assert not any(\n"
                "    name == 'meal_planner.telegram' or "
                "name.startswith('meal_planner.telegram.')\n"
                "    for name in sys.modules\n"
                ")\n"
                "import meal_planner.router\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr


def test_route_command_and_conversation() -> None:
    command = route_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 2},
                "text": "/plan@bot next week",
            }
        }
    )
    assert command.route_type is RouteType.COMMAND
    assert command.chat_type is None
    assert command.command == "plan"
    assert command.args == "next week"
    conversation = route_update(
        {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 2},
                "text": "swap lunch",
            }
        }
    )
    assert conversation.route_type is RouteType.CONVERSATIONAL


def test_route_callback_preserves_query_id() -> None:
    routed = route_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 1},
                "message": {"chat": {"id": 2}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        }
    )
    assert routed.route_type is RouteType.CALLBACK
    assert routed.callback_query_id == "callback-1"
    assert routed.chat_type is None


@pytest.mark.parametrize("chat_type", ["private", "group", "supergroup"])
def test_route_message_preserves_chat_type(chat_type: str) -> None:
    """Message routes retain Telegram's source chat type."""
    routed = route_update(
        {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": chat_type},
                "text": "/start",
            }
        }
    )

    assert routed.chat_type == chat_type


def test_route_callback_preserves_private_chat_type() -> None:
    """Callback routes retain the callback message's chat type."""
    routed = route_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        }
    )

    assert routed.chat_type == "private"


@pytest.mark.parametrize(
    "update",
    [
        {
            "message": {
                "from": {"id": 123},
                "chat": {"type": "private"},
                "text": "/start",
            }
        },
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {"chat": {"type": "private"}},
                "data": "checkin:2026-08-10:1:lunch:cooked",
            }
        },
    ],
)
def test_route_missing_chat_id_remains_missing(
    update: dict[str, object],
) -> None:
    """Do not synthesize a private chat ID from the sender ID."""
    assert route_update(update).chat_id is None


@pytest.mark.parametrize(
    "update",
    [
        {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": 42},
                "text": "/start",
            }
        },
        {
            "callback_query": {
                "from": {"id": 123},
                "message": {"chat": {"id": 123, "type": None}},
                "data": "callback",
            }
        },
        {
            "callback_query": {
                "from": {"id": 123},
                "message": "malformed",
                "data": "callback",
            }
        },
    ],
)
def test_route_malformed_chat_type_fails_open_to_no_type(
    update: dict[str, object],
) -> None:
    """Malformed or absent Telegram chat context is represented as missing."""
    assert route_update(update).chat_type is None


def test_parse_plan_specific_checkin_callback() -> None:
    callback = parse_checkin_callback("checkin:2026-08-10:7:dinner:swapped")
    assert callback is not None
    assert callback.week_start == "2026-08-10"
    assert callback.day == 7
    assert callback.meal_type is MealType.DINNER
    assert callback.outcome is MealOutcome.SWAPPED


@pytest.mark.parametrize(
    ("submitted", "expected_date", "expected_type", "expected_description"),
    [
        (
            "today, LUNCH, rice, beans, and salsa",
            date(2026, 8, 22),
            MealType.LUNCH,
            "rice, beans, and salsa",
        ),
        (
            "  YESTERDAY , breakfast , pancakes  ",
            date(2026, 8, 21),
            MealType.BREAKFAST,
            "pancakes",
        ),
        (
            "2026-08-16, Dinner, soup",
            date(2026, 8, 16),
            MealType.DINNER,
            "soup",
        ),
    ],
)
def test_parse_meal_input_uses_first_two_commas_and_normalizes_values(
    submitted: str,
    expected_date: date,
    expected_type: MealType,
    expected_description: str,
) -> None:
    result = parse_meal_input(submitted, date(2026, 8, 22))

    assert result.is_valid
    assert result.draft is not None
    assert result.draft.date == expected_date
    assert result.draft.meal_type is expected_type
    assert result.draft.description == expected_description
    assert result.errors == ()


@pytest.mark.parametrize(
    "submitted",
    [
        "today, lunch",
        "today lunch, lunch, soup",
        "today, lunch,",
        ", lunch, soup",
        "today, , soup",
        "today, lunch,   ",
    ],
)
def test_parse_meal_input_reports_field_specific_errors(
    submitted: str,
) -> None:
    result = parse_meal_input(submitted, date(2026, 8, 22))

    assert not result.is_valid
    assert result.draft is None
    assert result.errors
    assert all(isinstance(error, str) for error in result.errors)


def test_parse_meal_input_accepts_maximum_description_length() -> None:
    description = "x" * 500

    result = parse_meal_input(f"today, lunch, {description}", date(2026, 8, 22))

    assert result.is_valid
    assert result.draft is not None
    assert result.draft.description == description


def test_parse_meal_input_reports_overlong_description_error() -> None:
    description = "x" * 501

    result = parse_meal_input(f"today, lunch, {description}", date(2026, 8, 22))

    assert not result.is_valid
    assert result.draft is None
    assert result.errors == ("description must be 500 characters or fewer",)


@pytest.mark.parametrize(
    ("submitted", "expected_message"),
    [
        ("2026-08-22, lunch, soup", ""),
        ("2026-08-16, lunch, soup", ""),
        ("2026-08-15, lunch, soup", "date must be within the last 7 days"),
        ("2026-08-23, lunch, soup", "date cannot be in the future"),
        ("2026/08/22, lunch, soup", "date must be YYYY-MM-DD"),
        ("2026-2-2, lunch, soup", "date must be YYYY-MM-DD"),
        ("2026-02-30, lunch, soup", "date must be a real calendar date"),
        (
            "today, brunch, soup",
            "meal type must be breakfast, lunch, snack, or dinner",
        ),
    ],
)
def test_parse_meal_input_validates_dates_and_meal_types(
    submitted: str,
    expected_message: str,
) -> None:
    result = parse_meal_input(submitted, date(2026, 8, 22))

    if expected_message:
        assert expected_message in result.errors
    else:
        assert result.is_valid


@pytest.mark.parametrize("action", ["confirm", "cancel", "add", "done"])
def test_parse_meal_callback_accepts_all_actions(action: str) -> None:
    submission_id = "123e4567-e89b-12d3-a456-426614174000"

    callback = parse_meal_callback(f"meal:{action}:{submission_id}")

    assert callback is not None
    assert callback.action is MealCallbackAction(action)
    assert callback.submission_id == submission_id
    assert UUID(callback.submission_id)


def test_parse_batch_meal_confirmation_callback_is_bounded_and_typed() -> None:
    callback = parse_meal_callback(
        "meal:confirm:123e4567-e89b-12d3-a456-426614174000:preparation"
    )

    assert callback is not None
    assert callback.action is MealCallbackAction.CONFIRM
    assert callback.batch_role is BatchMealRole.PREPARATION


def test_parse_batch_role_is_rejected_for_non_confirmation_callbacks() -> None:
    assert (
        parse_meal_callback(
            "meal:add:123e4567-e89b-12d3-a456-426614174000:leftover"
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        "meal:confirm:not-a-uuid",
        "meal:unknown:123e4567-e89b-12d3-a456-426614174000",
        "meal:confirm:123e4567-e89b-12d3-a456-426614174000:extra",
        "checkin:2026-08-22:1:lunch:cooked",
        "meal:confirm:",
        "x" * 65,
        "é" * 64,
    ],
)
def test_parse_meal_callback_rejects_malformed_or_oversized_payload(
    payload: str,
) -> None:
    assert parse_meal_callback(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        "profile:root",
        "profile:back",
        "profile:done",
        "profile:close",
        "profile:category:family",
        "profile:category:dietary_constraints",
        "profile:category:dietary_preferences",
        "profile:operation:family:add",
        "profile:operation:family:remove",
        "profile:operation:family:change_calories",
        "profile:operation:family:change_protein",
        "profile:operation:family:change_fibre",
        "profile:operation:dietary_constraints:add",
        "profile:operation:dietary_constraints:remove",
        "profile:operation:dietary_preferences:add",
        "profile:operation:dietary_preferences:remove",
    ],
)
def test_parse_every_accepted_profile_callback(payload: str) -> None:
    """Accept only the documented profile navigation and operation actions."""
    callback = parse_profile_callback(payload)

    assert len(payload.encode("utf-8")) <= 64
    assert callback is not None
    assert callback.action in {
        ProfileCallbackAction.ROOT,
        ProfileCallbackAction.BACK,
        ProfileCallbackAction.DONE,
        ProfileCallbackAction.CLOSE,
        ProfileCallbackAction.CATEGORY,
        ProfileCallbackAction.OPERATION,
    }


@pytest.mark.parametrize(
    ("category", "index"),
    [
        (ProfileEditCategory.FAMILY, 1),
        (ProfileEditCategory.DIETARY_CONSTRAINTS, 2),
        (ProfileEditCategory.DIETARY_PREFERENCES, 3),
    ],
)
def test_parse_profile_removal_selection_callbacks(
    category: ProfileEditCategory,
    index: int,
) -> None:
    """Parse revision-stamped selections for every removable category."""
    payload = f"profile:remove:{category.value}:{index}:42"

    callback = parse_profile_callback(payload)

    assert callback is not None
    assert callback.action is ProfileCallbackAction.REMOVE_SELECTION
    assert callback.category is category
    assert callback.index == index
    assert callback.profile_revision == 42
    assert callback.operation is None
    assert callback.token is None


def test_profile_removal_selection_model_requires_all_selection_fields() -> (
    None
):
    """The typed model rejects incomplete removal selections."""
    with pytest.raises(ValueError):
        ProfileCallback(
            action=ProfileCallbackAction.REMOVE_SELECTION,
            category=ProfileEditCategory.FAMILY,
        )


@pytest.mark.parametrize(
    "payload",
    [
        "profile:remove:family:1",
        "profile:remove:family:1:0:extra",
        "profile:remove:unknown:1:0",
        "profile:remove:family:0:0",
        "profile:remove:family:-1:0",
        "profile:remove:family:41:0",
        "profile:remove:family:1:-1",
        "profile:remove:family:1:1.0",
        "profile:remove:family:one:0",
        "profile:remove:family:+1:0",
        "profile:remove:family:1:+1",
        "profile:remove:dietary_preferences:40:" + "9" * 40,
    ],
)
def test_parse_profile_removal_selection_rejects_malformed_payload(
    payload: str,
) -> None:
    """Reject malformed, invalid, and out-of-bounds selections."""
    assert parse_profile_callback(payload) is None


@pytest.mark.parametrize(
    "extra_field",
    ["operation", "token", "unexpected"],
)
def test_profile_removal_selection_model_rejects_extra_fields(
    extra_field: str,
) -> None:
    """Do not permit fields from another callback shape."""
    values: dict[str, object] = {
        "action": ProfileCallbackAction.REMOVE_SELECTION,
        "category": ProfileEditCategory.FAMILY,
        "index": 1,
        "profile_revision": 0,
        extra_field: "unexpected",
    }

    with pytest.raises(ValueError):
        ProfileCallback.model_validate(values)


@pytest.mark.parametrize(
    ("index", "revision"),
    [
        (0, 0),
        (-1, 0),
        (MAX_PROFILE_REMOVAL_INDEX + 1, 0),
        (1, -1),
        (1, MAX_PROFILE_REVISION + 1),
    ],
)
def test_profile_removal_selection_model_rejects_invalid_bounds(
    index: int,
    revision: int,
) -> None:
    """Keep model-level selection bounds independent of parsing."""
    with pytest.raises(ValueError):
        ProfileCallback(
            action=ProfileCallbackAction.REMOVE_SELECTION,
            category=ProfileEditCategory.FAMILY,
            index=index,
            profile_revision=revision,
        )


@pytest.mark.parametrize("category", list(ProfileEditCategory))
def test_profile_removal_callbacks_fit_telegram_byte_limit(
    category: ProfileEditCategory,
) -> None:
    """The generated worst-case supported callback stays below 64 bytes."""
    payload = (
        f"profile:remove:{category.value}:{MAX_PROFILE_REMOVAL_INDEX}:"
        f"{MAX_PROFILE_REVISION}"
    )

    assert len(payload.encode("utf-8")) < 64
    assert parse_profile_callback(payload) is not None


@pytest.mark.parametrize(
    ("payload", "operation"),
    [
        (
            "profile:operation:family:change_protein",
            ProfileEditOperation.CHANGE_PROTEIN,
        ),
        (
            "profile:operation:family:change_fibre",
            ProfileEditOperation.CHANGE_FIBRE,
        ),
    ],
)
def test_parse_family_nutrient_operation_callbacks(
    payload: str,
    operation: ProfileEditOperation,
) -> None:
    """Round-trip the exact Family nutrient operation payloads."""
    callback = parse_profile_callback(payload)

    assert callback is not None
    assert callback.action is ProfileCallbackAction.OPERATION
    assert callback.category is ProfileEditCategory.FAMILY
    assert callback.operation is operation


@pytest.mark.parametrize(
    "payload",
    [
        "profile",
        "profile:",
        "profile:unknown",
        "profile:root:extra",
        "profile:category",
        "profile:category:unknown",
        "profile:category:family:extra",
        "profile:operation",
        "profile:operation:family",
        "profile:operation:family:unknown",
        "profile:operation:family:change_protein:extra",
        "profile:operation:dietary_constraints:change_protein",
        "profile:operation:dietary_constraints:change_fibre",
        "profile:operation:dietary_preferences:change_protein",
        "profile:operation:dietary_preferences:change_fibre",
        "profile:operation:goals:change_protein",
        "profile:operation:goals:change_fibre",
        "profile:operation:unknown:add",
        "profile:operation:dietary_constraints:change_calories",
        "profile:operation:family:add:extra",
        "profile:category:goals",
        "profile:operation:goals:add",
        "profile:operation:goals:remove",
        "checkin:2026-08-10:1:lunch:cooked",
        "profile:category:family:with:arbitrary:data",
    ],
)
def test_parse_profile_callback_rejects_malformed_or_wrong_operations(
    payload: str,
) -> None:
    """Reject malformed payloads and operations for unrelated categories."""
    assert parse_profile_callback(payload) is None


def test_parse_profile_callback_rejects_oversized_nutrient_operation() -> None:
    """Keep the Telegram byte limit for nutrient operation callbacks."""
    payload = "profile:operation:family:change_fibre:" + "é" * 14

    assert len(payload.encode("utf-8")) > 64
    assert parse_profile_callback(payload) is None


def test_profile_callback_parser_enforces_telegram_byte_limit() -> None:
    """Reject callback data above Telegram's 64-byte UTF-8 limit."""
    oversized = "profile:category:" + "é" * 25

    assert len(oversized.encode("utf-8")) > 64
    assert parse_profile_callback(oversized) is None


def test_profile_callbacks_remain_within_telegram_byte_limit() -> None:
    """The longest documented profile operation fits the wire limit."""
    payload = "profile:operation:dietary_preferences:remove"

    assert len(payload.encode("utf-8")) <= 64
    assert parse_profile_callback(payload) is not None


@pytest.mark.parametrize(
    "payload",
    [
        "checkin:1:lunch:cooked",
        "checkin:not-a-date:1:lunch:cooked",
        "checkin:2026-08-10:0:lunch:cooked",
        "checkin:2026-08-10:1:brunch:cooked",
        "checkin:2026-08-10:1:lunch:liked",
        "x" * 65,
    ],
)
def test_reject_malformed_or_old_callbacks(payload: str) -> None:
    assert parse_checkin_callback(payload) is None


def test_route_unknown_updates() -> None:
    assert route_update({}).route_type is RouteType.UNKNOWN
    assert route_update("bad").route_type is RouteType.UNKNOWN
