"""Documentation assertions for user and operator-facing contracts."""

import re
from pathlib import Path

from meal_planner.telegram.commands import BOT_COMMANDS, render_help

PROJECT_ROOT = Path(__file__).parents[1]
README_PATH = PROJECT_ROOT / "README.md"
PROMPT_PATH = PROJECT_ROOT / "docs" / "prompt.md"


def _read_readme() -> str:
    """Load the project README for contract assertions."""
    return README_PATH.read_text(encoding="utf-8")


def _normalized_readme() -> str:
    """Load the README with Markdown line wrapping removed."""
    return " ".join(_read_readme().split())


def _read_prompt_documentation() -> str:
    """Load the planner prompt documentation."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_profile_command_description_is_consistent_across_surfaces() -> None:
    """Keep the documented profile command aligned with the bot catalogue."""
    readme = _read_readme()
    command_reference = readme.split(
        "The Telegram command menu and `/help` show the same command "
        "reference:\n",
        maxsplit=1,
    )[1].split("\n- `/start` begins onboarding.", maxsplit=1)[0]
    documented = re.search(
        r"^- `/profile` — (?P<description>.+)\.$",
        command_reference,
        re.MULTILINE,
    )
    assert documented is not None

    catalogue_description = next(
        command.description
        for command in BOT_COMMANDS
        if command.name == "profile"
    )
    description = documented.group("description")
    assert description == catalogue_description
    assert f"/profile — {description}" in render_help().splitlines()


def test_readme_documents_preference_clarification_contract() -> None:
    """Document supported rules and recoverable interpretation failures."""
    readme = _normalized_readme()

    assert "structured natural-language rules" in readme
    assert "asks a focused clarification question" in readme
    assert "original wording stays attached to the same `/plan` workflow" in (
        readme
    )
    assert "your next reply is combined with it" in readme


def test_readme_documents_optional_member_nutrient_targets() -> None:
    """Document optional targets and their deterministic amendment grammar."""
    readme = _normalized_readme().lower()

    assert (
        "protein and fibre targets are optional grams/day values per member"
    ) in readme
    assert "name calories" in readme
    assert "name calories protein fibre" in readme
    assert "name grams" in readme
    assert "send `name none`" in readme
    assert "does not block profile completion" in readme
    assert (
        "missed calorie, protein, or fibre targets are not automatically "
        "detected" in readme
    )


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


def test_readme_documents_two_field_priority_and_safe_confirmation() -> None:
    """Document the canonical profile and its safety-first workflow."""
    readme = _normalized_readme()

    assert (
        "two dietary fields: `dietary_constraints` and `dietary_preferences`"
    ) in readme
    assert (
        "dietary constraints > current plan preferences > stored dietary "
        "preferences"
    ) in readme
    assert "review the interpreted meaning before saving" in readme
    assert "constraints cannot be overridden" in readme.lower()
    assert "declared meal names and ingredient items" in readme
    assert "never saves or displays a failing candidate" in readme


def test_readme_documents_rule_strength_override_and_validation_limits() -> (
    None
):
    """Document strictness, override behavior, and validation boundaries."""
    readme = _normalized_readme()

    assert "I'd like eggs for breakfast" in readme
    assert "if convenient" in readme
    assert "three stored egg breakfasts" in readme
    assert "current maximum of two" in readme
    assert "apply only to newly generated plans" in readme
    assert "not medical cross-contamination certification" in readme


def test_prompt_documentation_matches_structured_rule_contract() -> None:
    """Document prompt priority and strict/best-effort rule sections."""
    prompt = " ".join(_read_prompt_documentation().split())

    assert "DIETARY CONSTRAINTS (HIGHEST PRIORITY)" in prompt
    assert "EFFECTIVE STRICT RULES" in prompt
    assert "EFFECTIVE BEST-EFFORT RULES" in prompt
    assert "goals" not in prompt.lower()
    assert "declared meal names and ingredient items" in prompt
    assert "not medical cross-contamination certification" in prompt


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


def test_readme_documents_single_json_secret_configuration() -> None:
    """Document the one-secret environment and synchronization contract."""
    readme = _normalized_readme()

    assert "APP_SECRETS_SECRET_NAME=meal-planner/app-secrets" in readme
    assert "TELEGRAM_BOT_TOKEN_SECRET_NAME" not in readme
    assert "TELEGRAM_WEBHOOK_SECRET_NAME" not in readme
    assert "LLM_API_KEY_SECRET_NAME" not in readme
    assert "one JSON secret" in readme
    assert "complete object" in readme
    assert "individual field updates are not merged" in readme


def test_readme_documents_safe_rotation_and_legacy_cleanup() -> None:
    """Document refresh, webhook ordering, and manual legacy cleanup."""
    readme = _normalized_readme()

    assert "restricted local" in readme
    assert "new unique `SecretRefreshToken`" in readme
    assert "publish the complete JSON object first" in readme
    assert "deploy the Lambda with the new refresh token second" in readme
    assert "register and verify the Telegram webhook" in readme
    assert "legacy Secrets Manager secrets" in readme
    assert "does not automate that destructive cleanup" in readme
    assert "real secret values" not in readme
