"""Tests for the one-time targeted dietary profile reset."""

from __future__ import annotations

from typing import Any, Generator

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from meal_planner.models.schemas import UserProfile
from scripts import reset_profile_dietary_fields as reset
from tests.factories import (
    make_constraint,
    make_plan,
    make_preference,
    make_profile,
)

TABLE_NAME = "test-meal-planner"
USER_ID = "123456789"


@pytest.fixture
def dynamodb_table() -> Generator[Any, None, None]:
    """Create the same simple table shape used by the application."""
    with mock_aws():
        table = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _profile_item(*, dietary_fields_empty: bool = False) -> dict[str, Any]:
    """Return a raw profile with family and nutrition data to preserve."""
    profile = make_profile(with_nutrient_targets=True).model_copy(
        update={
            "dietary_constraints": (
                [] if dietary_fields_empty else [make_constraint()]
            ),
            "dietary_preferences": (
                [] if dietary_fields_empty else [make_preference()]
            ),
            "profile_revision": 4,
        }
    )
    return {
        "PK": f"USER#{USER_ID}",
        "SK": "PROFILE",
        **profile.model_dump(mode="json"),
        "operator_note": "preserve this attribute",
    }


def _seed_related_items(table: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Seed plans, conversation state, and all meal-history item shapes."""
    related = {
        (f"USER#{USER_ID}", "PLAN#2026-08-24"): {
            "PK": f"USER#{USER_ID}",
            "SK": "PLAN#2026-08-24",
            **make_plan().model_dump(mode="json"),
        },
        (f"USER#{USER_ID}", "CONVERSATION_STATE"): {
            "PK": f"USER#{USER_ID}",
            "SK": "CONVERSATION_STATE",
            "workflow": "plan_request",
            "request_id": "request-1",
            "expires_at": 1_800_000_000,
        },
        (f"USER#{USER_ID}", "MEAL#2026-08-24#SUBMISSION#1"): {
            "PK": f"USER#{USER_ID}",
            "SK": "MEAL#2026-08-24#SUBMISSION#1",
            "description": "omelette",
            "meal_type": "breakfast",
        },
        (f"USER#{USER_ID}", "MEAL#2026-08-25#SUBMISSION#2"): {
            "PK": f"USER#{USER_ID}",
            "SK": "MEAL#2026-08-25#SUBMISSION#2",
            "description": "soup",
            "meal_type": "lunch",
        },
    }
    for item in related.values():
        table.put_item(Item=item)
    return related


def _items_for_user(table: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Read every item for the test user for byte-for-byte comparisons."""
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(
            f"USER#{USER_ID}"
        )
    )
    return {(item["PK"], item["SK"]): item for item in response["Items"]}


def test_reset_clears_only_dietary_fields_and_advances_revision(
    dynamodb_table: Any,
) -> None:
    """Reset exactly one profile while retaining every other user item."""
    profile = _profile_item()
    related = _seed_related_items(dynamodb_table)
    dynamodb_table.put_item(Item=profile)
    before = _items_for_user(dynamodb_table)

    result = reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)

    assert result == reset.ResetResult(changed=True, profile_revision=5)
    after = _items_for_user(dynamodb_table)
    assert set(after) == set(before)
    updated_profile = after[(f"USER#{USER_ID}", "PROFILE")]
    assert updated_profile["dietary_preferences"] == []
    assert updated_profile["dietary_constraints"] == []
    assert updated_profile["profile_revision"] == 5
    for key, item in before.items():
        if key == (f"USER#{USER_ID}", "PROFILE"):
            retained_before = {
                field: value
                for field, value in item.items()
                if field
                not in {
                    "dietary_preferences",
                    "dietary_constraints",
                    "profile_revision",
                }
            }
            retained_after = {
                field: value
                for field, value in after[key].items()
                if field
                not in {
                    "dietary_preferences",
                    "dietary_constraints",
                    "profile_revision",
                }
            }
            assert retained_after == retained_before
        else:
            assert after[key] == item
    assert all(after[key] == item for key, item in related.items())


def test_reset_rejects_missing_or_malformed_profiles(
    dynamodb_table: Any,
) -> None:
    """Never create or repair a missing or malformed canonical profile."""
    with pytest.raises(reset.ProfileNotFoundError):
        reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)

    malformed = _profile_item()
    malformed.pop("name")
    dynamodb_table.put_item(Item=malformed)
    with pytest.raises(reset.MalformedProfileError):
        reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)

    raw_preference = _profile_item()
    raw_preference["dietary_preferences"] = ["eggs for breakfast"]
    dynamodb_table.put_item(Item=raw_preference)
    with pytest.raises(reset.MalformedProfileError):
        reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)


