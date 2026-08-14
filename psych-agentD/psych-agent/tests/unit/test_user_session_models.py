"""Pure metadata tests for the User and Session ORM models."""

from typing import cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    DefaultClause,
    Index,
    Integer,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import ScalarElementColumnDefault

from storage.models.base import Base
from storage.models.session import SessionModel
from storage.models.user import UserModel


def _user_table() -> Table:
    """Return the registered users table with its concrete public type."""

    return Base.metadata.tables["users"]


def _session_table() -> Table:
    """Return the registered sessions table with its concrete public type."""

    return Base.metadata.tables["sessions"]


def _named_check_constraints(table: Table) -> dict[str, CheckConstraint]:
    """Return check constraints keyed by their public names."""

    checks: dict[str, CheckConstraint] = {}
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            assert isinstance(constraint.name, str)
            checks[constraint.name] = constraint
    return checks


def _named_indexes(table: Table) -> dict[str, Index]:
    """Return indexes keyed by their public names."""

    indexes: dict[str, Index] = {}
    for index in table.indexes:
        assert isinstance(index.name, str)
        indexes[index.name] = index
    return indexes


def test_models_use_shared_base_and_expected_table_names() -> None:
    """Both models should register their expected tables on the shared Base."""

    assert UserModel.__tablename__ == "users"
    assert SessionModel.__tablename__ == "sessions"
    assert UserModel.metadata is Base.metadata
    assert SessionModel.metadata is Base.metadata
    assert Base.metadata.tables["users"] is UserModel.__table__
    assert Base.metadata.tables["sessions"] is SessionModel.__table__


def test_user_columns_have_expected_types_and_defaults() -> None:
    """User columns should match the agreed storage contract."""

    table = _user_table()
    assert set(table.c.keys()) == {
        "id",
        "status",
        "memory_enabled",
        "locale",
        "timezone",
        "created_at",
        "deleted_at",
    }

    assert isinstance(table.c.id.type, String)
    assert table.c.id.type.length == 64
    assert table.c.id.primary_key
    assert table.c.id.nullable is False
    assert table.c.id.default is None
    assert table.c.id.server_default is None

    assert isinstance(table.c.status.type, String)
    assert table.c.status.type.length == 24
    assert table.c.status.nullable is False
    assert isinstance(table.c.status.default, ScalarElementColumnDefault)
    assert table.c.status.default.arg == "active"
    assert isinstance(table.c.status.server_default, DefaultClause)
    assert str(table.c.status.server_default.arg) == "'active'"

    assert isinstance(table.c.memory_enabled.type, Boolean)
    assert table.c.memory_enabled.nullable is False
    assert isinstance(table.c.memory_enabled.default, ScalarElementColumnDefault)
    assert table.c.memory_enabled.default.arg is False
    assert isinstance(table.c.memory_enabled.server_default, DefaultClause)
    assert str(table.c.memory_enabled.server_default.arg).lower() == "false"

    assert isinstance(table.c.locale.type, String)
    assert table.c.locale.type.length == 35
    assert table.c.locale.nullable
    assert isinstance(table.c.timezone.type, String)
    assert table.c.timezone.type.length == 64
    assert table.c.timezone.nullable

    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None
    assert isinstance(table.c.deleted_at.type, DateTime)
    assert table.c.deleted_at.type.timezone is True
    assert table.c.deleted_at.nullable


def test_user_constraints_and_indexes_are_named() -> None:
    """User status constraints and indexes should be deterministic."""

    table = _user_table()
    checks = _named_check_constraints(table)
    indexes = _named_indexes(table)

    assert table.primary_key.name == "pk_users"
    assert set(checks) == {"ck_users_status"}
    status_sql = str(checks["ck_users_status"].sqltext)
    assert "active" in status_sql
    assert "disabled" in status_sql
    assert "deleted" in status_sql
    assert set(indexes) == {"ix_users_status"}
    assert tuple(column.name for column in indexes["ix_users_status"].columns) == (
        "status",
    )


