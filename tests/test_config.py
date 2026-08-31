"""Tests for the separated Bot and Plan Chat settings."""

import pytest
from pydantic import ValidationError

from meal_planner.config import (
    PlanChatSettings,
    Settings,
    get_plan_chat_settings,
    get_settings,
)


def _set_bot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1, 2")


def _set_plan_chat_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("LLM_API_KEY", "key123")


def test_bot_settings_are_minimal_and_do_not_require_llm_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bot settings contain only Bot transport, storage, and access fields."""
    _set_bot_environment(monkeypatch)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("PLAN_CHAT_LLM_MODEL", raising=False)

    settings = get_settings()

    assert settings.telegram_bot_token == "token123"
    assert settings.telegram_allowed_user_ids == frozenset({"1", "2"})
    assert settings.dynamodb_table_name == "meal-planner"
    assert settings.aws_region == "us-east-1"
    assert settings.bot_telegram_request_timeout_seconds == 5.0
    assert "llm_api_key" not in Settings.model_fields
    assert "PLAN_CHAT_LLM_MODEL" not in Settings.model_config


def test_plan_chat_settings_load_model_timeout_telegram_llm_and_dynamo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan Chat settings expose each value consumed by its worker."""
    _set_plan_chat_environment(monkeypatch)
    monkeypatch.setenv("PLAN_CHAT_LLM_MODEL", "custom-model")
    monkeypatch.setenv("PLAN_CHAT_LLM_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("PLAN_CHAT_LLM_REQUEST_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("PLAN_CHAT_TELEGRAM_REQUEST_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "drafts")

    settings = get_plan_chat_settings()

    assert settings.plan_chat_llm_model == "custom-model"
    assert settings.plan_chat_llm_reasoning_effort == "xhigh"
    assert settings.plan_chat_llm_request_timeout_seconds == 120.0
    assert settings.plan_chat_telegram_request_timeout_seconds == 8.0
    assert settings.llm_api_key == "key123"
    assert settings.dynamodb_table_name == "drafts"


def test_plan_chat_settings_reject_empty_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan Chat cannot start without its Telegram and LLM secrets."""
    _set_plan_chat_environment(monkeypatch)

    monkeypatch.setenv("LLM_API_KEY", " ")
    with pytest.raises(ValidationError, match="(?i)llm_api_key"):
        PlanChatSettings()

    monkeypatch.setenv("LLM_API_KEY", "key123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " ")
    with pytest.raises(ValidationError, match="telegram_bot_token"):
        PlanChatSettings()


def test_plan_chat_retry_configuration_is_not_modelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The obsolete retry setting is absent from the settings model."""
    _set_plan_chat_environment(monkeypatch)
    monkeypatch.setenv("PLAN_CHAT_LLM_MAX_RETRIES", "2")

    settings = PlanChatSettings()

    assert "plan_chat_llm_max_retries" not in PlanChatSettings.model_fields
    assert not hasattr(settings, "plan_chat_llm_max_retries")


def test_bot_requires_allowlist_and_rejects_invalid_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bot access remains explicit and limited to positive numeric IDs."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    with pytest.raises(ValidationError, match="(?i)telegram_allowed_user_ids"):
        Settings()

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,")
    with pytest.raises(ValidationError, match="(?i)telegram_allowed_user_ids"):
        Settings()


def test_old_configuration_names_are_ignored_not_modelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy planner and conversational environment names are removed."""
    _set_bot_environment(monkeypatch)
    monkeypatch.setenv("PLANNER_LLM_MODEL", "stale")
    monkeypatch.setenv("CONVERSATIONAL_LLM_MODEL", "stale")

    settings = Settings()

    assert not hasattr(settings, "planner_llm_model")
    assert not hasattr(settings, "conversational_llm_model")
