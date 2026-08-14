"""Run automatic inline post-turn rolling summaries through SQLite."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DATABASE_PATH = Path(".tmp/psych_agent_post_turn_summary.db")


def prepare_database_url() -> str:
    """Create a clean SQLite path and return its asynchronous URL."""

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()
    return f"sqlite+aiosqlite:///{DATABASE_PATH.resolve().as_posix()}"


async def main(database_url: str) -> None:
    """Run three turns whose enqueue calls update rolling summaries inline."""

    from app.config import Settings
    from runtime.factory import build_sqlalchemy_orchestrator_with_post_turn_summary
    from schemas.common import SessionId, UserId
    from storage.database import (
        create_engine,
        create_session_factory,
        dispose_engine,
    )
    from storage.models.base import Base
    from storage.models.registry import load_all_models
    from storage.models.session import SessionModel
    from storage.models.summary import RollingSummaryVersionModel

    settings = Settings(database_url=database_url, _env_file=None)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    user_id = UserId("post-turn-smoke-user")
    session_id = SessionId("post-turn-smoke-session")
    try:
        load_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        orchestrator = build_sqlalchemy_orchestrator_with_post_turn_summary(
            session_factory
        )
        texts = [
            "Work conversations feel tense.",
            "I want a concrete next step.",
            "Please give me one small step at a time.",
        ]
        for text in texts:
            await orchestrator.handle_turn(
                user_id=user_id,
                session_id=session_id,
                text=text,
            )

        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(RollingSummaryVersionModel)
                        .join(
                            SessionModel,
                            RollingSummaryVersionModel.session_pk == SessionModel.id,
                        )
                        .where(
                            SessionModel.user_id == str(user_id),
                            SessionModel.session_id == str(session_id),
                        )
                    )
                ).scalars()
            )
            stored_session = await session.scalar(
                select(SessionModel).where(
                    SessionModel.user_id == str(user_id),
                    SessionModel.session_id == str(session_id),
                )
            )

        assert stored_session is not None
        print(f"turns={len(texts)}")
        print(f"rolling_summary_versions rows={len(rows)}")
        print(f"current_summary_version={stored_session.current_summary_version}")
        print(f"database_path={DATABASE_PATH}")
    finally:
        await dispose_engine(engine)


if __name__ == "__main__":
    asyncio.run(main(prepare_database_url()))
