"""Read-only component health checks for one ApplicationRuntime."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)

from api.health_models import (
    ComponentHealth,
    ComponentStatus,
    ReadinessComponents,
    ReadinessResponse,
)
from llm.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMNotFoundError,
    LLMPermissionError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMRetryExhaustedError,
    LLMServerError,
    LLMTimeoutError,
)
from runtime.application import ApplicationRuntime
from runtime.redis_exceptions import (
    RedisAuthenticationError,
    RedisConnectionError,
    RedisPermissionError,
    RedisProtocolError,
    RedisTimeoutError,
    RedisUnavailableError,
)


class ComponentHealthChecker(Protocol):
    """Stateless component checker used by the readiness aggregator."""

    def is_required(self, runtime: ApplicationRuntime) -> bool:
        """Return whether failure should block readiness."""

    async def check(self, runtime: ApplicationRuntime) -> ComponentHealth:
        """Run one bounded, read-only component check."""


class DatabaseHealthChecker:
    """Check database connectivity and cheap migration state."""

    def is_required(self, runtime: ApplicationRuntime) -> bool:
        return runtime.mode != "in_memory"

    async def check(self, runtime: ApplicationRuntime) -> ComponentHealth:
        required = self.is_required(runtime)
        if not required:
            return ComponentHealth(required=False, status="not_required")
        if runtime.engine is None:
            return ComponentHealth(
                required=True,
                status="missing_configuration",
                error_code="database_missing_configuration",
            )

        started = monotonic()
        try:
            async with asyncio.timeout(runtime.settings.health_check_timeout_seconds):
                async with runtime.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                    if connection.dialect.name != "sqlite":
                        revision = await connection.scalar(
                            text("SELECT version_num FROM alembic_version LIMIT 1")
                        )
                        if not isinstance(revision, str) or not revision.strip():
                            return ComponentHealth(
                                required=True,
                                status="schema_not_migrated",
                                latency_ms=_elapsed_ms(started),
                                error_code="alembic_revision_missing",
                            )
        except (TimeoutError, SQLAlchemyTimeoutError):
            return _failure(required, "timeout", started, "database_timeout")
        except (OperationalError, DBAPIError) as exc:
            status, code = _database_failure(exc)
            return _failure(required, status, started, code)
        except SQLAlchemyError:
            return _failure(required, "unavailable", started, "database_unavailable")
        return ComponentHealth(
            required=True,
            status="ok",
            latency_ms=_elapsed_ms(started),
        )


class RedisHealthChecker:
    """Check the shared Redis pool with read-only PING."""

    def is_required(self, runtime: ApplicationRuntime) -> bool:
        return runtime.redis_required

    async def check(self, runtime: ApplicationRuntime) -> ComponentHealth:
        required = self.is_required(runtime)
        if runtime.redis_client is None:
            return ComponentHealth(
                required=required,
                status=("missing_configuration" if required else "not_configured"),
                error_code=("redis_missing_configuration" if required else None),
            )
        started = monotonic()
        try:
            await runtime.redis_client.ping(
                timeout_seconds=runtime.settings.health_check_timeout_seconds
            )
        except RedisAuthenticationError:
            return _failure(required, "authentication_failed", started, "redis_authentication")
        except RedisPermissionError:
            return _failure(required, "permission_denied", started, "redis_permission")
        except RedisTimeoutError:
            return _failure(required, "timeout", started, "redis_timeout")
        except (RedisConnectionError, RedisUnavailableError):
            return _failure(required, "unavailable", started, "redis_unavailable")
        except RedisProtocolError:
            return _failure(required, "protocol_error", started, "redis_protocol")
        return ComponentHealth(
            required=required,
            status="ok",
            latency_ms=_elapsed_ms(started),
        )


class LLMHealthChecker:
    """Check configured model discovery without a completion or Agent call."""

    def is_required(self, runtime: ApplicationRuntime) -> bool:
        return runtime.mode == "sqlalchemy_model"

    async def check(self, runtime: ApplicationRuntime) -> ComponentHealth:
        required = self.is_required(runtime)
        if not required:
            return ComponentHealth(required=False, status="not_required")
        if runtime.http_client is None:
            return ComponentHealth(
                required=True,
                status="missing_configuration",
                error_code="llm_missing_configuration",
            )
        started = monotonic()
        try:
            async with asyncio.timeout(runtime.settings.health_check_timeout_seconds):
                models = await runtime.http_client.list_models(retry=False)
        except (TimeoutError, LLMTimeoutError):
            return _failure(required, "timeout", started, "llm_timeout")
        except LLMAuthenticationError:
            return _failure(required, "authentication_failed", started, "llm_authentication")
        except LLMPermissionError:
            return _failure(required, "permission_denied", started, "llm_permission")
        except LLMNotFoundError:
            return _failure(required, "unsupported", started, "llm_models_unsupported")
        except LLMConnectionError:
            return _failure(required, "unavailable", started, "llm_unavailable")
        except (LLMProtocolError, LLMRateLimitError, LLMServerError):
            return _failure(required, "protocol_error", started, "llm_protocol")
        except LLMRetryExhaustedError as exc:
            return _llm_retry_failure(required, started, exc)
        except LLMError:
            return _failure(required, "unavailable", started, "llm_unavailable")
        if runtime.settings.structured_model_name not in models:
            return _failure(required, "model_unavailable", started, "llm_model_unavailable")
        return ComponentHealth(
            required=True,
            status="ok",
            latency_ms=_elapsed_ms(started),
        )


class RuntimeHealthChecker:
    """Aggregate fresh component results without caching or side effects."""

    def __init__(
        self,
        *,
        database: ComponentHealthChecker | None = None,
        redis: ComponentHealthChecker | None = None,
        llm: ComponentHealthChecker | None = None,
    ) -> None:
        self._database = database or DatabaseHealthChecker()
        self._redis = redis or RedisHealthChecker()
        self._llm = llm or LLMHealthChecker()

    async def check_readiness(self, runtime: ApplicationRuntime) -> ReadinessResponse:
        """Check all components concurrently and safely map unexpected failures."""

        database, redis, llm = await asyncio.gather(
            _safe_component_check(self._database, runtime),
            _safe_component_check(self._redis, runtime),
            _safe_component_check(self._llm, runtime),
        )
        components = ReadinessComponents(
            database=database,
            redis=redis,
            llm=llm,
        )
        ready = all(
            not component.required or component.status == "ok"
            for component in (database, redis, llm)
        )
        return ReadinessResponse(
            status="ready" if ready else "not_ready",
            runtime_mode=runtime.mode,
            components=components,
        )


async def _safe_component_check(
    checker: ComponentHealthChecker,
    runtime: ApplicationRuntime,
) -> ComponentHealth:
    """Prevent one unknown checker error from hiding other component results."""

    try:
        return await checker.check(runtime)
    except Exception:
        return ComponentHealth(
            required=checker.is_required(runtime),
            status="internal_error",
            error_code="component_internal_error",
        )


def _database_failure(error: DBAPIError) -> tuple[ComponentStatus, str]:
    """Map known SQLSTATE classes without returning driver messages."""

    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate in {"28P01", "28000"}:
        return "authentication_failed", "database_authentication"
    if sqlstate == "3D000":
        return "database_missing", "database_missing"
    if sqlstate == "42P01":
        return "schema_not_migrated", "alembic_table_missing"
    return "unavailable", "database_unavailable"


def _llm_retry_failure(
    required: bool,
    started: float,
    error: LLMRetryExhaustedError,
) -> ComponentHealth:
    """Map a legacy retry wrapper without exposing provider details."""

    if isinstance(error.last_error, LLMTimeoutError):
        return _failure(required, "timeout", started, "llm_timeout")
    return _failure(required, "unavailable", started, "llm_unavailable")


def _failure(
    required: bool,
    status: ComponentStatus,
    started: float,
    error_code: str,
) -> ComponentHealth:
    """Build one safe failure with bounded latency metadata."""

    return ComponentHealth(
        required=required,
        status=status,
        latency_ms=_elapsed_ms(started),
        error_code=error_code,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))


__all__ = [
    "ComponentHealthChecker",
    "DatabaseHealthChecker",
    "LLMHealthChecker",
    "RedisHealthChecker",
    "RuntimeHealthChecker",
]
