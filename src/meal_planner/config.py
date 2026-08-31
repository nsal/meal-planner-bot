"""Configuration settings for the Bot and Plan Chat Lambdas."""

import re
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    SettingsConfigDict,
    SettingsError,
)


class BotConfigurationError(RuntimeError):
    """Raised when Bot-only configuration cannot be safely loaded."""


class _BaseApplicationSettings(BaseSettings):
    """Settings shared by both Lambda entry points."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    dynamodb_table_name: str = Field(
        default="meal-planner", alias="DYNAMODB_TABLE_NAME"
    )
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        """Require a usable Telegram bot token."""
        if not value.strip():
            raise ValueError("telegram_bot_token must not be empty")
        return value


class Settings(_BaseApplicationSettings):
    """Minimal Bot settings, including its access-control configuration."""

    bot_telegram_request_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=20,
        alias="BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    )
    telegram_allowed_user_ids: Annotated[frozenset[str], NoDecode] = Field(
        ..., alias="TELEGRAM_ALLOWED_USER_IDS"
    )

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def validate_allowed_user_ids(cls, value: object) -> frozenset[str]:
        """Parse positive numeric Telegram IDs into canonical strings."""
        if not isinstance(value, str):
            raise ValueError(
                "telegram_allowed_user_ids must be a comma-separated list"
            )
        entries = value.split(",")
        if not entries or any(not entry.strip() for entry in entries):
            raise ValueError(
                "telegram_allowed_user_ids must contain non-empty IDs"
            )
        normalized: set[str] = set()
        for entry in entries:
            candidate = entry.strip()
            if not re.fullmatch(r"[0-9]+", candidate):
                raise ValueError(
                    "telegram_allowed_user_ids must contain positive numeric "
                    "IDs"
                )
            canonical = str(int(candidate))
            if canonical == "0":
                raise ValueError(
                    "telegram_allowed_user_ids must contain positive numeric "
                    "IDs"
                )
            normalized.add(canonical)
        return frozenset(normalized)


class PlanChatSettings(_BaseApplicationSettings):
    """Plan Chat settings, including only its LLM and delivery controls."""

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    plan_chat_llm_model: str = Field(
        default="gpt-5.6-luna", alias="PLAN_CHAT_LLM_MODEL"
    )
    plan_chat_llm_reasoning_effort: str = Field(
        default="high", alias="PLAN_CHAT_LLM_REASONING_EFFORT"
    )
    plan_chat_llm_request_timeout_seconds: float = Field(
        default=240.0,
        gt=0,
        le=240,
        alias="PLAN_CHAT_LLM_REQUEST_TIMEOUT_SECONDS",
    )
    plan_chat_telegram_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=20,
        alias="PLAN_CHAT_TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    )

    @field_validator("llm_api_key", "plan_chat_llm_model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """Require non-empty Plan Chat provider configuration."""
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class WebhookSettings(BaseSettings):
    """Webhook authentication settings loaded by the Bot only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_webhook_secret: str = Field(
        default="", alias="TELEGRAM_WEBHOOK_SECRET"
    )

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        """Require a webhook secret for authenticated webhook handling."""
        if not value.strip():
            raise ValueError("telegram_webhook_secret must not be empty")
        return value


def get_settings() -> Settings:
    """Return Bot settings without exposing source values on failure."""
    try:
        return Settings()  # type: ignore[call-arg]
    except SettingsError, ValidationError:
        raise BotConfigurationError("Bot configuration is invalid") from None


def get_plan_chat_settings() -> PlanChatSettings:
    """Return settings required by the Plan Chat Lambda only."""
    return PlanChatSettings()


def get_webhook_secret() -> str:
    """Return the configured webhook secret without loading other settings."""
    return WebhookSettings().telegram_webhook_secret
