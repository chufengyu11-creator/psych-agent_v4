"""Application settings loaded from environment variables and optional .env files."""

from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.redis_config import has_placeholder_redis_password, parse_redis_url

if TYPE_CHECKING:
    from pathlib import Path

LLMThinkingMode = Literal["omit", "enabled", "disabled"]
AppRuntimeMode = Literal["in_memory", "sqlalchemy_fake", "sqlalchemy_model"]


class Settings(BaseSettings):
    """Typed configuration for the local psychological support application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    app_runtime_mode: AppRuntimeMode = "in_memory"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str = Field(
        default=(
            "postgresql+asyncpg://psych_agent:replace-me@127.0.0.1:5432/psych_agent"
        ),
        repr=False,
    )
    test_database_url: str | None = Field(default=None, repr=False)
    redis_url: SecretStr | None = Field(default=None, repr=False)
    redis_required: bool = False
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0)
    health_check_timeout_seconds: float = Field(default=3.0, gt=0)

    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: SecretStr = SecretStr("replace-me")
    main_model_name: str = "replace-me"
    structured_model_name: str = "replace-me"
    safety_model_name: str = "replace-me"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_thinking_mode: LLMThinkingMode = "omit"

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            app_env: str = ...,
            app_runtime_mode: AppRuntimeMode = ...,
            app_host: str = ...,
            app_port: int = ...,
            log_level: str = ...,
            database_url: str = ...,
            test_database_url: str | None = ...,
            redis_url: SecretStr | str | None = ...,
            redis_required: bool = ...,
            redis_connect_timeout_seconds: float = ...,
            redis_socket_timeout_seconds: float = ...,
            health_check_timeout_seconds: float = ...,
            llm_base_url: str = ...,
            llm_api_key: SecretStr | str = ...,
            main_model_name: str = ...,
            structured_model_name: str = ...,
            safety_model_name: str = ...,
            llm_timeout_seconds: float = ...,
            llm_thinking_mode: LLMThinkingMode = ...,
            _env_file: str | Path | None = ...,
        ) -> None: ...

    @field_validator("redis_url", mode="before")
    @classmethod
    def _empty_redis_url_is_unconfigured(cls, value: object) -> object:
        """Treat an empty environment variable as an omitted optional service."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_redis_configuration(self) -> Self:
        """Validate URL structure without exposing credentials in diagnostics."""

        if self.redis_url is None:
            return self
        value = self.redis_url.get_secret_value()
        parse_redis_url(value)
        if (
            self.app_env.casefold() == "production"
            and has_placeholder_redis_password(value)
        ):
            raise ValueError("production REDIS_URL contains a placeholder password")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-cached application settings."""

    return Settings()
