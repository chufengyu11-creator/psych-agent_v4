"""Run close-session memory smoke flow through SQLite and print persisted rows."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DATABASE_PATH = Path(".tmp/psych_agent_session_close_memory.db")
ASCII_OUTPUT = "--ascii" in sys.argv
JSON_COLUMNS = {
    "state_json",
    "summary_json",
    "expected_signals",
    "source_message_ids",
    "conflict_memory_ids",
}


def _render_text(value: str) -> str:
    """Render text in UTF-8 by default, or ASCII escapes for stubborn terminals."""

    if ASCII_OUTPUT:
        return value.encode("unicode_escape").decode("ascii")
    return value


def _render_row(data: dict[str, Any]) -> str:
    """Render a SQLite row as stable JSON for easier manual inspection."""

    return json.dumps(data, ensure_ascii=ASCII_OUTPUT, sort_keys=True, default=str)


def _parse_json_columns(data: dict[str, Any]) -> dict[str, Any]:
    """Decode JSON-looking SQLite text columns before printing."""

    for column in JSON_COLUMNS:
        value = data.get(column)
        if isinstance(value, str) and value:
            try:
                data[column] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return data


def prepare_database_url() -> tuple[str, Path]:
    """Create a clean SQLite database path for the smoke run."""

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()
    database_url = f"sqlite+aiosqlite:///{DATABASE_PATH.resolve().as_posix()}"
    return database_url, DATABASE_PATH


async def main(database_url: str, database_path: Path) -> None:
    """Run turns, summarize, finalize, write memory, close session, and print path."""

    from agents.feedback_evaluator import FakeFeedbackEvaluator
    from agents.output_guard import FakeOutputGuard
    from agents.response_agent import FakeResponseAgent
    from agents.risk_agent import RiskAgent
    from agents.state_tracker import StateTracker
    from agents.strategy_planner import StrategyPlanner
    from app.config import Settings
    from llm.structured_client import StructuredLLMClient
    from orchestrator.post_turn_pipeline import NoopTaskQueue
    from orchestrator.turn_orchestrator import TurnOrchestrator
    from runtime.factory import build_sqlalchemy_session_closer
    from schemas.common import SessionId, UserId
    from schemas.messages import MessageRole
    from scripts.smoke_model_assisted_database_corpus import ScriptedStructuredModelClient
    from services.context_builder import ContextBuilder
    from services.memory_retriever import MemoryRetriever
    from services.state_reducer import StateReducer
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
    from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
    from storage.repositories.message_repository import SqlAlchemyMessageRepository
    from storage.repositories.session_repository import SqlAlchemySessionRepository
    from storage.repositories.state_repository import SqlAlchemyStateRepository
    from storage.repositories.summary_repository import SqlAlchemySummaryRepository
    from storage.repositories.user_repository import SqlAlchemyUserRepository
    from tests.fixtures.messages import work_stress_dialogue

    settings = Settings(database_url=database_url, _env_file=None)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    model_client = ScriptedStructuredModelClient()
    structured_client = StructuredLLMClient(model_client)
    user_id = UserId("close-memory-user")
    session_id = SessionId("close-memory-session")
    try:
        load_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        user_messages = [
            message
            for message in work_stress_dialogue()
            if message.role == MessageRole.USER
        ]
        for index, message in enumerate(user_messages, start=1):
            async with transactional_session(session_factory) as session:
                await SqlAlchemyUserRepository(session).ensure_user(user_id)
                user_row = await session.get(UserModel, str(user_id))
                if user_row is not None:
                    user_row.memory_enabled = True
                await SqlAlchemySessionRepository(session).ensure_session(session_id, user_id)
                orchestrator = TurnOrchestrator(
                    risk_agent=RiskAgent(structured_client),
                    state_tracker=StateTracker(structured_client),
                    feedback_evaluator=FakeFeedbackEvaluator(),
                    strategy_planner=StrategyPlanner(structured_client),
                    response_agent=FakeResponseAgent(),
                    output_guard=FakeOutputGuard(),
                    state_reducer=StateReducer(),
                    context_builder=ContextBuilder(),
                    memory_retriever=MemoryRetriever(),
                    message_repository=SqlAlchemyMessageRepository(session),
                    state_repository=SqlAlchemyStateRepository(session),
                    summary_repository=SqlAlchemySummaryRepository(session),
                    intervention_repository=SqlAlchemyInterventionRepository(session),
                    task_queue=NoopTaskQueue(),
                )
                result = await orchestrator.handle_turn(
                    user_id=user_id,
                    session_id=session_id,
                    text=message.content,
                )
            print(f"TURN {index}: state_version={result.state_version}")
            print(f"response={_render_text(result.response)}")

        close_result = await build_sqlalchemy_session_closer(
            session_factory
        ).close_session(
            user_id=user_id,
            session_id=session_id,
            reason="smoke-session-close-memory",
        )
        async with session_factory() as session:
            summary = await SqlAlchemySummaryRepository(session).get_current(session_id)
            session_row = await session.get(SessionModel, str(session_id))

        assert summary is not None
        assert session_row is not None
        print(f"summary_version={summary.summary_version}")
        print(f"candidate_memories={close_result.candidate_memory_count}")
        print(f"memory_writes={close_result.memory_write_count}")
        print(f"session status={session_row.status}")

        print(f"structured_llm_calls={len(model_client.requests)}")
        print(f"database_path={database_path}")
    finally:
        await dispose_engine(engine)


def dump_database(path: Path) -> None:
    """Print table contents from the SQLite smoke database."""

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for table in [
            "users",
            "sessions",
            "messages",
            "session_state_versions",
            "intervention_events",
            "rolling_summary_versions",
            "long_term_memories",
        ]:
            print(f"\nTABLE {table}")
            rows = conn.execute(f"select * from {table}").fetchall()
            print(f"rows={len(rows)}")
            for row in rows:
                print(_render_row(_parse_json_columns(dict(row))))
    finally:
        conn.close()


if __name__ == "__main__":
    url, db_path = prepare_database_url()
    asyncio.run(main(url, db_path))
    dump_database(db_path)
