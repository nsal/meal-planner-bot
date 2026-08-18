"""Verify deployed Bot Lambda permission for DynamoDB transactions."""

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)

TRANSACTION_ACTION = "dynamodb:TransactWriteItems"
MAX_ERROR_MESSAGE_CHARS = 2_000
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\b"
    r"(?:\s*[:=]\s*|\s+)[^\s,;]+"
    r"|\b(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
)
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


class VerificationError(Exception):
    """Raised when deployed identifiers or authorization are not valid."""


@dataclass(frozen=True)
class DeploymentResources:
    """Resolved resources used by the authorization simulation."""

    function_name: str
    role_arn: str
    table_name: str
    table_arn: str


def _safe_diagnostic(text: str) -> str:
    """Redact common credential identifiers and bound AWS diagnostics."""
    without_url_userinfo = _URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", text)
    sanitized = _CREDENTIAL_PATTERN.sub("[REDACTED]", without_url_userinfo)
    if len(sanitized) <= MAX_ERROR_MESSAGE_CHARS:
        return sanitized
    return sanitized[:MAX_ERROR_MESSAGE_CHARS] + "..."


def _client_error_diagnostic(exc: ClientError) -> str:
    """Return useful, safe details for an AWS service error."""
    error = exc.response.get("Error", {})
    if isinstance(error, dict):
        code = error.get("Code", "unknown")
        message = error.get("Message", "request failed")
    else:
        code = "unknown"
        message = "request failed"
    safe_code = _safe_diagnostic(str(code))
    safe_message = _safe_diagnostic(str(message))
    return (
        f"AWS API call {exc.operation_name} failed "
        f"({safe_code}): {safe_message}"
    )


def _botocore_error_diagnostic(exc: BotoCoreError) -> str:
    """Return a safe type and message for a botocore failure."""
    return (
        f"AWS client error ({type(exc).__name__}): {_safe_diagnostic(str(exc))}"
    )


def _required_string(value: object, description: str) -> str:
    """Return a non-empty string or raise a safe verification error."""
    if not isinstance(value, str) or not value:
        raise VerificationError(
            f"malformed AWS response: missing {description}"
        )
    return value


def _stack_output(response: object, output_key: str, stack_name: str) -> str:
    """Read one required output from a CloudFormation response."""
    if not isinstance(response, dict):
        raise VerificationError("malformed AWS response from CloudFormation")
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise VerificationError(
            f"stack {stack_name} returned malformed CloudFormation output"
        )
    stack = stacks[0]
    if not isinstance(stack, dict):
        raise VerificationError("malformed AWS response: invalid stack")
    outputs = stack.get("Outputs")
    if not isinstance(outputs, list):
        raise VerificationError("malformed AWS response: invalid stack outputs")
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if output.get("OutputKey") == output_key:
            return _required_string(output.get("OutputValue"), output_key)
    raise VerificationError(f"missing CloudFormation output: {output_key}")


def resolve_resources(
    cloudformation: Any,
    lambda_client: Any,
    dynamodb: Any,
    stack_name: str,
) -> DeploymentResources:
    """Resolve the deployed Bot role and table ARN from stack resources."""
    stack_response = cloudformation.describe_stacks(StackName=stack_name)
    function_name = _stack_output(stack_response, "BotFunctionName", stack_name)
    table_name = _stack_output(
        stack_response, "MealPlannerTableName", stack_name
    )

    function_response = lambda_client.get_function_configuration(
        FunctionName=function_name
    )
    if not isinstance(function_response, dict):
        raise VerificationError("malformed AWS response from Lambda")
    role_arn = _required_string(
        function_response.get("Role"), "Lambda role ARN"
    )

    table_response = dynamodb.describe_table(TableName=table_name)
    if not isinstance(table_response, dict):
        raise VerificationError("malformed AWS response from DynamoDB")
    table = table_response.get("Table")
    if not isinstance(table, dict):
        raise VerificationError(
            "malformed AWS response: invalid DynamoDB table"
        )
    table_arn = _required_string(table.get("TableArn"), "DynamoDB table ARN")

    return DeploymentResources(
        function_name=function_name,
        role_arn=role_arn,
        table_name=table_name,
        table_arn=table_arn,
    )


def verify_permission(iam: Any, resources: DeploymentResources) -> None:
    """Require an explicit allow for the exact deployed table ARN."""
    response = iam.simulate_principal_policy(
        PolicySourceArn=resources.role_arn,
        ActionNames=[TRANSACTION_ACTION],
        ResourceArns=[resources.table_arn],
    )
    if not isinstance(response, dict):
        raise VerificationError("malformed AWS response from IAM")
    results = response.get("EvaluationResults")
    if not isinstance(results, list) or len(results) != 1:
        raise VerificationError("malformed IAM simulation response")
    result = results[0]
    if not isinstance(result, dict):
        raise VerificationError("malformed IAM evaluation result")
    if result.get("EvalActionName") != TRANSACTION_ACTION:
        raise VerificationError("malformed IAM evaluation action")
    if result.get("EvalResourceName") != resources.table_arn:
        raise VerificationError(
            "IAM result does not target the exact table ARN"
        )
    if result.get("EvalDecision") != "allowed":
        decision = result.get("EvalDecision", "missing")
        raise VerificationError(f"IAM transaction permission is {decision}")


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Verify the deployed Bot role can transact on its table."
    )
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS profile (the deployment orchestrator uses meal-planner)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only deployment verification command."""
    args = _parser().parse_args(argv)
    try:
        if args.profile is None:
            # Keep the standalone helper backwards compatible.  The release
            # orchestrator always supplies an explicit profile and therefore
            # takes the profile-aware path below.
            cloudformation = boto3.client(
                "cloudformation", region_name=args.region
            )
            lambda_client = boto3.client("lambda", region_name=args.region)
            dynamodb = boto3.client("dynamodb", region_name=args.region)
            iam = boto3.client("iam", region_name=args.region)
        else:
            session = boto3.Session(
                profile_name=args.profile, region_name=args.region
            )
            cloudformation = session.client("cloudformation")
            lambda_client = session.client("lambda")
            dynamodb = session.client("dynamodb")
            iam = session.client("iam")
        resources = resolve_resources(
            cloudformation, lambda_client, dynamodb, args.stack_name
        )
        verify_permission(iam, resources)
    except ClientError as exc:
        print(_client_error_diagnostic(exc), file=sys.stderr)
        return 1
    except BotoCoreError as exc:
        print(_botocore_error_diagnostic(exc), file=sys.stderr)
        return 1
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"explicitly allows {TRANSACTION_ACTION} on {resources.table_arn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
