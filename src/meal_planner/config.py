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

    @field_validator("telegram_bot_token", "llm_api_key")
    @classmethod
    def validate_non_empty(cls, v: str, info: ValidationInfo) -> str:
        """Validate that required fields are not empty."""
        if not v:
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


def get_settings() -> Settings:
    """Return an instance of Settings."""
    return Settings()
