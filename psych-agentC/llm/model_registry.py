"""Model configuration and per-agent model routing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from llm.exceptions import LLMConfigurationError


@dataclass(frozen=True)
class ModelConfig:
    """HTTP configuration for one OpenAI-compatible model endpoint."""

    name: str
    endpoint: str
    provider_model_name: str | None = None
    timeout_seconds: float = 30.0
    api_key_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject invalid endpoint and timeout configuration early."""

        if not self.name.strip():
            raise ValueError("model name cannot be empty")
        if not self.endpoint.strip():
            raise ValueError("model endpoint cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def resolved_headers(self) -> dict[str, str]:
        """Return static headers plus bearer authorization when configured."""

        resolved = dict(self.headers)
        resolved.setdefault("Content-Type", "application/json")
        if self.api_key_env:
            api_key = os.getenv(self.api_key_env)
            if not api_key:
                raise LLMConfigurationError(
                    f"environment variable {self.api_key_env} is required for model {self.name}"
                )
            resolved.setdefault("Authorization", f"Bearer {api_key}")
        return resolved


class ModelRegistry:
    """In-memory registry for model endpoints and agent-to-model routing."""

    def __init__(
        self,
        models: list[ModelConfig] | None = None,
        default_model: str | None = None,
        agent_models: Mapping[str, str] | None = None,
    ) -> None:
        """Create a registry and optionally seed endpoint and agent mappings."""

        self._models: dict[str, ModelConfig] = {}
        self._default_model = default_model
        self._agent_models: dict[str, str] = dict(agent_models or {})
        for model in models or []:
            self.register(model)
        self._validate_routes()

    @property
    def default_model(self) -> str | None:
        """Return the configured default model name."""

        return self._default_model

    def register(self, config: ModelConfig, *, make_default: bool = False) -> None:
        """Register or replace one endpoint configuration."""

        self._models[config.name] = config
        if make_default or self._default_model is None:
            self._default_model = config.name

    def register_agent_model(self, agent_name: str, model_name: str) -> None:
        """Route one agent name to a registered model."""

        if model_name not in self._models:
            raise LLMConfigurationError(f"unknown model for agent {agent_name}: {model_name}")
        self._agent_models[agent_name] = model_name

    def resolve_model_name(
        self,
        *,
        model_name: str | None = None,
        agent_name: str | None = None,
    ) -> str:
        """Resolve explicit model, then agent route, then default model."""

        selected = model_name
        if selected is None and agent_name is not None:
            selected = self._agent_models.get(agent_name)
        selected = selected or self._default_model
        if selected is None:
            raise LLMConfigurationError("no model name provided and no default model configured")
        if selected not in self._models:
            raise LLMConfigurationError(f"unknown model: {selected}")
        return selected

    def get(self, model_name: str | None = None) -> ModelConfig:
        """Return one model config, falling back to the default route."""

        selected = self.resolve_model_name(model_name=model_name)
        return self._models[selected]

    def get_for_agent(
        self,
        agent_name: str,
        *,
        model_name: str | None = None,
    ) -> ModelConfig:
        """Return the model config selected for one agent."""

        selected = self.resolve_model_name(model_name=model_name, agent_name=agent_name)
        return self._models[selected]

    def _validate_routes(self) -> None:
        """Validate configured default and agent routes after initialization."""

        if self._default_model is not None and self._default_model not in self._models:
            raise LLMConfigurationError(f"unknown default model: {self._default_model}")
        for agent_name, model_name in self._agent_models.items():
            if model_name not in self._models:
                raise LLMConfigurationError(
                    f"unknown model for agent {agent_name}: {model_name}"
                )
