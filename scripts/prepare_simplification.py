"""Prepare the repository for the conversational-draft simplification."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

if not __package__:  # pragma: no cover - used by direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.deploy import CommandExecutionError, CommandRunner


@dataclass(frozen=True, slots=True)
class PreparationStage:
    """One ordered, local-only preparation command."""

    name: str
    command: tuple[str, ...]


PREPARATION_STAGES: tuple[PreparationStage, ...] = (
    PreparationStage(
        "Build SAM artifacts",
        (
            "uvx",
            "--from",
            "aws-sam-cli",
            "sam",
            "build",
            "--beta-features",
        ),
    ),
    PreparationStage("Run Pytest", ("uv", "run", "pytest")),
    PreparationStage("Run Ruff lint", ("uv", "run", "ruff", "check", ".")),
    PreparationStage(
        "Check Ruff formatting",
        ("uv", "run", "ruff", "format", "--check", "."),
    ),
    PreparationStage("Run Mypy", ("uv", "run", "mypy")),
)


def run_preparation(runner: CommandRunner) -> None:
    """Run every local preparation stage, stopping at the first failure."""
    total = len(PREPARATION_STAGES)
    for index, stage in enumerate(PREPARATION_STAGES, start=1):
        print(f"[{index}/{total}] {stage.name}")
        runner.run(
            stage.command,
            stage=stage.name,
            interactive=True,
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    """Run repository preparation and return a process status."""
    if argv:
        print("This command does not accept arguments.", file=sys.stderr)
        return 2
    try:
        run_preparation(runner or CommandRunner())
    except CommandExecutionError as exc:
        print(str(exc), file=sys.stderr)
        return exc.returncode if exc.returncode not in {None, 0} else 1
    print("Repository preparation completed successfully.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
