"""Tests for the deployed transaction-permission verifier."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from scripts import verify_transaction_permission as verifier

STACK_NAME = "meal-planner-test"
REGION = "us-east-1"
ROLE_ARN = "arn:aws:iam::123456789012:role/bot-role"
TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/test-table"


def _clients(
    *,
    outputs: list[dict[str, str]] | None = None,
    evaluation_results: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "Outputs": outputs
                or [
                    {"OutputKey": "BotFunctionName", "OutputValue": "bot"},
                    {
                        "OutputKey": "MealPlannerTableName",
                        "OutputValue": "table",
                    },
                ]
            }
        ]
    }
    lambda_client = MagicMock()
    lambda_client.get_function_configuration.return_value = {"Role": ROLE_ARN}
    dynamodb = MagicMock()
    dynamodb.describe_table.return_value = {"Table": {"TableArn": TABLE_ARN}}
    iam = MagicMock()
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": evaluation_results
        or [
            {
                "EvalActionName": "dynamodb:TransactWriteItems",
                "EvalResourceName": TABLE_ARN,
                "EvalDecision": "allowed",
            }
        ]
    }
    return {
        "cloudformation": cloudformation,
        "lambda": lambda_client,
        "dynamodb": dynamodb,
        "iam": iam,
    }


def _patch_clients(mocker: Any, clients: dict[str, Any]) -> None:
    mocker.patch.object(
        verifier.boto3,
        "client",
        side_effect=lambda service_name, **_: clients[service_name],
    )


def test_allowed_transaction_permission_returns_zero(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exact explicit allow succeeds."""
    clients = _clients()
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 0
    assert "explicitly allows" in capsys.readouterr().out
    clients["iam"].simulate_principal_policy.assert_called_once_with(
        PolicySourceArn=ROLE_ARN,
        ActionNames=["dynamodb:TransactWriteItems"],
        ResourceArns=[TABLE_ARN],
    )


@pytest.mark.parametrize(
    "evaluation_results",
    [
        [
            {
                "EvalActionName": "dynamodb:TransactWriteItems",
                "EvalResourceName": TABLE_ARN,
                "EvalDecision": "implicitDeny",
            }
        ],
        [
            {
                "EvalActionName": "dynamodb:TransactWriteItems",
                "EvalResourceName": TABLE_ARN,
                "EvalDecision": "explicitDeny",
            }
        ],
        [{}],
    ],
)
def test_denied_or_malformed_authorization_returns_nonzero(
    mocker: Any,
    capsys: pytest.CaptureFixture[str],
    evaluation_results: list[dict[str, str]],
) -> None:
    """Denied and malformed simulation results fail closed."""
    clients = _clients(evaluation_results=evaluation_results)
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    error = capsys.readouterr().err
    assert "IAM transaction permission" in error or "malformed" in error


def test_botocore_error_returns_safe_type_and_message(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-service AWS failures retain a useful safe diagnostic."""
    clients = _clients()
    clients[
        "cloudformation"
    ].describe_stacks.side_effect = EndpointConnectionError(
        endpoint_url="https://example.invalid/AWS_SECRET_ACCESS_KEY"
    )
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    error = capsys.readouterr().err
    assert "EndpointConnectionError" in error
    assert "AWS_SECRET_ACCESS_KEY" not in error


def test_botocore_error_redacts_url_userinfo(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Endpoint diagnostics must not disclose proxy URL credentials."""
    clients = _clients()
    clients[
        "cloudformation"
    ].describe_stacks.side_effect = EndpointConnectionError(
        endpoint_url="https://AKIA1234567890ABCDEF:top-secret@example.invalid"
    )
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    error = capsys.readouterr().err
    assert "EndpointConnectionError" in error
    assert "top-secret" not in error
    assert "[REDACTED]@example.invalid" in error


def test_missing_stack_output_returns_nonzero(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing deployment outputs are reported as verification failures."""
    clients = _clients(
        outputs=[{"OutputKey": "BotFunctionName", "OutputValue": "bot"}]
    )
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    assert "MealPlannerTableName" in capsys.readouterr().err


def test_aws_client_error_returns_nonzero(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """AWS API errors produce a concise credential-safe failure."""
    clients = _clients()
    clients["cloudformation"].describe_stacks.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "secret AWS_ACCESS_KEY_ID should not be printed",
            }
        },
        "DescribeStacks",
    )
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    error = capsys.readouterr().err
    assert "DescribeStacks" in error
    assert "AccessDenied" in error
    assert "[REDACTED]" in error
    assert "AWS_ACCESS_KEY_ID" not in error


def test_mismatched_evaluation_resource_returns_nonzero(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """An allow for another resource is not accepted."""
    clients = _clients(
        evaluation_results=[
            {
                "EvalActionName": "dynamodb:TransactWriteItems",
                "EvalResourceName": "arn:aws:dynamodb:other-table",
                "EvalDecision": "allowed",
            }
        ]
    )
    _patch_clients(mocker, clients)

    assert verifier.main(["--stack-name", STACK_NAME, "--region", REGION]) == 1
    assert "exact table ARN" in capsys.readouterr().err


def test_explicit_profile_uses_profile_aware_session(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    clients = _clients()
    session = mocker.Mock()
    session.client.side_effect = lambda service_name: clients[service_name]
    session_factory = mocker.patch.object(verifier.boto3, "Session")
    session_factory.return_value = session

    assert (
        verifier.main(
            [
                "--stack-name",
                STACK_NAME,
                "--region",
                "eu-west-1",
                "--profile",
                "meal-planner",
            ]
        )
        == 0
    )
    session_factory.assert_called_once_with(
        profile_name="meal-planner", region_name="eu-west-1"
    )
    assert "explicitly allows" in capsys.readouterr().out
