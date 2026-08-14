"""Shared, safety-scoped database backend support for business smoke scripts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import (
    ArgumentError,
    DBAPIError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from app.config import Settings, get_settings
from schemas.common import SessionId, UserId
from storage.database import (
    create_engine,
    create_session_factory,
    dispose_engine,
    transactional_session,
)
from storage.models.base import Base
from storage.models.intervention import InterventionEventModel
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQLITE_DIRECTORY = PROJECT_ROOT / ".tmp"
CORE_TABLES = frozenset(
    {
        "users",
        "sessions",
        "messages",
        "session_state_versions",
        "intervention_events",
        "rolling_summary_versions",
        "long_term_memories",
    }
)

DatabaseBackend = Literal["sqlite", "postgres"]


class SmokeExitCode(IntEnum):
    """Stable process outcomes shared by both database smoke scripts."""

    SUCCESS = 0
    CONFIGURATION = 2
    CONNECTION = 3
    SCHEMA_NOT_MIGRATED = 4
    BUSINESS_FLOW = 5
    ACCEPTANCE_MISMATCH = 6
    CLEANUP_FAILED = 7
    DATABASE_ERROR = 8


class SmokeConfigurationError(ValueError):
    """Raised before connecting when a backend setting is unsafe or invalid."""


class SmokeSchemaError(RuntimeError):
    """Raised when the selected PostgreSQL database lacks migrated core tables."""


class SmokeAcceptanceError(RuntimeError):
    """Raised when isolated persisted counts do not match the smoke contract."""


class SmokeCleanupError(RuntimeError):
    """Raised when scoped cleanup cannot remove only the current smoke rows."""


@dataclass(frozen=True)
class SmokeOptions:
    """Consistent command-line options for both business smoke scripts."""

    database: DatabaseBackend = "sqlite"
    ascii_output: bool = False
    json_output: bool = False
    cleanup: bool = False


@dataclass(frozen=True)
class SmokeCase:
    """Unique artificial identifiers owned by one smoke invocation."""

    run_id: str
    user_id: UserId
    session_id: SessionId


@dataclass(frozen=True)
class BusinessOutcome:
    """Safe counters returned by one script-specific business flow."""

    turns: int
    structured_llm_calls: int
    summary_version: int | None = None
    candidate_memories: int | None = None
    memory_writes: int | None = None


@dataclass(frozen=True)
class ExpectedSmokeState:
    """Expected isolated persistence state for acceptance validation."""

    session_status: str
    long_term_memories_rows: int


@dataclass
class SmokeResult:
    """One safe result object used by both ASCII and JSON rendering."""

    success: bool
    database_backend: DatabaseBackend
    database_driver: str
    user_id: str
    session_id: str
    run_id: str
    turns: int = 0
    users_rows: int = 0
    sessions_rows: int = 0
    messages_rows: int = 0
    session_state_versions_rows: int = 0
    intervention_events_rows: int = 0
    rolling_summary_versions_rows: int = 0
    long_term_memories_rows: int = 0
    session_status: str | None = None
    structured_llm_calls: int = 0
    summary_version: int | None = None
    candidate_memories: int | None = None
    memory_writes: int | None = None
    source_message_ids_non_empty: bool = False
    cleanup_requested: bool = False
    cleanup_applied: bool = False
    error_type: str | None = None
    error_message_safe: str | None = None


@dataclass(frozen=True)
class SmokeDatabaseRuntime:
    """Engine resources selected for exactly one smoke invocation."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    database_path: Path | None


BusinessFlow = Callable[
    [async_sessionmaker[AsyncSession], SmokeCase],
    Awaitable[BusinessOutcome],
]
EngineFactory = Callable[[Settings], AsyncEngine]


def build_parser(description: str) -> argparse.ArgumentParser:
    """Build the identical CLI surface used by both smoke scripts."""

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--database",
        choices=("sqlite", "postgres"),
        default="sqlite",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--ascii", action="store_true", dest="ascii_output")
    output.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--cleanup", action="store_true")
    return parser


def parse_options(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None = None,
) -> SmokeOptions:
    """Parse one script's arguments into the shared option contract."""

    args = parser.parse_args(argv)
    return SmokeOptions(
        database=args.database,
        ascii_output=args.ascii_output,
        json_output=args.json_output,
        cleanup=args.cleanup,
    )


