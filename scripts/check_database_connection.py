"""Safely check the configured PostgreSQL connection and Alembic schema."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from time import monotonic

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import (
    ArgumentError,
    DBAPIError,
    NoSuchModuleError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import (
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings, get_settings
from storage.database import create_engine, dispose_engine

CORE_TABLES = (
    "users",
    "sessions",
    "messages",
    "session_state_versions",
    "intervention_events",
    "rolling_summary_versions",
    "long_term_memories",
    "deep_state_results",
    "knowledge_documents",
    "knowledge_chunks",
    "knowledge_usage_events",
)
KEY_SCHEMA_OBJECTS = (
    "pk_users",
    "fk_sessions_user_id_users",
    "uq_sessions_user_id_session_id",
    "uq_messages_session_pk_sequence_number",
    "uq_session_state_versions_session_pk_version",
    "uq_intervention_events_assistant_message_id",
    "uq_intervention_events_session_pk_pending",
    "uq_rolling_summary_versions_session_pk_summary_version",
    "fk_long_term_memories_user_id_users",
    "ck_long_term_memories_status",
    "uq_deep_state_results_source_pipeline",
    "ck_deep_state_results_status",
    "ck_knowledge_documents_status",
    "ck_knowledge_chunks_token_count",
    "uq_knowledge_documents_source_uri_version",
    "uq_knowledge_documents_content_hash",
    "fk_knowledge_documents_supersedes",
    "fk_knowledge_documents_replaced_by",
    "uq_knowledge_documents_one_active_source",
    "fk_knowledge_chunks_document_id_knowledge_documents",
    "uq_knowledge_chunks_document_id_chunk_index",
    "ix_knowledge_documents_status",
    "ix_knowledge_chunks_document_id_chunk_index",
    "ix_knowledge_chunks_embedding_hnsw",
    "fk_knowledge_usage_events_assistant_message_id_messages",
    "fk_knowledge_usage_events_chunk_id_knowledge_chunks",
    "fk_knowledge_usage_events_document_id_knowledge_documents",
    "uq_knowledge_usage_events_assistant_message_id_chunk_id",
    "ix_knowledge_usage_events_assistant_message_id",
    "ix_knowledge_usage_events_chunk_id",
)
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DatabaseExitCode(IntEnum):
    """Stable process outcomes for database diagnostics."""

    SUCCESS = 0
    CONFIGURATION = 2
    CONNECTION = 3
    AUTHORIZATION = 4
    DATABASE_NOT_FOUND = 5
    REVISION_MISMATCH = 6
    SCHEMA_INCOMPLETE = 7
    DATABASE_ERROR = 8


@dataclass(frozen=True)
class CheckOptions:
    """Command-line options separated from process-global parsing."""

    json_output: bool = False
    skip_schema_check: bool = False
    url_env: str = "DATABASE_URL"


@dataclass(frozen=True)
class SchemaSnapshot:
    """Non-sensitive schema names read through SQLAlchemy inspection."""

    tables: frozenset[str]
    objects: frozenset[str]


@dataclass
class DatabaseCheckResult:
    """Safe database diagnostics that never include credentials or rows."""

    url_env: str
    driver: str = "<unknown>"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    database_type: str | None = None
    connection_status: str = "not_run"
    server_version: str | None = None
    alembic_current_revision: str | None = None
    expected_head_revision: str | None = None
    schema_status: str = "not_run"
    tables: dict[str, bool] = field(default_factory=dict)
    schema_objects: dict[str, bool] = field(default_factory=dict)
    latency_ms: int = 0
    success: bool = False
    error_type: str | None = None


EngineFactory = Callable[[Settings], AsyncEngine]


def build_parser() -> argparse.ArgumentParser:
    """Build the safe database-check CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--skip-schema-check", action="store_true")
    parser.add_argument("--url-env", default="DATABASE_URL")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> CheckOptions:
    """Parse command-line arguments into a testable value object."""

    args = build_parser().parse_args(argv)
    return CheckOptions(
        json_output=args.json_output,
        skip_schema_check=args.skip_schema_check,
        url_env=args.url_env,
    )


