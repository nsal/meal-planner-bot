"""Unit tests for the safe local deployment orchestrator."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from meal_planner.telegram.commands import BOT_COMMANDS
from scripts import deploy


def _settings(**overrides: Any) -> deploy.DeploymentSettings:
    values: dict[str, Any] = {
        "AWS_PROFILE": "meal-planner",
        "AWS_REGION": "eu-west-1",
        "STACK_NAME": "meal-planner-test",
        "TELEGRAM_BOT_TOKEN": "bot-secret",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
        "LLM_API_KEY": "llm-secret",
        "TELEGRAM_ALLOWED_USER_IDS": "123456789,987654321",
        "APP_SECRETS_SECRET_NAME": "meal-planner/app-secrets",
    }
    values.update(overrides)
    return deploy.DeploymentSettings(**values)


class FakeRunner(deploy.CommandRunner):
    """Deterministic command boundary for orchestration tests."""

    def __init__(
        self,
        *,
        missing_secret: str | None = None,
        failed_stage: str | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []
        self.interactive: list[bool] = []
        self.secret_file_contents: list[str] = []
        self.missing_secret = missing_secret
        self.failed_stage = failed_stage

    def run(self, args: Any, **kwargs: Any) -> deploy.CommandResult:
        command = tuple(str(item) for item in args)
        self.commands.append(command)
        self.environments.append(kwargs.get("env"))
        self.interactive.append(bool(kwargs.get("interactive", False)))
        for argument in command:
            if argument.startswith("file://"):
                self.secret_file_contents.append(
                    Path(argument.removeprefix("file://")).read_text(
                        encoding="utf-8"
                    )
                )
        if kwargs.get("stage") == self.failed_stage:
            raise deploy.CommandExecutionError(
                command,
                1,
                "command failed",
                stderr="SAM preflight failed",
            )
        if command[:2] == ("aws", "--version"):
            return deploy.CommandResult(
                command, 0, "aws-cli/2.32.0 Python/3.14.0", ""
            )
        if command[:2] == ("uv", "--version"):
            return deploy.CommandResult(command, 0, "uv 0.8.0", "")
        if command[:2] == ("uvx", "--version"):
            return deploy.CommandResult(command, 0, "uvx 0.8.0", "")
        if command[0:2] == ("aws", "sts"):
            return deploy.CommandResult(
                command,
                0,
                json.dumps(
                    {
                        "Account": "123456789012",
                        "UserId": "AIDEXAMPLE",
                        "Arn": "arn:aws:iam::123456789012:user/deployer",
                    }
                ),
                "",
            )
        if command[0:2] == ("aws", "cloudformation"):
            return deploy.CommandResult(
                command,
                0,
                json.dumps(
                    {
                        "Stacks": [
                            {
                                "Outputs": [
                                    {
                                        "OutputKey": "WebhookUrl",
                                        "OutputValue": "https://example/webhook",
                                    },
                                    {
                                        "OutputKey": "MealPlannerTableName",
                                        "OutputValue": (
                                            "meal-planner-test-table"
                                        ),
                                    },
                                    {
                                        "OutputKey": "BotFunctionName",
                                        "OutputValue": "meal-planner-test-bot",
                                    },
                                    {
                                        "OutputKey": "PlanChatFunctionName",
                                        "OutputValue": (
                                            "meal-planner-test-plan-chat"
                                        ),
                                    },
                                ]
                            }
                        ]
                    }
                ),
                "",
            )
        if (
            self.missing_secret is not None
            and command[0:2] == ("aws", "secretsmanager")
            and "describe-secret" in command
            and self.missing_secret in command
        ):
            raise deploy.CommandExecutionError(
                command,
                254,
                "command failed",
                stderr="ResourceNotFoundException",
            )
        return deploy.CommandResult(command, 0, "", "")


class FakeTelegram:
    """Telegram boundary that records the post-deployment sequence."""

    calls: list[str] = []

    def __init__(self, token: str) -> None:
        assert token == "bot-secret"

    def set_my_commands(self, commands: Any) -> dict[str, Any]:
        assert len(commands) == len(BOT_COMMANDS)
        self.calls.append("commands")
        return {"ok": True}

    def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        assert url == "https://example/webhook"
        assert secret_token == "webhook-secret"
        self.calls.append("set-webhook")
        return {"ok": True}

    def get_webhook_info(self) -> dict[str, Any]:
        self.calls.append("get-webhook")
        return {"ok": True, "result": {"url": "https://example/webhook"}}


def _stack_outputs() -> deploy.StackOutputs:
    return deploy.StackOutputs(
        webhook_url="https://example/webhook",
        table_name="meal-planner-test-table",
        bot_function_name="meal-planner-test-bot",
        plan_chat_function_name="meal-planner-test-plan-chat",
    )


def test_configure_telegram_tolerates_historical_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class HistoricalErrorTelegram(FakeTelegram):
        def get_webhook_info(self) -> dict[str, Any]:
            self.calls.append("get-webhook")
            return {
                "ok": True,
                "result": {
                    "url": "https://example/webhook",
                    "last_error_message": (
                        "503 Service Unavailable bot-secret webhook-secret "
                        "llm-secret"
                    ),
                    "last_error_date": 1_755_683_212,
                },
            }

    settings = _settings()
    FakeTelegram.calls = []

    with caplog.at_level(logging.WARNING, logger="scripts.deploy"):
        deploy.configure_telegram(
            settings,
            _stack_outputs(),
            api_factory=HistoricalErrorTelegram,
        )

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "503 Service Unavailable" in warnings[0].message
    assert "last_error_date=2025-08-20 09:46:52 UTC" in warnings[0].message
    assert all(
        secret not in warnings[0].message for secret in settings.secret_values
    )
    assert FakeTelegram.calls == ["commands", "set-webhook", "get-webhook"]


def test_configure_telegram_rejects_malformed_webhook_result() -> None:
    class MalformedTelegram(FakeTelegram):
        def get_webhook_info(self) -> dict[str, Any]:
            self.calls.append("get-webhook")
            return {"ok": True, "result": None}

    with pytest.raises(
        deploy.DeploymentError, match="malformed Telegram webhook response"
    ):
        deploy.configure_telegram(
            _settings(),
            _stack_outputs(),
            api_factory=MalformedTelegram,
        )


def test_configure_telegram_rejects_webhook_url_mismatch() -> None:
    class MismatchedTelegram(FakeTelegram):
        def get_webhook_info(self) -> dict[str, Any]:
            self.calls.append("get-webhook")
            return {
                "ok": True,
                "result": {"url": "https://example/different-webhook"},
            }

    with pytest.raises(
        deploy.DeploymentError,
        match="Telegram webhook URL verification failed",
    ):
        deploy.configure_telegram(
            _settings(),
            _stack_outputs(),
            api_factory=MismatchedTelegram,
        )


def test_configure_telegram_preserves_telegram_api_errors() -> None:
    class FailingTelegram(FakeTelegram):
        def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
            raise deploy.TelegramAPIError("Telegram setWebhook failed")

    with pytest.raises(
        deploy.TelegramAPIError, match="Telegram setWebhook failed"
    ):
        deploy.configure_telegram(
            _settings(),
            _stack_outputs(),
            api_factory=FailingTelegram,
        )


def test_settings_load_env_file_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWS_PROFILE=meal-planner\n"
        "AWS_REGION=eu-west-1\n"
        "STACK_NAME=from-file\n"
        "TELEGRAM_BOT_TOKEN=file-token\n"
        "TELEGRAM_WEBHOOK_SECRET=file-webhook\n"
        "LLM_API_KEY=file-key\n"
        "TELEGRAM_ALLOWED_USER_IDS=123\n"
        "APP_SECRETS_SECRET_NAME=file/app-secrets\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STACK_NAME", "from-environment")

    settings = deploy.DeploymentSettings.load(env_file)

    assert settings.stack_name == "from-environment"
    assert settings.telegram_allowed_user_ids == ("123",)
    assert "file-token" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AWS_PROFILE", "other"),
        ("AWS_REGION", "us-east-1"),
        ("STACK_NAME", ""),
        ("TELEGRAM_ALLOWED_USER_IDS", "0,abc"),
        ("ENVIRONMENT", "test"),
        ("PLAN_CHAT_LLM_REASONING_EFFORT", "invalid"),
        ("APP_SECRETS_SECRET_NAME", "   "),
    ],
)
def test_invalid_settings_are_safe(
    field: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values: dict[str, Any] = {
        "AWS_PROFILE": "meal-planner",
        "AWS_REGION": "eu-west-1",
        "STACK_NAME": "stack",
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
        "LLM_API_KEY": "llm-secret",
        "TELEGRAM_ALLOWED_USER_IDS": "123",
        "APP_SECRETS_SECRET_NAME": "app-secrets",
    }
    values[field] = value
    for name, item in values.items():
        monkeypatch.setenv(name, item)

    with pytest.raises(deploy.DeploymentConfigurationError) as error:
        deploy.DeploymentSettings.load()
    assert "secret-token" not in str(error.value)
    assert "llm-secret" not in str(error.value)


def test_settings_require_one_secret_name_and_ignore_legacy_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str] = {
        "AWS_PROFILE": "meal-planner",
        "AWS_REGION": "eu-west-1",
        "STACK_NAME": "stack",
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
        "LLM_API_KEY": "llm-secret",
        "TELEGRAM_ALLOWED_USER_IDS": "123",
        "TELEGRAM_BOT_TOKEN_SECRET_NAME": "legacy-bot",
        "TELEGRAM_WEBHOOK_SECRET_NAME": "legacy-webhook",
        "LLM_API_KEY_SECRET_NAME": "legacy-llm",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("APP_SECRETS_SECRET_NAME", raising=False)

    with pytest.raises(deploy.DeploymentConfigurationError) as error:
        deploy.DeploymentSettings.load()

    assert "APP_SECRETS_SECRET_NAME" in str(error.value)
    assert "secret-token" not in str(error.value)
    assert "legacy-bot" not in str(error.value)


def test_cli_modes_are_independent_and_composable() -> None:
    assert deploy.parse_args([]).mode == "routine"
    assert deploy.parse_args(["--guided"]).mode == "guided"
    options = deploy.parse_args(["--guided", "--sync-secrets"])
    assert options.guided is True
    assert options.sync_secrets is True
    assert deploy.parse_args(["--post-deploy-only"]).mode == "post-deploy-only"


def test_post_deploy_only_help_describes_telegram_recovery(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Recovery help must not imply standalone IAM verification runs."""
    with pytest.raises(SystemExit, match="0"):
        deploy.parse_args(["--post-deploy-only", "--help"])

    help_text = capsys.readouterr().out
    assert "Recover Telegram configuration only." in help_text
    assert "verification configuration" not in help_text


