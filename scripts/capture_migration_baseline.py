"""Capture a read-only AWS baseline before the Plan Chat migration."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)

AWS_PROFILE = "meal-planner"
AWS_REGION = "eu-west-1"
MAX_AWS_TEXT_CHARS = 2_048
MAX_BASELINE_BYTES = 100_000
MAX_RESOURCE_PAGES = 100
MAX_RESOURCES = 500

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\b"
    r"(?:\s*[:=]\s*|\s+)[^\s,;]+"
    r"|\b(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
)
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


class BaselineError(RuntimeError):
    """The requested baseline could not be captured safely."""


def _safe_diagnostic(value: object) -> str:
    """Redact credential-shaped text and bound one diagnostic."""
    text = str(value)
    without_userinfo = _URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", text)
    redacted = _CREDENTIAL_PATTERN.sub("[REDACTED]", without_userinfo)
    return redacted[:MAX_AWS_TEXT_CHARS]


def _required_mapping(value: object, label: str) -> Mapping[str, Any]:
    """Return one AWS mapping or reject a malformed response."""
    if not isinstance(value, Mapping):
        raise BaselineError(f"Malformed AWS response: missing {label}.")
    return value


def _required_text(value: object, label: str) -> str:
    """Return bounded non-empty AWS text."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_AWS_TEXT_CHARS
    ):
        raise BaselineError(f"Malformed AWS response: invalid {label}.")
    return value


def _optional_text(value: object, label: str) -> str | None:
    """Return optional bounded AWS text."""
    if value is None:
        return None
    return _required_text(value, label)


def _stack_description(response: object, stack_name: str) -> Mapping[str, Any]:
    """Resolve exactly one described CloudFormation stack."""
    payload = _required_mapping(response, "CloudFormation payload")
    stacks = payload.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise BaselineError(
            f"Stack {_safe_diagnostic(stack_name)} did not resolve uniquely."
        )
    return _required_mapping(stacks[0], "stack description")


def _output_keys(stack: Mapping[str, Any]) -> list[str]:
    """Return sorted output names without exposing output values."""
    outputs = stack.get("Outputs", [])
    if not isinstance(outputs, list):
        raise BaselineError("Malformed AWS response: invalid stack outputs.")
    keys = [
        _required_text(
            _required_mapping(output, "stack output").get("OutputKey"),
            "stack output key",
        )
        for output in outputs
    ]
    if len(keys) != len(set(keys)):
        raise BaselineError("Malformed AWS response: duplicate stack output.")
    return sorted(keys)


def _resource_summaries(
    cloudformation: Any, stack_name: str
) -> list[dict[str, str]]:
    """List bounded stack resources while handling pagination."""
    resources: list[dict[str, str]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_RESOURCE_PAGES):
        arguments: dict[str, str] = {"StackName": stack_name}
        if next_token is not None:
            arguments["NextToken"] = next_token
        response = _required_mapping(
            cloudformation.list_stack_resources(**arguments),
            "stack resource payload",
        )
        summaries = response.get("StackResourceSummaries")
        if not isinstance(summaries, list):
            raise BaselineError(
                "Malformed AWS response: invalid stack resources."
            )
        for summary_value in summaries:
            summary = _required_mapping(summary_value, "stack resource")
            resources.append(
                {
                    "logical_id": _required_text(
                        summary.get("LogicalResourceId"),
                        "logical resource ID",
                    ),
                    "physical_id": _required_text(
                        summary.get("PhysicalResourceId"),
                        "physical resource ID",
                    ),
                    "resource_type": _required_text(
                        summary.get("ResourceType"),
                        "resource type",
                    ),
                    "status": _required_text(
                        summary.get("ResourceStatus"),
                        "resource status",
                    ),
                }
            )
            if len(resources) > MAX_RESOURCES:
                raise BaselineError("Stack resource count exceeds safe limit.")
        token_value = response.get("NextToken")
        if token_value is None:
            break
        next_token = _required_text(token_value, "pagination token")
        if next_token in seen_tokens:
            raise BaselineError("CloudFormation pagination token repeated.")
        seen_tokens.add(next_token)
    else:
        raise BaselineError("CloudFormation pagination exceeds safe limit.")
    return sorted(resources, key=lambda item: item["logical_id"])


