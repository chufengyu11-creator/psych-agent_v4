"""Pure metadata tests for rolling summary and long-term memory models."""

from typing import Protocol, cast

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    DefaultClause,
    Float,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.schema import (
    CallableColumnDefault,
    ColumnElementColumnDefault,
    ScalarElementColumnDefault,
)

from schemas.memory import (
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from storage.models import memory as memory_models
from storage.models.base import Base
from storage.models.memory import LongTermMemoryModel
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel


class _CreateIndexFactory(Protocol):
    """Typed constructor contract for SQLAlchemy's untyped DDL helper."""

    def __call__(self, index: Index) -> CreateIndex:
        """Construct DDL for one index."""


class _NamedListFactory(Protocol):
    """Typed contract for SQLAlchemy's wrapped list default factory."""

    __name__: str

    def __call__(self, context: object | None) -> list[str]:
        """Return a fresh list."""


def _table(name: str) -> Table:
    """Return a registered table with its concrete public type."""

    return Base.metadata.tables[name]


def _named_check_constraints(table: Table) -> dict[str, CheckConstraint]:
    """Return check constraints keyed by their public names."""

    checks: dict[str, CheckConstraint] = {}
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            assert isinstance(constraint.name, str)
            checks[constraint.name] = constraint
    return checks


def _named_unique_constraints(table: Table) -> dict[str, UniqueConstraint]:
    """Return unique constraints keyed by their public names."""

    unique_constraints: dict[str, UniqueConstraint] = {}
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            assert isinstance(constraint.name, str)
            unique_constraints[constraint.name] = constraint
    return unique_constraints


def _named_indexes(table: Table) -> dict[str, Index]:
    """Return indexes keyed by their public names."""

    indexes: dict[str, Index] = {}
    for index in table.indexes:
        assert isinstance(index.name, str)
        indexes[index.name] = index
    return indexes


def _foreign_key_contracts(table: Table) -> dict[str, tuple[str, str | None]]:
    """Return each named foreign key's target and deletion rule."""

    contracts: dict[str, tuple[str, str | None]] = {}
    for constraint in table.foreign_key_constraints:
        assert isinstance(constraint.name, str)
        elements = list(constraint.elements)
        assert len(elements) == 1
        contracts[constraint.name] = (
            elements[0].target_fullname,
            elements[0].ondelete,
        )
    return contracts


def _column_names(index: Index | UniqueConstraint) -> tuple[str, ...]:
    """Return ordered column names for an index or unique constraint."""

    return tuple(column.name for column in index.columns)


def _assert_fresh_list_default(table: Table, column_name: str) -> None:
    """Assert a JSON array column uses the list factory safely."""

    default = table.c[column_name].default
    assert isinstance(default, CallableColumnDefault)
    assert default.is_callable
    factory = cast(_NamedListFactory, default.arg)
    assert factory.__name__ == "list"
    first = factory(None)
    second = factory(None)
    assert first == []
    assert second == []
    assert first is not second


def test_models_register_expected_tables_on_shared_metadata() -> None:
    """New and parent models should retain one shared metadata registry."""

    assert UserModel.__tablename__ == "users"
    assert SessionModel.__tablename__ == "sessions"
    assert MessageModel.__tablename__ == "messages"
    assert RollingSummaryVersionModel.__tablename__ == (
        "rolling_summary_versions"
    )
    assert LongTermMemoryModel.__tablename__ == "long_term_memories"
    assert RollingSummaryVersionModel.metadata is Base.metadata
    assert LongTermMemoryModel.metadata is Base.metadata
    assert Base.metadata.tables["users"] is UserModel.__table__
    assert Base.metadata.tables["sessions"] is SessionModel.__table__
    assert Base.metadata.tables["messages"] is MessageModel.__table__
    assert (
        Base.metadata.tables["rolling_summary_versions"]
        is RollingSummaryVersionModel.__table__
    )
    assert (
        Base.metadata.tables["long_term_memories"]
        is LongTermMemoryModel.__table__
    )


def test_summary_columns_have_expected_types_and_defaults() -> None:
    """Summary versions should match the exact persistence contract."""

    table = _table("rolling_summary_versions")
    assert tuple(table.c.keys()) == (
        "id",
        "session_id",
        "summary_version",
        "summary_json",
        "covered_from_message_id",
        "covered_to_message_id",
        "previous_version_id",
        "created_at",
    )

    for column_name in (
        "id",
        "session_id",
        "covered_from_message_id",
        "covered_to_message_id",
        "previous_version_id",
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == 64

    assert table.c.id.primary_key
    assert table.c.id.nullable is False
    assert table.c.session_id.nullable is False
    assert isinstance(table.c.summary_version.type, Integer)
    assert table.c.summary_version.nullable is False
    assert isinstance(table.c.summary_json.type, JSON)
    assert table.c.summary_json.nullable is False
    assert table.c.covered_from_message_id.nullable is False
    assert table.c.covered_to_message_id.nullable is False
    assert table.c.previous_version_id.nullable
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None

    for column_name in table.c.keys():
        assert table.c[column_name].default is None
    for column_name in set(table.c.keys()) - {"created_at"}:
        assert table.c[column_name].server_default is None


def test_summary_constraints_and_foreign_keys_are_exact() -> None:
    """Summary version and traceability constraints should be deterministic."""

    table = _table("rolling_summary_versions")
    checks = _named_check_constraints(table)
    unique_constraints = _named_unique_constraints(table)

    assert table.primary_key.name == "pk_rolling_summary_versions"
    assert set(checks) == {"ck_rolling_summary_versions_summary_version"}
    assert "summary_version >= 1" in str(
        checks["ck_rolling_summary_versions_summary_version"].sqltext
    )
    assert set(unique_constraints) == {
        "uq_rolling_summary_versions_session_id_summary_version"
    }
    assert _column_names(
        unique_constraints[
            "uq_rolling_summary_versions_session_id_summary_version"
        ]
    ) == ("session_id", "summary_version")
    assert _foreign_key_contracts(table) == {
        "fk_rolling_summary_versions_session_id_sessions": (
            "sessions.id",
            "RESTRICT",
        ),
        "fk_rolling_summary_versions_covered_from_message_id_messages": (
            "messages.id",
            "RESTRICT",
        ),
        "fk_rolling_summary_versions_covered_to_message_id_messages": (
            "messages.id",
            "RESTRICT",
        ),
        "fk_rolling_summary_versions_previous_version_id_rolling_summary_versions": (
            "rolling_summary_versions.id",
            "RESTRICT",
        ),
    }


def test_summary_indexes_have_expected_names_and_column_order() -> None:
    """Summary lookup indexes should preserve stable ordered names."""

    indexes = _named_indexes(_table("rolling_summary_versions"))

    assert set(indexes) == {
        "ix_rolling_summary_versions_covered_to_message_id",
        "ix_rolling_summary_versions_session_id_summary_version",
    }
    assert _column_names(
        indexes["ix_rolling_summary_versions_covered_to_message_id"]
    ) == ("covered_to_message_id",)
    assert _column_names(
        indexes["ix_rolling_summary_versions_session_id_summary_version"]
    ) == ("session_id", "summary_version")


def test_memory_columns_have_exact_scalar_types_and_nullability() -> None:
    """Memory scalar fields should match the complete lifecycle contract."""

    table = _table("long_term_memories")
    assert tuple(table.c.keys()) == (
        "id",
        "user_id",
        "memory_type",
        "content",
        "sensitivity",
        "confidence",
        "source_message_ids",
        "source_type",
        "status",
        "requires_user_confirmation",
        "user_confirmed",
        "supersedes_memory_id",
        "conflict_memory_ids",
        "reinforcement_count",
        "valid_from",
        "valid_to",
        "last_reinforced_at",
        "expired_at",
        "deleted_at",
        "created_at",
        "updated_at",
    )

    for column_name, length in (
        ("id", 64),
        ("user_id", 64),
        ("memory_type", 40),
        ("sensitivity", 24),
        ("source_type", 40),
        ("status", 32),
        ("supersedes_memory_id", 64),
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == length

    assert table.c.id.primary_key
    assert table.c.id.nullable is False
    assert table.c.user_id.nullable is False
    assert table.c.memory_type.nullable is False
    assert isinstance(table.c.content.type, Text)
    assert table.c.content.nullable is False
    assert table.c.sensitivity.nullable is False
    assert isinstance(table.c.confidence.type, Float)
    assert table.c.confidence.nullable is False
    assert table.c.source_type.nullable is False
    assert table.c.status.nullable is False
    assert table.c.supersedes_memory_id.nullable
    assert isinstance(table.c.reinforcement_count.type, Integer)
    assert table.c.reinforcement_count.nullable is False

    for column_name in (
        "id",
        "user_id",
        "memory_type",
        "content",
        "sensitivity",
        "confidence",
        "source_type",
        "status",
        "supersedes_memory_id",
    ):
        column = table.c[column_name]
        assert column.default is None
        assert column.server_default is None


def test_memory_json_confirmation_and_counter_defaults_are_safe() -> None:
    """JSON arrays, confirmation flags, and counters should default safely."""

    table = _table("long_term_memories")
    for column_name in ("source_message_ids", "conflict_memory_ids"):
        column = table.c[column_name]
        assert isinstance(column.type, JSON)
        assert column.nullable is False
        _assert_fresh_list_default(table, column_name)
        assert isinstance(column.server_default, DefaultClause)
        assert str(column.server_default.arg) == "'[]'"

    for column_name in ("requires_user_confirmation", "user_confirmed"):
        column = table.c[column_name]
        assert isinstance(column.type, Boolean)
        assert column.nullable is False
        assert isinstance(column.default, ScalarElementColumnDefault)
        assert column.default.arg is False
        assert isinstance(column.server_default, DefaultClause)
        assert str(column.server_default.arg).lower() == "false"

    reinforcement_default = table.c.reinforcement_count.default
    assert isinstance(reinforcement_default, ScalarElementColumnDefault)
    assert reinforcement_default.arg == 0
    assert isinstance(table.c.reinforcement_count.server_default, DefaultClause)
    assert str(table.c.reinforcement_count.server_default.arg) == "0"
    assert table.c.status.default is None
    assert table.c.status.server_default is None


def test_memory_timestamp_fields_are_timezone_aware() -> None:
    """Memory lifecycle timestamps should retain their optionality and updates."""

    table = _table("long_term_memories")
    optional_timestamps = (
        "valid_from",
        "valid_to",
        "last_reinforced_at",
        "expired_at",
        "deleted_at",
    )
    for column_name in optional_timestamps:
        column = table.c[column_name]
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True
        assert column.nullable
        assert column.default is None
        assert column.server_default is None

    for column_name in ("created_at", "updated_at"):
        column = table.c[column_name]
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True
        assert column.nullable is False
        assert column.server_default is not None
    assert table.c.created_at.onupdate is None
    updated_onupdate = table.c.updated_at.onupdate
    assert isinstance(updated_onupdate, ColumnElementColumnDefault)
    assert str(updated_onupdate.arg) == "now()"


def test_memory_status_numeric_and_content_constraints_are_exact() -> None:
    """Memory lifecycle, confidence, counters, and content should be bounded."""

    table = _table("long_term_memories")
    checks = _named_check_constraints(table)

    assert table.primary_key.name == "pk_long_term_memories"
    assert set(checks) == {
        "ck_long_term_memories_status",
        "ck_long_term_memories_memory_type",
        "ck_long_term_memories_sensitivity",
        "ck_long_term_memories_source_type",
        "ck_long_term_memories_confidence",
        "ck_long_term_memories_reinforcement_count",
        "ck_long_term_memories_content_nonempty",
    }
    status_sql = str(checks["ck_long_term_memories_status"].sqltext)
    for status in (
        "pending_confirmation",
        "active",
        "superseded",
        "conflicted",
        "expired",
        "deleted",
    ):
        assert f"'{status}'" in status_sql
    assert "confidence >= 0" in str(
        checks["ck_long_term_memories_confidence"].sqltext
    )
    assert "confidence <= 1" in str(
        checks["ck_long_term_memories_confidence"].sqltext
    )
    assert "reinforcement_count >= 0" in str(
        checks["ck_long_term_memories_reinforcement_count"].sqltext
    )
    assert "length(content) > 0" in str(
        checks["ck_long_term_memories_content_nonempty"].sqltext
    )


def test_memory_schema_enum_constants_and_constraints_stay_aligned() -> None:
    """Persisted enum constants and checks should match public Schema values."""

    checks = _named_check_constraints(_table("long_term_memories"))
    orm_memory_types = {
        memory_models.MEMORY_TYPE_INTERACTION_PREFERENCE,
        memory_models.MEMORY_TYPE_ACTIVE_GOAL,
        memory_models.MEMORY_TYPE_UNFINISHED_TOPIC,
        memory_models.MEMORY_TYPE_SEMANTIC,
        memory_models.MEMORY_TYPE_EPISODIC,
        memory_models.MEMORY_TYPE_STRATEGY_OUTCOME,
    }
    orm_sensitivities = {
        memory_models.MEMORY_SENSITIVITY_LOW,
        memory_models.MEMORY_SENSITIVITY_MEDIUM,
        memory_models.MEMORY_SENSITIVITY_HIGH,
    }
    orm_source_types = {
        memory_models.MEMORY_SOURCE_TYPE_EXPLICIT_USER_STATEMENT,
        memory_models.MEMORY_SOURCE_TYPE_SESSION_SUMMARY,
        memory_models.MEMORY_SOURCE_TYPE_REPEATED_OBSERVATION,
        memory_models.MEMORY_SOURCE_TYPE_MODEL_INFERENCE,
    }

    assert orm_memory_types == {member.value for member in MemoryType}
    assert orm_sensitivities == {
        member.value for member in MemorySensitivity
    }
    assert orm_source_types == {member.value for member in MemorySourceType}
    assert {member.value for member in MemoryOperation} == {
        "CREATE",
        "REINFORCE",
        "SUPERSEDE",
        "MARK_CONFLICT",
        "EXPIRE",
        "DELETE",
    }

    enum_checks = (
        ("ck_long_term_memories_memory_type", orm_memory_types),
        ("ck_long_term_memories_sensitivity", orm_sensitivities),
        ("ck_long_term_memories_source_type", orm_source_types),
    )
    for constraint_name, expected_values in enum_checks:
        check_sql = str(checks[constraint_name].sqltext)
        for expected_value in expected_values:
            assert f"'{expected_value}'" in check_sql


def test_memory_foreign_keys_and_indexes_are_exact() -> None:
    """Memory ownership, supersession, and lookup indexes should be stable."""

    table = _table("long_term_memories")
    assert _foreign_key_contracts(table) == {
        "fk_long_term_memories_user_id_users": ("users.id", "RESTRICT"),
        "fk_long_term_memories_supersedes_memory_id_long_term_memories": (
            "long_term_memories.id",
            "RESTRICT",
        ),
    }
    indexes = _named_indexes(table)
    assert set(indexes) == {
        "ix_long_term_memories_user_id",
        "ix_long_term_memories_user_id_status_memory_type",
        "ix_long_term_memories_user_id_updated_at",
    }
    assert _column_names(indexes["ix_long_term_memories_user_id"]) == (
        "user_id",
    )
    assert _column_names(
        indexes["ix_long_term_memories_user_id_status_memory_type"]
    ) == ("user_id", "status", "memory_type")
    assert _column_names(
        indexes["ix_long_term_memories_user_id_updated_at"]
    ) == ("user_id", "updated_at")


def test_json_columns_compile_as_postgresql_jsonb() -> None:
    """Portable JSON columns should select JSONB for PostgreSQL."""

    dialect = cast(type[Dialect], PGDialect)()
    json_columns = (
        _table("rolling_summary_versions").c.summary_json,
        _table("long_term_memories").c.source_message_ids,
        _table("long_term_memories").c.conflict_memory_ids,
    )

    for column in json_columns:
        assert isinstance(column.type, JSON)
        assert isinstance(column.type.dialect_impl(dialect), JSONB)
        assert column.type.compile(dialect=dialect) == "JSONB"


def test_models_and_all_indexes_compile_to_postgresql_ddl() -> None:
    """New tables and indexes should compile entirely without a connection."""

    dialect = cast(type[Dialect], PGDialect)()
    summary_table = _table("rolling_summary_versions")
    memory_table = _table("long_term_memories")
    summary_ddl = str(CreateTable(summary_table).compile(dialect=dialect))
    memory_ddl = str(CreateTable(memory_table).compile(dialect=dialect))

    assert "CREATE TABLE rolling_summary_versions" in summary_ddl
    assert "summary_json JSONB NOT NULL" in summary_ddl
    assert "FOREIGN KEY" in summary_ddl
    assert "REFERENCES messages (id) ON DELETE RESTRICT" in summary_ddl
    assert "CHECK (summary_version >= 1)" in summary_ddl
    assert "UNIQUE (session_id, summary_version)" in summary_ddl
    assert "CREATE TABLE long_term_memories" in memory_ddl
    assert "source_message_ids JSONB" in memory_ddl
    assert "conflict_memory_ids JSONB" in memory_ddl
    assert "REFERENCES users (id) ON DELETE RESTRICT" in memory_ddl
    assert "CHECK (confidence >= 0 AND confidence <= 1)" in memory_ddl

    create_index = cast(_CreateIndexFactory, CreateIndex)
    all_indexes = summary_table.indexes | memory_table.indexes
    compiled_indexes: dict[str, str] = {}
    for index in all_indexes:
        assert isinstance(index.name, str)
        compiled_indexes[index.name] = str(
            create_index(index).compile(dialect=dialect)
        )
    assert set(compiled_indexes) == {
        "ix_rolling_summary_versions_covered_to_message_id",
        "ix_rolling_summary_versions_session_id_summary_version",
        "ix_long_term_memories_user_id",
        "ix_long_term_memories_user_id_status_memory_type",
        "ix_long_term_memories_user_id_updated_at",
    }
    for index_name, index_ddl in compiled_indexes.items():
        assert "CREATE INDEX" in index_ddl
        assert index_name in index_ddl


def test_sorted_tables_preserve_summary_and_memory_dependencies() -> None:
    """Parent tables should precede summary and memory child tables."""

    sorted_names = [table.name for table in Base.metadata.sorted_tables]

    assert sorted_names.index("users") < sorted_names.index("sessions")
    assert sorted_names.index("sessions") < sorted_names.index("messages")
    assert sorted_names.index("messages") < sorted_names.index(
        "rolling_summary_versions"
    )
    assert sorted_names.index("users") < sorted_names.index(
        "long_term_memories"
    )
