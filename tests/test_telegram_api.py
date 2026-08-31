"""Telegram API safety, formatting, and failure tests."""

import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from uuid import UUID

import pytest
from pytest_mock import MockerFixture

from meal_planner.models.schemas import (
    FamilyMember,
    ProfileEditCategory,
    ProfileEditOperation,
    UserProfile,
)
from meal_planner.telegram.api import (
    ProfilePresentationItem,
    TelegramAPI,
    TelegramAPIError,
    meal_continuation_keyboard,
    meal_review_keyboard,
    plan_chat_keyboard,
    profile_presentation_items,
    split_text,
)
from meal_planner.telegram.commands import BOT_COMMANDS, TelegramCommand
from tests.factories import make_profile


def _response() -> BytesIO:
    return BytesIO(b'{"ok": true, "result": {}}')


def test_split_text_preserves_plain_content() -> None:
    text = "line one\n" + "x" * 120 + "\nline three"
    chunks = split_text(text, max_length=50)
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_send_message_is_plain_text_and_uses_timeout(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    api = TelegramAPI("secret-token", request_timeout=4.5)
    api.send_message(1, "unsafe *markdown* _text_")
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data.decode())
    assert payload == {"chat_id": 1, "text": "unsafe *markdown* _text_"}
    assert urlopen.call_args.kwargs["timeout"] == 4.5


def test_set_my_commands_posts_canonical_payload_and_uses_timeout(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    api = TelegramAPI("secret-token", request_timeout=4.5)

    api.set_my_commands()

    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "https://api.telegram.org/botsecret-token/setMyCommands"
    )
    payload = json.loads(request.data.decode())
    assert payload["commands"][0] == {
        "command": "start",
        "description": "Start onboarding or view what to do next",
    }
    assert len(payload["commands"]) == len(BOT_COMMANDS)
    assert urlopen.call_args.kwargs["timeout"] == 4.5


def test_set_my_commands_validates_before_transport(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch("urllib.request.urlopen")

    with pytest.raises(ValueError):
        TelegramAPI("token").set_my_commands(
            (TelegramCommand("valid", "Valid"),) * 2
        )

    urlopen.assert_not_called()


def test_set_and_get_webhook_use_exact_payloads_and_timeout(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=[_response(), BytesIO(b'{"ok": true, "result": {}}')],
    )
    api = TelegramAPI("token", request_timeout=3.0)

    api.set_webhook("https://example.test/webhook", "webhook-secret")
    api.get_webhook_info()

    set_request = urlopen.call_args_list[0].args[0]
    assert (
        set_request.full_url == "https://api.telegram.org/bottoken/setWebhook"
    )
    assert json.loads(set_request.data.decode()) == {
        "url": "https://example.test/webhook",
        "secret_token": "webhook-secret",
    }
    get_request = urlopen.call_args_list[1].args[0]
    assert get_request.full_url == (
        "https://api.telegram.org/bottoken/getWebhookInfo"
    )
    assert urlopen.call_args_list[0].kwargs["timeout"] == 3.0


@pytest.mark.parametrize("url, secret", [("", "secret"), ("url", "")])
def test_set_webhook_rejects_empty_values(
    url: str, secret: str, mocker: MockerFixture
) -> None:
    urlopen = mocker.patch("urllib.request.urlopen")

    with pytest.raises(ValueError):
        TelegramAPI("token").set_webhook(url, secret)

    urlopen.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        URLError("offline"),
        TimeoutError("slow"),
        HTTPError("url", 500, "error", {}, BytesIO()),
    ],
)
def test_transport_errors_raise_controlled_exception(
    mocker: MockerFixture, error: Exception
) -> None:
    mocker.patch("urllib.request.urlopen", side_effect=error)
    with pytest.raises(TelegramAPIError):
        TelegramAPI("token").send_message(1, "hello")


def test_api_error_and_partial_chunk_failure_raise(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=[_response(), BytesIO(b'{"ok": false, "error_code": 400}')],
    )
    with pytest.raises(TelegramAPIError):
        TelegramAPI("token").send_message(1, "x" * 5000)
    assert urlopen.call_count == 2


