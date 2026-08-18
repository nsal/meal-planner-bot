"""Tests for the Telegram command registration deployment helper."""

from typing import Any

import pytest

from meal_planner.telegram.commands import BOT_COMMANDS
from scripts import configure_telegram_commands as configure


def test_missing_bot_token_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(configure.BOT_TOKEN_ENV_VAR, raising=False)

    assert configure.main([]) == 1
    assert configure.BOT_TOKEN_ENV_VAR in capsys.readouterr().err


def test_success_registers_canonical_commands(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(configure.BOT_TOKEN_ENV_VAR, "secret-token")
    api = mocker.patch.object(configure, "TelegramAPI")

    assert configure.main([]) == 0
    api.assert_called_once_with("secret-token")
    api.return_value.set_my_commands.assert_called_once_with(BOT_COMMANDS)


def test_telegram_failure_returns_safe_nonzero_status(
    mocker: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(configure.BOT_TOKEN_ENV_VAR, "secret-token")
    api = mocker.patch.object(configure, "TelegramAPI")
    api.return_value.set_my_commands.side_effect = configure.TelegramAPIError(
        "request failed with token that must not be printed"
    )

    assert configure.main([]) == 1
    output = capsys.readouterr().err
    assert "registration failed" in output
    assert "secret-token" not in output
