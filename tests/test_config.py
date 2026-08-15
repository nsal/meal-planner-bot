"""Tests for configuration settings."""

import pytest
from pydantic import ValidationError

from meal_planner.config import Settings, get_settings


def test_config_loading_valid_env(mock_env: None) -> None:
    """Test loading configuration with all valid environment variables."""
    settings = get_settings()
    expected_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert settings.telegram_bot_token == expected_token
    assert settings.llm_api_key == "test-api-key"
    assert settings.conversational_llm_model == "gpt-5.6-luna"
    assert settings.conversational_llm_reasoning_effort == "medium"
    assert settings.planner_llm_model == "gpt-5.6-terra"
    assert settings.planner_llm_reasoning_effort == "medium"
    assert settings.dynamodb_table_name == "test-meal-planner"
    assert settings.aws_region == "us-east-1"
    assert settings.telegram_allowed_user_ids == frozenset({"1", "2"})


def test_config_uppercase_properties(mock_env: None) -> None:
    """Test property aliases for uppercase attribute access."""
    settings = get_settings()
    expected_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert settings.TELEGRAM_BOT_TOKEN == expected_token
    assert settings.LLM_API_KEY == "test-api-key"
    assert settings.CONVERSATIONAL_LLM_MODEL == "gpt-5.6-luna"
    assert settings.CONVERSATIONAL_LLM_REASONING_EFFORT == "medium"
    assert settings.PLANNER_LLM_MODEL == "gpt-5.6-terra"
    assert settings.PLANNER_LLM_REASONING_EFFORT == "medium"
    assert settings.DYNAMODB_TABLE_NAME == "test-meal-planner"
    assert settings.AWS_REGION == "us-east-1"
    assert settings.TELEGRAM_ALLOWED_USER_IDS == frozenset({"1", "2"})


def test_config_default_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test configuration defaults for optional fields."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.delenv("CONVERSATIONAL_LLM_MODEL", raising=False)
    monkeypatch.delenv("CONVERSATIONAL_LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    monkeypatch.delenv("PLANNER_LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("DYNAMODB_TABLE_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    settings = Settings()
    assert settings.telegram_bot_token == "token123"
    assert settings.llm_api_key == "key123"
    assert settings.conversational_llm_model == "gpt-5.6-luna"
    assert settings.conversational_llm_reasoning_effort == "medium"
    assert settings.planner_llm_model == "gpt-5.6-terra"
    assert settings.planner_llm_reasoning_effort == "medium"
    assert settings.dynamodb_table_name == "meal-planner"
    assert settings.aws_region == "us-east-1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", frozenset({"123"})),
        ("123,456", frozenset({"123", "456"})),
        (" 123 , 456 ", frozenset({"123", "456"})),
        ("00123,123", frozenset({"123"})),
    ],
)
def test_config_parses_allowed_user_ids(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: frozenset[str],
) -> None:
    """Allowlist IDs are normalized and deduplicated immutably."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", raw)

    settings = Settings()

    assert settings.telegram_allowed_user_ids == expected
    assert isinstance(settings.telegram_allowed_user_ids, frozenset)


@pytest.mark.parametrize(
    "raw",
    ["", " ", ",123", "123,", "0", "000", "-1", "+1", "1.2", "one"],
)
def test_config_rejects_invalid_allowed_user_ids(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Empty, non-positive, and malformed IDs fail validation."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", raw)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert "telegram_allowed_user_ids" in str(exc_info.value)
    assert "key123" not in str(exc_info.value)


def test_config_rejects_missing_allowed_user_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bot settings require an explicit allowlist."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert "TELEGRAM_ALLOWED_USER_IDS" in str(exc_info.value)


def test_config_missing_required_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test error when TELEGRAM_BOT_TOKEN is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "telegram_bot_token" in str(exc_info.value)


def test_config_missing_required_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test error when LLM_API_KEY is missing."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "llm_api_key" in str(exc_info.value)
