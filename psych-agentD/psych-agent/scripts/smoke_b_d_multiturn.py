"""Smoke test for B storage wiring and D summary/session-close memory flow."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from runtime.factory import (
    build_sqlalchemy_orchestrator_with_post_turn_summary,
    build_sqlalchemy_session_closer,
)
from schemas.common import SessionId, UserId
from storage.database import create_session_factory
from storage.models.base import Base
from storage.models.intervention import InterventionEventModel
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel

DEFAULT_TURNS = [
    "最近工作压力很大，我感觉自己卡住了。",
    "我不想只是被安慰，我想要一个具体下一步。",
    "请每次只给我一个小步骤，不要一次太多建议。",
    "这个方向可以，我们继续。",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a SQLAlchemy fake multi-turn conversation and print B/D artifacts."
        )
    )
    parser.add_argument(
        "--db",
        default=".tmp/smoke_b_d_multiturn.db",
        help="SQLite database path used for this smoke run.",
    )
    parser.add_argument("--user-id", default="smoke-user")
    parser.add_argument("--session-id", default="smoke-session")
    parser.add_argument(
        "--turn",
        action="append",
        dest="turns",
        help="Override default turns. Pass multiple --turn values for multi-turn.",
    )
    return parser.parse_args()


def database_url_for(path_text: str) -> str:
    db_path = Path(path_text)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"


async def main() -> None:
    args = parse_args()
    database_url = database_url_for(args.db)
    turns = args.turns or DEFAULT_TURNS

    load_all_models()
    engine = create_async_engine(database_url)
    session_factory = create_session_factory(engine)

    try:
        await rebuild_schema(engine)
        await run_conversation(
            session_factory=session_factory,
            user_id=UserId(args.user_id),
            session_id=SessionId(args.session_id),
            turns=turns,
            database_url=database_url,
        )
    finally:
        await engine.dispose()


async def rebuild_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


async def run_conversation(
    *,
    session_factory: async_sessionmaker,
    user_id: UserId,
    session_id: SessionId,
    turns: list[str],
    database_url: str,
) -> None:
    orchestrator = build_sqlalchemy_orchestrator_with_post_turn_summary(session_factory)
    closer = build_sqlalchemy_session_closer(session_factory)

    print(f"DB: {database_url}")
    print(f"USER: {user_id}  SESSION: {session_id}")
    print()

    for index, text in enumerate(turns, 1):
        result = await orchestrator.handle_turn(user_id, session_id, text)
        print(f"TURN {index}")
        print(f"user: {text}")
        print(
            "result: "
            f"status={result.status} "
            f"state_version={result.state_version} "
            f"assistant_message_id={result.message_id}"
        )
        print(f"assistant: {_short(result.response, 180)}")
        await print_storage_snapshot(session_factory, session_id)
        print()

    close_result = await closer.close_session(
        user_id=user_id,
        session_id=session_id,
        reason="smoke_b_d_multiturn",
    )
    print("SESSION CLOSE")
    print(
        "result: "
        f"candidate_memory_count={close_result.candidate_memory_count} "
        f"memory_write_count={close_result.memory_write_count}"
    )
    print(f"final_summary: {_short(close_result.finalizer_result.session_summary, 220)}")
    if close_result.finalizer_result.candidate_memories:
        print("candidate_memories:")
        for candidate in close_result.finalizer_result.candidate_memories:
            print(
                "  - "
                f"type={candidate.candidate_type.value} "
                f"op={candidate.recommended_operation.value} "
                f"confidence={candidate.confidence} "
                f"sensitivity={candidate.sensitivity.value} "
                f"sources={[str(item) for item in candidate.source_message_ids]} "
                f"content={_short(candidate.content, 180)}"
            )
    else:
        print("candidate_memories: none")

    if close_result.memory_write_results:
        print("memory_write_results:")
        for item in close_result.memory_write_results:
            print(
                "  - "
                f"applied={item.applied} "
                f"operation={item.operation.value} "
                f"memory_id={item.memory_id} "
                f"reasons={item.reason_codes}"
            )
    else:
        print("memory_write_results: none")
    await print_storage_snapshot(session_factory, session_id, include_memories=True)


async def print_storage_snapshot(
    session_factory: async_sessionmaker,
    session_id: SessionId,
    *,
    include_memories: bool = False,
) -> None:
    async with session_factory() as session:
        counts = {
            "users": await session.scalar(select(func.count()).select_from(UserModel)),
            "sessions": await session.scalar(select(func.count()).select_from(SessionModel)),
            "messages": await session.scalar(select(func.count()).select_from(MessageModel)),
            "state_versions": await session.scalar(
                select(func.count()).select_from(SessionStateVersionModel)
            ),
            "interventions": await session.scalar(
                select(func.count()).select_from(InterventionEventModel)
            ),
            "summaries": await session.scalar(
                select(func.count()).select_from(RollingSummaryVersionModel)
            ),
            "memories": await session.scalar(
                select(func.count()).select_from(LongTermMemoryModel)
            ),
        }
        stored_session = await session.get(SessionModel, str(session_id))
        messages = list(
            (
                await session.execute(
                    select(MessageModel)
                    .where(MessageModel.session_id == str(session_id))
                    .order_by(MessageModel.sequence_number)
                )
            ).scalars()
        )
        states = list(
            (
                await session.execute(
                    select(SessionStateVersionModel)
                    .where(SessionStateVersionModel.session_id == str(session_id))
                    .order_by(SessionStateVersionModel.version)
                )
            ).scalars()
        )
        interventions = list(
            (
                await session.execute(
                    select(InterventionEventModel)
                    .where(InterventionEventModel.session_id == str(session_id))
                    .order_by(InterventionEventModel.created_at, InterventionEventModel.id)
                )
            ).scalars()
        )
        summaries = list(
            (
                await session.execute(
                    select(RollingSummaryVersionModel)
                    .where(RollingSummaryVersionModel.session_id == str(session_id))
                    .order_by(RollingSummaryVersionModel.summary_version)
                )
            ).scalars()
        )
        memories = list(
            (
                await session.execute(
                    select(LongTermMemoryModel).order_by(LongTermMemoryModel.created_at)
                )
            ).scalars()
        )

    print("B storage counts:", counts)
    if stored_session is not None:
        print(
            "B session: "
            f"status={stored_session.status} "
            f"current_state_version={stored_session.current_state_version} "
            f"current_summary_version={stored_session.current_summary_version} "
            f"next_message_sequence={stored_session.next_message_sequence}"
        )
    print("B messages:")
    for message in messages:
        print(
            "  - "
            f"#{message.sequence_number} {message.role} "
            f"{message.id}: {_short(message.content, 100)}"
        )
    if states:
        latest_state = states[-1].state_json
        print(
            "B latest_state: "
            f"version={latest_state.get('version')} "
            f"goal={_short(str(latest_state.get('session_goal')), 80)} "
            f"preferences={_values(latest_state.get('user_preferences'))}"
        )
    print("B interventions:")
    for intervention in interventions:
        print(
            "  - "
            f"{intervention.id} status={intervention.status} "
            f"strategy={intervention.strategy} "
            f"feedback={intervention.explicit_feedback} "
            f"fit={intervention.strategy_fit}"
        )
    print("D rolling_summaries:")
    if summaries:
        for summary in summaries:
            payload = summary.summary_json
            print(
                "  - "
                f"v{summary.summary_version} "
                f"covered={summary.covered_from_message_id}->{summary.covered_to_message_id} "
                f"problem={_short(str(payload.get('current_problem')), 100)} "
                f"strategies={payload.get('strategies_attempted')}"
            )
    else:
        print("  - none")
    if include_memories:
        print("B long_term_memories:")
        if memories:
            for memory in memories:
                print(
                    "  - "
                    f"{memory.id} status={memory.status} "
                    f"type={memory.memory_type} "
                    f"sources={memory.source_message_ids} "
                    f"content={_short(memory.content, 160)}"
                )
        else:
            print("  - none")


def _values(raw_items: Any) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    values: list[str] = []
    for item in raw_items:
        if isinstance(item, dict) and "value" in item:
            values.append(_short(str(item["value"]), 80))
    return values


def _short(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


if __name__ == "__main__":
    asyncio.run(main())