def test_command_runner_redacts_secret_failures(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["tool"],
            1,
            "SAM requires --beta-features for bot-secret",
            "failed with llm-secret",
        ),
    )

    with pytest.raises(deploy.CommandExecutionError) as error:
        deploy.CommandRunner().run(
            ["tool", "--safe-argument"],
            stage="build SAM artifacts",
            input_text="bot-secret",
            sensitive_values=("bot-secret", "llm-secret"),
        )

    message = str(error.value)
    assert "build SAM artifacts failed" in message
    assert "stdout:" in message
    assert "--beta-features" in message
    assert "bot-secret" not in message
    assert "llm-secret" not in message
    assert "Command: tool --safe-argument" in capsys.readouterr().out


def test_command_runner_bounds_verbose_diagnostics(
    mocker: MockerFixture,
) -> None:
    output = "x" * (deploy.MAX_COMMAND_DIAGNOSTIC_CHARS * 2)
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["tool"], 1, output, ""),
    )

    with pytest.raises(deploy.CommandExecutionError) as error:
        deploy.CommandRunner().run(["tool"], stage="verbose failure")

    message = str(error.value)
    assert (
        f"{deploy.MAX_COMMAND_DIAGNOSTIC_CHARS} earlier characters omitted"
        in message
    )
    assert len(message) < len(output)


