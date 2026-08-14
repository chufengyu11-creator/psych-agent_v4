"""Stage, publish, retire, and expire reviewed public knowledge."""

import argparse
import asyncio
import hashlib
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from services.embedding_service import (
    EmbeddingProvider,
    create_embedding_provider,
)
from storage.database import create_engine, create_session_factory, transactional_session
from storage.repositories.knowledge_repository import SqlAlchemyKnowledgeRepository


async def _stage(args: argparse.Namespace) -> None:
    settings = get_settings()
    os.environ["EMBEDDING_MODEL_ENABLED"] = str(settings.embedding_model_enabled)
    os.environ["EMBEDDING_MODEL_NAME"] = settings.embedding_model_name
    provider = (
        create_embedding_provider(expected_dimensions=settings.rag_embedding_dimensions)
        if settings.embedding_model_enabled
        else None
    )
    if settings.rag_enabled and settings.embedding_model_enabled and provider is None:
        raise RuntimeError(
            "RAG is enabled but the configured local BGE embedding provider is unavailable"
        )
    engine = create_engine(settings)
    try:
        async with transactional_session(create_session_factory(engine)) as session:
            repository = SqlAlchemyKnowledgeRepository(session)
            document_id = await _stage_text(
                repository,
                provider,
                embedding_dimensions=settings.rag_embedding_dimensions,
                source_uri=args.source_uri,
                title=args.title,
                version=args.version,
                content=Path(args.file).read_text(encoding="utf-8"),
                language=args.language,
            )
            if args.publish and (not args.reviewer or not await repository.publish(document_id, args.reviewer)):
                raise RuntimeError("publishing requires a named reviewer and staged chunks")
        print(f"staged document_id={document_id}")
    finally:
        await engine.dispose()


async def _manage(args: argparse.Namespace) -> None:
    engine = create_engine(get_settings())
    try:
        async with transactional_session(create_session_factory(engine)) as session:
            repository = SqlAlchemyKnowledgeRepository(session)
            changed = (
                await repository.retire(args.document_id)
                if args.command == "retire"
                else await repository.expire(
                    args.document_id,
                    datetime.fromisoformat(args.expires_at).astimezone(UTC),
                )
            )
            if not changed:
                raise RuntimeError("document was not found or is already retired")
    finally:
        await engine.dispose()


async def _stage_text(
    repository: SqlAlchemyKnowledgeRepository,
    provider: EmbeddingProvider | None,
    *,
    embedding_dimensions: int,
    source_uri: str,
    title: str,
    version: str,
    content: str,
    language: str,
) -> str:
    normalized = "\n".join(
        line.strip()
        for line in content.replace("\r\n", "\n").splitlines()
        if line.strip()
    )
    if not normalized:
        raise ValueError("knowledge document has no usable content")
    chunks = _chunk(normalized)
    embeddings = await provider.embed_texts(chunks) if provider is not None else [None] * len(chunks)
    if any(
        vector is not None and len(vector) != embedding_dimensions
        for vector in embeddings
    ):
        raise ValueError(
            f"local embedding dimension must be {embedding_dimensions}"
        )
    return await repository.replace_draft(
        source_uri=source_uri,
        title=title,
        version=version,
        content_hash=_sha256(normalized),
        language=language,
        chunks=[
            (item, _sha256(item), _estimate_tokens(item), None, embedding)
            for item, embedding in zip(chunks, embeddings, strict=True)
        ],
    )


def _chunk(content: str, max_chars: int = 1500, overlap_chars: int = 180) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}|\n", content):
        if current and len(current) + len(paragraph) + 1 > max_chars:
            chunks.append(current)
            current = current[-overlap_chars:] + "\n" + paragraph
        else:
            current = f"{current}\n{paragraph}".strip()
    return chunks + ([current] if current else [])


def _estimate_tokens(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("file")
    stage.add_argument("--source-uri", required=True)
    stage.add_argument("--title", required=True)
    stage.add_argument("--version", required=True)
    stage.add_argument("--language", default="zh")
    stage.add_argument("--publish", action="store_true")
    stage.add_argument("--reviewer")
    retire = commands.add_parser("retire")
    retire.add_argument("document_id")
    expire = commands.add_parser("expire")
    expire.add_argument("document_id")
    expire.add_argument("--expires-at", required=True)
    args = parser.parse_args()
    asyncio.run(_stage(args) if args.command == "stage" else _manage(args))


if __name__ == "__main__":
    main()