def test_session_columns_have_expected_types_and_defaults() -> None:
    """Session columns should match the agreed storage contract."""

    table = _session_table()
    assert set(table.c.keys()) == {
        "id",
        "user_id",
        "status",
        "started_at",
        "ended_at",
        "current_state_version",
        "current_summary_version",
        "next_message_sequence",
    }

    assert isinstance(table.c.id.type, String)
    assert table.c.id.type.length == 64
    assert table.c.id.primary_key
    assert table.c.id.default is None
    assert table.c.id.server_default is None

    assert isinstance(table.c.user_id.type, String)
    assert table.c.user_id.type.length == 64
    assert table.c.user_id.nullable is False
    assert isinstance(table.c.status.type, String)
    assert table.c.status.type.length == 24
    assert table.c.status.nullable is False
    assert isinstance(table.c.status.default, ScalarElementColumnDefault)
    assert table.c.status.default.arg == "active"
    assert isinstance(table.c.status.server_default, DefaultClause)
    assert str(table.c.status.server_default.arg) == "'active'"

    assert isinstance(table.c.started_at.type, DateTime)
    assert table.c.started_at.type.timezone is True
    assert table.c.started_at.nullable is False
    assert table.c.started_at.server_default is not None
    assert isinstance(table.c.ended_at.type, DateTime)
    assert table.c.ended_at.type.timezone is True
    assert table.c.ended_at.nullable

    for column_name in ("current_state_version", "current_summary_version"):
        column = table.c[column_name]
        assert isinstance(column.type, Integer)
        assert column.nullable is False
        assert isinstance(column.default, ScalarElementColumnDefault)
        assert column.default.arg == 0
        assert isinstance(column.server_default, DefaultClause)
        assert str(column.server_default.arg) == "0"

    assert isinstance(table.c.next_message_sequence.type, Integer)
    assert table.c.next_message_sequence.nullable is False
    assert isinstance(
        table.c.next_message_sequence.default,
        ScalarElementColumnDefault,
    )
    assert table.c.next_message_sequence.default.arg == 1
    assert isinstance(table.c.next_message_sequence.server_default, DefaultClause)
    assert str(table.c.next_message_sequence.server_default.arg) == "1"


def test_session_constraints_are_named_and_complete() -> None:
    """Session status and counter checks should preserve their agreed bounds."""

    table = _session_table()
    checks = _named_check_constraints(table)
    foreign_keys = list(table.foreign_key_constraints)

    assert table.primary_key.name == "pk_sessions"
    assert len(foreign_keys) == 1
    assert foreign_keys[0].name == "fk_sessions_user_id_users"
    assert set(checks) == {
        "ck_sessions_status",
        "ck_sessions_current_state_version",
        "ck_sessions_current_summary_version",
        "ck_sessions_next_message_sequence",
    }

    status_sql = str(checks["ck_sessions_status"].sqltext)
    assert "active" in status_sql
    assert "closed" in status_sql
    assert "cancelled" in status_sql
    assert "current_state_version >= 0" in str(
        checks["ck_sessions_current_state_version"].sqltext
    )
    assert "current_summary_version >= 0" in str(
        checks["ck_sessions_current_summary_version"].sqltext
    )
    assert "next_message_sequence >= 1" in str(
        checks["ck_sessions_next_message_sequence"].sqltext
    )


def test_session_indexes_have_expected_names_and_column_order() -> None:
    """Session lookup indexes should use stable names and ordered columns."""

    indexes = _named_indexes(_session_table())

    assert set(indexes) == {
        "ix_sessions_user_id",
        "ix_sessions_user_id_status",
        "ix_sessions_status_started_at",
    }
    assert tuple(column.name for column in indexes["ix_sessions_user_id"].columns) == (
        "user_id",
    )
    assert tuple(
        column.name for column in indexes["ix_sessions_user_id_status"].columns
    ) == ("user_id", "status")
    assert tuple(
        column.name for column in indexes["ix_sessions_status_started_at"].columns
    ) == ("status", "started_at")


def test_session_foreign_key_and_table_order_are_correct() -> None:
    """Sessions should depend on users through one restrictive foreign key."""

    foreign_keys = list(_session_table().c.user_id.foreign_keys)
    sorted_names = [table.name for table in Base.metadata.sorted_tables]

    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "users.id"
    assert foreign_keys[0].ondelete == "RESTRICT"
    assert sorted_names.index("users") < sorted_names.index("sessions")


def test_id_and_counter_default_responsibilities_are_explicit() -> None:
    """IDs should be caller-provided while counters retain model defaults."""

    user_table = _user_table()
    session_table = _session_table()

    assert user_table.c.id.default is None
    assert user_table.c.id.server_default is None
    assert session_table.c.id.default is None
    assert session_table.c.id.server_default is None
    state_default = session_table.c.current_state_version.default
    summary_default = session_table.c.current_summary_version.default
    sequence_default = session_table.c.next_message_sequence.default
    assert isinstance(state_default, ScalarElementColumnDefault)
    assert isinstance(summary_default, ScalarElementColumnDefault)
    assert isinstance(sequence_default, ScalarElementColumnDefault)
    assert state_default.arg == 0
    assert summary_default.arg == 0
    assert sequence_default.arg == 1


def test_models_compile_to_postgresql_ddl_without_connection() -> None:
    """Both tables should compile to PostgreSQL DDL entirely in memory."""

    dialect = cast(type[Dialect], PGDialect)()
    user_ddl = str(CreateTable(_user_table()).compile(dialect=dialect))
    session_ddl = str(CreateTable(_session_table()).compile(dialect=dialect))

    assert "CREATE TABLE users" in user_ddl
    assert "CREATE TABLE sessions" in session_ddl
    assert "FOREIGN KEY(user_id)" in session_ddl
    assert "REFERENCES users (id)" in session_ddl
