"""FastAPI application factory."""

from fastapi import FastAPI

from api.routers import chat, health, sessions
from app.lifecycle import RuntimeBuilder, SettingsProvider, create_lifespan


def create_app(
    *,
    runtime_builder: RuntimeBuilder | None = None,
    settings_provider: SettingsProvider | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(
        title="Psychological Support Agent",
        lifespan=create_lifespan(
            runtime_builder=runtime_builder,
            settings_provider=settings_provider,
        ),
    )
    app.include_router(health.router, prefix="/health", tags=["health"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
    return app


app = create_app()
