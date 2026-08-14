"""Exercise chat and session-close APIs against a real SQLite database."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DATABASE_PATH = Path(".tmp/psych_agent_api_sqlalchemy_runtime.db")


def prepare_database() -> tuple[str, Path]:
    """Prepare a clean database path and configure the API runtime environment."""

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()
    database_url = f"sqlite+aiosqlite:///{DATABASE_PATH.resolve().as_posix()}"
    os.environ["RUNTIME_MODE"] = "sqlalchemy"
    os.environ["DATABASE_URL"] = database_url
    return database_url, DATABASE_PATH


async def main(database_url: str) -> None:
    """Create the schema, call both APIs, and validate their public responses."""

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.dependencies import dispose_runtime_dependencies
    from app.main import create_app
    from storage.models.base import Base
    from storage.models.registry import load_all_models

    load_all_models()
    setup_engine = create_async_engine(database_url)
    async with setup_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    transport = httpx.ASGITransport(app=create_app())
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://smoke",
        ) as client:
            turn = None
            for message in [
                "最近工作让我很焦虑。",
                "我想先把这件事拆小一点。",
                "请每次一个小步骤，不要一次太多建议。",
            ]:
                turn = await client.post(
                    "/chat/turn",
                    json={
                        "user_id": "api-smoke-user",
                        "session_id": "api-smoke-session",
                        "message": message,
                    },
                )
                turn.raise_for_status()
            close = await client.post(
                "/sessions/close",
                json={
                    "user_id": "api-smoke-user",
                    "session_id": "api-smoke-session",
                    "reason": "api-smoke",
                },
            )
        assert turn is not None
        close.raise_for_status()
        assert close.json()["memory_write_count"] >= 1
        assert close.json()["status"] == "closed"
        print(f"turn_status={turn.json()['status']}")
        print(f"close_result={close.json()}")
    finally:
        await dispose_runtime_dependencies()


def print_persisted_counts(path: Path) -> None:
    """Print compact persistence evidence for manual smoke inspection."""

    connection = sqlite3.connect(path)
    try:
        counts: dict[str, int] = {}
        for table in [
            "messages",
            "rolling_summary_versions",
            "long_term_memories",
        ]:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            counts[table] = count
            print(f"{table}_rows={count}")
        status = connection.execute(
            "SELECT status FROM sessions WHERE id = ?",
            ("api-smoke-session",),
        ).fetchone()[0]
        print(f"session_status={status}")
        assert counts["messages"] > 0
        assert counts["rolling_summary_versions"] >= 1
        assert counts["long_term_memories"] >= 1
        assert status == "closed"
    finally:
        connection.close()


if __name__ == "__main__":
    url, path = prepare_database()
    asyncio.run(main(url))
    print_persisted_counts(path)