def test_plan_chat_keyboard_uses_canonical_uuid_and_fits_limit() -> None:
    session_id = UUID("12345678-1234-5678-1234-567812345678")

    keyboard = plan_chat_keyboard(session_id)

    button = keyboard["inline_keyboard"][0][0]
    assert button == {
        "text": "End planning",
        "callback_data": ("plan_chat:end:12345678-1234-5678-1234-567812345678"),
        "style": "danger",
    }
    assert len(button["callback_data"].encode("utf-8")) <= 64


@pytest.mark.parametrize("session_id", ["not-a-uuid", "é" * 64])
def test_plan_chat_keyboard_rejects_invalid_session_id(session_id: str) -> None:
    with pytest.raises(ValueError):
        plan_chat_keyboard(session_id)


def test_send_plan_chat_attaches_end_button_to_final_split_chunk(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    session_id = UUID("12345678-1234-5678-1234-567812345678")
    text = "first line\n" + "x" * 4090

    TelegramAPI("token").send_plan_chat(1, text, session_id)

    assert urlopen.call_count == 2
    first_payload = json.loads(urlopen.call_args_list[0].args[0].data)
    last_payload = json.loads(urlopen.call_args_list[-1].args[0].data)
    assert "reply_markup" not in first_payload
    assert last_payload["reply_markup"] == plan_chat_keyboard(session_id)


def test_send_plan_chat_sends_initial_generated_and_error_text_with_control(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    api = TelegramAPI("token")
    session_id = "12345678-1234-5678-1234-567812345678"

    for message in (
        "Tell me what information you need.",
        "Draft response",
        "I could not generate a draft. Please try again.",
    ):
        api.send_plan_chat(1, message, session_id)
        payload = json.loads(urlopen.call_args.args[0].data)
        assert payload["text"] == message
        assert payload["reply_markup"] == plan_chat_keyboard(session_id)


def test_send_plan_chat_propagates_telegram_api_failure(
    mocker: MockerFixture,
) -> None:
    mocker.patch(
        "urllib.request.urlopen",
        side_effect=HTTPError("url", 500, "error", {}, BytesIO()),
    )

    with pytest.raises(TelegramAPIError):
        TelegramAPI("token").send_plan_chat(
            1,
            "Draft response",
            "12345678-1234-5678-1234-567812345678",
        )


def test_answer_callback_query(mocker: MockerFixture) -> None:
    urlopen = mocker.patch("urllib.request.urlopen", return_value=_response())
    TelegramAPI("token").answer_callback_query("query-id", "done")
    payload = json.loads(urlopen.call_args.args[0].data.decode())
    assert payload == {"callback_query_id": "query-id", "text": "done"}


def _last_payload(urlopen: object) -> dict[str, object]:
    """Decode the most recently posted Telegram payload."""
    request = urlopen.call_args.args[0]  # type: ignore[attr-defined]
    return json.loads(request.data.decode())


def test_profile_summary_renders_canonical_text_and_root_controls(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )

    TelegramAPI("token").send_profile(1, make_profile())

    payload = _last_payload(urlopen)
    assert "Dietary constraints:\nNone" in payload["text"]
    assert "Allergies:" not in payload["text"]
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Amend profile", "callback_data": "profile:root"}],
            [{"text": "Close", "callback_data": "profile:close"}],
        ]
    }


