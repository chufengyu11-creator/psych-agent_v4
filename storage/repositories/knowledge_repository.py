"""Persistence boundary for reviewed public knowledge and usage audit."""

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

import re
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.knowledge import KnowledgeEvidence, RetrievedKnowledge
from storage.models.knowledge import KnowledgeChunkModel, KnowledgeDocumentModel, KnowledgeUsageEventModel


class KnowledgeRepository(Protocol):
    async def search_active(self, query: str, limit: int) -> list[KnowledgeEvidence]: ...
    async def search_active_semantic(self, embedding: list[float], limit: int) -> list[KnowledgeEvidence]: ...


class KnowledgeUsageRepository(Protocol):
    async def record(
        self,
        assistant_message_id: str,
        knowledge: RetrievedKnowledge,
        referenced_chunk_ids: list[str],
        trace_id: str | None,
    ) -> None: ...


class NoopKnowledgeUsageRepository:
    async def record(
        self,
        assistant_message_id: str,
        knowledge: RetrievedKnowledge,
        referenced_chunk_ids: list[str],
        trace_id: str | None,
    ) -> None:
        _ = (assistant_message_id, knowledge, referenced_chunk_ids, trace_id)


class SqlAlchemyKnowledgeRepository:
    """Database implementation that never receives user identifiers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_active(self, query: str, limit: int) -> list[KnowledgeEvidence]:
        if limit < 1 or not query.strip():
            return []
        terms = _query_terms(query)
        if not terms:
            return []
        statement = (
            select(KnowledgeChunkModel, KnowledgeDocumentModel)
            .join(KnowledgeDocumentModel, KnowledgeChunkModel.document_id == KnowledgeDocumentModel.id)
            .where(
                KnowledgeDocumentModel.status == "active",
                or_(KnowledgeDocumentModel.expires_at.is_(None), KnowledgeDocumentModel.expires_at > datetime.now(UTC)),
                or_(*(KnowledgeChunkModel.content.ilike(f"%{term}%") for term in terms)),
            )
            .order_by(desc(KnowledgeDocumentModel.updated_at), KnowledgeChunkModel.chunk_index)
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return [self._to_evidence(chunk, document, 1.0) for chunk, document in rows]

    async def search_active_semantic(self, embedding: list[float], limit: int) -> list[KnowledgeEvidence]:
        if limit < 1:
            return []
        distance = KnowledgeChunkModel.embedding.cosine_distance(embedding)
        statement = (
            select(KnowledgeChunkModel, KnowledgeDocumentModel, distance.label("distance"))
            .join(KnowledgeDocumentModel, KnowledgeChunkModel.document_id == KnowledgeDocumentModel.id)
            .where(
                KnowledgeDocumentModel.status == "active",
                KnowledgeChunkModel.embedding.is_not(None),
                or_(KnowledgeDocumentModel.expires_at.is_(None), KnowledgeDocumentModel.expires_at > datetime.now(UTC)),
            )
            .order_by(distance)
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            self._to_evidence(chunk, document, 1.0 - float(distance_value))
            for chunk, document, distance_value in rows
        ]

    async def replace_draft(
        self,
        *,
        source_uri: str,
        title: str,
        version: str,
        content_hash: str,
        language: str,
        chunks: list[tuple[str, str, int, str | None, list[float] | None]],
    ) -> str:
        statement = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.source_uri == source_uri,
            KnowledgeDocumentModel.version == version,
        )
        document = (await self._session.execute(statement)).scalar_one_or_none()
        if document is None:
            document = KnowledgeDocumentModel(
                id=f"kdoc_{uuid4().hex}",
                source_uri=source_uri,
                title=title,
                version=version,
                content_hash=content_hash,
                language=language,
                status="draft",
            )
            self._session.add(document)
            await self._session.flush()
        else:
            document.title = title
            document.content_hash = content_hash
            document.language = language
            document.status = "draft"
            document.reviewed_by = None
            document.reviewed_at = None
            existing = await self._session.execute(
                select(KnowledgeChunkModel).where(KnowledgeChunkModel.document_id == document.id)
            )
            for row in existing.scalars():
                await self._session.delete(row)
            await self._session.flush()
        for index, (content, chunk_hash, token_count, section_path, embedding) in enumerate(chunks):
            self._session.add(
                KnowledgeChunkModel(
                    id=f"kchunk_{uuid4().hex}",
                    document_id=document.id,
                    chunk_index=index,
                    content=content,
                    content_hash=chunk_hash,
                    token_count=token_count,
                    section_path=section_path,
                    embedding=embedding,
                    metadata_json={},
                )
            )
        await self._session.flush()
        return document.id

    async def publish(self, document_id: str, reviewer: str) -> bool:
        document = await self._session.get(KnowledgeDocumentModel, document_id)
        if document is None or document.status != "draft" or not reviewer.strip():
            return False
        chunk_id = await self._session.scalar(
            select(KnowledgeChunkModel.id).where(KnowledgeChunkModel.document_id == document_id).limit(1)
        )
        if chunk_id is None:
            return False
        previous = (
            await self._session.execute(
                select(KnowledgeDocumentModel).where(
                    KnowledgeDocumentModel.source_uri == document.source_uri,
                    KnowledgeDocumentModel.status == "active",
                    KnowledgeDocumentModel.id != document.id,
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            previous.status = "retired"
            previous.retired_at = datetime.now(UTC)
            previous.replaced_by_document_id = document.id
            document.supersedes_document_id = previous.id
        document.status = "active"
        document.reviewed_by = reviewer.strip()
        document.reviewed_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def retire(self, document_id: str, *, reason: str = "manual") -> bool:
        _ = reason
        document = await self._session.get(KnowledgeDocumentModel, document_id)
        if document is None or document.status in {"retired", "failed"}:
            return False
        document.status = "retired"
        document.retired_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def expire(self, document_id: str, expires_at: datetime) -> bool:
        document = await self._session.get(KnowledgeDocumentModel, document_id)
        if document is None:
            return False
        document.expires_at = expires_at
        await self._session.flush()
        return True

    async def record(
        self,
        assistant_message_id: str,
        knowledge: RetrievedKnowledge,
        referenced_chunk_ids: list[str],
        trace_id: str | None,
    ) -> None:
        # Keep an audit failure from poisoning the turn transaction. The
        # response is already valid when usage logging is best-effort.
        async with self._session.begin_nested():
            referenced = set(referenced_chunk_ids)
            for evidence in knowledge.evidences:
                self._session.add(
                    KnowledgeUsageEventModel(
                        id=f"kuse_{uuid4().hex}",
                        assistant_message_id=assistant_message_id,
                        chunk_id=evidence.chunk_id,
                        document_id=evidence.document_id,
                        retrieval_score=evidence.retrieval_score,
                        used_in_response=evidence.chunk_id in referenced,
                        trace_id=trace_id,
                    )
                )
            await self._session.flush()

    def _to_evidence(
        self,
        chunk: KnowledgeChunkModel,
        document: KnowledgeDocumentModel,
        score: float,
    ) -> KnowledgeEvidence:
        return KnowledgeEvidence(
            chunk_id=chunk.id,
            document_id=document.id,
            title=document.title,
            source_uri=document.source_uri,
            version=document.version,
            content=chunk.content,
            token_count=chunk.token_count,
            retrieval_score=score,
            section_path=chunk.section_path,
        )


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2}", query.casefold())
    return list(dict.fromkeys(terms))[:12]


__all__ = [
    "KnowledgeRepository",
    "KnowledgeUsageRepository",
    "NoopKnowledgeUsageRepository",
    "SqlAlchemyKnowledgeRepository",
]
