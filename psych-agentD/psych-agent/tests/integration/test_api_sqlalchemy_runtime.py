"""Integration tests for FastAPI runtime selection and session close flow."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
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
from storage.models.user import UserModel


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


async def test_sqlalchemy_api_default_user_closes_without_memory_write(
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

    assert turn_response.status_code == 200
    assert close_response.status_code == 200
    assert close_response.json() == {
        "session_id": "api-sql-session",
        "candidate_memory_count": 1,
        "memory_write_count": 0,
        "status": "closed",
    }

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            message_count = await connection.scalar(select(func.count()).select_from(MessageModel))
            summary_count = await connection.scalar(
                select(func.count()).select_from(RollingSummaryVersionModel)
            )
            memory_count = await connection.scalar(
                select(func.count()).select_from(LongTermMemoryModel)
            )
            session_status = await connection.scalar(
                select(SessionModel.status).where(SessionModel.id == "api-sql-session")
            )
            memory_enabled = await connection.scalar(
                select(UserModel.memory_enabled).where(UserModel.id == "api-sql-user")
            )
    finally:
        await engine.dispose()

    assert message_count == 2
    assert summary_count is not None and summary_count >= 1
    assert memory_count == 0
    assert session_status == SESSION_STATUS_CLOSED
    assert memory_enabled is False


async def test_sqlalchemy_api_authorized_user_writes_memory(
    tmp_path: Path,
) -> None:
    """Persisted user consent, not close input, enables a memory write."""

    database_url = _database_url(tmp_path / "api-runtime-authorized.db")
    await _create_schema(database_url)
    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings("sqlalchemy_fake", database_url),
    )
    payload = {
        "user_id": "api-authorized-user",
        "session_id": "api-authorized-session",
    }
    with TestClient(app) as client:
        turn_response = client.post(
            "/chat/turn",
            json={
                **payload,
                "message": "请每次一个小步骤，不要一次太多建议。",
            },
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    update(UserModel)
                    .where(UserModel.id == payload["user_id"])
                    .values(memory_enabled=True)
                )
        finally:
            await engine.dispose()
        close_response = client.post(
            "/sessions/close",
            json={**payload, "reason": "authorized-integration-test"},
        )

    assert turn_response.status_code == 200
    assert close_response.status_code == 200
    assert close_response.json() == {
        "session_id": "api-authorized-session",
        "candidate_memory_count": 1,
        "memory_write_count": 1,
        "status": "closed",
    }

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            memory_count = await connection.scalar(
                select(func.count()).select_from(LongTermMemoryModel)
            )
            session_status = await connection.scalar(
                select(SessionModel.status).where(SessionModel.id == "api-authorized-session")
            )
    finally:
        await engine.dispose()

    assert memory_count == 1
    assert session_status == SESSION_STATUS_CLOSED


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
