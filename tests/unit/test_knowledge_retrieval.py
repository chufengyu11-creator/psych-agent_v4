"""Focused tests for the local BGE adapter and public RAG retrieval."""

import asyncio

import pytest

from schemas.knowledge import KnowledgeEvidence, KnowledgeRetrievalStatus
from services.embedding_service import LocalBgeEmbeddingProvider
from services.knowledge_retriever import RepositoryKnowledgeRetriever


def _evidence(chunk_id: str, content: str) -> KnowledgeEvidence:
    return KnowledgeEvidence(
        chunk_id=chunk_id,
        document_id="doc-1",
        title="fixture",
        source_uri="fixture://knowledge",
        version="1",
        content=content,
        token_count=4,
        retrieval_score=1.0,
    )


class _FakeEmbeddingService:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def embed_texts_sync(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(index) for index in range(self.dimensions)] for _ in texts]


@pytest.mark.asyncio
async def test_local_bge_provider_returns_expected_batch_and_rejects_wrong_dimension() -> None:
    service = _FakeEmbeddingService(512)
    provider = LocalBgeEmbeddingProvider(service)  # type: ignore[arg-type]

    vectors = await provider.embed_texts(["a", "b"])

    assert len(vectors) == 2
    assert all(len(vector) == 512 for vector in vectors)
    assert service.calls == [["a", "b"]]

    wrong_provider = LocalBgeEmbeddingProvider(
        _FakeEmbeddingService(3),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="dimension"):
        await wrong_provider.embed_texts(["a"])


class _FakeKnowledgeRepository:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def search_active(self, query: str, limit: int) -> list[KnowledgeEvidence]:
        self.events.append("lexical:start")
        await asyncio.sleep(0)
        self.events.append("lexical:end")
        return [_evidence("lexical", query)]

    async def search_active_semantic(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[KnowledgeEvidence]:
        _ = (embedding, limit)
        assert self.events == ["lexical:start", "lexical:end"]
        self.events.append("semantic")
        return [_evidence("semantic", "related evidence")]


class _FakeProvider:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_retriever_runs_one_session_search_sequentially_and_fuses_hits() -> None:
    repository = _FakeKnowledgeRepository()
    retriever = RepositoryKnowledgeRetriever(
        repository,
        top_k=2,
        timeout_ms=500,
        embedding_provider=_FakeProvider(),
    )

    result = await retriever.retrieve("query")

    assert result.retrieval_status is KnowledgeRetrievalStatus.HIT
    assert {item.chunk_id for item in result.evidences} == {"lexical", "semantic"}
    assert repository.events == ["lexical:start", "lexical:end", "semantic"]


class _FailingProvider:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        _ = texts
        raise RuntimeError("model unavailable")


@pytest.mark.asyncio
async def test_semantic_failure_keeps_lexical_knowledge_available() -> None:
    repository = _FakeKnowledgeRepository()
    retriever = RepositoryKnowledgeRetriever(
        repository,
        top_k=2,
        timeout_ms=500,
        embedding_provider=_FailingProvider(),
    )

    result = await retriever.retrieve("query")

    assert result.retrieval_status is KnowledgeRetrievalStatus.HIT
    assert [item.chunk_id for item in result.evidences] == ["lexical"]
