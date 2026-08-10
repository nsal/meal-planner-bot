"""Shared test fixtures."""

import pytest

from meal_planner.config import Settings


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up default environment variables for testing."""
    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    )
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "test-meal-planner")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def test_settings(mock_env: None) -> Settings:
    """Return Settings instance populated with mock environment variables."""
    return Settings()
