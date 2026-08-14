"""Tests for the deployed transaction-permission verifier."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

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
    assert "AWS_ACCESS_KEY_ID" not in capsys.readouterr().err


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
    assert "AWS_ACCESS_KEY_ID" not in capsys.readouterr().err


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
