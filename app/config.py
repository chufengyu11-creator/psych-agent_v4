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
    llm_model_routes: dict[str, str] = Field(default_factory=dict)

    risk_model_name: str | None = None
    state_model_name: str | None = None
    feedback_model_name: str | None = None
    strategy_model_name: str | None = None
    response_model_name: str | None = None
    output_guard_model_name: str | None = None
    summary_model_name: str | None = None
    session_finalizer_model_name: str | None = None
    memory_curator_model_name: str | None = None

    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_thinking_mode: LLMThinkingMode = "omit"
    async_state_pipeline_enabled: bool = False
    rag_enabled: bool = False
    rag_top_k: int = Field(default=4, ge=1, le=10)
    rag_timeout_ms: int = Field(default=5_000, ge=1, le=10_000)
    rag_max_context_tokens: int = Field(default=1_200, ge=100, le=4_000)
    rag_rerank_enabled: bool = False
    rag_embedding_timeout_ms: int = Field(default=1_200, ge=1, le=30_000)
    rag_embedding_dimensions: int = Field(default=512, ge=1, le=4096)
    embedding_model_enabled: bool = True
    embedding_model_name: str = "/data/agent/psych-agent/psych-agent_v4_test/models/bge-small-zh-v1.5"
    aie_vision_url: str = "http://127.0.0.1:8888"
    multimodal_silence_ms: int = Field(default=900, gt=0)
    multimodal_min_speech_ms: int = Field(default=600, gt=0)
    multimodal_max_utterance_ms: int = Field(default=20_000, gt=0)
    multimodal_rms_threshold: int = Field(default=350, gt=0)

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
            llm_model_routes: dict[str, str] = ...,
            risk_model_name: str | None = ...,
            state_model_name: str | None = ...,
            feedback_model_name: str | None = ...,
            strategy_model_name: str | None = ...,
            response_model_name: str | None = ...,
            output_guard_model_name: str | None = ...,
            summary_model_name: str | None = ...,
            session_finalizer_model_name: str | None = ...,
            memory_curator_model_name: str | None = ...,
            llm_timeout_seconds: float = ...,
            llm_thinking_mode: LLMThinkingMode = ...,
            async_state_pipeline_enabled: bool = ...,
            rag_enabled: bool = ...,
            rag_top_k: int = ...,
            rag_timeout_ms: int = ...,
            rag_max_context_tokens: int = ...,
            rag_rerank_enabled: bool = ...,
            rag_embedding_timeout_ms: int = ...,
            rag_embedding_dimensions: int = ...,
            embedding_model_enabled: bool = ...,
            embedding_model_name: str = ...,
            aie_vision_url: str = ...,
            multimodal_silence_ms: int = ...,
            multimodal_min_speech_ms: int = ...,
            multimodal_max_utterance_ms: int = ...,
            multimodal_rms_threshold: int = ...,
            _env_file: str | Path | None = ...,
        ) -> None: ...

    @field_validator(
        "risk_model_name",
        "state_model_name",
        "feedback_model_name",
        "strategy_model_name",
        "response_model_name",
        "output_guard_model_name",
        "summary_model_name",
        "session_finalizer_model_name",
        "memory_curator_model_name",
        mode="before",
    )
    @classmethod
    def _empty_agent_model_name_uses_default(cls, value: object) -> object:
        """Treat blank optional Agent model overrides as omitted."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_model_routes")
    @classmethod
    def _validate_llm_model_routes(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject blank model aliases or endpoint URLs in explicit routes."""

        normalized: dict[str, str] = {}
        for raw_model_name, raw_base_url in value.items():
            model_name = raw_model_name.strip()
            base_url = raw_base_url.strip()
            if not model_name or not base_url:
                raise ValueError("LLM_MODEL_ROUTES contains a blank model name or URL")
            normalized[model_name] = base_url
        return normalized

    def agent_model_names(self) -> dict[str, str]:
        """Resolve the model selected for each model-backed Agent."""

        return {
            "risk_agent": self.risk_model_name or self.safety_model_name,
            "state_tracker": self.state_model_name or self.structured_model_name,
            "feedback_evaluator": (
                self.feedback_model_name or self.structured_model_name
            ),
            "strategy_planner": (
                self.strategy_model_name or self.structured_model_name
            ),
            "response_agent": self.response_model_name or self.main_model_name,
            "output_guard": (
                self.output_guard_model_name or self.safety_model_name
            ),
            "rolling_summarizer": (
                self.summary_model_name or self.structured_model_name
            ),
            "session_finalizer": (
                self.session_finalizer_model_name or self.structured_model_name
            ),
            "memory_curator": (
                self.memory_curator_model_name or self.structured_model_name
            ),
        }

    def required_llm_model_names(self) -> tuple[str, ...]:
        """Return unique served model IDs required by the selected Agent graph."""

        return tuple(dict.fromkeys(self.agent_model_names().values()))

    def resolved_llm_model_routes(self) -> dict[str, str]:
        """Map every required model to an endpoint, using LLM_BASE_URL as fallback."""

        routes = dict(self.llm_model_routes)
        fallback_models = (
            self.main_model_name,
            self.structured_model_name,
            self.safety_model_name,
            *self.required_llm_model_names(),
        )
        for model_name in fallback_models:
            routes.setdefault(model_name, self.llm_base_url)
        return routes

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