@pytest.mark.parametrize(
    ("protein_target", "fibre_target", "expected_targets"),
    [
        (None, None, "protein: not set, fibre: not set"),
        (120, None, "protein: 120 g/day, fibre: not set"),
        (120, 30, "protein: 120 g/day, fibre: 30 g/day"),
    ],
)
def test_profile_summary_renders_optional_targets_and_not_set_copy(
    mocker: MockerFixture,
    protein_target: int | None,
    fibre_target: int | None,
    expected_targets: str,
) -> None:
    """Show each member's supplied and absent nutrient targets."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = UserProfile(
        name="Alex",
        people_count=1,
        family_members=[
            FamilyMember(
                name="Alex",
                calorie_target=2000,
                protein_target=protein_target,
                fibre_target=fibre_target,
            )
        ],
    )

    TelegramAPI("token").send_profile(1, profile)

    payload = _last_payload(urlopen)
    assert "- Alex (2000 kcal/day, " + expected_targets + ")" in payload["text"]


def test_profile_presentation_items_keep_stored_order_and_type_labels() -> None:
    """The removal projection preserves raw preference order."""
    profile = make_profile().model_copy(
        update={
            "dietary_preferences": [
                "first preference",
                "second preference",
            ],
        }
    )

    items = profile_presentation_items(
        profile, ProfileEditCategory.DIETARY_PREFERENCES
    )

    assert all(isinstance(item, ProfilePresentationItem) for item in items)
    assert [item.label for item in items] == [
        "Dietary preference: first preference",
        "Dietary preference: second preference",
    ]
    assert [item.value for item in items] == [
        "first preference",
        "second preference",
    ]


def test_profile_presentation_items_keep_family_order() -> None:
    """Family removal entries follow the persisted member order."""
    profile = make_profile()

    items = profile_presentation_items(profile, ProfileEditCategory.FAMILY)

    assert [item.label for item in items] == [
        "Alex (2000 kcal/day, protein: not set, fibre: not set)",
        "Sam (1800 kcal/day, protein: not set, fibre: not set)",
    ]


def test_profile_summary_numbers_rules_in_stored_order(
    mocker: MockerFixture,
) -> None:
    """Profile rule sections use one numbered, dot-separated item per line."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": [
                "first constraint",
                "second constraint",
            ],
            "dietary_preferences": [
                "first preference",
                "second preference",
            ],
        }
    )

    TelegramAPI("token").send_profile(1, profile)

    text = _last_payload(urlopen)["text"]
    assert (
        "Dietary constraints:\n1. first constraint\n2. second constraint"
        in text
    )
    assert (
        "Dietary preferences:\n1. first preference\n2. second preference"
        in text
    )
    assert "Batch rules" not in text


def test_profile_removal_operation_renders_raw_numbered_buttons(
    mocker: MockerFixture,
) -> None:
    """Combined dietary removal buttons are numbered and revision-stamped."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = make_profile().model_copy(
        update={
            "profile_revision": 17,
            "dietary_preferences": [
                f"preference {index}" for index in range(1, 5)
            ],
        }
    )

    TelegramAPI("token").send_profile_operation(
        1,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.REMOVE,
        profile,
    )

    payload = _last_payload(urlopen)
    assert "1. Dietary preference: preference 1" in payload["text"]
    assert "4. Dietary preference: preference 4" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"]
    assert [[button["text"] for button in row] for row in buttons] == [
        ["1", "2", "3", "4"],
        ["Back"],
        ["Done"],
        ["Close"],
    ]
    callbacks = [
        button["callback_data"] for row in buttons[:1] for button in row
    ]
    assert callbacks == [
        f"profile:remove:dietary_preferences:{index}:17"
        for index in range(1, 5)
    ]
    assert all(len(callback.encode("utf-8")) < 64 for callback in callbacks)


def test_profile_removal_keeps_duplicate_labels_at_distinct_indices(
    mocker: MockerFixture,
) -> None:
    """Duplicate display text still produces distinct numbered callbacks."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = make_profile().model_copy(
        update={
            "profile_revision": 3,
            "dietary_preferences": [
                "same wording",
                "same wording",
            ],
        }
    )

    TelegramAPI("token").send_profile_operation(
        1,
        ProfileEditCategory.DIETARY_PREFERENCES,
        ProfileEditOperation.REMOVE,
        profile,
    )

    payload = _last_payload(urlopen)
    assert "1. Dietary preference: same wording" in payload["text"]
    assert "2. Dietary preference: same wording" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"]
    assert [button["callback_data"] for button in buttons[0]] == [
        "profile:remove:dietary_preferences:1:3",
        "profile:remove:dietary_preferences:2:3",
    ]