def create_smoke_case(smoke_name: str) -> SmokeCase:
    """Create repeatable-length IDs with a unique per-run suffix."""

    run_id = uuid4().hex[:12]
    normalized_name = smoke_name.replace("_", "-")
    return SmokeCase(
        run_id=run_id,
        user_id=UserId(f"smoke-{normalized_name}-user-{run_id}"),
        session_id=SessionId(f"smoke-{normalized_name}-session-{run_id}"),
    )


def validate_postgres_database_url(database_url: str) -> str:
    """Require an explicitly configured asyncpg URL without printing it."""

    try:
        parsed_url = make_url(database_url)
    except ArgumentError as exc:
        msg = "DATABASE_URL is not a valid SQLAlchemy URL"
        raise SmokeConfigurationError(msg) from exc
    if parsed_url.drivername != "postgresql+asyncpg":
        msg = "PostgreSQL smoke requires DATABASE_URL to use postgresql+asyncpg"
        raise SmokeConfigurationError(msg)
    if not parsed_url.database:
        msg = "PostgreSQL smoke requires a database name"
        raise SmokeConfigurationError(msg)
    password = parsed_url.password or ""
    if password.casefold() == "replace-me":
        msg = "DATABASE_URL is not explicitly configured for PostgreSQL smoke"
        raise SmokeConfigurationError(msg)
    return database_url


async def run_database_smoke(
    *,
    smoke_name: str,
    options: SmokeOptions,
    expected: ExpectedSmokeState,
    business_flow: BusinessFlow,
    settings: Settings | None = None,
    engine_factory: EngineFactory = create_engine,
) -> tuple[SmokeResult, SmokeExitCode]:
    """Run one isolated business smoke against SQLite or pre-migrated PostgreSQL."""

    case = create_smoke_case(smoke_name)
    active_settings = settings or get_settings()
    database_driver = (
        "sqlite+aiosqlite"
        if options.database == "sqlite"
        else "postgresql+asyncpg"
    )
    result = SmokeResult(
        success=False,
        database_backend=options.database,
        database_driver=database_driver,
        user_id=str(case.user_id),
        session_id=str(case.session_id),
        run_id=case.run_id,
        cleanup_requested=options.cleanup,
    )
    runtime: SmokeDatabaseRuntime | None = None
    exit_code = SmokeExitCode.SUCCESS
    try:
        runtime = await _open_database_runtime(
            smoke_name=smoke_name,
            options=options,
            case=case,
            settings=active_settings,
            engine_factory=engine_factory,
        )
        result.database_driver = runtime.engine.url.drivername
        outcome = await business_flow(runtime.session_factory, case)
        await _populate_isolated_statistics(result, runtime.session_factory, case)
        result.turns = outcome.turns
        result.structured_llm_calls = outcome.structured_llm_calls
        result.summary_version = outcome.summary_version
        result.candidate_memories = outcome.candidate_memories
        result.memory_writes = outcome.memory_writes
        _validate_acceptance(result, expected)
        if options.cleanup:
            await cleanup_smoke_rows(runtime.session_factory, case)
            result.cleanup_applied = True
        result.success = True
    except SmokeConfigurationError as exc:
        exit_code = SmokeExitCode.CONFIGURATION
        _set_safe_error(result, exc, str(exc))
    except SmokeSchemaError as exc:
        exit_code = SmokeExitCode.SCHEMA_NOT_MIGRATED
        _set_safe_error(result, exc, str(exc))
    except SmokeAcceptanceError as exc:
        exit_code = SmokeExitCode.ACCEPTANCE_MISMATCH
        _set_safe_error(result, exc, str(exc))
    except SmokeCleanupError as exc:
        exit_code = SmokeExitCode.CLEANUP_FAILED
        _set_safe_error(result, exc, str(exc))
    except ProgrammingError as exc:
        exit_code = SmokeExitCode.SCHEMA_NOT_MIGRATED
        _set_safe_error(
            result,
            exc.orig or exc,
            "database schema is not migrated; run Alembic",
        )
    except OperationalError as exc:
        exit_code = SmokeExitCode.CONNECTION
        _set_safe_error(result, exc.orig or exc, "database connection failed")
    except DBAPIError as exc:
        exit_code = SmokeExitCode.DATABASE_ERROR
        _set_safe_error(result, exc.orig or exc, "database operation failed safely")
    except SQLAlchemyError as exc:
        exit_code = SmokeExitCode.DATABASE_ERROR
        _set_safe_error(result, exc, "database operation failed safely")
    except Exception as exc:
        exit_code = SmokeExitCode.BUSINESS_FLOW
        _set_safe_error(result, exc, "business smoke flow failed")
    finally:
        if runtime is not None:
            await dispose_engine(runtime.engine)
            if runtime.database_path is not None:
                try:
                    runtime.database_path.unlink(missing_ok=True)
                except OSError as exc:
                    if exit_code is SmokeExitCode.SUCCESS:
                        result.success = False
                        exit_code = SmokeExitCode.CLEANUP_FAILED
                        _set_safe_error(
                            result,
                            exc,
                            "temporary SQLite file cleanup failed",
                        )
    return result, exit_code


