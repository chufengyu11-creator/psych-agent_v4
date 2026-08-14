"""Create reviewed public knowledge storage for RAG.

Revision ID: 0005_knowledge_rag
Revises: 0004_memory_normalization
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0005_knowledge_rag"
down_revision: str | Sequence[str] | None = "0004_memory_normalization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The vector type must exist before the table is created.  Keeping the
    # column typed correctly in this revision also makes fresh installs match
    # the ORM model without a JSONB-to-vector conversion step.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=16), server_default=sa.text("'zh'"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'reviewed', 'active', 'retired', 'failed')", name="ck_knowledge_documents_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_uri", "version"),
        sa.UniqueConstraint("content_hash"),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.String(length=512), nullable=True),
        sa.Column("embedding", Vector(512), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("token_count >= 0", name="ck_knowledge_chunks_token_count"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index"),
    )
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"], unique=False)
    op.create_index("ix_knowledge_chunks_document_id_chunk_index", "knowledge_chunks", ["document_id", "chunk_index"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_document_id_chunk_index", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_documents_status", table_name="knowledge_documents")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
