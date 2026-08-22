"""Safe, repeatable deployment orchestration for the Meal Planner Bot."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, NoReturn, Self

from pydantic import (
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from meal_planner.telegram.api import TelegramAPI, TelegramAPIError
from meal_planner.telegram.commands import BOT_COMMANDS

AWS_PROFILE = "meal-planner"
AWS_REGION = "eu-west-1"
AWS_CLI_MINIMUM = (2, 32, 0)
DEPLOYMENT_ENVIRONMENTS = ("dev", "prod")
LLM_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_COMMAND_DIAGNOSTIC_CHARS = 8_000


class DeploymentError(RuntimeError):
    """A controlled, credential-safe deployment failure."""


class DeploymentConfigurationError(DeploymentError):
    """Deployment settings are absent or invalid."""


class PostDeploymentError(DeploymentError):
    """A deployment completed but post-deployment configuration failed."""


class DeploymentSettings(BaseSettings):
    """Operator configuration loaded from ``.env`` and environment values."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    aws_profile: str = Field(..., alias="AWS_PROFILE")
    aws_region: str = Field(..., alias="AWS_REGION")
    stack_name: str = Field(..., alias="STACK_NAME")
    environment: str = Field(default="dev", alias="ENVIRONMENT")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN", repr=False)
    telegram_webhook_secret: str = Field(
        ..., alias="TELEGRAM_WEBHOOK_SECRET", repr=False
    )
    llm_api_key: str = Field(..., alias="LLM_API_KEY", repr=False)
    telegram_allowed_user_ids: Annotated[tuple[str, ...], NoDecode] = Field(
        ..., alias="TELEGRAM_ALLOWED_USER_IDS"
    )
    telegram_bot_token_secret_name: str = Field(
        ..., alias="TELEGRAM_BOT_TOKEN_SECRET_NAME"
    )
    telegram_webhook_secret_name: str = Field(
        ..., alias="TELEGRAM_WEBHOOK_SECRET_NAME"
    )
    llm_api_key_secret_name: str = Field(..., alias="LLM_API_KEY_SECRET_NAME")
    sync_secrets: bool = Field(default=False, alias="SYNC_SECRETS")
    conversational_llm_model: str = Field(
        default="gpt-5.6-luna", alias="CONVERSATIONAL_LLM_MODEL"
    )
    conversational_llm_reasoning_effort: str = Field(
        default="medium", alias="CONVERSATIONAL_LLM_REASONING_EFFORT"
    )
    planner_llm_model: str = Field(
        default="gpt-5.6-luna", alias="PLANNER_LLM_MODEL"
    )
    planner_llm_reasoning_effort: str = Field(
        default="high", alias="PLANNER_LLM_REASONING_EFFORT"
    )

    @field_validator(
        "aws_profile",
        "aws_region",
        "stack_name",
        "environment",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "llm_api_key",
        "telegram_bot_token_secret_name",
        "telegram_webhook_secret_name",
        "llm_api_key_secret_name",
        "conversational_llm_model",
        "conversational_llm_reasoning_effort",
        "planner_llm_model",
        "planner_llm_reasoning_effort",
    )
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        if value not in DEPLOYMENT_ENVIRONMENTS:
            raise ValueError(
                "must be one of: " + ", ".join(DEPLOYMENT_ENVIRONMENTS)
            )
        return value

    @field_validator(
        "conversational_llm_reasoning_effort",
        "planner_llm_reasoning_effort",
    )
    @classmethod
    def _validate_reasoning_effort(cls, value: str) -> str:
        if value not in LLM_REASONING_EFFORTS:
            raise ValueError(
                "must be one of: " + ", ".join(LLM_REASONING_EFFORTS)
            )
        return value

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_user_ids(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise ValueError("must be a comma-separated list")

        normalized = tuple(str(item).strip() for item in values)
        if not normalized or any(
            re.fullmatch(r"[1-9][0-9]*", item) is None for item in normalized
        ):
            raise ValueError("must contain positive numeric IDs")
        if len(set(normalized)) != len(normalized):
            raise ValueError("must not contain duplicate IDs")
        return normalized

    @model_validator(mode="after")
    def _validate_aws_target(self) -> Self:
        if self.aws_profile != AWS_PROFILE:
            raise ValueError(f"AWS_PROFILE must be {AWS_PROFILE}")
        if self.aws_region != AWS_REGION:
            raise ValueError(f"AWS_REGION must be {AWS_REGION}")
        return self

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> Self:
        """Load settings while replacing Pydantic's potentially leaky errors."""
        try:
            if env_file is None:
                return cls()  # type: ignore[call-arg]
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        except ValidationError as exc:
            fields = sorted(
                {
                    str(location[0])
                    for error in exc.errors()
                    if (location := error.get("loc"))
                }
            )
            field_text = ", ".join(fields) or "unknown fields"
            raise DeploymentConfigurationError(
                f"invalid deployment configuration fields: {field_text}"
            ) from None

    @property
    def allowed_user_ids_value(self) -> str:
        """Return the SAM parameter representation of the allowlist."""
        return ",".join(self.telegram_allowed_user_ids)

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Return values that must be redacted from diagnostics."""
        return (
            self.telegram_bot_token,
            self.telegram_webhook_secret,
            self.llm_api_key,
        )

    @property
    def secret_specs(self) -> tuple[tuple[str, str], ...]:
        """Return secret name/value pairs in stable template order."""
        return (
            (
                self.telegram_bot_token_secret_name,
                self.telegram_bot_token,
            ),
            (
                self.telegram_webhook_secret_name,
                self.telegram_webhook_secret,
            ),
            (self.llm_api_key_secret_name, self.llm_api_key),
        )

    def child_environment(self) -> dict[str, str]:
        """Return an environment pinned to the deployment account target."""
        environment = dict(os.environ)
        environment.update(
            {
                "AWS_PROFILE": AWS_PROFILE,
                "AWS_REGION": AWS_REGION,
            }
        )
        return environment


@dataclass(frozen=True)
class DeploymentOptions:
    """CLI switches controlling a deployment run."""

    guided: bool = False
    sync_secrets: bool = False
    post_deploy_only: bool = False

    @property
    def mode(self) -> str:
        if self.post_deploy_only:
            return "post-deploy-only"
        if self.guided:
            return "guided"
        return "routine"


@dataclass(frozen=True)
class CommandResult:
    """Safe captured result from an external command."""

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandExecutionError(DeploymentError):
    """An external command failed without exposing its input or output."""

    def __init__(
        self,
        args: Sequence[str],
        returncode: int | None,
        message: str,
        *,
        stderr: str = "",
    ) -> None:
        self.args = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        self.message = message
        super().__init__(message)

    @property
    def is_not_found(self) -> bool:
        """Whether AWS reported a missing Secrets Manager resource."""
        return "ResourceNotFoundException" in self.stderr


def _redact(text: str, sensitive_values: Sequence[str]) -> str:
    """Remove configured secret values from a diagnostic string."""
    redacted = text
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _bounded_diagnostic(text: str) -> str:
    """Keep the useful end of verbose command output."""
    if len(text) <= MAX_COMMAND_DIAGNOSTIC_CHARS:
        return text
    omitted = len(text) - MAX_COMMAND_DIAGNOSTIC_CHARS
    return (
        f"[... {omitted} earlier characters omitted ...]\n"
        f"{text[-MAX_COMMAND_DIAGNOSTIC_CHARS:]}"
    )


def _command_diagnostics(
    stdout: str,
    stderr: str,
    sensitive_values: Sequence[str],
) -> str:
    """Return bounded, redacted output sections for a failed command."""
    sections: list[str] = []
    for label, output in (("stdout", stdout), ("stderr", stderr)):
        if not output.strip():
            continue
        redacted = _redact(output, sensitive_values)
        sections.append(f"{label}:\n{_bounded_diagnostic(redacted).rstrip()}")
    return "\n".join(sections)


class CommandRunner:
    """Run commands without shell interpolation and with safe failures."""

    def __init__(self, *, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root

    def run(
        self,
        args: Sequence[str],
        *,
        stage: str = "external command",
        env: dict[str, str] | None = None,
        interactive: bool = False,
        input_text: str | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> CommandResult:
        """Run one command and raise a sanitized error on failure."""
        command = tuple(str(argument) for argument in args)
        command_text = _redact(shlex.join(command), sensitive_values)
        print(f"Command: {command_text}")
        try:
            completed = subprocess.run(
                list(command),
                check=False,
                cwd=self.project_root,
                env=env,
                input=input_text,
                text=True,
                capture_output=not interactive,
            )
        except (OSError, ValueError) as exc:
            message = _redact(str(exc), sensitive_values)
            raise CommandExecutionError(
                command,
                None,
                f"{stage} could not execute {command[0]}: {message}",
            ) from None

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            safe_stderr = _redact(stderr, sensitive_values)
            diagnostics = _command_diagnostics(stdout, stderr, sensitive_values)
            detail = f"\n{diagnostics}" if diagnostics else ""
            raise CommandExecutionError(
                command,
                completed.returncode,
                (
                    f"{stage} failed: {command_text} exited with code "
                    f"{completed.returncode}{detail}"
                ),
                stderr=safe_stderr,
            )
        return CommandResult(command, completed.returncode, stdout, stderr)


def parse_args(argv: Sequence[str] | None = None) -> DeploymentOptions:
    """Parse deployment mode switches."""
    parser = argparse.ArgumentParser(
        description="Authenticate, deploy, and configure the Meal Planner Bot."
    )
    parser.add_argument(
        "--guided",
        action="store_true",
        help="Use interactive SAM guided deployment.",
    )
    parser.add_argument(
        "--sync-secrets",
        action="store_true",
        help="Enable secret writes when SYNC_SECRETS=true in .env.",
    )
    parser.add_argument(
        "--post-deploy-only",
        action="store_true",
        help="Recover Telegram configuration only.",
    )
    parsed = parser.parse_args(argv)
    return DeploymentOptions(
        guided=parsed.guided,
        sync_secrets=parsed.sync_secrets,
        post_deploy_only=parsed.post_deploy_only,
    )


def _aws_command(
    settings: DeploymentSettings, *parts: str, output_json: bool = False
) -> list[str]:
    """Build an AWS CLI command with the fixed account target."""
    command = ["aws", *parts, "--profile", AWS_PROFILE, "--region", AWS_REGION]
    if output_json:
        command.extend(["--output", "json"])
    return command


def _safe_error(exc: Exception, settings: DeploymentSettings) -> str:
    """Convert an exception to a credential-safe message."""
    return _redact(str(exc), settings.secret_values)


def _announce(number: int, title: str) -> None:
    """Print a stable, human-readable deployment stage heading."""
    print(f"{number}. {title}")


def _post_deployment_message(detail: str) -> str:
    """Explain the recoverable boundary after SAM deployment succeeds."""
    return (
        "AWS deployment completed, but post-deployment configuration failed: "
        f"{detail}. Rerun `uv run python scripts/deploy.py "
        "--post-deploy-only` after correcting the issue."
    )


def _raise_post_deployment_error(
    exc: Exception, settings: DeploymentSettings
) -> NoReturn:
    """Raise the recoverable post-deployment form of a stage failure."""
    raise PostDeploymentError(
        _post_deployment_message(_safe_error(exc, settings))
    ) from None


def _parse_aws_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def check_prerequisites(
    runner: CommandRunner, settings: DeploymentSettings
) -> None:
    """Require supported AWS CLI, uv, and uvx executables."""
    try:
        aws = runner.run(
            ["aws", "--version"],
            stage="check AWS CLI version",
            sensitive_values=settings.secret_values,
        )
        version = _parse_aws_version(aws.stdout or aws.stderr)
        if version is None or version < AWS_CLI_MINIMUM:
            minimum = ".".join(map(str, AWS_CLI_MINIMUM))
            raise DeploymentError(
                f"AWS CLI 2.32.0 or newer is required (minimum {minimum})"
            )
        runner.run(
            ["uv", "--version"],
            stage="check uv version",
            sensitive_values=settings.secret_values,
        )
        runner.run(
            ["uvx", "--version"],
            stage="check uvx version",
            sensitive_values=settings.secret_values,
        )
    except CommandExecutionError as exc:
        raise DeploymentError(
            f"deployment prerequisite failed: {exc}"
        ) from None


@dataclass(frozen=True)
class AwsIdentity:
    """The non-secret identity confirmed before deployment mutations."""

    account: str
    user_id: str
    arn: str


def _parse_identity(stdout: str) -> AwsIdentity:
    """Parse the minimal STS identity contract."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        raise DeploymentError("malformed AWS identity response") from None
    if not isinstance(parsed, dict):
        raise DeploymentError("malformed AWS identity response")
    values = {key: parsed.get(key) for key in ("Account", "UserId", "Arn")}
    if any(
        not isinstance(value, str) or not value for value in values.values()
    ):
        raise DeploymentError("malformed AWS identity response")
    return AwsIdentity(
        account=values["Account"],  # type: ignore[arg-type]
        user_id=values["UserId"],  # type: ignore[arg-type]
        arn=values["Arn"],  # type: ignore[arg-type]
    )


def authenticate_and_confirm(
    runner: CommandRunner,
    settings: DeploymentSettings,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> AwsIdentity:
    """Perform remote login, show the identity, and require confirmation."""
    runner.run(
        _aws_command(settings, "login", "--remote"),
        stage="authenticate with AWS",
        env=settings.child_environment(),
        interactive=True,
        sensitive_values=settings.secret_values,
    )
    result = runner.run(
        _aws_command(settings, "sts", "get-caller-identity", output_json=True),
        stage="resolve AWS identity",
        env=settings.child_environment(),
        sensitive_values=settings.secret_values,
    )
    identity = _parse_identity(result.stdout)
    print(
        "Authenticated AWS identity: "
        f"account={identity.account}, user={identity.user_id}, "
        f"arn={identity.arn}"
    )
    reader = input_fn or input
    try:
        answer = reader("Deploy to this AWS identity? Type 'yes' to continue: ")
    except EOFError, KeyboardInterrupt:
        raise DeploymentError("AWS identity confirmation cancelled") from None
    if answer.strip().lower() != "yes":
        raise DeploymentError("AWS identity confirmation cancelled")
    return identity


@contextmanager
def _secret_file(value: str) -> Iterator[Path]:
    """Expose a secret through a short-lived mode-600 file, not argv."""
    descriptor, path_string = tempfile.mkstemp(prefix="meal-planner-secret-")
    path = Path(path_string)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        os.write(descriptor, value.encode("utf-8"))
        os.close(descriptor)
        yield path
    finally:
        try:
            if path.exists():
                path.write_bytes(b"\x00" * len(value.encode("utf-8")))
                path.unlink()
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass


def synchronize_secrets(
    runner: CommandRunner,
    settings: DeploymentSettings,
    *,
    requested: bool,
) -> None:
    """Verify or optionally sync secrets with double opt-in."""
    if requested and not settings.sync_secrets:
        raise DeploymentError(
            "--sync-secrets requires SYNC_SECRETS=true; no secrets were changed"
        )

    for name, value in settings.secret_specs:
        try:
            runner.run(
                _aws_command(
                    settings,
                    "secretsmanager",
                    "describe-secret",
                    "--secret-id",
                    name,
                ),
                stage=f"check secret {name}",
                env=settings.child_environment(),
                sensitive_values=settings.secret_values,
            )
        except CommandExecutionError as exc:
            if (
                not requested
                or not settings.sync_secrets
                or not exc.is_not_found
            ):
                raise DeploymentError(
                    f"secret check failed for configured secret {name}"
                ) from None
            with _secret_file(value) as secret_path:
                runner.run(
                    _aws_command(
                        settings,
                        "secretsmanager",
                        "create-secret",
                        "--name",
                        name,
                        "--secret-string",
                        f"file://{secret_path}",
                    ),
                    stage=f"create secret {name}",
                    env=settings.child_environment(),
                    sensitive_values=settings.secret_values,
                )
            continue

        if requested and settings.sync_secrets:
            with _secret_file(value) as secret_path:
                runner.run(
                    _aws_command(
                        settings,
                        "secretsmanager",
                        "put-secret-value",
                        "--secret-id",
                        name,
                        "--secret-string",
                        f"file://{secret_path}",
                    ),
                    stage=f"update secret {name}",
                    env=settings.child_environment(),
                    sensitive_values=settings.secret_values,
                )


def run_sam_preflight(
    runner: CommandRunner,
    settings: DeploymentSettings,
    *,
    stages: Sequence[str] = ("validate", "build"),
) -> None:
    """Validate and build the SAM application before deployment."""
    environment = settings.child_environment()
    for command_name in stages:
        if command_name == "validate":
            stage = "validate SAM template"
            command = _aws_sam_command(
                settings, "validate", "--lint", output_json=False
            )
        elif command_name == "build":
            stage = "build SAM artifacts"
            command = _aws_sam_command(
                settings, "build", "--beta-features", output_json=False
            )
        else:
            raise ValueError(f"unsupported SAM preflight stage: {command_name}")
        runner.run(
            command,
            stage=stage,
            env=environment,
            sensitive_values=settings.secret_values,
        )


def _aws_sam_command(
    settings: DeploymentSettings,
    *parts: str,
    output_json: bool = False,
) -> list[str]:
    """Build a SAM command with explicit profile and region parameters."""
    command = ["uvx", "--from", "aws-sam-cli", "sam", *parts]
    command.extend(["--profile", AWS_PROFILE, "--region", AWS_REGION])
    if output_json:
        command.extend(["--output", "json"])
    return command


def deploy_sam(
    runner: CommandRunner,
    settings: DeploymentSettings,
    *,
    guided: bool,
    refresh_token_factory: Callable[[], str] = secrets.token_urlsafe,
) -> str:
    """Build and deploy the stack, returning the non-secret refresh token."""
    parameter_overrides = [
        "--parameter-overrides",
        f"Environment={settings.environment}",
        f"TelegramBotTokenSecretName={settings.telegram_bot_token_secret_name}",
        f"TelegramWebhookSecretName={settings.telegram_webhook_secret_name}",
        f"LlmApiKeySecretName={settings.llm_api_key_secret_name}",
        f"TelegramAllowedUserIds={settings.allowed_user_ids_value}",
        f"ConversationalLlmModel={settings.conversational_llm_model}",
        f"ConversationalLlmReasoningEffort={settings.conversational_llm_reasoning_effort}",
        f"PlannerLlmModel={settings.planner_llm_model}",
        f"PlannerLlmReasoningEffort={settings.planner_llm_reasoning_effort}",
        f"SecretRefreshToken={refresh_token_factory()}",
    ]
    command = _aws_sam_command(
        settings,
        "deploy",
        *(["--guided"] if guided else []),
        "--stack-name",
        settings.stack_name,
        "--capabilities",
        "CAPABILITY_IAM",
        *(
            []
            if guided
            else [
                "--resolve-s3",
                "--no-confirm-changeset",
                "--no-fail-on-empty-changeset",
            ]
        ),
        *parameter_overrides,
    )
    runner.run(
        command,
        stage="deploy SAM stack",
        env=settings.child_environment(),
        interactive=guided,
        sensitive_values=settings.secret_values,
    )
    return parameter_overrides[-1].split("=", 1)[1]


@dataclass(frozen=True)
class StackOutputs:
    """Required CloudFormation outputs for post-deployment configuration."""

    webhook_url: str
    table_name: str
    bot_function_name: str
    planner_function_name: str


def _required_output(outputs: object, key: str) -> str:
    if not isinstance(outputs, list):
        raise DeploymentError("malformed CloudFormation outputs")
    for output in outputs:
        if isinstance(output, dict) and output.get("OutputKey") == key:
            value = output.get("OutputValue")
            if isinstance(value, str) and value.strip():
                return value
    raise DeploymentError(f"missing CloudFormation output: {key}")


def resolve_stack_outputs(
    runner: CommandRunner, settings: DeploymentSettings
) -> StackOutputs:
    """Resolve all outputs required by Telegram and IAM verification."""
    result = runner.run(
        _aws_command(
            settings,
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            settings.stack_name,
            output_json=True,
        ),
        stage="resolve CloudFormation stack outputs",
        env=settings.child_environment(),
        sensitive_values=settings.secret_values,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DeploymentError("malformed CloudFormation response") from None
    if not isinstance(parsed, dict):
        raise DeploymentError("malformed CloudFormation response")
    stacks = parsed.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise DeploymentError("malformed CloudFormation response")
    stack = stacks[0]
    if not isinstance(stack, dict):
        raise DeploymentError("malformed CloudFormation response")
    outputs = stack.get("Outputs")
    return StackOutputs(
        webhook_url=_required_output(outputs, "WebhookUrl"),
        table_name=_required_output(outputs, "MealPlannerTableName"),
        bot_function_name=_required_output(outputs, "BotFunctionName"),
        planner_function_name=_required_output(outputs, "PlannerFunctionName"),
    )


def configure_telegram(
    settings: DeploymentSettings,
    outputs: StackOutputs,
    *,
    api_factory: Callable[[str], TelegramAPI] = TelegramAPI,
    announce: Callable[[str], None] | None = None,
) -> None:
    """Replace the command menu and configure then verify the webhook."""
    api = api_factory(settings.telegram_bot_token)
    if announce is not None:
        announce("Register Telegram commands")
    api.set_my_commands(BOT_COMMANDS)
    if announce is not None:
        announce("Set Telegram webhook")
    api.set_webhook(outputs.webhook_url, settings.telegram_webhook_secret)
    if announce is not None:
        announce("Verify Telegram webhook")
    info = api.get_webhook_info()
    result = info.get("result")
    if not isinstance(result, dict):
        raise DeploymentError("malformed Telegram webhook response")
    if result.get("url") != outputs.webhook_url:
        raise DeploymentError("Telegram webhook URL verification failed")
    if result.get("last_error_message"):
        error_message = result["last_error_message"]
        error_date = result.get("last_error_date")
        if isinstance(error_date, int) and not isinstance(error_date, bool):
            try:
                formatted_error_date = datetime.fromtimestamp(
                    error_date, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
            except OverflowError, OSError, ValueError:
                formatted_error_date = str(error_date)
        else:
            formatted_error_date = str(error_date)
        raise DeploymentError(
            "Telegram webhook reports an error: "
            f"last_error_message={error_message}, "
            f"last_error_date={formatted_error_date}"
        )


def run_deployment(
    settings: DeploymentSettings,
    options: DeploymentOptions,
    *,
    runner: CommandRunner | None = None,
    input_fn: Callable[[str], str] | None = None,
    api_factory: Callable[[str], TelegramAPI] = TelegramAPI,
) -> None:
    """Run the workflow and fail immediately at the first failed stage."""
    command_runner = runner or CommandRunner()
    _announce(1, "Check deployment prerequisites")
    check_prerequisites(command_runner, settings)
    _announce(2, "Authenticate with AWS and confirm identity")
    authenticate_and_confirm(command_runner, settings, input_fn=input_fn)

    if options.post_deploy_only:
        _announce(3, "Skip secret checks (post-deploy-only)")
        _announce(4, "Skip SAM validation, build, and deployment")
        _announce(5, "Resolve CloudFormation stack outputs")
        try:
            outputs = resolve_stack_outputs(command_runner, settings)
        except (CommandExecutionError, DeploymentError) as exc:
            _raise_post_deployment_error(exc, settings)
    else:
        _announce(3, "Check configured Secrets Manager secrets")
        synchronize_secrets(
            command_runner, settings, requested=options.sync_secrets
        )
        _announce(4, "Validate SAM template")
        run_sam_preflight(command_runner, settings, stages=("validate",))
        _announce(5, "Build SAM artifacts")
        run_sam_preflight(command_runner, settings, stages=("build",))
        _announce(6, "Deploy SAM stack")
        deploy_sam(command_runner, settings, guided=options.guided)
        _announce(7, "AWS deployment completed")
        _announce(8, "Resolve CloudFormation stack outputs")
        try:
            outputs = resolve_stack_outputs(command_runner, settings)
        except (CommandExecutionError, DeploymentError) as exc:
            _raise_post_deployment_error(exc, settings)

    try:
        next_stage = 6 if options.post_deploy_only else 9

        def announce_telegram(title: str) -> None:
            nonlocal next_stage
            _announce(next_stage, title)
            next_stage += 1

        configure_telegram(
            settings,
            outputs,
            api_factory=api_factory,
            announce=announce_telegram,
        )
    except TelegramAPIError as exc:
        _raise_post_deployment_error(exc, settings)
    except (CommandExecutionError, DeploymentError) as exc:
        _raise_post_deployment_error(exc, settings)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deployment CLI and print only a non-secret outcome."""
    try:
        options = parse_args(argv)
        settings = DeploymentSettings.load()
        run_deployment(settings, options)
    except (DeploymentError, TelegramAPIError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Deployment cancelled.", file=sys.stderr)
        return 1

    print(
        f"Deployment complete: mode={options.mode}, stack={settings.stack_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