def resolve_database_url(settings: Settings, env_name: str) -> str:
    """Resolve a configured URL without returning it through diagnostics."""

    value: str | None
    if not ENV_NAME_PATTERN.fullmatch(env_name):
        msg = "database URL environment name is invalid"
        raise ValueError(msg)
    if env_name == "DATABASE_URL":
        value = settings.database_url
    elif env_name == "TEST_DATABASE_URL":
        value = settings.test_database_url
    else:
        value = os.getenv(env_name)
    if value is None or not value.strip():
        msg = f"{env_name} is not configured"
        raise ValueError(msg)
    return value.strip()


def get_expected_head_revision() -> str:
    """Read the single expected head from the packaged migration directory."""

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "storage" / "migrations"),
    )
    heads = ScriptDirectory.from_config(alembic_config).get_heads()
    if len(heads) != 1:
        msg = "database check requires exactly one Alembic head"
        raise ValueError(msg)
    return heads[0]


async def run_database_checks(
    settings: Settings,
    options: CheckOptions,
    *,
    engine_factory: EngineFactory = create_engine,
) -> tuple[DatabaseCheckResult, DatabaseExitCode]:
    """Run connection and optional schema checks through project infrastructure."""

    started = monotonic()
    result = DatabaseCheckResult(url_env=options.url_env)
    engine: AsyncEngine | None = None
    try:
        database_url = resolve_database_url(settings, options.url_env)
        parsed_url = make_url(database_url)
        if parsed_url.drivername != "postgresql+asyncpg":
            msg = "database check requires a postgresql+asyncpg URL"
            raise ValueError(msg)
        if not parsed_url.database:
            msg = "database URL must include a database name"
            raise ValueError(msg)

        result.driver = parsed_url.drivername
        result.host = parsed_url.host
        result.port = parsed_url.port or 5432
        result.database = parsed_url.database
        result.expected_head_revision = get_expected_head_revision()
        check_settings = settings.model_copy(update={"database_url": database_url})
        engine = engine_factory(check_settings)

        async with engine.connect() as connection:
            scalar = await connection.scalar(text("SELECT 1"))
            if scalar != 1:
                msg = "database SELECT 1 returned an unexpected value"
                raise RuntimeError(msg)
            result.connection_status = "ok"
            result.database_type = connection.dialect.name
            version_value = await connection.scalar(text("SHOW server_version"))
            result.server_version = str(version_value) if version_value is not None else None
            database_value = await connection.scalar(text("SELECT current_database()"))
            if database_value is not None:
                result.database = str(database_value)

            if options.skip_schema_check:
                result.schema_status = "skipped"
                result.success = True
                return result, DatabaseExitCode.SUCCESS

            snapshot = await connection.run_sync(_inspect_schema)
            result.tables = {
                table_name: table_name in snapshot.tables for table_name in CORE_TABLES
            }
            result.schema_objects = {
                object_name: object_name in snapshot.objects
                for object_name in KEY_SCHEMA_OBJECTS
            }
            if "alembic_version" not in snapshot.tables:
                result.schema_status = "not_migrated"
                return result, DatabaseExitCode.REVISION_MISMATCH

            revision_value = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            result.alembic_current_revision = (
                str(revision_value) if revision_value is not None else None
            )
            if result.alembic_current_revision != result.expected_head_revision:
                result.schema_status = "revision_mismatch"
                return result, DatabaseExitCode.REVISION_MISMATCH
            if not all(result.tables.values()) or not all(
                result.schema_objects.values()
            ):
                result.schema_status = "incomplete"
                return result, DatabaseExitCode.SCHEMA_INCOMPLETE

            result.schema_status = "ok"
            result.success = True
            return result, DatabaseExitCode.SUCCESS
    except (ValueError, ArgumentError, NoSuchModuleError) as exc:
        result.error_type = type(exc).__name__
        return result, DatabaseExitCode.CONFIGURATION
    except DBAPIError as exc:
        result.error_type = type(exc.orig).__name__
        return result, database_exit_code_for_error(exc)
    except (ConnectionError, OSError, TimeoutError, SQLAlchemyTimeoutError) as exc:
        result.error_type = type(exc).__name__
        return result, DatabaseExitCode.CONNECTION
    except SQLAlchemyError as exc:
        result.error_type = type(exc).__name__
        return result, DatabaseExitCode.DATABASE_ERROR
    except Exception as exc:  # defensive CLI boundary; output remains sanitized
        result.error_type = type(exc).__name__
        return result, DatabaseExitCode.DATABASE_ERROR
    finally:
        result.latency_ms = max(0, round((monotonic() - started) * 1000))
        if engine is not None:
            await dispose_engine(engine)


