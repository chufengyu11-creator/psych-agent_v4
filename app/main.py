"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routers import chat, health, multimodal, sessions, users
from app.lifecycle import RuntimeBuilder, SettingsProvider, create_lifespan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


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
    app.include_router(multimodal.router, prefix="/multimodal", tags=["multimodal"])
    app.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
    app.include_router(users.router, prefix="/users", tags=["users"])
    if WEB_ROOT.exists():
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html")

    return app


app = create_app()
