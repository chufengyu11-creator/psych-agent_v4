"""Integration tests for FastAPI runtime selection and session close flow."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.main import create_app
from runtime.application import ApplicationRuntime
from runtime.factory import build_application_runtime
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SESSION_STATUS_CLOSED, SessionModel
from storage.models.summary import RollingSummaryVersionModel


def _database_url(path: Path) -> str:
    """Build an async SQLite URL for a temporary database file."""

    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _settings(mode: str, database_url: str | None = None) -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode=mode,
        database_url=database_url or "sqlite+aiosqlite:///:memory:",
        llm_api_key="api-integration-test-key",
        main_model_name="api-integration-model",
        structured_model_name="api-integration-model",
        safety_model_name="api-integration-model",
        _env_file=None,
    )


async def _runtime_builder(settings: Settings) -> ApplicationRuntime:
    return await build_application_runtime(settings)


async def _create_schema(database_url: str) -> None:
    """Create the test schema before the API runtime opens its own engine."""

    load_all_models()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def test_api_keeps_in_memory_chat_as_default() -> None:
    """The legacy API path should keep working without database configuration."""

    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("in_memory"),
    )
    with TestClient(app) as client:
        response = client.post(
            "/chat/turn",
            json={
                "user_id": "api-memory-user",
                "session_id": "api-memory-session",
                "message": "I need one concrete next step.",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_sqlalchemy_api_chat_summary_memory_and_close(
    tmp_path: Path,
) -> None:
    """SQLAlchemy mode should persist chat artifacts and close through the API."""

    database_url = _database_url(tmp_path / "api-runtime.db")
    await _create_schema(database_url)
    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("sqlalchemy_fake", database_url),
    )
    payload = {
        "user_id": "api-sql-user",
        "session_id": "api-sql-session",
    }
    with TestClient(app) as client:
        memory_response = client.put(
            f"/users/{payload['user_id']}/memory",
            json={"enabled": True},
        )
        turn_response = client.post(
            "/chat/turn",
            json={
                **payload,
                "message": "请每次一个小步骤，不要一次太多建议。",
            },
        )
        close_response = client.post(
            "/sessions/close",
            json={**payload, "reason": "integration-test"},
        )

    assert memory_response.status_code == 200
    assert memory_response.json() == {
        "user_id": "api-sql-user",
        "memory_enabled": True,
    }
    assert turn_response.status_code == 200
    assert close_response.status_code == 200
    assert close_response.json() == {
        "session_id": "api-sql-session",
        "candidate_memory_count": 1,
        "memory_write_count": 1,
        "status": "closed",
    }

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            message_count = await connection.scalar(
                select(func.count()).select_from(MessageModel)
            )
            summary_count = await connection.scalar(
                select(func.count()).select_from(RollingSummaryVersionModel)
            )
            memory_count = await connection.scalar(
                select(func.count()).select_from(LongTermMemoryModel)
            )
            session_status = await connection.scalar(
                select(SessionModel.status).where(
                    SessionModel.user_id == "api-sql-user",
                    SessionModel.session_id == "api-sql-session",
                )
            )
    finally:
        await engine.dispose()

    assert message_count == 2
    assert summary_count is not None and summary_count >= 1
    assert memory_count == 1
    assert session_status == SESSION_STATUS_CLOSED


async def test_session_close_api_rejects_another_users_session(
    tmp_path: Path,
) -> None:
    """A foreign user cannot close a session by guessing its display ID."""

    database_url = _database_url(tmp_path / "api-session-ownership.db")
    await _create_schema(database_url)
    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("sqlalchemy_fake", database_url),
    )
    with TestClient(app) as client:
        turn_response = client.post(
            "/chat/turn",
            json={
                "user_id": "A",
                "session_id": "session-1",
                "message": "This session belongs to A.",
            },
        )
        foreign_close = client.post(
            "/sessions/close",
            json={
                "user_id": "B",
                "session_id": "session-1",
                "reason": "unauthorized-test",
            },
        )

    assert turn_response.status_code == 200
    assert foreign_close.status_code == 404

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            owner_status = await connection.scalar(
                select(SessionModel.status).where(
                    SessionModel.user_id == "A",
                    SessionModel.session_id == "session-1",
                )
            )
    finally:
        await engine.dispose()

    assert owner_status != SESSION_STATUS_CLOSED


def test_session_close_api_rejects_in_memory_mode() -> None:
    """Session close is explicitly unavailable for the in-memory runtime."""

    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("in_memory"),
    )
    with TestClient(app) as client:
        response = client.post(
            "/sessions/close",
            json={"user_id": "user", "session_id": "session"},
        )

    assert response.status_code == 501


def test_user_memory_api_rejects_in_memory_mode() -> None:
    """Memory settings require a database-backed runtime."""

    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("in_memory"),
    )
    with TestClient(app) as client:
        response = client.put("/users/user/memory", json={"enabled": True})

    assert response.status_code == 501
