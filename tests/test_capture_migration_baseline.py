"""Tests for the read-only AWS migration baseline command."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import EndpointConnectionError

from scripts import capture_migration_baseline as baseline

STACK_NAME = "meal-planner-test"
REGION = "eu-west-1"
CAPTURED_AT = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def _clients() -> tuple[MagicMock, MagicMock, MagicMock]:
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackName": STACK_NAME,
                "StackId": (
                    "arn:aws:cloudformation:eu-west-1:123456789012:"
                    "stack/meal-planner-test/example"
                ),
                "StackStatus": "UPDATE_COMPLETE",
                "Outputs": [
                    {
                        "OutputKey": "PlanChatFunctionName",
                        "OutputValue": "meal-planner-test-plan-chat",
                    },
                    {
                        "OutputKey": "WebhookUrl",
                        "OutputValue": "https://example.invalid/secret-path",
                    },
                ],
            }
        ]
    }
    cloudformation.list_stack_resources.side_effect = [
        {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": "BotFunction",
                    "PhysicalResourceId": "meal-planner-test-bot",
                    "ResourceType": "AWS::Lambda::Function",
                    "ResourceStatus": "UPDATE_COMPLETE",
                },
                {
                    "LogicalResourceId": "MealPlannerTable",
                    "PhysicalResourceId": "meal-planner-test-table",
                    "ResourceType": "AWS::DynamoDB::Table",
                    "ResourceStatus": "UPDATE_COMPLETE",
                },
            ],
            "NextToken": "page-2",
        },
        {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": "PlanChatFunction",
                    "PhysicalResourceId": "meal-planner-test-plan-chat",
                    "ResourceType": "AWS::Lambda::Function",
                    "ResourceStatus": "UPDATE_COMPLETE",
                }
            ]
        },
    ]
    lambda_client = MagicMock()
    lambda_client.get_function_configuration.side_effect = [
        {
            "Role": "arn:aws:iam::123456789012:role/bot-role",
            "Runtime": "python3.14",
            "Environment": {
                "Variables": {
                    "TELEGRAM_BOT_TOKEN": "do-not-record-bot-token",
                    "TABLE_NAME": "meal-planner-test-table",
                }
            },
        },
        {
            "Role": "arn:aws:iam::123456789012:role/planner-role",
            "Runtime": "python3.14",
            "Environment": {
                "Variables": {"LLM_API_KEY": "do-not-record-llm-key"}
            },
        },
    ]
    dynamodb = MagicMock()
    dynamodb.describe_table.return_value = {
        "Table": {
            "TableName": "meal-planner-test-table",
            "TableArn": (
                "arn:aws:dynamodb:eu-west-1:123456789012:"
                "table/meal-planner-test-table"
            ),
            "TableStatus": "ACTIVE",
            "ItemCount": 42,
        }
    }
    return cloudformation, lambda_client, dynamodb


def _session_factory(
    cloudformation: MagicMock,
    lambda_client: MagicMock,
    dynamodb: MagicMock,
) -> tuple[Any, MagicMock]:
    session = MagicMock()
    clients = {
        "cloudformation": cloudformation,
        "lambda": lambda_client,
        "dynamodb": dynamodb,
    }
    session.client.side_effect = lambda service: clients[service]
    factory = MagicMock(return_value=session)
    return factory, session


def test_capture_baseline_records_only_read_only_non_secret_metadata() -> None:
    """The baseline excludes output values, secrets, and DynamoDB records."""
    cloudformation, lambda_client, dynamodb = _clients()

    result = baseline.capture_baseline(
        cloudformation,
        lambda_client,
        dynamodb,
        stack_name=STACK_NAME,
        region=REGION,
        captured_at=CAPTURED_AT,
    )
    serialized = json.dumps(result)

    assert result["captured_at"] == "2026-08-28T12:00:00+00:00"
    assert result["stack"]["output_keys"] == [
        "PlanChatFunctionName",
        "WebhookUrl",
    ]
    assert result["lambdas"][0]["environment_variable_names"] == [
        "TABLE_NAME",
        "TELEGRAM_BOT_TOKEN",
    ]
    assert result["dynamodb_tables"] == [
        {
            "logical_id": "MealPlannerTable",
            "table_name": "meal-planner-test-table",
            "table_arn": (
                "arn:aws:dynamodb:eu-west-1:123456789012:"
                "table/meal-planner-test-table"
            ),
            "status": "ACTIVE",
        }
    ]
    assert "do-not-record" not in serialized
    assert "secret-path" not in serialized
    assert "ItemCount" not in serialized
    cloudformation.describe_stacks.assert_called_once_with(StackName=STACK_NAME)
    cloudformation.list_stack_resources.assert_has_calls(
        [
            call(StackName=STACK_NAME),
            call(StackName=STACK_NAME, NextToken="page-2"),
        ]
    )
    lambda_client.get_function_configuration.assert_has_calls(
        [
            call(FunctionName="meal-planner-test-bot"),
            call(FunctionName="meal-planner-test-plan-chat"),
        ]
    )
    dynamodb.describe_table.assert_called_once_with(
        TableName="meal-planner-test-table"
    )
    dynamodb.scan.assert_not_called()
    dynamodb.update_item.assert_not_called()
    dynamodb.delete_item.assert_not_called()


def test_main_uses_the_requested_target_and_prints_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command uses one explicit AWS target and writes JSON to stdout."""
    cloudformation, lambda_client, dynamodb = _clients()
    factory, session = _session_factory(cloudformation, lambda_client, dynamodb)

    result = baseline.main(
        [
            "--stack-name",
            STACK_NAME,
            "--profile",
            "migration-audit",
            "--region",
            REGION,
        ],
        session_factory=factory,
        captured_at=CAPTURED_AT,
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stack"]["name"] == STACK_NAME
    factory.assert_called_once_with(
        profile_name="migration-audit", region_name=REGION
    )
    session.client.assert_has_calls(
        [call("cloudformation"), call("lambda"), call("dynamodb")]
    )


def test_main_refuses_to_overwrite_an_existing_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Baseline output never silently replaces an existing local file."""
    cloudformation, lambda_client, dynamodb = _clients()
    factory, _ = _session_factory(cloudformation, lambda_client, dynamodb)
    output_path = tmp_path / "baseline.json"
    output_path.write_text("keep-me", encoding="utf-8")

    result = baseline.main(
        ["--stack-name", STACK_NAME, "--output", str(output_path)],
        session_factory=factory,
        captured_at=CAPTURED_AT,
    )

    assert result == 1
    assert output_path.read_text(encoding="utf-8") == "keep-me"
    assert "Baseline capture failed" in capsys.readouterr().err


def test_capture_rejects_malformed_lambda_configuration() -> None:
    """Missing Lambda role identity fails the baseline closed."""
    cloudformation, lambda_client, dynamodb = _clients()
    lambda_client.get_function_configuration.side_effect = [
        {"Runtime": "python3.14"}
    ]

    with pytest.raises(baseline.BaselineError, match="Lambda role ARN"):
        baseline.capture_baseline(
            cloudformation,
            lambda_client,
            dynamodb,
            stack_name=STACK_NAME,
            region=REGION,
            captured_at=CAPTURED_AT,
        )


def test_main_redacts_credentials_from_aws_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS diagnostics cannot expose URL credentials or credential names."""
    session = MagicMock()
    cloudformation = MagicMock()
    cloudformation.describe_stacks.side_effect = EndpointConnectionError(
        endpoint_url=(
            "https://AKIA1234567890ABCDEF:top-secret@example.invalid/"
            "AWS_SECRET_ACCESS_KEY"
        )
    )
    session.client.side_effect = lambda _: cloudformation
    factory = MagicMock(return_value=session)

    result = baseline.main(
        ["--stack-name", STACK_NAME], session_factory=factory
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "top-secret" not in error
    assert "AWS_SECRET_ACCESS_KEY" not in error
    assert "[REDACTED]@example.invalid" in error


def test_capture_rejects_repeated_pagination_tokens() -> None:
    """Malformed pagination cannot loop indefinitely."""
    cloudformation, lambda_client, dynamodb = _clients()
    page = {
        "StackResourceSummaries": [],
        "NextToken": "repeated-token",
    }
    cloudformation.list_stack_resources.side_effect = [page, page]

    with pytest.raises(baseline.BaselineError, match="token repeated"):
        baseline.capture_baseline(
            cloudformation,
            lambda_client,
            dynamodb,
            stack_name=STACK_NAME,
            region=REGION,
            captured_at=CAPTURED_AT,
        )
