"""Inspect persisted PostgreSQL user/session state without mutating data."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from storage.database import create_engine, create_session_factory, dispose_engine
from storage.models.intervention import InterventionEventModel
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel


def build_parser() -> argparse.ArgumentParser:
    """Build the inspector CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--session-id",
        default=None,
        help="Inspect one session. If omitted, list sessions for the user.",
    )
    parser.add_argument(
        "--show-content",
        action="store_true",
        help="Print shortened message and memory content.",
    )
    parser.add_argument(
        "--message-limit",
        type=int,
        default=20,
        help="Maximum messages to print for one session.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    args = build_parser().parse_args(argv)
    if args.message_limit < 1:
        raise SystemExit("--message-limit must be at least 1")
    return args


async def inspect(args: argparse.Namespace) -> int:
    """Inspect one user and optional session."""

    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    print(f"database_url={_safe_database_url(settings.database_url)}")
    try:
        async with session_factory() as session:
            user = await session.get(UserModel, args.user_id)
            if user is None:
                print(f"user_not_found={args.user_id}")
                return 1
            print(
                "USER "
                f"id={user.id} status={user.status} memory_enabled={user.memory_enabled}"
            )
            memories = list(
                (
                    await session.execute(
                        select(LongTermMemoryModel)
                        .where(LongTermMemoryModel.user_id == args.user_id)
                        .order_by(
                            LongTermMemoryModel.status,
                            LongTermMemoryModel.memory_type,
                            LongTermMemoryModel.created_at,
                        )
                    )
                ).scalars()
            )
            if args.session_id is None:
                await _print_session_list(session, args.user_id)
            else:
                await _print_session_detail(
                    session,
                    user_id=args.user_id,
                    session_id=args.session_id,
                    show_content=args.show_content,
                    message_limit=args.message_limit,
                )
            _print_memories(memories, show_content=args.show_content)
    finally:
        await dispose_engine(engine)
    return 0


async def _print_session_list(session: AsyncSession, user_id: str) -> None:
    """Print sessions owned by the user."""

    sessions = list(
        (
            await session.execute(
                select(SessionModel)
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.started_at)
            )
        ).scalars()
    )
    print(f"SESSIONS rows={len(sessions)}")
    for row in sessions:
        counts = await _session_counts(session, row.id)
        print(
            "  - "
            f"id={row.session_id} session_pk={row.id} status={row.status} "
            f"state={row.current_state_version} summary={row.current_summary_version} "
            f"next_seq={row.next_message_sequence} "
            f"messages={counts['messages']} interventions={counts['interventions']}"
        )


async def _print_session_detail(
    session: AsyncSession,
    *,
    user_id: str,
    session_id: str,
    show_content: bool,
    message_limit: int,
) -> None:
    """Print one session with state, summary, intervention, and messages."""

    row = await session.scalar(
        select(SessionModel).where(
            SessionModel.user_id == user_id,
            SessionModel.session_id == session_id,
        )
    )
    if row is None:
        print(f"SESSION not_found_or_not_owned id={session_id}")
        return
    session_pk = row.id
    counts = await _session_counts(session, session_pk)
    print(
        "SESSION "
        f"id={row.session_id} session_pk={row.id} status={row.status} "
        f"current_state={row.current_state_version} "
        f"current_summary={row.current_summary_version} "
        f"next_seq={row.next_message_sequence}"
    )
    print(
        "COUNTS "
        f"messages={counts['messages']} state_versions={counts['states']} "
        f"summaries={counts['summaries']} interventions={counts['interventions']}"
    )
    await _print_state_versions(session, session_pk)
    await _print_summaries(session, session_pk)
    await _print_interventions(session, session_pk)
    await _print_messages(
        session,
        session_pk,
        show_content=show_content,
        limit=message_limit,
    )


async def _session_counts(session: AsyncSession, session_pk: UUID) -> dict[str, int]:
    """Return safe row counts for one session."""

    result: dict[str, int] = {}
    for label, model in (
        ("messages", MessageModel),
        ("states", SessionStateVersionModel),
        ("summaries", RollingSummaryVersionModel),
        ("interventions", InterventionEventModel),
    ):
        result[label] = int(
            await session.scalar(
                select(func.count()).select_from(model).where(
                    model.session_pk == session_pk
                )
            )
            or 0
        )
    return result


