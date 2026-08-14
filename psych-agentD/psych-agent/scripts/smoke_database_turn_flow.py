"""Manual smoke test for a DB-backed one-turn conversation."""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def prepare_database_url() -> tuple[str, Path]:
    """Create a clean local SQLite path before entering async code."""

    database_path = Path(".tmp/psych_agent_turn_flow_smoke.db")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    database_url = f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}"
    return database_url, database_path


async def main(database_url: str, database_path: Path) -> None:
    """Run one DB-backed turn and print persisted row counts."""

    from sqlalchemy import select

    from app.config import Settings
    from runtime.factory import build_sqlalchemy_orchestrator
    from schemas.common import SessionId, UserId
    from storage.database import create_engine, create_session_factory, dispose_engine
    from storage.models.base import Base
    from storage.models.intervention import InterventionEventModel
    from storage.models.message import MessageModel
    from storage.models.registry import load_all_models
    from storage.models.session import SessionModel
    from storage.models.session_state import SessionStateVersionModel
    from storage.models.user import UserModel

    settings = Settings(database_url=database_url, _env_file=None)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        load_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        orchestrator = build_sqlalchemy_orchestrator(session_factory)
        result = await orchestrator.handle_turn(
            user_id=UserId("smoke-turn-user"),
            session_id=SessionId("smoke-turn-session"),
            text="I feel stuck at work and want to talk it through.",
        )

        async with session_factory() as session:
            users = list((await session.execute(select(UserModel))).scalars())
            sessions = list((await session.execute(select(SessionModel))).scalars())
            messages = list((await session.execute(select(MessageModel))).scalars())
            states = list((await session.execute(select(SessionStateVersionModel))).scalars())
            interventions = list((await session.execute(select(InterventionEventModel))).scalars())

        print(f"response={result.response}")
        print(f"users={len(users)} sessions={len(sessions)}")
        print(f"messages={len(messages)} state_versions={len(states)}")
        print(f"interventions={len(interventions)}")
        print(f"database_path={database_path}")
    finally:
        await dispose_engine(engine)


if __name__ == "__main__":
    url, path = prepare_database_url()
    asyncio.run(main(url, path))