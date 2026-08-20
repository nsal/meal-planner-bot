"""Documentation assertions for user and operator-facing contracts."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
README_PATH = PROJECT_ROOT / "README.md"


def _read_readme() -> str:
    """Load the project README for contract assertions."""
    return README_PATH.read_text(encoding="utf-8")


def _normalized_readme() -> str:
    """Load the README with Markdown line wrapping removed."""
    return " ".join(_read_readme().split())


def test_readme_documents_preference_clarification_contract() -> None:
    """Document supported rules and recoverable interpretation failures."""
    readme = _normalized_readme()

    assert "exact-count rules in natural language" in readme
    assert "asks a focused clarification question" in readme
    assert "original wording stays attached to the same `/plan` workflow" in (
        readme
    )
    assert "your next reply is combined with it" in readme


def test_readme_documents_validation_and_repair_contract() -> None:
    """Document evidence validation, repair, and manual retry behavior."""
    readme = _normalized_readme()

    assert "before anything is persisted or displayed" in readme
    assert "one automatic repair in a fresh asynchronous invocation" in readme
    assert "A second invalid result is terminal" in readme
    assert "can be retried manually with `/plan`" in readme
    assert "cancelled or replaced request cannot save or display a stale" in (
        readme
    )


def test_readme_documents_operator_failure_diagnostics() -> None:
    """Document bounded, sanitized Planner failure diagnostics."""
    readme = _normalized_readme()

    assert "one whole-week provider request per invocation" in readme
    assert (
        "one sanitized CloudWatch warning per failed typed provider attempt"
        in readme
    )
    assert "elapsed_ms" in readme
    assert "do not include prompts, preferences, generated plans" in readme


def test_readme_documents_three_planner_success_sends_and_budget() -> None:
    """Document the complete bounded Planner success delivery budget."""
    readme = _normalized_readme()

    assert "three sequential 10-second Telegram allowances" in readme
    assert "plan, bounded summary, and review follow-up" in readme
    assert "290 seconds" in readme