def test_secret_sync_is_double_opt_in_and_uses_no_secret_arguments() -> None:
    settings = _settings(SYNC_SECRETS=True)
    runner = FakeRunner(missing_secret="meal-planner/app-secrets")

    deploy.synchronize_secrets(runner, settings, requested=True)

    secret_args = [
        argument for command in runner.commands for argument in command
    ]
    assert "bot-secret" not in secret_args
    assert "webhook-secret" not in secret_args
    assert "llm-secret" not in secret_args
    assert settings.secret_string not in secret_args
    assert sum("describe-secret" in command for command in runner.commands) == 1
    assert sum("create-secret" in command for command in runner.commands) == 1
    assert (
        sum("put-secret-value" in command for command in runner.commands) == 0
    )
    assert runner.secret_file_contents == [settings.secret_string]


def test_secret_payload_has_stable_keys_and_serialization() -> None:
    settings = _settings()

    assert settings.secret_payload == {
        "telegram_bot_token": "bot-secret",
        "telegram_webhook_secret": "webhook-secret",
        "llm_api_key": "llm-secret",
    }
    assert settings.secret_string == (
        '{"llm_api_key":"llm-secret",'
        '"telegram_bot_token":"bot-secret",'
        '"telegram_webhook_secret":"webhook-secret"}'
    )