@pytest.mark.parametrize(
    ("category", "profile_update", "expected_text"),
    [
        (
            ProfileEditCategory.FAMILY,
            {"family_members": [], "people_count": 1},
            "There are no family members to remove.",
        ),
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            {"dietary_constraints": []},
            "There are no dietary constraints to remove.",
        ),
        (
            ProfileEditCategory.DIETARY_PREFERENCES,
            {"dietary_preferences": []},
            "There are no dietary preferences to remove.",
        ),
    ],
)
def test_empty_profile_removal_keeps_navigation(
    mocker: MockerFixture,
    category: ProfileEditCategory,
    profile_update: dict[str, object],
    expected_text: str,
) -> None:
    """Empty removal categories still provide all navigation controls."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = make_profile().model_copy(update=profile_update)

    TelegramAPI("token").send_profile_operation(
        1, category, ProfileEditOperation.REMOVE, profile
    )

    payload = _last_payload(urlopen)
    assert payload["text"] == expected_text
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "Back", "callback_data": "profile:back"}],
        [{"text": "Done", "callback_data": "profile:done"}],
        [{"text": "Close", "callback_data": "profile:close"}],
    ]


def test_profile_root_renders_all_categories_and_close(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )

    TelegramAPI("token").send_profile_root(1)

    payload = _last_payload(urlopen)
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "Family",
                    "callback_data": "profile:category:family",
                }
            ],
            [
                {
                    "text": "Dietary constraints",
                    "callback_data": ("profile:category:dietary_constraints"),
                }
            ],
            [
                {
                    "text": "Dietary preferences",
                    "callback_data": ("profile:category:dietary_preferences"),
                }
            ],
            [{"text": "Done", "callback_data": "profile:done"}],
            [{"text": "Close", "callback_data": "profile:close"}],
        ]
    }


@pytest.mark.parametrize(
    "category, expected_operations",
    [
        (
            ProfileEditCategory.FAMILY,
            [
                (ProfileEditOperation.ADD, "Add member"),
                (ProfileEditOperation.REMOVE, "Remove member"),
                (ProfileEditOperation.CHANGE_CALORIES, "Change calories"),
                (ProfileEditOperation.CHANGE_PROTEIN, "Change protein"),
                (ProfileEditOperation.CHANGE_FIBRE, "Change fibre"),
            ],
        ),
        (
            ProfileEditCategory.DIETARY_CONSTRAINTS,
            [
                (ProfileEditOperation.ADD, "Add constraint"),
                (ProfileEditOperation.REMOVE, "Remove constraint"),
            ],
        ),
        (
            ProfileEditCategory.DIETARY_PREFERENCES,
            [
                (ProfileEditOperation.ADD, "Add preference"),
                (ProfileEditOperation.REMOVE, "Remove preference"),
            ],
        ),
    ],
)
def test_profile_category_renders_valid_operations_and_navigation(
    mocker: MockerFixture,
    category: ProfileEditCategory,
    expected_operations: list[tuple[ProfileEditOperation, str]],
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )

    TelegramAPI("token").send_profile_category(1, category)

    payload = _last_payload(urlopen)
    buttons = payload["reply_markup"]["inline_keyboard"]
    assert [
        (button["text"], button["callback_data"])
        for row in buttons[:-2]
        for button in row
    ] == [
        (
            label,
            f"profile:operation:{category.value}:{operation.value}",
        )
        for operation, label in expected_operations
    ]
    assert buttons[-2:] == [
        [{"text": "Back", "callback_data": "profile:back"}],
        [{"text": "Done", "callback_data": "profile:done"}],
    ]


def test_profile_operation_renders_guidance_and_compact_navigation(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )

    TelegramAPI("token").send_profile_operation(
        1,
        ProfileEditCategory.FAMILY,
        ProfileEditOperation.CHANGE_CALORIES,
    )

    payload = _last_payload(urlopen)
    assert "name and new calorie target" in payload["text"]
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Back", "callback_data": "profile:back"}],
            [{"text": "Done", "callback_data": "profile:done"}],
            [{"text": "Close", "callback_data": "profile:close"}],
        ]
    }
    for row in payload["reply_markup"]["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


@pytest.mark.parametrize(
    ("operation", "expected_fragments"),
    [
        (
            ProfileEditOperation.ADD,
            ["John 1500", "John 2000 120 30"],
        ),
        (
            ProfileEditOperation.CHANGE_PROTEIN,
            ["John 120", "John none"],
        ),
        (
            ProfileEditOperation.CHANGE_FIBRE,
            ["John 30", "John none"],
        ),
    ],
)
def test_profile_operations_render_target_guidance(
    mocker: MockerFixture,
    operation: ProfileEditOperation,
    expected_fragments: list[str],
) -> None:
    """Document nutrient updates, clearing, and both member-add forms."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )

    TelegramAPI("token").send_profile_operation(
        1, ProfileEditCategory.FAMILY, operation
    )

    payload = _last_payload(urlopen)
    for fragment in expected_fragments:
        assert fragment in payload["text"]


