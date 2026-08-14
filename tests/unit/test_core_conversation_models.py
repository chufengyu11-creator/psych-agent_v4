"""Pure metadata tests for the core conversation ORM models."""

from typing import Protocol, cast

from sqlalchemy import (
    JSON,
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
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.schema import (
    CallableColumnDefault,
    ScalarElementColumnDefault,
)

from storage.models.base import Base
from storage.models.intervention import (
    INTERVENTION_STATUS_CANCELLED,
    INTERVENTION_STATUS_EVALUATED,
    INTERVENTION_STATUS_PENDING,
    InterventionEventModel,
)
from storage.models.message import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_SYSTEM,
    MESSAGE_ROLE_USER,
    MessageModel,
)
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.user import UserModel


class _CreateIndexFactory(Protocol):
    """Typed constructor contract for SQLAlchemy's untyped DDL helper."""

    def __call__(self, index: Index) -> CreateIndex:
        """Construct DDL for one index."""


class _NamedSignalsFactory(Protocol):
    """Typed contract for the wrapped list default factory."""

    __name__: str

    def __call__(self, context: object | None) -> list[str]:
        """Return a fresh expected-signals list."""


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
    """Return an index or unique constraint's ordered column names."""

    return tuple(column.name for column in index.columns)


def test_models_register_expected_tables_and_constants() -> None:
    """Models should share Base and expose the agreed lifecycle values."""

    assert UserModel.__tablename__ == "users"
    assert SessionModel.__tablename__ == "sessions"
    assert MessageModel.__tablename__ == "messages"
    assert SessionStateVersionModel.__tablename__ == "session_state_versions"
    assert InterventionEventModel.__tablename__ == "intervention_events"
    assert MessageModel.metadata is Base.metadata
    assert SessionStateVersionModel.metadata is Base.metadata
    assert InterventionEventModel.metadata is Base.metadata
    assert Base.metadata.tables["users"] is UserModel.__table__
    assert Base.metadata.tables["sessions"] is SessionModel.__table__
    assert Base.metadata.tables["messages"] is MessageModel.__table__
    assert (
        Base.metadata.tables["session_state_versions"]
        is SessionStateVersionModel.__table__
    )
    assert (
        Base.metadata.tables["intervention_events"]
        is InterventionEventModel.__table__
    )

    assert (MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT, MESSAGE_ROLE_SYSTEM) == (
        "user",
        "assistant",
        "system",
    )
    assert (
        INTERVENTION_STATUS_PENDING,
        INTERVENTION_STATUS_EVALUATED,
        INTERVENTION_STATUS_CANCELLED,
    ) == ("pending", "evaluated", "cancelled")


