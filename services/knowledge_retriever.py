"""Fail-open retrieval of reviewed public knowledge."""

import asyncio
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from schemas.knowledge import KnowledgeRetrievalStatus, RetrievedKnowledge
from services.embedding_service import EmbeddingProvider
from services.reranker_service import LexicalReranker
from storage.repositories.knowledge_repository import KnowledgeRepository, SqlAlchemyKnowledgeRepository


class KnowledgeRetrieverProtocol(Protocol):
    async def retrieve(self, query: str) -> RetrievedKnowledge: ...


class DisabledKnowledgeRetriever:
    async def retrieve(self, query: str) -> RetrievedKnowledge:
        _ = query
        return RetrievedKnowledge(retrieval_status=KnowledgeRetrievalStatus.DISABLED)


class RepositoryKnowledgeRetriever:
    """Run lexical and optional semantic search under one bounded timeout."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        top_k: int,
        timeout_ms: int,
        embedding_timeout_ms: int = 1200,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: LexicalReranker | None = None,
        rerank_enabled: bool = False,
    ) -> None:
        self._repository = repository
        self._top_k = top_k
        self._timeout_seconds = timeout_ms / 1000
        self._embedding_timeout_seconds = embedding_timeout_ms / 1000
        self._embedding_provider = embedding_provider
        self._reranker = reranker or LexicalReranker()
        self._rerank_enabled = rerank_enabled

    async def retrieve(self, query: str) -> RetrievedKnowledge:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                # A repository may be backed by one AsyncSession. SQLAlchemy
                # sessions do not support concurrent statements, so keep both
                # searches inside the same bounded operation but execute them
                # in order.
                lexical = await self._repository.search_active(query, self._top_k * 4)
                semantic = await self._semantic_candidates(query)
        except TimeoutError:
            return RetrievedKnowledge(retrieval_status=KnowledgeRetrievalStatus.TIMEOUT)
        except Exception:
            return RetrievedKnowledge(retrieval_status=KnowledgeRetrievalStatus.FAILED)
        evidences = _rrf(lexical, semantic)
        if self._rerank_enabled:
            evidences = self._reranker.rerank(query, evidences)
        return RetrievedKnowledge(
            evidences=evidences[: self._top_k],
            retrieval_status=(
                KnowledgeRetrievalStatus.HIT
                if lexical or semantic
                else KnowledgeRetrievalStatus.EMPTY
            ),
        )

    async def _semantic_candidates(self, query: str) -> list:
        if self._embedding_provider is None:
            return []
        try:
            async with asyncio.timeout(self._embedding_timeout_seconds):
                vectors = await self._embedding_provider.embed_texts([query])
            return await self._repository.search_active_semantic(vectors[0], self._top_k * 4)
        except Exception:
            return []


class SessionFactoryKnowledgeRetriever:
    """Open a short-lived read session for every public knowledge query."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        top_k: int,
        timeout_ms: int,
        embedding_timeout_ms: int = 1200,
        embedding_provider: EmbeddingProvider | None = None,
        rerank_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._top_k = top_k
        self._timeout_ms = timeout_ms
        self._embedding_timeout_ms = embedding_timeout_ms
        self._embedding_provider = embedding_provider
        self._rerank_enabled = rerank_enabled

    async def retrieve(self, query: str) -> RetrievedKnowledge:
        async with self._session_factory() as session:
            return await RepositoryKnowledgeRetriever(
                SqlAlchemyKnowledgeRepository(session),
                top_k=self._top_k,
                timeout_ms=self._timeout_ms,
                embedding_timeout_ms=self._embedding_timeout_ms,
                embedding_provider=self._embedding_provider,
                rerank_enabled=self._rerank_enabled,
            ).retrieve(query)


def _rrf(lexical: list, semantic: list, k: int = 60) -> list:
    merged: dict[str, tuple[object, float]] = {}
    for candidates in (lexical, semantic):
        for rank, evidence in enumerate(candidates, start=1):
            previous = merged.get(evidence.chunk_id)
            score = (previous[1] if previous else 0.0) + 1 / (k + rank)
            merged[evidence.chunk_id] = (evidence, score)
    return [
        evidence.model_copy(update={"retrieval_score": score})
        for evidence, score in sorted(merged.values(), key=lambda item: item[1], reverse=True)
    ]


__all__ = [
    "DisabledKnowledgeRetriever",
    "KnowledgeRetrieverProtocol",
    "RepositoryKnowledgeRetriever",
    "SessionFactoryKnowledgeRetriever",
]