async def _open_database_runtime(
    *,
    smoke_name: str,
    options: SmokeOptions,
    case: SmokeCase,
    settings: Settings,
    engine_factory: EngineFactory,
) -> SmokeDatabaseRuntime:
    """Select one backend without fallback and initialize only temporary SQLite."""

    database_path: Path | None = None
    if options.database == "sqlite":
        SQLITE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        database_path = SQLITE_DIRECTORY / f"{smoke_name}_{case.run_id}.db"
        if database_path.exists():
            msg = "unique SQLite smoke path already exists"
            raise SmokeConfigurationError(msg)
        database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    else:
        database_url = validate_postgres_database_url(settings.database_url)

    selected_settings = settings.model_copy(update={"database_url": database_url})
    engine = engine_factory(selected_settings)
    runtime = SmokeDatabaseRuntime(
        engine=engine,
        session_factory=create_session_factory(engine),
        database_path=database_path,
    )
    try:
        if options.database == "sqlite":
            load_all_models()
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            await _require_migrated_postgres_schema(engine)
    except Exception:
        await dispose_engine(engine)
        if database_path is not None:
            database_path.unlink(missing_ok=True)
        raise
    return runtime


async def _require_migrated_postgres_schema(engine: AsyncEngine) -> None:
    """Fail before business writes unless all Alembic-managed tables exist."""

    async with engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: frozenset(inspect(sync_connection).get_table_names())
        )
    missing = CORE_TABLES.difference(table_names)
    if "alembic_version" not in table_names or missing:
        msg = "database schema is not migrated; run alembic upgrade head first"
        raise SmokeSchemaError(msg)


async def _populate_isolated_statistics(
    result: SmokeResult,
    session_factory: async_sessionmaker[AsyncSession],
    case: SmokeCase,
) -> None:
    """Read only rows owned by this smoke user/session."""

    user_id = str(case.user_id)
    session_id = str(case.session_id)
    async with session_factory() as session:
        result.users_rows = await _count_rows(
            session,
            select(func.count()).select_from(UserModel).where(UserModel.id == user_id),
        )
        result.sessions_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(SessionModel)
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            ),
        )
        result.messages_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(MessageModel)
            .join(SessionModel, MessageModel.session_pk == SessionModel.id)
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            ),
        )
        result.session_state_versions_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(SessionStateVersionModel)
            .join(
                SessionModel,
                SessionStateVersionModel.session_pk == SessionModel.id,
            )
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            ),
        )
        result.intervention_events_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(InterventionEventModel)
            .join(SessionModel, InterventionEventModel.session_pk == SessionModel.id)
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            ),
        )
        result.rolling_summary_versions_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(RollingSummaryVersionModel)
            .join(
                SessionModel,
                RollingSummaryVersionModel.session_pk == SessionModel.id,
            )
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            ),
        )
        result.long_term_memories_rows = await _count_rows(
            session,
            select(func.count())
            .select_from(LongTermMemoryModel)
            .where(LongTermMemoryModel.user_id == user_id),
        )
        session_row = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            )
        )
        result.session_status = session_row.status if session_row is not None else None
        summary_row = await session.scalar(
            select(RollingSummaryVersionModel)
            .join(
                SessionModel,
                RollingSummaryVersionModel.session_pk == SessionModel.id,
            )
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_id == session_id,
            )
            .order_by(RollingSummaryVersionModel.summary_version.desc())
            .limit(1)
        )
        summary_sources_valid = bool(
            summary_row is not None
            and isinstance(summary_row.summary_json.get("source_message_ids"), list)
            and summary_row.summary_json["source_message_ids"]
        )
        memory_rows = list(
            (
                await session.scalars(
                    select(LongTermMemoryModel).where(
                        LongTermMemoryModel.user_id == user_id
                    )
                )
            ).all()
        )
        memory_sources_valid = all(row.source_message_ids for row in memory_rows)
        result.source_message_ids_non_empty = (
            summary_sources_valid and memory_sources_valid
        )


