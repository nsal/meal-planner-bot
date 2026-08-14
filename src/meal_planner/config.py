"""Configuration settings for meal planner bot."""

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_PROVIDER_RETRY_DELAY_SECONDS = 5.0


def worst_case_retry_wait_seconds(
    attempts: int,
    initial_backoff_seconds: float,
    *,
    max_delay_seconds: float = MAX_PROVIDER_RETRY_DELAY_SECONDS,
) -> float:
    """Calculate bounded waits between all possible transient attempts."""
    waits = max(attempts - 1, 0)
    exponential_wait = min(initial_backoff_seconds * 2.0, max_delay_seconds)
    return waits * max(exponential_wait, max_delay_seconds)


def external_call_budget_seconds(
    *,
    llm_attempts: int,
    llm_request_timeout_seconds: float,
    llm_initial_backoff_seconds: float,
    telegram_allowance_seconds: float,
    handler_safety_margin_seconds: float,
    telegram_request_count: int = 1,
) -> float:
    """Return the conservative external-call time budget."""
    return (
        llm_attempts * llm_request_timeout_seconds
        + worst_case_retry_wait_seconds(
            llm_attempts, llm_initial_backoff_seconds
        )
        + telegram_request_count * telegram_allowance_seconds
        + handler_safety_margin_seconds
    )


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
    conversational_llm_model: str = Field(
        default="gpt-5.6-luna", alias="CONVERSATIONAL_LLM_MODEL"
    )
    conversational_llm_reasoning_effort: str = Field(
        default="medium", alias="CONVERSATIONAL_LLM_REASONING_EFFORT"
    )
    planner_llm_model: str = Field(
        default="gpt-5.6-terra", alias="PLANNER_LLM_MODEL"
    )
    planner_llm_reasoning_effort: str = Field(
        default="medium", alias="PLANNER_LLM_REASONING_EFFORT"
    )
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    dynamodb_table_name: str = Field(
        default="meal-planner", alias="DYNAMODB_TABLE_NAME"
    )
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    bot_function_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=900,
        alias="BOT_FUNCTION_TIMEOUT_SECONDS",
    )
    planner_function_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=900,
        alias="PLANNER_FUNCTION_TIMEOUT_SECONDS",
    )
    bot_telegram_request_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=20,
        alias="BOT_TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    )
    planner_telegram_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=20,
        alias="PLANNER_TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    )
    bot_llm_request_timeout_seconds: float = Field(
        default=6.0,
        gt=0,
        le=25,
        alias="BOT_LLM_REQUEST_TIMEOUT_SECONDS",
    )
    planner_llm_request_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        le=25,
        alias="PLANNER_LLM_REQUEST_TIMEOUT_SECONDS",
    )
    bot_llm_max_retries: int = Field(
        default=2, ge=1, le=5, alias="BOT_LLM_MAX_RETRIES"
    )
    planner_llm_max_retries: int = Field(
        default=3, ge=1, le=5, alias="PLANNER_LLM_MAX_RETRIES"
    )
    bot_llm_initial_backoff_seconds: float = Field(
        default=1.0,
        ge=0,
        le=5,
        alias="BOT_LLM_INITIAL_BACKOFF_SECONDS",
    )
    planner_llm_initial_backoff_seconds: float = Field(
        default=1.0,
        ge=0,
        le=5,
        alias="PLANNER_LLM_INITIAL_BACKOFF_SECONDS",
    )
    bot_handler_safety_margin_seconds: float = Field(
        default=4.0,
        ge=0,
        le=30,
        alias="BOT_HANDLER_SAFETY_MARGIN_SECONDS",
    )
    planner_handler_safety_margin_seconds: float = Field(
        default=20.0,
        ge=0,
        le=120,
        alias="PLANNER_HANDLER_SAFETY_MARGIN_SECONDS",
    )

    @model_validator(mode="after")
    def validate_function_budgets(self) -> "Settings":
        """Keep worst-case provider and Telegram calls within deadlines."""
        bot_budget = external_call_budget_seconds(
            llm_attempts=self.bot_llm_max_retries,
            llm_request_timeout_seconds=self.bot_llm_request_timeout_seconds,
            llm_initial_backoff_seconds=self.bot_llm_initial_backoff_seconds,
            telegram_allowance_seconds=(
                self.bot_telegram_request_timeout_seconds
            ),
            handler_safety_margin_seconds=self.bot_handler_safety_margin_seconds,
        )
        planner_budget = external_call_budget_seconds(
            llm_attempts=self.planner_llm_max_retries,
            llm_request_timeout_seconds=(
                self.planner_llm_request_timeout_seconds
            ),
            llm_initial_backoff_seconds=(
                self.planner_llm_initial_backoff_seconds
            ),
            telegram_allowance_seconds=(
                self.planner_telegram_request_timeout_seconds
            ),
            handler_safety_margin_seconds=(
                self.planner_handler_safety_margin_seconds
            ),
            telegram_request_count=2,
        )
        if bot_budget > self.bot_function_timeout_seconds:
            raise ValueError(
                "Bot external-call budget exceeds its function timeout"
            )
        if planner_budget > self.planner_function_timeout_seconds:
            raise ValueError(
                "Planner external-call budget exceeds its function timeout"
            )
        return self

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
    def CONVERSATIONAL_LLM_MODEL(self) -> str:
        return self.conversational_llm_model

    @property
    def CONVERSATIONAL_LLM_REASONING_EFFORT(self) -> str:
        return self.conversational_llm_reasoning_effort

    @property
    def PLANNER_LLM_MODEL(self) -> str:
        return self.planner_llm_model

    @property
    def PLANNER_LLM_REASONING_EFFORT(self) -> str:
        return self.planner_llm_reasoning_effort

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