def database_exit_code_for_error(error: DBAPIError) -> DatabaseExitCode:
    """Classify wrapped asyncpg/PostgreSQL failures without exposing details."""

    original = error.orig
    sqlstate_value = getattr(original, "sqlstate", None)
    if sqlstate_value is None:
        sqlstate_value = getattr(original, "pgcode", None)
    sqlstate = str(sqlstate_value) if sqlstate_value is not None else ""
    if sqlstate.startswith("28") or sqlstate == "42501":
        return DatabaseExitCode.AUTHORIZATION
    if sqlstate == "3D000":
        return DatabaseExitCode.DATABASE_NOT_FOUND
    if sqlstate.startswith("08"):
        return DatabaseExitCode.CONNECTION
    if isinstance(error, OperationalError):
        return DatabaseExitCode.CONNECTION
    return DatabaseExitCode.DATABASE_ERROR


def _inspect_schema(connection: Connection) -> SchemaSnapshot:
    """Collect only table, constraint, and index names from the live schema."""

    inspector = inspect(connection)
    tables = frozenset(inspector.get_table_names())
    object_names: set[str] = set()
    for table_name in CORE_TABLES:
        if table_name not in tables:
            continue
        primary_key = inspector.get_pk_constraint(table_name)
        _add_schema_name(object_names, primary_key.get("name"))
        for foreign_key in inspector.get_foreign_keys(table_name):
            _add_schema_name(object_names, foreign_key.get("name"))
        for unique_constraint in inspector.get_unique_constraints(table_name):
            _add_schema_name(object_names, unique_constraint.get("name"))
        for check_constraint in inspector.get_check_constraints(table_name):
            _add_schema_name(object_names, check_constraint.get("name"))
        for index in inspector.get_indexes(table_name):
            _add_schema_name(object_names, index.get("name"))
    return SchemaSnapshot(tables=tables, objects=frozenset(object_names))


def _add_schema_name(names: set[str], value: object) -> None:
    """Add one valid reflected schema-object name."""

    if isinstance(value, str) and value:
        names.add(value)


def render_result(result: DatabaseCheckResult, *, json_output: bool) -> str:
    """Render diagnostics without a URL, username, password, or row content."""

    if json_output:
        return json.dumps(asdict(result), ensure_ascii=True, sort_keys=True)
    lines = [
        f"url_env={result.url_env}",
        f"driver={result.driver}",
        f"host={result.host}",
        f"port={result.port}",
        f"database={result.database}",
        f"database_type={result.database_type}",
        f"connection={result.connection_status}",
        f"server_version={result.server_version}",
        f"alembic_current_revision={result.alembic_current_revision}",
        f"expected_head_revision={result.expected_head_revision}",
        f"schema={result.schema_status}",
    ]
    lines.extend(
        f"table.{name}={present}" for name, present in result.tables.items()
    )
    lines.extend(
        f"schema_object.{name}={present}"
        for name, present in result.schema_objects.items()
    )
    lines.extend(
        [
            f"latency_ms={result.latency_ms}",
            f"success={result.success}",
            f"error_type={result.error_type}",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run checks and return a stable, documented process exit code."""

    options = parse_options(argv)
    settings = get_settings()
    result, exit_code = asyncio.run(run_database_checks(settings, options))
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
