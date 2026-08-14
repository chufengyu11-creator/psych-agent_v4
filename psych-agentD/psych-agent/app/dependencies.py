"""FastAPI dependencies for application-owned runtime resources."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from orchestrator.turn_orchestrator import TurnOrchestrator
from runtime.application import ApplicationRuntime, TurnHandler
from runtime.factory import build_in_memory_orchestrator, get_shared_orchestrator
from runtime.health import RuntimeHealthChecker
from runtime.sqlalchemy_session_closer import SqlAlchemySessionCloser


def get_application_runtime(request: Request) -> ApplicationRuntime:
    """Return the initialized runtime without creating fallback resources."""

    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, ApplicationRuntime):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Application runtime is not initialized",
        )
    return runtime


def get_runtime_orchestrator(
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> TurnHandler:
    """Return the shared turn handler selected during application startup."""

    return runtime.orchestrator


def get_session_closer(
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> SqlAlchemySessionCloser:
    """Return the configured SQLAlchemy session-close runtime."""

    if not isinstance(runtime.session_closer, SqlAlchemySessionCloser):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Session close API requires a SQLAlchemy runtime mode.",
        )
    return runtime.session_closer


def get_health_checker() -> RuntimeHealthChecker:
    """Return a stateless checker that reuses only Runtime-owned resources."""

    return RuntimeHealthChecker()


def get_orchestrator() -> TurnOrchestrator:
    """Return the legacy in-memory singleton for direct tests and local callers."""

    return get_shared_orchestrator()


__all__ = [
    "build_in_memory_orchestrator",
    "get_application_runtime",
    "get_health_checker",
    "get_orchestrator",
    "get_runtime_orchestrator",
    "get_session_closer",
]
