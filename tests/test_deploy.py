"""Unit tests for the safe local deployment orchestrator."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

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
        "TELEGRAM_BOT_TOKEN_SECRET_NAME": "meal-planner/bot-token",
        "TELEGRAM_WEBHOOK_SECRET_NAME": "meal-planner/webhook-secret",
        "LLM_API_KEY_SECRET_NAME": "meal-planner/llm-key",
    }
    values.update(overrides)
    return deploy.DeploymentSettings(**values)


class FakeRunner(deploy.CommandRunner):
    """Deterministic command boundary for orchestration tests."""

    def __init__(self, *, missing_secret: str | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []
        self.interactive: list[bool] = []
        self.missing_secret = missing_secret

    def run(self, args: Any, **kwargs: Any) -> deploy.CommandResult:
        command = tuple(str(item) for item in args)
        self.commands.append(command)
        self.environments.append(kwargs.get("env"))
        self.interactive.append(bool(kwargs.get("interactive", False)))
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
                                        "OutputKey": "PlannerFunctionName",
                                        "OutputValue": (
                                            "meal-planner-test-planner"
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
        assert len(commands) == 9
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
        "TELEGRAM_BOT_TOKEN_SECRET_NAME=file/bot\n"
        "TELEGRAM_WEBHOOK_SECRET_NAME=file/webhook\n"
        "LLM_API_KEY_SECRET_NAME=file/llm\n",
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
        ("CONVERSATIONAL_LLM_REASONING_EFFORT", "invalid"),
        ("PLANNER_LLM_REASONING_EFFORT", "invalid"),
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
        "TELEGRAM_BOT_TOKEN_SECRET_NAME": "bot",
        "TELEGRAM_WEBHOOK_SECRET_NAME": "webhook",
        "LLM_API_KEY_SECRET_NAME": "llm",
    }
    values[field] = value
    for name, item in values.items():
        monkeypatch.setenv(name, item)

    with pytest.raises(deploy.DeploymentConfigurationError) as error:
        deploy.DeploymentSettings.load()
    assert "secret-token" not in str(error.value)
    assert "llm-secret" not in str(error.value)


def test_cli_modes_are_independent_and_composable() -> None:
    assert deploy.parse_args([]).mode is deploy.DeploymentMode.ROUTINE
    assert deploy.parse_args(["--guided"]).mode is deploy.DeploymentMode.GUIDED
    options = deploy.parse_args(["--guided", "--sync-secrets"])
    assert options.guided is True
    assert options.sync_secrets is True
    assert (
        deploy.parse_args(["--post-deploy-only"]).mode
        is deploy.DeploymentMode.POST_DEPLOY_ONLY
    )


def test_command_runner_redacts_secret_failures(
    mocker: MockerFixture,
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
    runner = FakeRunner(missing_secret="meal-planner/llm-key")

    deploy.synchronize_secrets(runner, settings, requested=True)

    secret_args = [
        argument for command in runner.commands for argument in command
    ]
    assert "bot-secret" not in secret_args
    assert "webhook-secret" not in secret_args
    assert "llm-secret" not in secret_args
    assert any("create-secret" in command for command in runner.commands)


def test_secret_sync_flag_without_env_opt_in_fails_before_aws() -> None:
    settings = _settings(SYNC_SECRETS=False)
    runner = FakeRunner()

    with pytest.raises(deploy.DeploymentError, match="SYNC_SECRETS=true"):
        deploy.synchronize_secrets(runner, settings, requested=True)

    assert runner.commands == []


def test_routine_sam_deploy_resolves_an_artifact_bucket() -> None:
    runner = FakeRunner()
    settings = _settings()

    assert settings.conversational_llm_model == "gpt-5.6-luna"
    assert settings.conversational_llm_reasoning_effort == "medium"
    assert settings.planner_llm_model == "gpt-5.6-luna"
    assert settings.planner_llm_reasoning_effort == "high"

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
    assert "PlannerLlmModel=gpt-5.6-luna" in command
    assert "PlannerLlmReasoningEffort=high" in command


def test_quality_gates_enable_sam_beta_features() -> None:
    runner = FakeRunner()

    deploy.run_quality_gates(runner, _settings())

    build_command = next(
        command
        for command in runner.commands
        if command[:5] == ("uvx", "--from", "aws-sam-cli", "sam", "build")
    )
    assert "--beta-features" in build_command


def test_routine_and_post_deploy_workflows_have_expected_boundaries() -> None:
    settings = _settings()
    runner = FakeRunner()
    FakeTelegram.calls = []

    summary = deploy.run_deployment(
        settings,
        deploy.DeploymentOptions(),
        runner=runner,
        input_fn=lambda _: "yes",
        api_factory=FakeTelegram,
    )

    assert summary.mode is deploy.DeploymentMode.ROUTINE
    assert FakeTelegram.calls == ["commands", "set-webhook", "get-webhook"]
    command_text = [" ".join(command) for command in runner.commands]
    assert command_text.index(
        "aws login --remote --profile meal-planner --region eu-west-1"
    ) < command_text.index("uv run ruff format --check .")
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
    assert "secretsmanager" not in recovery_text
    assert "ruff" not in recovery_text
    assert "verify_transaction_permission.py" in recovery_text
