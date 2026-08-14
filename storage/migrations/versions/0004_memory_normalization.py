"""Add write-side normalization columns to long-term memories.

Revision ID: 0004_memory_normalization
Revises: 0003_deep_state_results
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_memory_normalization"
down_revision: str | Sequence[str] | None = "0003_deep_state_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add normalized content hash, source session, and active dedup index."""

    op.add_column(
        "long_term_memories",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "long_term_memories",
        sa.Column("source_session_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_long_term_memories_active_content_hash",
        "long_term_memories",
        ["user_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    """Remove the normalization columns and the active dedup index."""

    op.drop_index(
        "uq_long_term_memories_active_content_hash",
        table_name="long_term_memories",
    )
    op.drop_column("long_term_memories", "source_session_id")
    op.drop_column("long_term_memories", "content_hash")
