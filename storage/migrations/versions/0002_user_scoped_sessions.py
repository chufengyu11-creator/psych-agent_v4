"""Introduce internal UUID session keys and user-scoped display IDs.

Revision ID: 0002_user_scoped_sessions
Revises: 0001_initial_core_schema
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_scoped_sessions"
down_revision: str | Sequence[str] | None = "0001_initial_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHILD_TABLES = (
    "messages",
    "session_state_versions",
    "intervention_events",
    "rolling_summary_versions",
)


def upgrade() -> None:
    """Migrate external session IDs to owner-scoped IDs without losing data."""

    _require_postgresql()

    op.add_column(
        "sessions",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("session_pk", sa.Uuid(), nullable=True),
    )
    op.execute("UPDATE sessions SET session_id = id")
    op.execute("UPDATE sessions SET session_pk = CAST(md5(id) AS UUID)")

    for table_name in _CHILD_TABLES:
        op.add_column(
            table_name,
            sa.Column("session_pk", sa.Uuid(), nullable=True),
        )
        op.execute(
            sa.text(
                f"UPDATE {table_name} AS child "
                "SET session_pk = sessions.session_pk "
                "FROM sessions "
                "WHERE child.session_id = sessions.id"
            )
        )

    _drop_legacy_child_session_constraints()

    for table_name in _CHILD_TABLES:
        op.drop_column(table_name, "session_id")

    op.drop_constraint("pk_sessions", "sessions", type_="primary")
    op.drop_column("sessions", "id")
    op.alter_column(
        "sessions",
        "session_pk",
        new_column_name="id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.alter_column(
        "sessions",
        "session_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_primary_key("pk_sessions", "sessions", ["id"])
    op.create_unique_constraint(
        "uq_sessions_user_id_session_id",
        "sessions",
        ["user_id", "session_id"],
    )

    _create_scoped_child_session_constraints()


def downgrade() -> None:
    """Restore global session IDs only when the current data can represent them."""

    _require_postgresql()
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT session_id
                FROM sessions
                GROUP BY session_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: session_id values are not globally unique';
            END IF;
        END
        $$;
        """
    )

    op.add_column(
        "sessions",
        sa.Column("legacy_id", sa.String(length=64), nullable=True),
    )
    op.execute("UPDATE sessions SET legacy_id = session_id")

    for table_name in _CHILD_TABLES:
        op.add_column(
            table_name,
            sa.Column("session_id", sa.String(length=64), nullable=True),
        )
        op.execute(
            sa.text(
                f"UPDATE {table_name} AS child "
                "SET session_id = sessions.session_id "
                "FROM sessions "
                "WHERE child.session_pk = sessions.id"
            )
        )

    _drop_scoped_child_session_constraints()
    for table_name in _CHILD_TABLES:
        op.drop_column(table_name, "session_pk")

    op.drop_constraint(
        "uq_sessions_user_id_session_id",
        "sessions",
        type_="unique",
    )
    op.drop_constraint("pk_sessions", "sessions", type_="primary")
    op.drop_column("sessions", "id")
    op.alter_column(
        "sessions",
        "legacy_id",
        new_column_name="id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_primary_key("pk_sessions", "sessions", ["id"])

    for table_name in _CHILD_TABLES:
        op.alter_column(
            table_name,
            "session_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    _create_legacy_child_session_constraints()
    op.drop_column("sessions", "session_id")


def _drop_legacy_child_session_constraints() -> None:
    op.drop_index("ix_messages_session_id_created_at", table_name="messages")
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_constraint(
        "uq_messages_session_id_sequence_number",
        "messages",
        type_="unique",
    )
    op.drop_constraint(
        "fk_messages_session_id_sessions",
        "messages",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_session_state_versions_session_id_version",
        table_name="session_state_versions",
    )
    op.drop_constraint(
        "uq_session_state_versions_session_id_version",
        "session_state_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_session_state_versions_session_id_sessions",
        "session_state_versions",
        type_="foreignkey",
    )

    op.drop_index(
        "uq_intervention_events_session_id_pending",
        table_name="intervention_events",
    )
    op.drop_index(
        "ix_intervention_events_session_id_status_created_at",
        table_name="intervention_events",
    )
    op.drop_constraint(
        "fk_intervention_events_session_id_sessions",
        "intervention_events",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_rolling_summary_versions_session_id_summary_version",
        table_name="rolling_summary_versions",
    )
    op.drop_constraint(
        "uq_rolling_summary_versions_session_id_summary_version",
        "rolling_summary_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_rolling_summary_versions_session_id_sessions",
        "rolling_summary_versions",
        type_="foreignkey",
    )


def _create_scoped_child_session_constraints() -> None:
    for table_name in _CHILD_TABLES:
        op.alter_column(
            table_name,
            "session_pk",
            existing_type=sa.Uuid(),
            nullable=False,
        )

    op.create_foreign_key(
        "fk_messages_session_pk_sessions",
        "messages",
        "sessions",
        ["session_pk"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_messages_session_pk_sequence_number",
        "messages",
        ["session_pk", "sequence_number"],
    )
    op.create_index(
        "ix_messages_session_pk",
        "messages",
        ["session_pk"],
        unique=False,
    )
    op.create_index(
        "ix_messages_session_pk_created_at",
        "messages",
        ["session_pk", "created_at"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_session_state_versions_session_pk_sessions",
        "session_state_versions",
        "sessions",
        ["session_pk"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_session_state_versions_session_pk_version",
        "session_state_versions",
        ["session_pk", "version"],
    )
    op.create_index(
        "ix_session_state_versions_session_pk_version",
        "session_state_versions",
        ["session_pk", "version"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_intervention_events_session_pk_sessions",
        "intervention_events",
        "sessions",
        ["session_pk"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_intervention_events_session_pk_status_created_at",
        "intervention_events",
        ["session_pk", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_intervention_events_session_pk_pending",
        "intervention_events",
        ["session_pk"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_foreign_key(
        "fk_rolling_summary_versions_session_pk_sessions",
        "rolling_summary_versions",
        "sessions",
        ["session_pk"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_rolling_summary_versions_session_pk_summary_version",
        "rolling_summary_versions",
        ["session_pk", "summary_version"],
    )
    op.create_index(
        "ix_rolling_summary_versions_session_pk_summary_version",
        "rolling_summary_versions",
        ["session_pk", "summary_version"],
        unique=False,
    )


def _drop_scoped_child_session_constraints() -> None:
    op.drop_index("ix_messages_session_pk_created_at", table_name="messages")
    op.drop_index("ix_messages_session_pk", table_name="messages")
    op.drop_constraint(
        "uq_messages_session_pk_sequence_number",
        "messages",
        type_="unique",
    )
    op.drop_constraint(
        "fk_messages_session_pk_sessions",
        "messages",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_session_state_versions_session_pk_version",
        table_name="session_state_versions",
    )
    op.drop_constraint(
        "uq_session_state_versions_session_pk_version",
        "session_state_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_session_state_versions_session_pk_sessions",
        "session_state_versions",
        type_="foreignkey",
    )

    op.drop_index(
        "uq_intervention_events_session_pk_pending",
        table_name="intervention_events",
    )
    op.drop_index(
        "ix_intervention_events_session_pk_status_created_at",
        table_name="intervention_events",
    )
    op.drop_constraint(
        "fk_intervention_events_session_pk_sessions",
        "intervention_events",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_rolling_summary_versions_session_pk_summary_version",
        table_name="rolling_summary_versions",
    )
    op.drop_constraint(
        "uq_rolling_summary_versions_session_pk_summary_version",
        "rolling_summary_versions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_rolling_summary_versions_session_pk_sessions",
        "rolling_summary_versions",
        type_="foreignkey",
    )


def _create_legacy_child_session_constraints() -> None:
    op.create_foreign_key(
        "fk_messages_session_id_sessions",
        "messages",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_messages_session_id_sequence_number",
        "messages",
        ["session_id", "sequence_number"],
    )
    op.create_index(
        "ix_messages_session_id",
        "messages",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_messages_session_id_created_at",
        "messages",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_session_state_versions_session_id_sessions",
        "session_state_versions",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_session_state_versions_session_id_version",
        "session_state_versions",
        ["session_id", "version"],
    )
    op.create_index(
        "ix_session_state_versions_session_id_version",
        "session_state_versions",
        ["session_id", "version"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_intervention_events_session_id_sessions",
        "intervention_events",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_intervention_events_session_id_status_created_at",
        "intervention_events",
        ["session_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_intervention_events_session_id_pending",
        "intervention_events",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_foreign_key(
        "fk_rolling_summary_versions_session_id_sessions",
        "rolling_summary_versions",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_rolling_summary_versions_session_id_summary_version",
        "rolling_summary_versions",
        ["session_id", "summary_version"],
    )
    op.create_index(
        "ix_rolling_summary_versions_session_id_summary_version",
        "rolling_summary_versions",
        ["session_id", "summary_version"],
        unique=False,
    )


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError(
            "0002_user_scoped_sessions requires PostgreSQL for data-preserving migration"
        )
