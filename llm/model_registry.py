"""Model configuration registry for local or remote LLM clients."""

import os
from dataclasses import dataclass, field

from llm.exceptions import LLMConfigurationError


@dataclass(frozen=True)
class ModelConfig:
    """HTTP configuration for one named model endpoint."""

    name: str
    endpoint: str
    timeout_seconds: float = 30.0
    api_key_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def resolved_headers(self) -> dict[str, str]:
        """Return static headers plus Authorization when an API-key env var is set."""

        resolved = dict(self.headers)
        if self.api_key_env:
            api_key = os.getenv(self.api_key_env)
            if not api_key:
                msg = f"environment variable {self.api_key_env} is required for model {self.name}"
                raise LLMConfigurationError(msg)
            resolved.setdefault("Authorization", f"Bearer {api_key}")
        return resolved


class ModelRegistry:
    """In-memory registry for model endpoint lookup."""

    def __init__(
        self,
        models: list[ModelConfig] | None = None,
        default_model: str | None = None,
    ) -> None:
        """Create a registry and optionally seed it with model configs."""

        self._models: dict[str, ModelConfig] = {}
        self._default_model = default_model
        for model in models or []:
            self.register(model)

    @property
    def default_model(self) -> str | None:
        """Return the default model name, if one has been configured."""

        return self._default_model

    def register(self, config: ModelConfig, *, make_default: bool = False) -> None:
        """Register or replace one model config."""

        self._models[config.name] = config
        if make_default or self._default_model is None:
            self._default_model = config.name

    def get(self, model_name: str | None = None) -> ModelConfig:
        """Return a model config by name, falling back to the default."""

        name = model_name or self._default_model
        if not name:
            msg = "no model name provided and no default model configured"
            raise LLMConfigurationError(msg)
        try:
            return self._models[name]
        except KeyError as exc:
            msg = f"unknown model: {name}"
            raise LLMConfigurationError(msg) from exc