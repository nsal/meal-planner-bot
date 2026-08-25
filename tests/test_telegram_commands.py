"""Tests for the canonical Telegram command catalogue."""

import pytest

from meal_planner.telegram.commands import (
    BOT_COMMANDS,
    TelegramCommand,
    render_help,
    validate_commands,
)


def test_catalogue_has_stable_order_and_telegram_payloads() -> None:
    assert [command.name for command in BOT_COMMANDS] == [
        "start",
        "help",
        "profile",
        "plan",
        "grocery",
        "today",
        "submit_meals",
        "checkin",
        "cancel",
    ]
    assert BOT_COMMANDS[0].to_payload() == {
        "command": "start",
        "description": "Start onboarding or view what to do next",
    }


def test_catalogue_is_immutable() -> None:
    with pytest.raises(AttributeError):
        BOT_COMMANDS[0].name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        BOT_COMMANDS[0:1] += (TelegramCommand("extra", "Extra"),)  # type: ignore[misc]


def test_render_help_uses_every_command_once() -> None:
    rendered = render_help()
    lines = rendered.splitlines()

    assert len(lines) == len(BOT_COMMANDS)
    assert lines == [
        f"/{command.name} — {command.description}" for command in BOT_COMMANDS
    ]
    assert all(line.count("/") == 1 for line in lines)


def test_plan_command_description_is_duration_neutral() -> None:
    plan_command = next(
        command for command in BOT_COMMANDS if command.name == "plan"
    )

    assert plan_command.description == "Create or retry a meal plan"
    assert "weekly" not in render_help().lower()


@pytest.mark.parametrize(
    "name,description",
    [
        ("", "Description"),
        ("A", "Description"),
        ("bad-command", "Description"),
        ("x" * 33, "Description"),
        ("valid", ""),
        ("valid", "x" * 257),
        ("valid", "line one\nline two"),
    ],
)
def test_invalid_command_entries_are_rejected(
    name: str, description: str
) -> None:
    with pytest.raises(ValueError):
        TelegramCommand(name, description)


def test_duplicate_or_non_command_entries_are_rejected() -> None:
    command = TelegramCommand("one", "One")
    with pytest.raises(ValueError, match="Duplicate"):
        validate_commands((command, command))
    with pytest.raises(ValueError, match="TelegramCommand"):
        validate_commands((command, "invalid"))  # type: ignore[arg-type]
