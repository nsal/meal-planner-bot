"""Telegram API safety, formatting, and failure tests."""

import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest
from pytest_mock import MockerFixture

from meal_planner.models.schemas import MealOutcome, MealType
from meal_planner.telegram.api import (
    TelegramAPI,
    TelegramAPIError,
    split_text,
)
from meal_planner.telegram.commands import TelegramCommand
from tests.factories import make_plan


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
    assert len(payload["commands"]) == 9
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


def test_plan_and_checkin_use_safe_text_and_specific_week(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    api = TelegramAPI("token")
    plan = make_plan()
    plan.days[0].meals[0].name = "Soup_*[]"
    api.send_plan(1, plan)
    plan_payload = json.loads(urlopen.call_args.args[0].data.decode())
    assert "parse_mode" not in plan_payload
    assert "Soup_*[]" in plan_payload["text"]
    api.send_meal_checkin(
        1,
        plan.days[0].meals,
        week_start=plan.week_start_date,
        day=1,
    )
    checkin_payload = json.loads(urlopen.call_args.args[0].data.decode())
    callback = checkin_payload["reply_markup"]["inline_keyboard"][0][0]
    assert callback["callback_data"] == (
        f"checkin:{plan.week_start_date}:1:lunch:cooked"
    )
    assert len(callback["callback_data"].encode()) <= 64


def test_maximum_valid_plan_fits_one_notification(
    mocker: MockerFixture,
) -> None:
    urlopen = mocker.patch(
        "urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: _response(),
    )
    plan = make_plan()
    template_meal = plan.days[0].meals[0]
    for day in plan.days:
        day.meals = [
            template_meal.model_copy(
                update={
                    "meal_type": MealType.BREAKFAST,
                    "name": "x" * 100,
                    "est_calories": 10_000,
                    "outcome": MealOutcome.SKIPPED,
                }
            )
            for _ in range(4)
        ]

    TelegramAPI("token").send_plan(1, plan)

    assert urlopen.call_count == 1


def test_answer_callback_query(mocker: MockerFixture) -> None:
    urlopen = mocker.patch("urllib.request.urlopen", return_value=_response())
    TelegramAPI("token").answer_callback_query("query-id", "done")
    payload = json.loads(urlopen.call_args.args[0].data.decode())
    assert payload == {"callback_query_id": "query-id", "text": "done"}
