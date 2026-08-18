"""Register the canonical Telegram command menu for a bot token."""

import argparse
import os
import sys
from collections.abc import Sequence

from meal_planner.telegram.api import TelegramAPI, TelegramAPIError
from meal_planner.telegram.commands import BOT_COMMANDS

BOT_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    return argparse.ArgumentParser(
        description="Register the Meal Planner Telegram command menu."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Register commands and return a safe process status."""
    _parser().parse_args(argv)
    bot_token = os.environ.get(BOT_TOKEN_ENV_VAR)
    if not bot_token:
        print(
            f"{BOT_TOKEN_ENV_VAR} is required to register Telegram commands.",
            file=sys.stderr,
        )
        return 1

    try:
        TelegramAPI(bot_token).set_my_commands(BOT_COMMANDS)
    except TelegramAPIError:
        print("Telegram command registration failed.", file=sys.stderr)
        return 1

    print("Telegram command menu registered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