def test_secret_sync_updates_existing_secret_once() -> None:
    settings = _settings(SYNC_SECRETS=True)
    runner = FakeRunner()

    deploy.synchronize_secrets(runner, settings, requested=True)

    assert sum("describe-secret" in command for command in runner.commands) == 1
    assert sum("create-secret" in command for command in runner.commands) == 0
    assert (
        sum("put-secret-value" in command for command in runner.commands) == 1
    )
    assert runner.secret_file_contents == [settings.secret_string]


def test_missing_secret_without_opt_in_fails_safely() -> None:
    settings = _settings(SYNC_SECRETS=False)
    runner = FakeRunner(missing_secret=settings.app_secrets_secret_name)

    with pytest.raises(deploy.DeploymentError) as error:
        deploy.synchronize_secrets(runner, settings, requested=False)

    message = str(error.value)
    assert "secret check failed" in message
    assert settings.secret_string not in message
    assert all(value not in message for value in settings.secret_values)
    assert sum("describe-secret" in command for command in runner.commands) == 1
    assert not any(
        "create-secret" in command or "put-secret-value" in command
        for command in runner.commands
    )


@pytest.mark.parametrize("missing_secret", [False, True])
def test_secret_sync_write_failures_redact_payload_and_values(
    missing_secret: bool,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(SYNC_SECRETS=True)
    calls: list[tuple[str, ...]] = []

    def run_command(
        args: list[str], **_: Any
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        calls.append(command)
        if "describe-secret" in command and missing_secret:
            return subprocess.CompletedProcess(
                args, 254, "", "ResourceNotFoundException"
            )
        if "create-secret" in command or "put-secret-value" in command:
            return subprocess.CompletedProcess(
                args, 1, "", settings.secret_string
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    mocker.patch("subprocess.run", side_effect=run_command)

    with pytest.raises(deploy.CommandExecutionError) as error:
        deploy.synchronize_secrets(
            deploy.CommandRunner(), settings, requested=True
        )

    message = str(error.value)
    command_output = capsys.readouterr().out
    assert settings.secret_string not in message
    assert settings.secret_string not in command_output
    assert all(value not in message for value in settings.secret_values)
    assert all(value not in command_output for value in settings.secret_values)
    assert sum("describe-secret" in command for command in calls) == 1
    assert (
        sum(
            "create-secret" in command or "put-secret-value" in command
            for command in calls
        )
        == 1
    )


def test_secret_sync_flag_without_env_opt_in_fails_before_aws() -> None:
    settings = _settings(SYNC_SECRETS=False)
    runner = FakeRunner()

    with pytest.raises(deploy.DeploymentError, match="SYNC_SECRETS=true"):
        deploy.synchronize_secrets(runner, settings, requested=True)

    assert runner.commands == []


def test_routine_sam_deploy_resolves_an_artifact_bucket() -> None:
    runner = FakeRunner()
    settings = _settings()

    assert settings.plan_chat_llm_model == "gpt-5.6-luna"
    assert settings.plan_chat_llm_reasoning_effort == "high"

    deploy.deploy_sam(
        runner,
        settings,
        guided=False,
        refresh_token_factory=lambda: "refresh-token",
    )

    command = runner.commands[0]
    assert "--resolve-s3" in command
    assert "--no-confirm-changeset" in command
    assert "--no-fail-on-empty-changeset" in command
    assert "PlanChatLlmModel=gpt-5.6-luna" in command
    assert "PlanChatLlmReasoningEffort=high" in command
    assert "AppSecretsSecretName=meal-planner/app-secrets" in command
    assert not any(
        "SecretName=" in argument and "AppSecretsSecretName=" not in argument
        for argument in command
    )


def test_sam_preflight_runs_exact_sam_only_sequence() -> None:
    runner = FakeRunner()
    settings = _settings()

    deploy.run_sam_preflight(runner, settings)

    assert runner.commands == [
        (
            "uvx",
            "--from",
            "aws-sam-cli",
            "sam",
            "validate",
            "--lint",
            "--profile",
            "meal-planner",
            "--region",
            "eu-west-1",
        ),
        (
            "uvx",
            "--from",
            "aws-sam-cli",
            "sam",
            "build",
            "--beta-features",
            "--profile",
            "meal-planner",
            "--region",
            "eu-west-1",
        ),
    ]
    assert all("pytest" not in command for command in runner.commands)
    assert all(command[0] != "uv" for command in runner.commands)
    assert runner.environments == [settings.child_environment()] * 2


def test_failed_sam_preflight_prevents_deployment() -> None:
    runner = FakeRunner(failed_stage="build SAM artifacts")

    with pytest.raises(deploy.CommandExecutionError, match="command failed"):
        deploy.run_deployment(
            _settings(),
            deploy.DeploymentOptions(),
            runner=runner,
            input_fn=lambda _: "yes",
            api_factory=FakeTelegram,
        )

    assert not any(
        "sam deploy" in " ".join(command) for command in runner.commands
    )


def test_routine_and_post_deploy_workflows_have_expected_boundaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings()
    runner = FakeRunner()
    FakeTelegram.calls = []

    deploy.run_deployment(
        settings,
        deploy.DeploymentOptions(),
        runner=runner,
        input_fn=lambda _: "yes",
        api_factory=FakeTelegram,
    )

    assert FakeTelegram.calls == ["commands", "set-webhook", "get-webhook"]
    command_text = [" ".join(command) for command in runner.commands]
    assert command_text.index(
        "aws login --remote --profile meal-planner --region eu-west-1"
    ) < command_text.index(
        "uvx --from aws-sam-cli sam validate --lint --profile meal-planner "
        "--region eu-west-1"
    )
    assert command_text.index(
        "uvx --from aws-sam-cli sam validate --lint --profile meal-planner "
        "--region eu-west-1"
    ) < command_text.index(
        "uvx --from aws-sam-cli sam build --beta-features --profile "
        "meal-planner --region eu-west-1"
    )
    deploy_index = next(
        index
        for index, command in enumerate(command_text)
        if "sam deploy" in command
    )
    assert (
        command_text.index(
            "uvx --from aws-sam-cli sam validate --lint --profile meal-planner "
            "--region eu-west-1"
        )
        < deploy_index
    )
    assert (
        command_text.index(
            "uvx --from aws-sam-cli sam build --beta-features --profile "
            "meal-planner --region eu-west-1"
        )
        < deploy_index
    )
    assert all("pytest" not in command for command in runner.commands)
    assert all("ruff" not in command for command in runner.commands)
    assert all("mypy" not in command for command in runner.commands)
    assert any("sam deploy" in command for command in command_text)

    recovery_runner = FakeRunner()
    deploy.run_deployment(
        settings,
        deploy.DeploymentOptions(post_deploy_only=True),
        runner=recovery_runner,
        input_fn=lambda _: "yes",
        api_factory=FakeTelegram,
    )
    recovery_text = " ".join(
        " ".join(command) for command in recovery_runner.commands
    )
    assert "sam deploy" not in recovery_text
    assert "sam validate" not in recovery_text
    assert "sam build" not in recovery_text
    assert "secretsmanager" not in recovery_text
    assert "ruff" not in recovery_text
    assert "verify_transaction_permission.py" not in recovery_text
    output = capsys.readouterr().out
    assert "7. AWS deployment completed" in output
    assert "3. Skip secret checks (post-deploy-only)" in output
    assert "4. Skip SAM validation, build, and deployment" in output
    assert "9. Register Telegram commands" in output
    assert "10. Set Telegram webhook" in output
    assert "11. Verify Telegram webhook" in output


def test_telegram_failure_identifies_successful_aws_deployment() -> None:
    class FailingTelegram(FakeTelegram):
        def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
            raise deploy.TelegramAPIError("Telegram setWebhook failed")

    with pytest.raises(deploy.PostDeploymentError) as error:
        deploy.run_deployment(
            _settings(),
            deploy.DeploymentOptions(),
            runner=FakeRunner(),
            input_fn=lambda _: "yes",
            api_factory=FailingTelegram,
        )

    message = str(error.value)
    assert "AWS deployment completed" in message
    assert "post-deployment configuration failed" in message
    assert "--post-deploy-only" in message


def test_telegram_webhook_error_formats_historical_delivery_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class HistoricalErrorTelegram(FakeTelegram):
        def get_webhook_info(self) -> dict[str, Any]:
            return {
                "ok": True,
                "result": {
                    "url": "https://example/webhook",
                    "last_error_message": (
                        "Wrong response from the webhook: 503 "
                        "Service Unavailable"
                    ),
                    "last_error_date": 1787220412,
                },
            }

    with caplog.at_level(logging.WARNING, logger="scripts.deploy"):
        deploy.run_deployment(
            _settings(),
            deploy.DeploymentOptions(post_deploy_only=True),
            runner=FakeRunner(),
            input_fn=lambda _: "yes",
            api_factory=HistoricalErrorTelegram,
        )

    messages = [record.message for record in caplog.records]
    assert len(messages) == 1
    message = messages[0]
    assert (
        "last_error_message=Wrong response from the webhook: "
        "503 Service Unavailable"
    ) in message
    assert "last_error_date=2026-08-20 10:06:52 UTC" in message


def test_readme_documents_runner_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert "1. Check deployment prerequisites" in readme
    assert "7. AWS deployment completed" in readme
    assert "7. Announce AWS deployment completion" not in readme
    assert "AWS deployment-completed boundary" in readme
    assert "--post-deploy-only" in readme
    assert "not part of routine, guided, or recovery deployment" in readme
    assert "exact deployed `WebhookUrl`" in normalized_readme
    assert (
        "reported as a warning and does not by itself fail" in normalized_readme
    )
    assert "current webhook status and the Bot Lambda logs" in normalized_readme
    assert (
        "Retained historical delivery metadata is a warning"
        in normalized_readme
    )
    assert "Recovery uses the same webhook contract" in normalized_readme