def test_send_meal_review_renders_exact_text_and_review_buttons(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch("urllib.request.urlopen", return_value=_response())
    submission_id = UUID("12345678-1234-5678-1234-567812345678")

    TelegramAPI("token").send_meal_review(
        1,
        "today, lunch, rice, beans, and salsa",
        submission_id,
    )

    payload = json.loads(urlopen.call_args.args[0].data.decode())
    assert payload["text"] == (
        "Review this meal submission:\n"
        "today, lunch, rice, beans, and salsa\n\n"
        "Confirm to save it or cancel."
    )
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Confirm",
                    "callback_data": (
                        "meal:confirm:12345678-1234-5678-1234-567812345678"
                    ),
                    "style": "success",
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": (
                        "meal:cancel:12345678-1234-5678-1234-567812345678"
                    ),
                    "style": "danger",
                },
            ]
        ]
    }


def test_send_meal_saved_renders_continuation_buttons(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch("urllib.request.urlopen", return_value=_response())
    submission_id = "12345678-1234-5678-1234-567812345678"

    TelegramAPI("token").send_meal_saved(1, "rice and beans", submission_id)

    payload = json.loads(urlopen.call_args.args[0].data.decode())
    assert payload["text"] == "✅ Meal saved: rice and beans"
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Add more",
                    "callback_data": (
                        "meal:add:12345678-1234-5678-1234-567812345678"
                    ),
                    "style": "primary",
                },
                {
                    "text": "✅ Done",
                    "callback_data": (
                        "meal:done:12345678-1234-5678-1234-567812345678"
                    ),
                    "style": "success",
                },
            ]
        ]
    }


def test_meal_keyboard_callbacks_fit_telegram_byte_limit() -> None:
    submission_id = UUID("12345678-1234-5678-1234-567812345678")

    keyboards = (
        meal_review_keyboard(submission_id),
        meal_continuation_keyboard(submission_id),
    )
    for keyboard in keyboards:
        for row in keyboard["inline_keyboard"]:
            assert len(row) == 2
            for button in row:
                assert len(button["callback_data"].encode("utf-8")) <= 64


def test_profile_summary_renders_raw_dietary_text_without_batch_section(
    mocker: MockerFixture,
) -> None:
    """Profile output displays the retained raw lists only."""
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    profile = make_profile().model_copy(
        update={
            "dietary_constraints": ["Peanuts"],
            "dietary_preferences": ["More vegetables"],
        }
    )

    TelegramAPI("token").send_profile(1, profile)

    payload = _last_payload(urlopen)
    assert "Dietary constraints:\n1. Peanuts" in payload["text"]
    assert "Dietary preferences:\n1. More vegetables" in payload["text"]
    assert "Batch rules" not in payload["text"]
