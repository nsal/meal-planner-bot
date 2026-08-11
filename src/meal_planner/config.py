"""Configuration settings for meal planner bot."""

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    dynamodb_table_name: str = Field(
        default="meal-planner", alias="DYNAMODB_TABLE_NAME"
    )
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    telegram_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=20,
        alias="TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    )
    llm_request_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        le=25,
        alias="LLM_REQUEST_TIMEOUT_SECONDS",
    )
    llm_max_retries: int = Field(default=3, ge=1, le=5, alias="LLM_MAX_RETRIES")
    llm_initial_backoff_seconds: float = Field(
        default=1.0,
        ge=0,
        le=5,
        alias="LLM_INITIAL_BACKOFF_SECONDS",
    )

    @field_validator("telegram_bot_token", "llm_api_key")
    @classmethod
    def validate_non_empty(cls, v: str, info: ValidationInfo) -> str:
        """Validate that required fields are not empty."""
        if not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v

    @property
    def TELEGRAM_BOT_TOKEN(self) -> str:
        return self.telegram_bot_token

    @property
    def LLM_MODEL(self) -> str:
        return self.llm_model

    @property
    def LLM_API_KEY(self) -> str:
        return self.llm_api_key

    @property
    def DYNAMODB_TABLE_NAME(self) -> str:
        return self.dynamodb_table_name

    @property
    def AWS_REGION(self) -> str:
        return self.aws_region


class WebhookSettings(BaseSettings):
    """Webhook authentication settings loaded from environment variables."""

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
    def validate_non_empty(cls, v: str) -> str:
        """Validate that the webhook secret is not empty."""
        if not v.strip():
            raise ValueError("telegram_webhook_secret must not be empty")
        return v


def get_settings() -> Settings:
    """Return an instance of Settings."""
    return Settings()


def get_webhook_secret() -> str:
    """Return the configured webhook secret without loading other settings."""
    return WebhookSettings().telegram_webhook_secret
