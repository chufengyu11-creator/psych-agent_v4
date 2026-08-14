"""Tests for staging reviewed public knowledge with local embeddings."""

import pytest

from scripts.ingest_knowledge import _stage_text


class _FakeRepository:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def replace_draft(self, **kwargs: object) -> str:
        self.kwargs = kwargs
        return "kdoc_fixture"


class _FakeProvider:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimensions for _ in texts]


@pytest.mark.asyncio
async def test_stage_text_uses_explicit_embedding_dimension() -> None:
    repository = _FakeRepository()

    document_id = await _stage_text(
        repository,  # type: ignore[arg-type]
        _FakeProvider(512),  # type: ignore[arg-type]
        embedding_dimensions=512,
        source_uri="fixture://knowledge",
        title="fixture",
        version="1",
        content="One durable knowledge paragraph.",
        language="en",
    )

    assert document_id == "kdoc_fixture"
    assert repository.kwargs is not None


@pytest.mark.asyncio
async def test_stage_text_rejects_wrong_embedding_dimension() -> None:
    with pytest.raises(ValueError, match="dimension must be 512"):
        await _stage_text(
            _FakeRepository(),  # type: ignore[arg-type]
            _FakeProvider(3),  # type: ignore[arg-type]
            embedding_dimensions=512,
            source_uri="fixture://knowledge",
            title="fixture",
            version="1",
            content="One durable knowledge paragraph.",
            language="en",
        )