def test_reset_is_idempotent_and_does_not_advance_twice(
    dynamodb_table: Any,
) -> None:
    """A repeated reset is a safe no-op after the first successful update."""
    dynamodb_table.put_item(Item=_profile_item())

    first = reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)
    second = reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)

    assert first == reset.ResetResult(changed=True, profile_revision=5)
    assert second == reset.ResetResult(changed=False, profile_revision=5)
    item = dynamodb_table.get_item(
        Key={"PK": f"USER#{USER_ID}", "SK": "PROFILE"}
    )["Item"]
    assert item["profile_revision"] == 5
    assert item["dietary_preferences"] == []
    assert item["dietary_constraints"] == []


def test_reset_uses_one_exact_key_without_scan_or_recursive_target(
    mocker: Any,
) -> None:
    """The implementation must use only one keyed read and one keyed write."""
    table = mocker.MagicMock()
    table.get_item.return_value = {"Item": _profile_item()}
    table.update_item.return_value = {}

    reset.reset_profile_dietary_fields(table, USER_ID)

    table.get_item.assert_called_once_with(
        Key={"PK": f"USER#{USER_ID}", "SK": "PROFILE"},
        ConsistentRead=True,
    )
    table.update_item.assert_called_once()
    table.scan.assert_not_called()
    table.query.assert_not_called()
    assert table.update_item.call_args.kwargs["Key"] == {
        "PK": f"USER#{USER_ID}",
        "SK": "PROFILE",
    }
    assert "ConditionExpression" in table.update_item.call_args.kwargs


def test_reset_rejects_conditional_race_without_leaking_aws_details(
    mocker: Any,
) -> None:
    """A concurrent profile change must not be overwritten or exposed."""
    table = mocker.MagicMock()
    table.get_item.return_value = {"Item": _profile_item()}
    table.update_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "secret profile payload should stay private",
            }
        },
        "UpdateItem",
    )

    with pytest.raises(reset.ResetConflictError) as raised:
        reset.reset_profile_dietary_fields(table, USER_ID)

    assert "secret profile payload" not in str(raised.value)
    assert "ConditionalCheckFailedException" not in str(raised.value)


def test_cli_requires_explicit_identifiers_and_rejects_broad_targets(
    mocker: Any,
) -> None:
    """No identifier may silently come from ambient configuration."""
    session = mocker.patch.object(reset.boto3, "Session")
    complete = [
        "--table",
        TABLE_NAME,
        "--profile",
        "meal-planner",
        "--region",
        "us-east-1",
        "--user-id",
        USER_ID,
    ]
    for index in range(0, len(complete), 2):
        missing = complete[:index] + complete[index + 2 :]
        with pytest.raises(SystemExit):
            reset.main(missing)

    assert reset.main(complete[:-1] + ["*"]) == 2
    assert reset.main(complete[:-1] + ["all"]) == 2
    session.assert_not_called()


def test_cli_redacts_aws_errors(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI diagnostics use bounded categories rather than AWS response text."""
    session = mocker.patch.object(reset.boto3, "Session")
    table = session.return_value.resource.return_value.Table.return_value
    table.get_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "credential and profile payload",
            }
        },
        "GetItem",
    )

    result = reset.main(
        [
            "--table",
            TABLE_NAME,
            "--profile",
            "meal-planner",
            "--region",
            "us-east-1",
            "--user-id",
            USER_ID,
        ]
    )

    assert result == 1
    output = capsys.readouterr().err
    assert "credential and profile payload" not in output
    assert "AccessDeniedException" not in output
    assert "AWS request failed" in output


def test_reset_validates_the_stored_profile_with_the_canonical_model(
    dynamodb_table: Any,
) -> None:
    """The reset boundary rejects values the persisted profile cannot parse."""
    item = _profile_item()
    item["family_members"] = [
        {"name": "Alex", "calorie_target": "not-a-number"}
    ]
    dynamodb_table.put_item(Item=item)

    with pytest.raises(reset.MalformedProfileError):
        reset.reset_profile_dietary_fields(dynamodb_table, USER_ID)

    # Keep the import in the test module intentional: this asserts the fixture
    # remains a real canonical profile before the malformed mutation above.
    assert isinstance(make_profile(with_nutrient_targets=True), UserProfile)
