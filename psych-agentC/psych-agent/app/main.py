"""FastAPI application factory."""

from fastapi import FastAPI

from api.routers import chat, health


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(title="Psychological Support Agent")
    app.include_router(health.router, prefix="/health", tags=["health"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    return app


app = create_app()
