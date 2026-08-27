"""Repair malformed dietary preferences for one exact development user."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
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


class RepairError(RuntimeError):
    """Base class for safe repair failures."""


class RepairConfigurationError(RepairError):
    """The command target is absent, malformed, or too broad."""


class ProfileNotFoundError(RepairError):
    """The exact requested profile does not exist."""


class MalformedProfileError(RepairError):
    """The exact requested profile is not safe to repair."""


class RepairConflictError(RepairError):
    """The profile changed between the read and conditional update."""


@dataclass(frozen=True)
class RepairResult:
    """Result of one malformed-preference repair attempt."""

    changed: bool
    profile_revision: int
    retained_count: int
    removed_count: int


# Keep old imports working for callers that only use the module as a library.
ResetError = RepairError
ResetConfigurationError = RepairConfigurationError
ResetConflictError = RepairConflictError
ResetResult = RepairResult


def _validate_target(value: Any, *, label: str) -> str:
    """Validate one explicit target without accepting wildcard selectors."""
    if not isinstance(value, str):
        raise RepairConfigurationError("Invalid explicit repair target.")
    if (
        value != value.strip()
        or not value
        or value.casefold() in _BROAD_TARGETS
    ):
        raise RepairConfigurationError("Invalid explicit repair target.")
    pattern = _TABLE_NAME if label == "table" else _EXPLICIT_IDENTIFIER
    if pattern.fullmatch(value) is None:
        raise RepairConfigurationError("Invalid explicit repair target.")
    return value


def _classify_preferences(
    preferences: Any,
) -> tuple[list[Any], int]:
    """Return valid entries and the count of known legacy shapes."""
    if not isinstance(preferences, list):
        raise MalformedProfileError("Stored profile is malformed.")

    retained: list[Any] = []
    removed_count = 0
    for entry in preferences:
        if isinstance(entry, str) or (
            isinstance(entry, Mapping)
            and ("rule" not in entry or entry.get("rule") is None)
        ):
            removed_count += 1
            continue
        retained.append(entry)
    return retained, removed_count


def _validate_profile_item(
    item: Any, *, expected_pk: str
) -> tuple[UserProfile, list[Any], int]:
    """Classify and validate one complete profile without broad repair."""
    if not isinstance(item, dict):
        raise MalformedProfileError("Stored profile is malformed.")
    if item.get("PK") != expected_pk or item.get("SK") != "PROFILE":
        raise MalformedProfileError("Stored profile is malformed.")
    if not isinstance(item.get("dietary_constraints"), list):
        raise MalformedProfileError("Stored profile is malformed.")
    preferences, removed_count = _classify_preferences(
        item.get("dietary_preferences")
    )
    revision = item.get("profile_revision")
    if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)):
        raise MalformedProfileError("Stored profile is malformed.")
    if (
        isinstance(revision, Decimal)
        and not revision == revision.to_integral_value()
    ):
        raise MalformedProfileError("Stored profile is malformed.")

    profile_data: dict[str, Any] = {
        key: value for key, value in item.items() if key not in {"PK", "SK"}
    }
    profile_data["dietary_preferences"] = preferences
    try:
        profile = UserProfile.model_validate(profile_data)
    except ValidationError as exc:
        raise MalformedProfileError("Stored profile is malformed.") from exc
    if not profile.is_complete:
        raise MalformedProfileError("Stored profile is incomplete.")
    return profile, preferences, removed_count


def repair_profile_dietary_preferences(
    table: Any, user_id: str
) -> RepairResult:
    """Remove known malformed preferences with a revision guard.

    The read classifies each preference independently, validates the cleaned
    complete profile, and determines whether the operation is already complete.
    The write names only dietary preferences and the profile revision, so
    constraints and all other profile and partition items remain untouched.
    """
    user_id = _validate_target(user_id, label="user")
    expected_pk = f"USER#{user_id}"
    key = {"PK": expected_pk, "SK": "PROFILE"}
    try:
        response = table.get_item(Key=key, ConsistentRead=True)
    except (BotoCoreError, ClientError) as exc:
        raise RepairError("AWS request failed.") from exc

    item = response.get("Item") if isinstance(response, dict) else None
    if item is None:
        raise ProfileNotFoundError("Profile was not found.")
    profile, preferences, removed_count = _validate_profile_item(
        item, expected_pk=expected_pk
    )
    retained_count = len(preferences)
    if removed_count == 0:
        return RepairResult(
            changed=False,
            profile_revision=profile.profile_revision,
            retained_count=retained_count,
            removed_count=0,
        )

    names = {
        "#pk": "PK",
        "#sk": "SK",
        "#preferences": "dietary_preferences",
        "#revision": "profile_revision",
    }
    values = {
        ":preferences": preferences,
        ":zero": 0,
        ":expected_revision": profile.profile_revision,
        ":next_revision": profile.profile_revision + 1,
    }
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET #preferences = :preferences, #revision = :next_revision"
            ),
            ConditionExpression=(
                "attribute_exists(#pk) AND attribute_exists(#sk) AND "
                "attribute_exists(#revision) AND "
                "#revision = :expected_revision AND "
                "size(#preferences) > :zero"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise RepairConflictError(
                "Profile changed concurrently; no repair was applied."
            ) from exc
        raise RepairError("AWS request failed.") from exc
    except BotoCoreError as exc:
        raise RepairError("AWS request failed.") from exc

    return RepairResult(
        changed=True,
        profile_revision=profile.profile_revision + 1,
        retained_count=retained_count,
        removed_count=removed_count,
    )


def reset_profile_dietary_fields(table: Any, user_id: str) -> RepairResult:
    """Keep the former library entry point as a narrowed repair alias."""
    return repair_profile_dietary_preferences(table, user_id)


def _parser() -> argparse.ArgumentParser:
    """Build the explicit repair command parser."""
    parser = argparse.ArgumentParser(
        description=(
            "One-time development repair for one user's malformed "
            "dietary preferences."
        )
    )
    parser.add_argument("--table", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--user-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the repair and return a safe process status."""
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
        result = repair_profile_dietary_preferences(table, user_id)
    except RepairConfigurationError:
        print("Invalid explicit repair target.", file=sys.stderr)
        return 2
    except ProfileNotFoundError:
        print("Profile was not found.", file=sys.stderr)
        return 1
    except MalformedProfileError:
        print("Stored profile is malformed.", file=sys.stderr)
        return 1
    except RepairConflictError:
        print(
            "Profile changed concurrently; no repair was applied.",
            file=sys.stderr,
        )
        return 1
    except RepairError:
        print("AWS request failed.", file=sys.stderr)
        return 1
    except BotoCoreError, ClientError:
        print("AWS request failed.", file=sys.stderr)
        return 1

    if result.changed:
        print(
            "Repaired dietary preferences: removed "
            f"{result.removed_count}, retained {result.retained_count}; "
            "profile revision advanced to "
            f"{result.profile_revision}."
        )
    else:
        print(
            "Dietary preferences already canonical: removed 0, retained "
            f"{result.retained_count}; no changes were made."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
