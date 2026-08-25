"""Canonical Telegram bot command catalogue and help rendering."""

import re
from dataclasses import dataclass
from typing import Final, Sequence

MAX_COMMAND_NAME_LENGTH: Final = 32
MAX_COMMAND_DESCRIPTION_LENGTH: Final = 256
_COMMAND_NAME_PATTERN: Final = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    """A validated command definition accepted by Telegram."""

    name: str
    description: str

    def __post_init__(self) -> None:
        """Validate Telegram's command name and description limits."""
        if not 1 <= len(self.name) <= MAX_COMMAND_NAME_LENGTH:
            raise ValueError(
                "Telegram command names must contain 1 to 32 characters"
            )
        if _COMMAND_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError(
                "Telegram command names must use lowercase letters, digits, "
                "and underscores"
            )
        if not 1 <= len(self.description) <= (MAX_COMMAND_DESCRIPTION_LENGTH):
            raise ValueError(
                "Telegram command descriptions must contain 1 to 256 characters"
            )
        if any(character in self.description for character in "\r\n"):
            raise ValueError("Telegram command descriptions must be one line")

    def to_payload(self) -> dict[str, str]:
        """Return the JSON object expected by Telegram's command API."""
        return {"command": self.name, "description": self.description}


BOT_COMMANDS: Final[tuple[TelegramCommand, ...]] = (
    TelegramCommand("start", "Start onboarding or view what to do next"),
    TelegramCommand("help", "Show the available commands"),
    TelegramCommand("profile", "View and amend the household profile"),
    TelegramCommand("plan", "Create or retry a meal plan"),
    TelegramCommand("grocery", "View the active grocery list"),
    TelegramCommand("today", "View today's planned meals"),
    TelegramCommand("submit_meals", "Log meals eaten in the past week"),
    TelegramCommand("checkin", "Record today's planned meal outcomes"),
    TelegramCommand("cancel", "Cancel an unfinished workflow"),
)


def validate_commands(
    commands: Sequence[TelegramCommand],
) -> tuple[TelegramCommand, ...]:
    """Validate a command sequence before sending it to Telegram."""
    validated = tuple(commands)
    names: set[str] = set()
    for command in validated:
        if not isinstance(command, TelegramCommand):
            raise ValueError("Telegram commands must be TelegramCommand values")
        if command.name in names:
            raise ValueError(f"Duplicate Telegram command: {command.name}")
        names.add(command.name)
    return validated


def render_help(
    commands: Sequence[TelegramCommand] = BOT_COMMANDS,
) -> str:
    """Render a deterministic plain-text command reference."""
    return "\n".join(
        f"/{command.name} — {command.description}"
        for command in validate_commands(commands)
    )