def _lambda_baseline(
    lambda_client: Any, resource: Mapping[str, str]
) -> dict[str, Any]:
    """Capture one Lambda's identity without environment values."""
    response = _required_mapping(
        lambda_client.get_function_configuration(
            FunctionName=resource["physical_id"]
        ),
        "Lambda configuration",
    )
    environment = response.get("Environment", {})
    environment_mapping = _required_mapping(environment, "Lambda environment")
    variables = environment_mapping.get("Variables", {})
    variable_mapping = _required_mapping(
        variables, "Lambda environment variables"
    )
    variable_names = sorted(
        _required_text(key, "environment variable name")
        for key in variable_mapping
    )
    return {
        "logical_id": resource["logical_id"],
        "function_name": resource["physical_id"],
        "role_arn": _required_text(response.get("Role"), "Lambda role ARN"),
        "runtime": _optional_text(response.get("Runtime"), "Lambda runtime"),
        "environment_variable_names": variable_names,
    }


def _table_baseline(
    dynamodb: Any, resource: Mapping[str, str]
) -> dict[str, Any]:
    """Capture one DynamoDB table identity without reading table items."""
    response = _required_mapping(
        dynamodb.describe_table(TableName=resource["physical_id"]),
        "DynamoDB table payload",
    )
    table = _required_mapping(response.get("Table"), "DynamoDB table")
    return {
        "logical_id": resource["logical_id"],
        "table_name": _required_text(table.get("TableName"), "table name"),
        "table_arn": _required_text(table.get("TableArn"), "table ARN"),
        "status": _required_text(table.get("TableStatus"), "table status"),
    }


def capture_baseline(
    cloudformation: Any,
    lambda_client: Any,
    dynamodb: Any,
    *,
    stack_name: str,
    region: str,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Capture bounded, non-secret deployment metadata using read APIs."""
    stack = _stack_description(
        cloudformation.describe_stacks(StackName=stack_name), stack_name
    )
    resources = _resource_summaries(cloudformation, stack_name)
    lambdas = [
        _lambda_baseline(lambda_client, resource)
        for resource in resources
        if resource["resource_type"] == "AWS::Lambda::Function"
    ]
    tables = [
        _table_baseline(dynamodb, resource)
        for resource in resources
        if resource["resource_type"] == "AWS::DynamoDB::Table"
    ]
    timestamp = captured_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BaselineError("Capture timestamp must be timezone-aware.")
    return {
        "schema_version": 1,
        "captured_at": timestamp.astimezone(timezone.utc).isoformat(),
        "region": region,
        "stack": {
            "name": _required_text(stack.get("StackName"), "stack name"),
            "id": _required_text(stack.get("StackId"), "stack ID"),
            "status": _required_text(stack.get("StackStatus"), "stack status"),
            "output_keys": _output_keys(stack),
        },
        "resources": resources,
        "lambdas": lambdas,
        "dynamodb_tables": tables,
    }


def _serialize_baseline(baseline: Mapping[str, Any]) -> str:
    """Serialize one bounded baseline deterministically."""
    text = json.dumps(baseline, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_BASELINE_BYTES:
        raise BaselineError("Migration baseline exceeds safe output limit.")
    return text


def _write_output(text: str, output_path: Path | None) -> None:
    """Write to stdout or create one explicitly requested new file."""
    if output_path is None:
        print(text, end="")
        return
    with output_path.open("x", encoding="utf-8") as output_file:
        output_file.write(text)


def _parser() -> argparse.ArgumentParser:
    """Build the read-only baseline command parser."""
    parser = argparse.ArgumentParser(
        description="Capture a read-only AWS migration baseline."
    )
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--profile", default=AWS_PROFILE)
    parser.add_argument("--region", default=AWS_REGION)
    parser.add_argument("--output", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[..., Any] = boto3.Session,
    captured_at: datetime | None = None,
) -> int:
    """Capture the requested baseline and return a safe process status."""
    args = _parser().parse_args(argv)
    try:
        session = session_factory(
            profile_name=args.profile,
            region_name=args.region,
        )
        baseline = capture_baseline(
            session.client("cloudformation"),
            session.client("lambda"),
            session.client("dynamodb"),
            stack_name=args.stack_name,
            region=args.region,
            captured_at=captured_at,
        )
        _write_output(_serialize_baseline(baseline), args.output)
    except (BaselineError, BotoCoreError, ClientError, OSError) as exc:
        print(
            f"Baseline capture failed: {_safe_diagnostic(exc)}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