async def _count_rows(
    session: AsyncSession,
    statement: Select[tuple[int]],
) -> int:
    """Return one typed count for an already isolated SQL statement."""

    value = await session.scalar(statement)
    return int(value or 0)


def _validate_acceptance(
    result: SmokeResult,
    expected: ExpectedSmokeState,
) -> None:
    """Require the persisted business contract without reading other rows."""

    expected_counts = (
        result.users_rows == 1,
        result.sessions_rows == 1,
        result.messages_rows == 6,
        result.session_state_versions_rows == 3,
        result.intervention_events_rows == 3,
        result.rolling_summary_versions_rows == 1,
        result.long_term_memories_rows == expected.long_term_memories_rows,
        result.session_status == expected.session_status,
        result.turns == 3,
        result.structured_llm_calls == 9,
        result.source_message_ids_non_empty,
    )
    if not all(expected_counts):
        msg = "isolated smoke statistics did not match the expected contract"
        raise SmokeAcceptanceError(msg)


async def cleanup_smoke_rows(
    session_factory: async_sessionmaker[AsyncSession],
    case: SmokeCase,
) -> None:
    """Delete only rows belonging to the current smoke identifiers."""

    user_id = str(case.user_id)
    session_id = str(case.session_id)
    try:
        async with transactional_session(session_factory) as session:
            session_pk = await session.scalar(
                select(SessionModel.id).where(
                    SessionModel.user_id == user_id,
                    SessionModel.session_id == session_id,
                )
            )
            await session.execute(
                delete(LongTermMemoryModel).where(
                    LongTermMemoryModel.user_id == user_id
                )
            )
            if session_pk is not None:
                await session.execute(
                    delete(RollingSummaryVersionModel).where(
                        RollingSummaryVersionModel.session_pk == session_pk
                    )
                )
                await session.execute(
                    delete(InterventionEventModel).where(
                        InterventionEventModel.session_pk == session_pk
                    )
                )
                await session.execute(
                    delete(SessionStateVersionModel).where(
                        SessionStateVersionModel.session_pk == session_pk
                    )
                )
                await session.execute(
                    delete(MessageModel).where(MessageModel.session_pk == session_pk)
                )
            await session.execute(
                delete(SessionModel).where(
                    SessionModel.user_id == user_id,
                    SessionModel.session_id == session_id,
                )
            )
            await session.execute(delete(UserModel).where(UserModel.id == user_id))
    except SQLAlchemyError as exc:
        msg = "scoped smoke cleanup failed"
        raise SmokeCleanupError(msg) from exc


def render_result(result: SmokeResult, *, json_output: bool) -> str:
    """Render the same safe result object as human text or JSON."""

    if json_output:
        return json.dumps(asdict(result), ensure_ascii=True, sort_keys=True)
    values = asdict(result)
    return "\n".join(f"{name}={value}" for name, value in values.items())


def _set_safe_error(
    result: SmokeResult,
    error: BaseException,
    safe_message: str,
) -> None:
    """Attach only an exception type and a pre-sanitized message."""

    result.error_type = type(error).__name__
    result.error_message_safe = safe_message


__all__ = [
    "BusinessOutcome",
    "ExpectedSmokeState",
    "SmokeCase",
    "SmokeExitCode",
    "SmokeOptions",
    "SmokeResult",
    "build_parser",
    "cleanup_smoke_rows",
    "create_smoke_case",
    "parse_options",
    "render_result",
    "run_database_smoke",
    "validate_postgres_database_url",
]