async def _print_state_versions(session: AsyncSession, session_pk: UUID) -> None:
    """Print compact state version history."""

    rows = list(
        (
            await session.execute(
                select(SessionStateVersionModel)
                .where(SessionStateVersionModel.session_pk == session_pk)
                .order_by(SessionStateVersionModel.version)
            )
        ).scalars()
    )
    print(f"STATE_VERSIONS rows={len(rows)}")
    for row in rows:
        state = row.state_json
        preferences = _values_from_state_list(state.get("user_preferences", []))
        topics = _values_from_state_list(state.get("active_topics", []))
        print(
            "  - "
            f"version={row.version} source={row.source_message_id} "
            f"phase={state.get('phase')} risk={_risk_level(state)} "
            f"goal={_short(str(state.get('session_goal') or ''))}"
        )
        if topics:
            print(f"    topics={[_short(item, 80) for item in topics]}")
        if preferences:
            print(f"    preferences={[_short(item, 80) for item in preferences]}")


async def _print_summaries(session: AsyncSession, session_pk: UUID) -> None:
    """Print rolling summary versions and coverage."""

    rows = list(
        (
            await session.execute(
                select(RollingSummaryVersionModel)
                .where(RollingSummaryVersionModel.session_pk == session_pk)
                .order_by(RollingSummaryVersionModel.summary_version)
            )
        ).scalars()
    )
    print(f"ROLLING_SUMMARIES rows={len(rows)}")
    for row in rows:
        summary = row.summary_json
        print(
            "  - "
            f"version={row.summary_version} "
            f"covered={row.covered_from_message_id}->{row.covered_to_message_id} "
            f"problem={_short(str(summary.get('current_problem') or ''))}"
        )


async def _print_interventions(session: AsyncSession, session_pk: UUID) -> None:
    """Print intervention lifecycle rows."""

    rows = list(
        (
            await session.execute(
                select(InterventionEventModel)
                .where(InterventionEventModel.session_pk == session_pk)
                .order_by(InterventionEventModel.created_at)
            )
        ).scalars()
    )
    print(f"INTERVENTIONS rows={len(rows)}")
    for row in rows:
        print(
            "  - "
            f"strategy={row.strategy} status={row.status} "
            f"feedback={row.explicit_feedback} fit={row.strategy_fit} "
            f"progress={row.objective_progress}"
        )


async def _print_messages(
    session: AsyncSession,
    session_pk: UUID,
    *,
    show_content: bool,
    limit: int,
) -> None:
    """Print message ids and optional shortened content."""

    rows = list(
        (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.session_pk == session_pk)
                .order_by(MessageModel.sequence_number.desc())
                .limit(limit)
            )
        ).scalars()
    )
    rows.reverse()
    print(f"MESSAGES shown={len(rows)} limit={limit}")
    for row in rows:
        line = f"  - #{row.sequence_number} {row.role} {row.id}"
        if show_content:
            line += f" content={_short(row.content, 140)}"
        print(line)


def _print_memories(
    rows: list[LongTermMemoryModel],
    *,
    show_content: bool,
) -> None:
    """Print memory rows for the user."""

    print(f"MEMORIES rows={len(rows)}")
    for row in rows:
        line = (
            "  - "
            f"id={row.id} type={row.memory_type} status={row.status} "
            f"sensitivity={row.sensitivity} confirmation={row.requires_user_confirmation} "
            f"sources={row.source_message_ids}"
        )
        print(line)
        if show_content:
            print(f"    content={_short(row.content, 180)}")


def _values_from_state_list(value: object) -> list[str]:
    """Extract StateItem values from state JSON."""

    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("active", True):
            item_value = item.get("value")
            if isinstance(item_value, str):
                result.append(item_value)
    return result


def _risk_level(state: dict[str, object]) -> str:
    """Extract compact risk level from state JSON."""

    risk = state.get("risk_state")
    if isinstance(risk, dict):
        level = risk.get("level")
        if isinstance(level, str):
            return level
    return ""


def _safe_database_url(url: str) -> str:
    """Render URL without exposing credentials."""

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database url>"


def _short(text: str, limit: int = 120) -> str:
    """Keep terminal output compact."""

    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def main(argv: Sequence[str] | None = None) -> int:
    """Run the inspector."""

    return asyncio.run(inspect(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
