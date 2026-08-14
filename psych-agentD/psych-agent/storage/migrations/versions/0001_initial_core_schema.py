"""Create the initial seven-table core schema.

Revision ID: 0001_initial_core_schema
Revises:
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_core_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all core tables, constraints, and indexes."""

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("locale", sa.String(length=35), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deleted')",
            name="ck_users_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index(
        "ix_users_status",
        "users",
        ["status"],
        unique=False,
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "current_state_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "current_summary_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_message_sequence",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'closed', 'cancelled')",
            name="ck_sessions_status",
        ),
        sa.CheckConstraint(
            "current_state_version >= 0",
            name="ck_sessions_current_state_version",
        ),
        sa.CheckConstraint(
            "current_summary_version >= 0",
            name="ck_sessions_current_summary_version",
        ),
        sa.CheckConstraint(
            "next_message_sequence >= 1",
            name="ck_sessions_next_message_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index(
        "ix_sessions_user_id",
        "sessions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_user_id_status",
        "sessions",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_status_started_at",
        "sessions",
        ["status", "started_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column(
            "parent_message_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_messages_role",
        ),
        sa.CheckConstraint(
            "sequence_number >= 1",
            name="ck_messages_sequence_number",
        ),
        sa.CheckConstraint(
            "length(content) > 0",
            name="ck_messages_content_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"],
            ["messages.id"],
            name="fk_messages_parent_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_messages_session_id_sessions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint(
            "session_id",
            "sequence_number",
            name="uq_messages_session_id_sequence_number",
        ),
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

    op.create_table(
        "session_state_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "state_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "previous_version_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_session_state_versions_version",
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["session_state_versions.id"],
            name=sa.schema.conv(
                "fk_session_state_versions_previous_version_id_"
                "session_state_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_state_versions_session_id_sessions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name="fk_session_state_versions_source_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session_state_versions"),
        sa.UniqueConstraint(
            "session_id",
            "version",
            name="uq_session_state_versions_session_id_version",
        ),
    )
    op.create_index(
        "ix_session_state_versions_source_message_id",
        "session_state_versions",
        ["source_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_state_versions_session_id_version",
        "session_state_versions",
        ["session_id", "version"],
        unique=False,
    )

    op.create_table(
        "intervention_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column(
            "assistant_message_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "expected_signals",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("observed_response", sa.Text(), nullable=True),
        sa.Column(
            "explicit_feedback",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column("strategy_fit", sa.String(length=32), nullable=True),
        sa.Column(
            "objective_progress",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column("recommended_adjustment", sa.Text(), nullable=True),
        sa.Column("feedback_confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'evaluated', 'cancelled')",
            name="ck_intervention_events_status",
        ),
        sa.CheckConstraint(
            "feedback_confidence IS NULL OR "
            "(feedback_confidence >= 0 AND feedback_confidence <= 1)",
            name="ck_intervention_events_feedback_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["messages.id"],
            name=(
                "fk_intervention_events_assistant_message_id_messages"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_intervention_events_session_id_sessions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_intervention_events"),
        sa.UniqueConstraint(
            "assistant_message_id",
            name="uq_intervention_events_assistant_message_id",
        ),
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
        sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "rolling_summary_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("summary_version", sa.Integer(), nullable=False),
        sa.Column(
            "summary_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "covered_from_message_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "covered_to_message_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "previous_version_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "summary_version >= 1",
            name="ck_rolling_summary_versions_summary_version",
        ),
        sa.ForeignKeyConstraint(
            ["covered_from_message_id"],
            ["messages.id"],
            name=(
                "fk_rolling_summary_versions_"
                "covered_from_message_id_messages"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["covered_to_message_id"],
            ["messages.id"],
            name=(
                "fk_rolling_summary_versions_"
                "covered_to_message_id_messages"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["rolling_summary_versions.id"],
            name=sa.schema.conv(
                "fk_rolling_summary_versions_previous_version_id_"
                "rolling_summary_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_rolling_summary_versions_session_id_sessions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_rolling_summary_versions",
        ),
        sa.UniqueConstraint(
            "session_id",
            "summary_version",
            name=(
                "uq_rolling_summary_versions_"
                "session_id_summary_version"
            ),
        ),
    )
    op.create_index(
        "ix_rolling_summary_versions_covered_to_message_id",
        "rolling_summary_versions",
        ["covered_to_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_rolling_summary_versions_session_id_summary_version",
        "rolling_summary_versions",
        ["session_id", "summary_version"],
        unique=False,
    )

    op.create_table(
        "long_term_memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=40), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sensitivity", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "source_message_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "requires_user_confirmation",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "user_confirmed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "supersedes_memory_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "conflict_memory_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "reinforcement_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "valid_to",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_reinforced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expired_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending_confirmation', "
            "'active', "
            "'superseded', "
            "'conflicted', "
            "'expired', "
            "'deleted'"
            ")",
            name="ck_long_term_memories_status",
        ),
        sa.CheckConstraint(
            "memory_type IN ("
            "'interaction_preference', "
            "'active_goal', "
            "'unfinished_topic', "
            "'semantic_memory', "
            "'episodic_memory', "
            "'strategy_outcome'"
            ")",
            name="ck_long_term_memories_memory_type",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high')",
            name="ck_long_term_memories_sensitivity",
        ),
        sa.CheckConstraint(
            "source_type IN ("
            "'explicit_user_statement', "
            "'session_summary', "
            "'repeated_observation', "
            "'model_inference'"
            ")",
            name="ck_long_term_memories_source_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_long_term_memories_confidence",
        ),
        sa.CheckConstraint(
            "reinforcement_count >= 0",
            name="ck_long_term_memories_reinforcement_count",
        ),
        sa.CheckConstraint(
            "length(content) > 0",
            name="ck_long_term_memories_content_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_memory_id"],
            ["long_term_memories.id"],
            name=(
                "fk_long_term_memories_supersedes_memory_id_"
                "long_term_memories"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_long_term_memories_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_long_term_memories"),
    )
    op.create_index(
        "ix_long_term_memories_user_id",
        "long_term_memories",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_long_term_memories_user_id_status_memory_type",
        "long_term_memories",
        ["user_id", "status", "memory_type"],
        unique=False,
    )
    op.create_index(
        "ix_long_term_memories_user_id_updated_at",
        "long_term_memories",
        ["user_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop all core indexes and tables in reverse dependency order."""

    op.drop_index(
        "ix_long_term_memories_user_id_updated_at",
        table_name="long_term_memories",
    )
    op.drop_index(
        "ix_long_term_memories_user_id_status_memory_type",
        table_name="long_term_memories",
    )
    op.drop_index(
        "ix_long_term_memories_user_id",
        table_name="long_term_memories",
    )
    op.drop_table("long_term_memories")

    op.drop_index(
        "ix_rolling_summary_versions_session_id_summary_version",
        table_name="rolling_summary_versions",
    )
    op.drop_index(
        "ix_rolling_summary_versions_covered_to_message_id",
        table_name="rolling_summary_versions",
    )
    op.drop_table("rolling_summary_versions")

    op.drop_index(
        "uq_intervention_events_session_id_pending",
        table_name="intervention_events",
    )
    op.drop_index(
        "ix_intervention_events_session_id_status_created_at",
        table_name="intervention_events",
    )
    op.drop_table("intervention_events")

    op.drop_index(
        "ix_session_state_versions_session_id_version",
        table_name="session_state_versions",
    )
    op.drop_index(
        "ix_session_state_versions_source_message_id",
        table_name="session_state_versions",
    )
    op.drop_table("session_state_versions")

    op.drop_index(
        "ix_messages_session_id_created_at",
        table_name="messages",
    )
    op.drop_index(
        "ix_messages_session_id",
        table_name="messages",
    )
    op.drop_table("messages")

    op.drop_index(
        "ix_sessions_status_started_at",
        table_name="sessions",
    )
    op.drop_index(
        "ix_sessions_user_id_status",
        table_name="sessions",
    )
    op.drop_index(
        "ix_sessions_user_id",
        table_name="sessions",
    )
    op.drop_table("sessions")

    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