def test_message_columns_have_expected_types_and_defaults() -> None:
    """Message columns should preserve ordering and ownership contracts."""

    table = _table("messages")
    assert tuple(table.c.keys()) == (
        "id",
        "session_pk",
        "role",
        "content",
        "sequence_number",
        "created_at",
        "model_name",
        "parent_message_id",
    )

    for column_name, length in (
        ("id", 64),
        ("role", 16),
        ("model_name", 128),
        ("parent_message_id", 64),
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == length

    assert isinstance(table.c.session_pk.type, Uuid)
    assert table.c.session_pk.type.as_uuid is True
    assert table.c.id.primary_key
    assert table.c.id.nullable is False
    assert table.c.session_pk.nullable is False
    assert table.c.role.nullable is False
    assert isinstance(table.c.content.type, Text)
    assert table.c.content.nullable is False
    assert isinstance(table.c.sequence_number.type, Integer)
    assert table.c.sequence_number.nullable is False
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None
    assert table.c.model_name.nullable
    assert table.c.parent_message_id.nullable

    for column_name in table.c.keys():
        assert table.c[column_name].default is None
    for column_name in set(table.c.keys()) - {"created_at"}:
        assert table.c[column_name].server_default is None


def test_message_constraints_and_foreign_keys_are_exact() -> None:
    """Message checks, uniqueness, and references should be deterministic."""

    table = _table("messages")
    checks = _named_check_constraints(table)
    unique_constraints = _named_unique_constraints(table)

    assert table.primary_key.name == "pk_messages"
    assert set(checks) == {
        "ck_messages_role",
        "ck_messages_sequence_number",
        "ck_messages_content_nonempty",
    }
    assert "'user'" in str(checks["ck_messages_role"].sqltext)
    assert "'assistant'" in str(checks["ck_messages_role"].sqltext)
    assert "'system'" in str(checks["ck_messages_role"].sqltext)
    assert "sequence_number >= 1" in str(
        checks["ck_messages_sequence_number"].sqltext
    )
    assert "length(content) > 0" in str(
        checks["ck_messages_content_nonempty"].sqltext
    )
    assert set(unique_constraints) == {
        "uq_messages_session_pk_sequence_number"
    }
    assert _column_names(
        unique_constraints["uq_messages_session_pk_sequence_number"]
    ) == ("session_pk", "sequence_number")
    assert _foreign_key_contracts(table) == {
        "fk_messages_session_pk_sessions": ("sessions.id", "RESTRICT"),
        "fk_messages_parent_message_id_messages": (
            "messages.id",
            "SET NULL",
        ),
    }


def test_message_indexes_have_expected_names_and_column_order() -> None:
    """Message lookup indexes should preserve stable ordered names."""

    indexes = _named_indexes(_table("messages"))

    assert set(indexes) == {
        "ix_messages_session_pk",
        "ix_messages_session_pk_created_at",
    }
    assert _column_names(indexes["ix_messages_session_pk"]) == ("session_pk",)
    assert _column_names(indexes["ix_messages_session_pk_created_at"]) == (
        "session_pk",
        "created_at",
    )


def test_session_state_columns_have_expected_types_and_nullability() -> None:
    """State versions should retain immutable snapshot column semantics."""

    table = _table("session_state_versions")
    assert tuple(table.c.keys()) == (
        "id",
        "session_pk",
        "version",
        "state_json",
        "source_message_id",
        "previous_version_id",
        "created_at",
    )

    for column_name in (
        "id",
        "source_message_id",
        "previous_version_id",
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == 64

    assert isinstance(table.c.session_pk.type, Uuid)
    assert table.c.session_pk.type.as_uuid is True
    assert table.c.id.primary_key
    assert table.c.id.nullable is False
    assert table.c.session_pk.nullable is False
    assert isinstance(table.c.version.type, Integer)
    assert table.c.version.nullable is False
    assert isinstance(table.c.state_json.type, JSON)
    assert table.c.state_json.nullable is False
    assert table.c.source_message_id.nullable
    assert table.c.previous_version_id.nullable
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None

    for column_name in table.c.keys():
        assert table.c[column_name].default is None
    for column_name in set(table.c.keys()) - {"created_at"}:
        assert table.c[column_name].server_default is None


def test_session_state_constraints_foreign_keys_and_indexes_are_exact() -> None:
    """State versions should be ordered, unique per session, and traceable."""

    table = _table("session_state_versions")
    checks = _named_check_constraints(table)
    unique_constraints = _named_unique_constraints(table)
    indexes = _named_indexes(table)

    assert table.primary_key.name == "pk_session_state_versions"
    assert set(checks) == {"ck_session_state_versions_version"}
    assert "version >= 0" in str(
        checks["ck_session_state_versions_version"].sqltext
    )
    assert set(unique_constraints) == {
        "uq_session_state_versions_session_pk_version"
    }
    assert _column_names(
        unique_constraints["uq_session_state_versions_session_pk_version"]
    ) == ("session_pk", "version")
    assert _foreign_key_contracts(table) == {
        "fk_session_state_versions_session_pk_sessions": (
            "sessions.id",
            "RESTRICT",
        ),
        "fk_session_state_versions_source_message_id_messages": (
            "messages.id",
            "RESTRICT",
        ),
        "fk_session_state_versions_previous_version_id_session_state_versions": (
            "session_state_versions.id",
            "RESTRICT",
        ),
    }
    assert set(indexes) == {
        "ix_session_state_versions_source_message_id",
        "ix_session_state_versions_session_pk_version",
    }
    assert _column_names(
        indexes["ix_session_state_versions_source_message_id"]
    ) == ("source_message_id",)
    assert _column_names(
        indexes["ix_session_state_versions_session_pk_version"]
    ) == ("session_pk", "version")


def test_intervention_required_columns_types_and_defaults_are_exact() -> None:
    """Required intervention fields should match the persistence contract."""

    table = _table("intervention_events")
    assert tuple(table.c.keys()) == (
        "id",
        "session_pk",
        "assistant_message_id",
        "strategy",
        "objective",
        "expected_signals",
        "status",
        "observed_response",
        "explicit_feedback",
        "strategy_fit",
        "objective_progress",
        "recommended_adjustment",
        "feedback_confidence",
        "created_at",
        "evaluated_at",
    )

    for column_name, length in (
        ("id", 64),
        ("assistant_message_id", 64),
        ("strategy", 64),
        ("status", 24),
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == length
        assert column.nullable is False

    assert isinstance(table.c.session_pk.type, Uuid)
    assert table.c.session_pk.type.as_uuid is True
    assert table.c.session_pk.nullable is False
    assert table.c.id.primary_key
    assert isinstance(table.c.objective.type, Text)
    assert table.c.objective.nullable is False
    assert isinstance(table.c.expected_signals.type, JSON)
    assert table.c.expected_signals.nullable is False
    expected_signals_default = table.c.expected_signals.default
    assert isinstance(expected_signals_default, CallableColumnDefault)
    assert expected_signals_default.is_callable
    expected_signals_factory = cast(
        _NamedSignalsFactory,
        expected_signals_default.arg,
    )
    assert expected_signals_factory.__name__ == "list"
    first_default = expected_signals_factory(None)
    second_default = expected_signals_factory(None)
    assert first_default == []
    assert second_default == []
    assert first_default is not second_default
    assert isinstance(table.c.expected_signals.server_default, DefaultClause)
    assert str(table.c.expected_signals.server_default.arg) == "'[]'"

    status_default = table.c.status.default
    assert isinstance(status_default, ScalarElementColumnDefault)
    assert status_default.arg == "pending"
    assert isinstance(table.c.status.server_default, DefaultClause)
    assert str(table.c.status.server_default.arg) == "'pending'"
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None

    for column_name in (
        "id",
        "assistant_message_id",
        "strategy",
        "objective",
    ):
        column = table.c[column_name]
        assert column.default is None
        assert column.server_default is None


def test_intervention_feedback_columns_are_nullable_and_typed() -> None:
    """Evaluation fields should remain absent until feedback is available."""

    table = _table("intervention_events")
    for column_name in (
        "observed_response",
        "recommended_adjustment",
    ):
        column = table.c[column_name]
        assert isinstance(column.type, Text)
        assert column.nullable

    for column_name in (
        "explicit_feedback",
        "strategy_fit",
        "objective_progress",
    ):
        column = table.c[column_name]
        assert isinstance(column.type, String)
        assert column.type.length == 32
        assert column.nullable

    assert isinstance(table.c.feedback_confidence.type, Float)
    assert table.c.feedback_confidence.nullable
    assert isinstance(table.c.evaluated_at.type, DateTime)
    assert table.c.evaluated_at.type.timezone is True
    assert table.c.evaluated_at.nullable

    for column_name in (
        "observed_response",
        "explicit_feedback",
        "strategy_fit",
        "objective_progress",
        "recommended_adjustment",
        "feedback_confidence",
        "evaluated_at",
    ):
        column = table.c[column_name]
        assert column.default is None
        assert column.server_default is None


def test_intervention_constraints_and_foreign_keys_are_exact() -> None:
    """Intervention lifecycle checks and ownership should be deterministic."""

    table = _table("intervention_events")
    checks = _named_check_constraints(table)
    unique_constraints = _named_unique_constraints(table)

    assert table.primary_key.name == "pk_intervention_events"
    assert set(checks) == {
        "ck_intervention_events_status",
        "ck_intervention_events_feedback_confidence",
    }
    status_sql = str(checks["ck_intervention_events_status"].sqltext)
    assert "'pending'" in status_sql
    assert "'evaluated'" in status_sql
    assert "'cancelled'" in status_sql
    confidence_sql = str(
        checks["ck_intervention_events_feedback_confidence"].sqltext
    )
    assert "feedback_confidence IS NULL" in confidence_sql
    assert "feedback_confidence >= 0" in confidence_sql
    assert "feedback_confidence <= 1" in confidence_sql
    assert set(unique_constraints) == {
        "uq_intervention_events_assistant_message_id"
    }
    assert _column_names(
        unique_constraints["uq_intervention_events_assistant_message_id"]
    ) == ("assistant_message_id",)
    assert _foreign_key_contracts(table) == {
        "fk_intervention_events_session_pk_sessions": (
            "sessions.id",
            "RESTRICT",
        ),
        "fk_intervention_events_assistant_message_id_messages": (
            "messages.id",
            "RESTRICT",
        ),
    }


def test_intervention_indexes_include_cross_dialect_pending_uniqueness() -> None:
    """Only one pending intervention should be indexable per session."""

    indexes = _named_indexes(_table("intervention_events"))
    assert set(indexes) == {
        "ix_intervention_events_session_pk_status_created_at",
        "uq_intervention_events_session_pk_pending",
    }
    assert _column_names(
        indexes["ix_intervention_events_session_pk_status_created_at"]
    ) == ("session_pk", "status", "created_at")

    partial_index = indexes["uq_intervention_events_session_pk_pending"]
    assert partial_index.unique
    assert _column_names(partial_index) == ("session_pk",)
    postgresql_where = partial_index.dialect_options["postgresql"]["where"]
    sqlite_where = partial_index.dialect_options["sqlite"]["where"]
    assert isinstance(postgresql_where, TextClause)
    assert isinstance(sqlite_where, TextClause)
    assert str(postgresql_where) == "status = 'pending'"
    assert str(sqlite_where) == "status = 'pending'"


def test_json_columns_compile_as_postgresql_jsonb() -> None:
    """Portable JSON columns should select JSONB for PostgreSQL compilation."""

    dialect = cast(type[Dialect], PGDialect)()
    state_json = _table("session_state_versions").c.state_json
    expected_signals = _table("intervention_events").c.expected_signals

    assert isinstance(state_json.type, JSON)
    assert isinstance(expected_signals.type, JSON)
    assert isinstance(state_json.type.dialect_impl(dialect), JSONB)
    assert isinstance(expected_signals.type.dialect_impl(dialect), JSONB)
    assert state_json.type.compile(dialect=dialect) == "JSONB"
    assert expected_signals.type.compile(dialect=dialect) == "JSONB"


def test_models_and_partial_index_compile_to_postgresql_ddl() -> None:
    """Core tables and their partial index should compile without a connection."""

    dialect = cast(type[Dialect], PGDialect)()
    message_ddl = str(
        CreateTable(_table("messages")).compile(dialect=dialect)
    )
    state_ddl = str(
        CreateTable(_table("session_state_versions")).compile(dialect=dialect)
    )
    intervention_ddl = str(
        CreateTable(_table("intervention_events")).compile(dialect=dialect)
    )
    partial_index = _named_indexes(_table("intervention_events"))[
        "uq_intervention_events_session_pk_pending"
    ]
    create_index = cast(_CreateIndexFactory, CreateIndex)
    partial_index_ddl = str(create_index(partial_index).compile(dialect=dialect))

    assert "CREATE TABLE messages" in message_ddl
    assert "FOREIGN KEY(session_pk)" in message_ddl
    assert "REFERENCES sessions (id) ON DELETE RESTRICT" in message_ddl
    assert "FOREIGN KEY(parent_message_id)" in message_ddl
    assert "REFERENCES messages (id) ON DELETE SET NULL" in message_ddl
    assert "CREATE TABLE session_state_versions" in state_ddl
    assert "state_json JSONB NOT NULL" in state_ddl
    assert "REFERENCES messages (id) ON DELETE RESTRICT" in state_ddl
    assert "CREATE TABLE intervention_events" in intervention_ddl
    assert "expected_signals JSONB DEFAULT '[]' NOT NULL" in intervention_ddl
    assert "status VARCHAR(24) DEFAULT 'pending' NOT NULL" in intervention_ddl
    assert "CREATE UNIQUE INDEX" in partial_index_ddl
    assert "uq_intervention_events_session_pk_pending" in partial_index_ddl
    assert "ON intervention_events (session_pk)" in partial_index_ddl
    assert "WHERE status = 'pending'" in partial_index_ddl


def test_sorted_tables_preserve_conversation_dependency_order() -> None:
    """Metadata sorting should place every referenced parent before its child."""

    sorted_names = [table.name for table in Base.metadata.sorted_tables]

    assert sorted_names.index("users") < sorted_names.index("sessions")
    assert sorted_names.index("sessions") < sorted_names.index("messages")
    assert sorted_names.index("messages") < sorted_names.index(
        "session_state_versions"
    )
    assert sorted_names.index("messages") < sorted_names.index(
        "intervention_events"
    )
