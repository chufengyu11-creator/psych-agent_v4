"""Interactive real-LLM PostgreSQL chat with persisted state diagnostics."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from llm.client import ManagedLLMClient
from llm.exceptions import LLMError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient
from runtime.application import RuntimeConfigurationError
from runtime.factory import build_application_runtime
from schemas.common import SessionId, UserId
from scripts.smoke_real_structured_agents import ObservingStructuredClient
from storage.models.intervention import InterventionEventModel
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel
from storage.repositories.user_repository import SqlAlchemyUserRepository


def build_parser() -> argparse.ArgumentParser:
    """Build the manual PostgreSQL chat command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--show-messages",
        action="store_true",
        help="Print persisted message ids after each turn.",
    )
    parser.add_argument(
        "--show-memory-content",
        action="store_true",
        help="Print persisted memory content for manual carry-over checks.",
    )
    memory_group = parser.add_mutually_exclusive_group()
    memory_group.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable long-term memory writes for this user before chatting.",
    )
    memory_group.add_argument(
        "--disable-memory",
        action="store_true",
        help="Disable long-term memory writes for this user before chatting.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    return build_parser().parse_args(argv)


async def run_chat(args: argparse.Namespace) -> int:
    """Run a real model-backed SQLAlchemy conversation."""

    try:
        settings = Settings()
        settings = settings.model_copy(update={"app_runtime_mode": "sqlalchemy_model"})
    except ValidationError as exc:
        print(f"配置不可用: {type(exc).__name__}", file=sys.stderr)
        return 2

    observer_holder: dict[str, ObservingStructuredClient] = {}

    def wrap_client(
        structured_client: StructuredLLMClient,
        http_client: ManagedLLMClient,
    ) -> ObservingStructuredClient:
        observer = ObservingStructuredClient(structured_client, http_client)
        observer_holder["observer"] = observer
        return observer

    try:
        runtime = await build_application_runtime(
            settings,
            structured_client_wrapper=wrap_client,
        )
    except (RuntimeConfigurationError, ValidationError, LLMError) as exc:
        print(f"runtime 启动失败: {type(exc).__name__}", file=sys.stderr)
        return 2

    user_id = UserId(args.user_id or f"manual-pg-user-{uuid4().hex[:8]}")
    session_id = SessionId(args.session_id or f"manual-pg-session-{uuid4().hex[:8]}")
    memory_override = _memory_override(args)
    if memory_override is not None:
        await _set_user_memory_enabled(
            runtime=runtime,
            user_id=user_id,
            enabled=memory_override,
        )
    observation_cursor = 0
    closed = False

    print("真实模型 PostgreSQL 多轮诊断聊天")
    print(f"user_id={user_id}")
    print(f"session_id={session_id}")
    if memory_override is not None:
        print(f"memory_enabled={memory_override} (command-line override)")
    print(f"database_url={_safe_database_url(settings.database_url)}")
    print(
        "models: "
        f"main={settings.main_model_name} "
        f"structured={settings.structured_model_name} "
        f"safety={settings.safety_model_name}"
    )
    print("输入 /close 关闭 session 并写 memory；输入 q / quit / exit 退出。")

    try:
        while True:
            try:
                text = await _read_turn_text()
            except EOFError:
                print()
                break
            text = text.strip()
            if not text:
                continue
            lowered = text.casefold()
            if lowered in {"q", "quit", "exit"}:
                break
            if lowered == "/close":
                await _close_and_print(
                runtime=runtime,
                user_id=user_id,
                session_id=session_id,
                args=args,
                )
                closed = True
                continue
            if closed:
                print("session 已关闭；换一个 --session-id 或重新运行脚本继续测试。")
                continue

            try:
                result = await runtime.orchestrator.handle_turn(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                )
            except (LLMError, SQLAlchemyError) as exc:
                print(f"\n本轮失败: {type(exc).__name__}", file=sys.stderr)
                continue

            observer = observer_holder.get("observer")
            observations = (
                observer.observations[observation_cursor:] if observer is not None else []
            )
            observation_cursor += len(observations)

            print(f"\nAI: {result.response}")
            print(
                f"[status={result.status} state_version={result.state_version} "
                f"message_id={result.message_id}]"
            )
            _print_observations(observations)
            await _print_persisted_snapshot(
                runtime=runtime,
                user_id=user_id,
                session_id=session_id,
                show_messages=args.show_messages,
                show_memory_content=args.show_memory_content,
            )
    finally:
        http_client = runtime.http_client
        if http_client is not None:
            print(
                "\n统计: "
                f"api_attempts={http_client.request_count} "
                f"successful_http_responses={http_client.successful_http_response_count}"
            )
        await runtime.aclose()
    return 0


async def _read_turn_text() -> str:
    """Read one line from a terminal or piped stdin."""

    if sys.stdin.isatty():
        return await asyncio.to_thread(input, "\n你: ")
    print("\n你: ", end="", flush=True)
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line


def _memory_override(args: argparse.Namespace) -> bool | None:
    """Return the requested memory setting, or None when the CLI leaves it unchanged."""

    if args.enable_memory:
        return True
    if args.disable_memory:
        return False
    return None


async def _set_user_memory_enabled(
    *,
    runtime: object,
    user_id: UserId,
    enabled: bool,
) -> None:
    """Create or update the user row so manual memory tests are explicit."""

    if runtime.session_factory is None:
        return
    async with runtime.session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(user_id)
        user = await session.get(UserModel, str(user_id))
        if user is not None:
            user.memory_enabled = enabled
        await session.commit()


async def _close_and_print(
    *,
    runtime: object,
    user_id: UserId,
    session_id: SessionId,
    args: argparse.Namespace,
) -> None:
    """Close the session, then print persisted memory evidence."""

    session_closer = runtime.session_closer
    if session_closer is None:
        print("当前 runtime 没有 session_closer。")
        return
    result = await session_closer.close_session(
        user_id=user_id,
        session_id=session_id,
        reason="manual-postgres-chat",
    )
    print("\n已关闭 session")
    print(
        f"candidate_memories={result.candidate_memory_count} "
        f"memory_writes={result.memory_write_count}"
    )
    await _print_persisted_snapshot(
        runtime=runtime,
        user_id=user_id,
        session_id=session_id,
        show_messages=args.show_messages,
        show_memory_content=args.show_memory_content,
    )


async def _print_persisted_snapshot(
    *,
    runtime: object,
    user_id: UserId,
    session_id: SessionId,
    show_messages: bool,
    show_memory_content: bool,
) -> None:
    """Print compact persisted state, summary, intervention, and memory evidence."""

    if runtime.session_factory is None:
        print("DB: session_factory=None")
        return
    async with runtime.session_factory() as session:
        owner_scope = (
            SessionModel.user_id == str(user_id),
            SessionModel.session_id == str(session_id),
        )
        session_row = await session.scalar(
            select(SessionModel).where(*owner_scope)
        )
        messages = list(
            (
                await session.execute(
                    select(MessageModel)
                    .join(SessionModel, MessageModel.session_pk == SessionModel.id)
                    .where(*owner_scope)
                    .order_by(MessageModel.sequence_number)
                )
            ).scalars()
        )
        state_rows = list(
            (
                await session.execute(
                    select(SessionStateVersionModel)
                    .join(
                        SessionModel,
                        SessionStateVersionModel.session_pk == SessionModel.id,
                    )
                    .where(*owner_scope)
                    .order_by(SessionStateVersionModel.version)
                )
            ).scalars()
        )
        summary_rows = list(
            (
                await session.execute(
                    select(RollingSummaryVersionModel)
                    .join(
                        SessionModel,
                        RollingSummaryVersionModel.session_pk == SessionModel.id,
                    )
                    .where(*owner_scope)
                    .order_by(RollingSummaryVersionModel.summary_version)
                )
            ).scalars()
        )
        interventions = list(
            (
                await session.execute(
                    select(InterventionEventModel)
                    .join(
                        SessionModel,
                        InterventionEventModel.session_pk == SessionModel.id,
                    )
                    .where(*owner_scope)
                    .order_by(InterventionEventModel.created_at)
                )
            ).scalars()
        )
        memories = list(
            (
                await session.execute(
                    select(LongTermMemoryModel)
                    .where(LongTermMemoryModel.user_id == str(user_id))
                    .order_by(LongTermMemoryModel.created_at)
                )
            ).scalars()
        )
        memory_status_counts = {
            status: sum(row.status == status for row in memories)
            for status in sorted({row.status for row in memories})
        }

    print("DB:")
    print(
        "  "
        f"session_status={session_row.status if session_row else None} "
        f"messages={len(messages)} state_versions={len(state_rows)} "
        f"summaries={len(summary_rows)} interventions={len(interventions)} "
        f"memories_total={len(memories)} "
        f"active_memories={memory_status_counts.get('active', 0)} "
        f"pending_memories={memory_status_counts.get('pending_confirmation', 0)}"
    )
    _print_latest_state(state_rows)
    _print_latest_summary(summary_rows)
    _print_interventions(interventions)
    _print_memories(memories, show_content=show_memory_content)
    if show_messages:
        _print_messages(messages)


def _print_observations(observations: list[object]) -> None:
    """Print real model-call observations for the latest turn."""

    print("通路/model calls:")
    if not observations:
        print("  - none")
        return
    for item in observations:
        status = "ok" if item.schema_success else f"fallback_or_error:{item.error_type}"
        print(
            "  - "
            f"{item.agent} model={item.model} "
            f"api={item.api_success} schema={item.schema_success} "
            f"latency_ms={item.latency_ms} status={status}"
        )


def _print_latest_state(rows: list[SessionStateVersionModel]) -> None:
    """Print the latest persisted state row."""

    if not rows:
        print("  state: none")
        return
    latest = rows[-1]
    state = latest.state_json
    print(
        "  "
        f"state: version={latest.version} phase={state.get('phase')} "
        f"goal={_short(str(state.get('session_goal') or ''))}"
    )
    preferences = [
        item.get("value")
        for item in state.get("user_preferences", [])
        if isinstance(item, dict)
    ]
    if preferences:
        print(f"  preferences={[_short(str(item), 80) for item in preferences]}")


def _print_latest_summary(rows: list[RollingSummaryVersionModel]) -> None:
    """Print the latest persisted rolling summary row."""

    if not rows:
        print("  rolling_summary: none")
        return
    latest = rows[-1]
    summary = latest.summary_json
    print(
        "  "
        f"rolling_summary: version={latest.summary_version} "
        f"covered={latest.covered_from_message_id}->{latest.covered_to_message_id} "
        f"problem={_short(str(summary.get('current_problem') or ''))}"
    )


def _print_interventions(rows: list[InterventionEventModel]) -> None:
    """Print persisted intervention records."""

    if not rows:
        print("  interventions: none")
        return
    print("  interventions:")
    for row in rows:
        print(
            "    - "
            f"strategy={row.strategy} status={row.status} "
            f"feedback={row.explicit_feedback} fit={row.strategy_fit}"
        )


def _print_memories(
    rows: list[LongTermMemoryModel],
    *,
    show_content: bool,
) -> None:
    """Print persisted memory records."""

    if not rows:
        print("  memories: none")
        return
    print("  memories:")
    for row in rows:
        print(
            "    - "
            f"type={row.memory_type} status={row.status} "
            f"sensitivity={row.sensitivity} sources={row.source_message_ids}"
        )
        if show_content:
            print(f"      content={_short(row.content, 160)}")


def _print_messages(rows: list[MessageModel]) -> None:
    """Print persisted messages without dumping full user text."""

    print("  messages:")
    for row in rows:
        print(f"    - #{row.sequence_number} {row.role} {row.id}")


def _safe_database_url(url: str) -> str:
    """Show only non-secret database endpoint information."""

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database url>"


def _short(text: str, limit: int = 120) -> str:
    """Keep terminal diagnostics compact."""

    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def main(argv: Sequence[str] | None = None) -> int:
    """Run the interactive PostgreSQL chat."""

    return asyncio.run(run_chat(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
