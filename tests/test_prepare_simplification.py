"""Tests for the local conversational-draft preparation command."""

from collections.abc import Sequence

import pytest

from scripts import prepare_simplification as preparation
from scripts.deploy import CommandExecutionError, CommandResult, CommandRunner


class RecordingRunner(CommandRunner):
    """Record preparation commands without starting subprocesses."""

    def __init__(self, *, failed_stage: str | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.stages: list[str] = []
        self.interactive_values: list[bool] = []
        self.failed_stage = failed_stage

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
        del env, input_text, sensitive_values
        command = tuple(args)
        self.commands.append(command)
        self.stages.append(stage)
        self.interactive_values.append(interactive)
        if stage == self.failed_stage:
            raise CommandExecutionError(
                command,
                7,
                f"{stage} failed safely",
            )
        return CommandResult(command, 0)


def test_preparation_runs_all_local_stages_in_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The successful command runs the complete local verification sequence."""
    runner = RecordingRunner()

    assert preparation.main(runner=runner) == 0

    assert runner.commands == [
        stage.command for stage in preparation.PREPARATION_STAGES
    ]
    assert runner.stages == [
        stage.name for stage in preparation.PREPARATION_STAGES
    ]
    assert runner.interactive_values == [True] * len(
        preparation.PREPARATION_STAGES
    )
    output = capsys.readouterr().out
    assert "[1/5] Build SAM artifacts" in output
    assert "Repository preparation completed successfully." in output


def test_preparation_stops_at_the_first_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed stage prevents every later command from running."""
    runner = RecordingRunner(failed_stage="Run Pytest")

    assert preparation.main(runner=runner) == 7

    assert runner.commands == [
        preparation.PREPARATION_STAGES[0].command,
        preparation.PREPARATION_STAGES[1].command,
    ]
    assert "Run Pytest failed safely" in capsys.readouterr().err


def test_preparation_rejects_arguments_without_running_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected arguments fail before any local command is launched."""
    runner = RecordingRunner()

    assert preparation.main(["--unexpected"], runner=runner) == 2

    assert runner.commands == []
    assert "does not accept arguments" in capsys.readouterr().err


def test_preparation_commands_do_not_call_aws() -> None:
    """Local preparation never authenticates or invokes the AWS CLI."""
    executable_names = {
        stage.command[0] for stage in preparation.PREPARATION_STAGES
    }

    assert executable_names == {"uv", "uvx"}
    assert all(
        "deploy" not in stage.command
        for stage in preparation.PREPARATION_STAGES
    )
