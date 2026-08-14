"""Typed non-sensitive API contracts for liveness and readiness."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.config import AppRuntimeMode

ComponentStatus = Literal[
    "ok",
    "not_required",
    "not_configured",
    "missing_configuration",
    "unavailable",
    "timeout",
    "authentication_failed",
    "permission_denied",
    "protocol_error",
    "model_unavailable",
    "unsupported",
    "schema_not_migrated",
    "database_missing",
    "internal_error",
]


class LivenessResponse(BaseModel):
    """Process-only liveness result."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class ComponentHealth(BaseModel):
    """One dependency result without endpoint details or raw exceptions."""

    model_config = ConfigDict(extra="forbid")

    required: bool
    status: ComponentStatus
    latency_ms: int | None = None
    error_code: str | None = None


class ReadinessComponents(BaseModel):
    """Fixed component set checked by the current application runtime."""

    model_config = ConfigDict(extra="forbid")

    database: ComponentHealth
    redis: ComponentHealth
    llm: ComponentHealth


class ReadinessResponse(BaseModel):
    """Overall readiness derived only from required components."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    runtime_mode: AppRuntimeMode
    components: ReadinessComponents


__all__ = [
    "ComponentHealth",
    "ComponentStatus",
    "LivenessResponse",
    "ReadinessComponents",
    "ReadinessResponse",
]
