"""FastAPI lifespan wiring for process-owned runtime resources."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from runtime.application import ApplicationRuntime
from runtime.factory import build_application_runtime

RuntimeBuilder = Callable[[Settings], Awaitable[ApplicationRuntime]]
SettingsProvider = Callable[[], Settings]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_lifespan(
    *,
    runtime_builder: RuntimeBuilder | None = None,
    settings_provider: SettingsProvider | None = None,
) -> Lifespan:
    """Create an injectable lifespan without allocating resources at import."""

    builder = runtime_builder or build_application_runtime
    provider = settings_provider or get_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = await builder(provider())
        app.state.runtime = runtime
        try:
            yield
        finally:
            try:
                await runtime.aclose()
            finally:
                if getattr(app.state, "runtime", None) is runtime:
                    del app.state.runtime

    return lifespan


__all__ = ["Lifespan", "RuntimeBuilder", "SettingsProvider", "create_lifespan"]
