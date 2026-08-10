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
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.dynamodb_table_name == "test-meal-planner"
    assert settings.aws_region == "us-east-1"


def test_config_uppercase_properties(mock_env: None) -> None:
    """Test property aliases for uppercase attribute access."""
    settings = get_settings()
    expected_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert settings.TELEGRAM_BOT_TOKEN == expected_token
    assert settings.LLM_API_KEY == "test-api-key"
    assert settings.LLM_MODEL == "gpt-4o-mini"
    assert settings.DYNAMODB_TABLE_NAME == "test-meal-planner"
    assert settings.AWS_REGION == "us-east-1"


def test_config_default_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test configuration defaults for optional fields."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("DYNAMODB_TABLE_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    settings = Settings()
    assert settings.telegram_bot_token == "token123"
    assert settings.llm_api_key == "key123"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.dynamodb_table_name == "meal-planner"
    assert settings.aws_region == "us-east-1"


def test_config_missing_required_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test error when TELEGRAM_BOT_TOKEN is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "key123")

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "telegram_bot_token" in str(exc_info.value)


def test_config_missing_required_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test error when LLM_API_KEY is missing."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "llm_api_key" in str(exc_info.value)
