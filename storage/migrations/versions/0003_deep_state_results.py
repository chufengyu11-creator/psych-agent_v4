"""Persist completed deep-state results for later turns.

Revision ID: 0003_deep_state_results
Revises: 0002_user_scoped_sessions
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_deep_state_results"
down_revision: str | Sequence[str] | None = "0002_user_scoped_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the additive deep-state result table and lookup indexes."""

    op.create_table(
        "deep_state_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_pk", sa.Uuid(), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("source_message_sequence", sa.Integer(), nullable=False),
        sa.Column("base_state_version", sa.Integer(), nullable=False),
        sa.Column(
            "pipeline_version",
            sa.String(length=32),
            server_default=sa.text("'v1'"),
            nullable=False,
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'ready'"),
            nullable=False,
        ),
        sa.Column("error_category", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ready', 'applied', 'expired', 'failed')",
            name="ck_deep_state_results_status",
        ),
        sa.CheckConstraint(
            "source_message_sequence >= 1",
            name="ck_deep_state_results_source_message_sequence",
        ),
        sa.CheckConstraint(
            "base_state_version >= 0",
            name="ck_deep_state_results_base_state_version",
        ),
        sa.ForeignKeyConstraint(
            ["session_pk"],
            ["sessions.id"],
            name="fk_deep_state_results_session_pk_sessions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name="fk_deep_state_results_source_message_id_messages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_deep_state_results_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deep_state_results"),
        sa.UniqueConstraint(
            "session_pk",
            "source_message_id",
            "pipeline_version",
            name="uq_deep_state_results_source_pipeline",
        ),
    )
    op.create_index(
        "ix_deep_state_results_source_message_id",
        "deep_state_results",
        ["source_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_deep_state_results_user_id",
        "deep_state_results",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_deep_state_results_session_pk_status_source_message_sequence",
        "deep_state_results",
        ["session_pk", "status", "source_message_sequence"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the additive deep-state storage."""

    op.drop_index(
        "ix_deep_state_results_session_pk_status_source_message_sequence",
        table_name="deep_state_results",
    )
    op.drop_index(
        "ix_deep_state_results_user_id",
        table_name="deep_state_results",
    )
    op.drop_index(
        "ix_deep_state_results_source_message_id",
        table_name="deep_state_results",
    )
    op.drop_table("deep_state_results")
