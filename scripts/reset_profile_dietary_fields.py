"""Run the one-time, exact-user development dietary profile reset."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from pydantic import ValidationError

from meal_planner.models.schemas import UserProfile

_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_EXPLICIT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/#-]{0,127}$")
_BROAD_TARGETS = frozenset({"*", "all", "any", "everyone", "users"})


class ResetError(RuntimeError):
    """Base class for safe reset failures."""


class ResetConfigurationError(ResetError):
    """The command target is absent, malformed, or too broad."""


class ProfileNotFoundError(ResetError):
    """The exact requested profile does not exist."""


class MalformedProfileError(ResetError):
    """The exact requested profile is not canonical and safe to update."""


class ResetConflictError(ResetError):
    """The profile changed between the read and conditional update."""


@dataclass(frozen=True)
class ResetResult:
    """Result of one reset attempt."""

    changed: bool
    profile_revision: int


def _validate_target(value: Any, *, label: str) -> str:
    """Validate one explicit target without accepting wildcard selectors."""
    if not isinstance(value, str):
        raise ResetConfigurationError("Invalid explicit reset target.")
    if (
        value != value.strip()
        or not value
        or value.casefold() in _BROAD_TARGETS
    ):
        raise ResetConfigurationError("Invalid explicit reset target.")
    pattern = _TABLE_NAME if label == "table" else _EXPLICIT_IDENTIFIER
    if pattern.fullmatch(value) is None:
        raise ResetConfigurationError("Invalid explicit reset target.")
    return value


def _validate_profile_item(
    item: Any, *, expected_pk: str
) -> tuple[UserProfile, dict[str, Any]]:
    """Validate a complete canonical profile without rewriting its item."""
    if not isinstance(item, dict):
        raise MalformedProfileError("Stored profile is malformed.")
    if item.get("PK") != expected_pk or item.get("SK") != "PROFILE":
        raise MalformedProfileError("Stored profile is malformed.")
    if not isinstance(item.get("dietary_constraints"), list):
        raise MalformedProfileError("Stored profile is malformed.")
    if not isinstance(item.get("dietary_preferences"), list):
        raise MalformedProfileError("Stored profile is malformed.")
    revision = item.get("profile_revision")
    if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)):
        raise MalformedProfileError("Stored profile is malformed.")
    if (
        isinstance(revision, Decimal)
        and not revision == revision.to_integral_value()
    ):
        raise MalformedProfileError("Stored profile is malformed.")

    profile_data = {
        key: value for key, value in item.items() if key not in {"PK", "SK"}
    }
    try:
        profile = UserProfile.model_validate(profile_data)
    except ValidationError as exc:
        raise MalformedProfileError("Stored profile is malformed.") from exc
    return profile, item


def reset_profile_dietary_fields(table: Any, user_id: str) -> ResetResult:
    """Clear dietary fields for one profile with a revision guard.

    The read validates the canonical profile and determines whether the
    operation is already complete. The write names only the two dietary fields
    and the profile revision, so all other profile and partition items remain
    untouched.
    """
    user_id = _validate_target(user_id, label="user")
    expected_pk = f"USER#{user_id}"
    key = {"PK": expected_pk, "SK": "PROFILE"}
    try:
        response = table.get_item(Key=key, ConsistentRead=True)
    except (BotoCoreError, ClientError) as exc:
        raise ResetError("AWS request failed.") from exc

    item = response.get("Item") if isinstance(response, dict) else None
    if item is None:
        raise ProfileNotFoundError("Profile was not found.")
    profile, _ = _validate_profile_item(item, expected_pk=expected_pk)
    if not profile.dietary_constraints and not profile.dietary_preferences:
        return ResetResult(
            changed=False, profile_revision=profile.profile_revision
        )

    names = {
        "#pk": "PK",
        "#sk": "SK",
        "#constraints": "dietary_constraints",
        "#preferences": "dietary_preferences",
        "#revision": "profile_revision",
    }
    values = {
        ":empty": [],
        ":zero": 0,
        ":expected_revision": profile.profile_revision,
        ":next_revision": profile.profile_revision + 1,
    }
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET #constraints = :empty, #preferences = :empty, "
                "#revision = :next_revision"
            ),
            ConditionExpression=(
                "attribute_exists(#pk) AND attribute_exists(#sk) AND "
                "attribute_exists(#revision) AND "
                "#revision = :expected_revision AND "
                "(size(#constraints) > :zero OR "
                "size(#preferences) > :zero)"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise ResetConflictError(
                "Profile changed concurrently; no reset was applied."
            ) from exc
        raise ResetError("AWS request failed.") from exc
    except BotoCoreError as exc:
        raise ResetError("AWS request failed.") from exc

    return ResetResult(
        changed=True, profile_revision=profile.profile_revision + 1
    )


def _parser() -> argparse.ArgumentParser:
    """Build the explicit reset command parser."""
    parser = argparse.ArgumentParser(
        description=(
            "One-time development reset for one user's dietary profile fields."
        )
    )
    parser.add_argument("--table", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--user-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the reset and return a safe process status."""
    args = _parser().parse_args(argv)
    try:
        table_name = _validate_target(args.table, label="table")
        aws_profile = _validate_target(args.profile, label="profile")
        region = _validate_target(args.region, label="region")
        user_id = _validate_target(args.user_id, label="user")
        session = boto3.Session(
            profile_name=aws_profile,
            region_name=region,
        )
        table = session.resource("dynamodb").Table(table_name)
        result = reset_profile_dietary_fields(table, user_id)
    except ResetConfigurationError:
        print("Invalid explicit reset target.", file=sys.stderr)
        return 2
    except ProfileNotFoundError:
        print("Profile was not found.", file=sys.stderr)
        return 1
    except MalformedProfileError:
        print("Stored profile is malformed.", file=sys.stderr)
        return 1
    except ResetConflictError:
        print(
            "Profile changed concurrently; no reset was applied.",
            file=sys.stderr,
        )
        return 1
    except ResetError:
        print("AWS request failed.", file=sys.stderr)
        return 1
    except BotoCoreError, ClientError:
        print("AWS request failed.", file=sys.stderr)
        return 1

    if result.changed:
        print(
            "Dietary fields reset; profile revision advanced to "
            f"{result.profile_revision}."
        )
    else:
        print("Dietary fields already empty; no changes were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
