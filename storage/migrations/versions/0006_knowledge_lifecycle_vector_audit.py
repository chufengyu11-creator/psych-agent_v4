"""Add knowledge lifecycle, 512-dimension vector search, and usage audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_knowledge_lifecycle"
down_revision: str | Sequence[str] | None = "0005_knowledge_rag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("supersedes_document_id", sa.String(length=64), nullable=True))
    op.add_column("knowledge_documents", sa.Column("replaced_by_document_id", sa.String(length=64), nullable=True))
    op.add_column("knowledge_documents", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("knowledge_documents", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_knowledge_documents_supersedes", "knowledge_documents", "knowledge_documents", ["supersedes_document_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_knowledge_documents_replaced_by", "knowledge_documents", "knowledge_documents", ["replaced_by_document_id"], ["id"], ondelete="RESTRICT")
    op.execute("CREATE UNIQUE INDEX uq_knowledge_documents_one_active_source ON knowledge_documents (source_uri) WHERE status = 'active'")
    op.execute("CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL")
    op.create_table(
        "knowledge_usage_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("retrieval_score", sa.Float(), nullable=False),
        sa.Column("used_in_response", sa.Boolean(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["chunk_id"], ["knowledge_chunks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assistant_message_id", "chunk_id"),
    )
    op.create_index("ix_knowledge_usage_events_assistant_message_id", "knowledge_usage_events", ["assistant_message_id"], unique=False)
    op.create_index("ix_knowledge_usage_events_chunk_id", "knowledge_usage_events", ["chunk_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_usage_events_chunk_id", table_name="knowledge_usage_events")
    op.drop_index("ix_knowledge_usage_events_assistant_message_id", table_name="knowledge_usage_events")
    op.drop_table("knowledge_usage_events")
    op.execute("DROP INDEX IF EXISTS uq_knowledge_documents_one_active_source")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
    op.drop_constraint("fk_knowledge_documents_replaced_by", "knowledge_documents", type_="foreignkey")
    op.drop_constraint("fk_knowledge_documents_supersedes", "knowledge_documents", type_="foreignkey")
    op.drop_column("knowledge_documents", "expires_at")
    op.drop_column("knowledge_documents", "retired_at")
    op.drop_column("knowledge_documents", "replaced_by_document_id")
    op.drop_column("knowledge_documents", "supersedes_document_id")
