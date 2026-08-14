"""Offline tests for the Alembic initial core-schema migration."""

import re
from functools import lru_cache
from importlib.resources import files
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from storage.models.base import Base
from storage.models.registry import load_all_models

BASE_REVISION = "0001_initial_core_schema"
HEAD_REVISION = "0006_knowledge_lifecycle"
EXPECTED_TABLE_ORDER = (
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


def _config(*, output_buffer: StringIO | None = None) -> Config:
    """Return the project Alembic configuration for offline use."""

    return Config("alembic.ini", output_buffer=output_buffer)


@lru_cache
def _offline_upgrade_sql() -> str:
    """Generate the full offline upgrade SQL without a connection."""

    output = StringIO()
    command.upgrade(_config(output_buffer=output), "head", sql=True)
    return output.getvalue()


@lru_cache
def _offline_downgrade_sql() -> str:
    """Generate the full offline downgrade SQL without a connection."""

    output = StringIO()
    command.downgrade(_config(output_buffer=output), "head:base", sql=True)
    return output.getvalue()


def _created_business_tables(sql: str) -> tuple[str, ...]:
    """Extract ordered business-table names from PostgreSQL SQL output."""

    names = re.findall(r"CREATE TABLE ([a-z_]+)", sql)
    return tuple(name for name in names if name != "alembic_version")


def test_alembic_config_uses_the_project_migration_directory() -> None:
    """The project config should use a relative script directory and no URL."""

    config = _config()
    config_text = Path("alembic.ini").read_text(encoding="utf-8")

    assert config.get_main_option("script_location") == "storage/migrations"
    assert config.get_main_option("prepend_sys_path") == "."
    assert config.get_main_option("sqlalchemy.url") == ""
    assert "postgresql+asyncpg://" not in config_text


def test_script_directory_has_one_base_and_one_head() -> None:
    """The migration chain should have one stable base and one current head."""

    scripts = ScriptDirectory.from_config(_config())
    revision = scripts.get_revision(HEAD_REVISION)

    assert scripts.get_heads() == [HEAD_REVISION]
    assert scripts.get_base() == BASE_REVISION
    assert revision is not None
    assert revision.revision == HEAD_REVISION
    assert revision.down_revision == "0005_knowledge_rag"


def test_initial_migration_module_is_importable_and_callable() -> None:
    """Alembic should load both migration directions from the revision."""

    scripts = ScriptDirectory.from_config(_config())
    revision = scripts.get_revision(BASE_REVISION)

    assert revision is not None
    assert revision.module.revision == BASE_REVISION
    assert revision.module.down_revision is None
    assert callable(revision.module.upgrade)
    assert callable(revision.module.downgrade)


def test_offline_upgrade_creates_all_tables_jsonb_and_partial_index() -> None:
    """Offline upgrade SQL should contain the complete PostgreSQL schema."""

    sql = _offline_upgrade_sql()
    normalized_sql = " ".join(sql.split())

    assert _created_business_tables(sql) == EXPECTED_TABLE_ORDER
    assert "state_json JSONB NOT NULL" in normalized_sql
    assert "expected_signals JSONB DEFAULT '[]' NOT NULL" in normalized_sql
    assert "summary_json JSONB NOT NULL" in normalized_sql
    assert "source_message_ids JSONB DEFAULT '[]' NOT NULL" in normalized_sql
    assert "conflict_memory_ids JSONB DEFAULT '[]' NOT NULL" in normalized_sql
    assert "result_json JSONB NOT NULL" in normalized_sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in normalized_sql
    assert "vector(512)" in normalized_sql.casefold()
    assert "ix_knowledge_chunks_embedding_hnsw" in normalized_sql
    assert "ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE" not in normalized_sql
    assert (
        "CREATE UNIQUE INDEX uq_intervention_events_session_pk_pending "
        "ON intervention_events (session_pk) WHERE status = 'pending'"
        in normalized_sql
    )
    assert (
        "CONSTRAINT uq_sessions_user_id_session_id UNIQUE (user_id, session_id)"
        in normalized_sql
    )


def test_offline_downgrade_drops_indexes_and_tables_in_reverse_order() -> None:
    """Offline downgrade SQL should explicitly unwind every dependency."""

    sql = _offline_downgrade_sql()
    normalized_sql = " ".join(sql.split())
    reverse_order = tuple(reversed(EXPECTED_TABLE_ORDER))
    positions = [
        normalized_sql.index(f"DROP TABLE {table_name}")
        for table_name in reverse_order
    ]

    assert positions == sorted(positions)
    assert "DROP INDEX uq_intervention_events_session_id_pending" in (
        normalized_sql
    )
    for table_name in reverse_order:
        assert f"DROP TABLE {table_name}" in normalized_sql


def test_model_registry_and_migration_table_sets_are_identical() -> None:
    """The hand-written migration should create every registered model table."""

    load_all_models()
    migration_tables = set(_created_business_tables(_offline_upgrade_sql()))

    assert migration_tables == set(Base.metadata.tables)
    assert migration_tables == set(EXPECTED_TABLE_ORDER)


def test_offline_upgrade_contains_key_constraints_and_indexes() -> None:
    """Critical stable names should be present in generated SQL."""

    normalized_sql = " ".join(_offline_upgrade_sql().split())
    required_names = (
        "pk_users",
        "fk_sessions_user_id_users",
        "uq_sessions_user_id_session_id",
        "uq_messages_session_pk_sequence_number",
        "uq_session_state_versions_session_pk_version",
        "uq_intervention_events_assistant_message_id",
        "uq_intervention_events_session_pk_pending",
        "uq_rolling_summary_versions_session_pk_summary_version",
        "ck_long_term_memories_status",
        "ix_long_term_memories_user_id_status_memory_type",
        "uq_deep_state_results_source_pipeline",
        "ck_deep_state_results_status",
        "ck_knowledge_documents_status",
        "uq_knowledge_documents_one_active_source",
        "fk_knowledge_documents_supersedes",
        "fk_knowledge_documents_replaced_by",
        "ix_knowledge_documents_status",
        "uq_knowledge_chunks_document_id_chunk_index",
        "ix_knowledge_chunks_embedding_hnsw",
        "uq_knowledge_usage_events_assistant_message_id_chunk_id",
    )

    for required_name in required_names:
        assert required_name in normalized_sql


def test_migration_resources_include_template_and_revision() -> None:
    """Non-editable installs must retain the Alembic template and revision."""

    storage_resources = files("storage")

    assert storage_resources.joinpath("migrations/script.py.mako").is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0001_initial_core_schema.py"
    ).is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0002_user_scoped_sessions.py"
    ).is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0003_deep_state_results.py"
    ).is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0004_memory_normalization.py"
    ).is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0005_knowledge_rag.py"
    ).is_file()
    assert storage_resources.joinpath(
        "migrations/versions/0006_knowledge_lifecycle_vector_audit.py"
    ).is_file()


def test_migration_code_never_uses_metadata_create_all() -> None:
    """Formal PostgreSQL initialization must remain Alembic-only."""

    migration_root = Path("storage/migrations")
    migration_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in migration_root.rglob("*.py")
    )

    assert "create_all" not in migration_sources
