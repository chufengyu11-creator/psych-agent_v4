"""Manual smoke test for the SQLAlchemy persistence layer.

This script creates a local SQLite database file, writes a user and a session
through repositories, then opens a fresh session to verify the rows were really
committed. It is intentionally separate from pytest so humans can run it during
integration checks.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def prepare_database_url() -> tuple[str, Path]:
    """Create a clean local SQLite path before entering async code."""

    database_path = Path(".tmp/psych_agent_storage_smoke.db")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    database_url = f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}"
    return database_url, database_path


async def main(database_url: str, database_path: Path) -> None:
    """Write and verify one user/session pair in a real local database file."""

    from sqlalchemy import select

    from app.config import Settings
    from schemas.common import SessionId, UserId
    from storage.database import (
        create_engine,
        create_session_factory,
        dispose_engine,
        transactional_session,
    )
    from storage.models.base import Base
    from storage.models.registry import load_all_models
    from storage.models.session import SessionModel
    from storage.models.user import UserModel
    from storage.repositories.session_repository import SqlAlchemySessionRepository
    from storage.repositories.user_repository import SqlAlchemyUserRepository

    settings = Settings(database_url=database_url, _env_file=None)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    user_id = UserId("smoke-user")
    session_id = SessionId("smoke-session")

    try:
        load_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            await SqlAlchemySessionRepository(session).ensure_session(
                user_id,
                session_id,
            )

        async with session_factory() as verification_session:
            stored_user = await verification_session.get(UserModel, str(user_id))
            stored_session = await verification_session.scalar(
                select(SessionModel).where(
                    SessionModel.user_id == str(user_id),
                    SessionModel.session_id == str(session_id),
                )
            )
            if stored_user is None or stored_session is None:
                raise RuntimeError("Smoke test write did not persist rows")
            print(f"stored_user={stored_user.id}")
            print(
                f"stored_session={stored_session.session_id}, "
                f"session_pk={stored_session.id}, owner={stored_session.user_id}"
            )
            print(f"database_path={database_path}")
    finally:
        await dispose_engine(engine)


if __name__ == "__main__":
    url, path = prepare_database_url()
    asyncio.run(main(url, path))
