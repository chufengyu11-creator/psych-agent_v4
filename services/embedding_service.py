"""Zero-model text similarity service for memory write-side judgment.

V1 implements char-bigram TF-IDF cosine similarity in pure Python, so the
write-side deduplication and conflict layers never depend on a model service.
V2 may replace the vector source with a real embedding model while keeping
the same cosine interface.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

_CJK_STOP_CHARS = frozenset(
    "我你他她它们的是了呢吗么啊呀在有过和与及还都很就也把被这那哪什么之前以前上次说提记得"
)
_LATIN_STOP_WORDS = frozenset(
    {
        "about",
        "before",
        "remember",
        "said",
        "that",
        "the",
        "what",
        "when",
        "you",
    }
)
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SimilarityService(Protocol):
    """Cosine interface shared by TF-IDF and future embedding services."""

    def similarity(
        self,
        left: str,
        right: str,
        corpus: Sequence[str] | None = None,
    ) -> float:
        """Return cosine similarity in [0, 1] between two texts.

        Implementations may ignore the optional corpus; TF-IDF uses it for
        inverse-document-frequency weighting, embedding models do not need it.
        """


class EmbeddingProvider(Protocol):
    """Async batch-vector contract used by the public knowledge RAG."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one vector for each input text in the same order."""


class UnavailableEmbeddingProvider:
    """Explicit disabled implementation for lexical-only RAG."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        _ = texts
        raise RuntimeError("local embedding provider is not configured")


class TfidfSimilarityService:
    """Char-bigram TF-IDF cosine similarity without any model dependency."""

    def tokenize(self, text: str) -> Counter[str]:
        """Split text into weighted chars, char-bigrams, and latin tokens.

        Digits participate in the char stream and bigrams so value-bearing
        statements ("18岁" vs "40岁") stay distinguishable instead of
        collapsing into identical vectors. Single CJK chars are also kept:
        short phrases would otherwise become unrecognizable when a one-char
        insertion shifts every bigram boundary.
        """

        normalized = re.sub(r"\s+", " ", text.casefold()).strip()
        cjk_chars = [
            char
            for char in normalized
            if ("一" <= char <= "鿿" or char.isdigit())
            and char not in _CJK_STOP_CHARS
        ]
        tokens: Counter[str] = Counter()
        for char in cjk_chars:
            tokens[char] += 1
        for index in range(max(0, len(cjk_chars) - 1)):
            tokens["".join(cjk_chars[index : index + 2])] += 1
        for token in _LATIN_TOKEN_RE.findall(normalized):
            if (len(token) >= 3 or token.isdigit()) and token not in _LATIN_STOP_WORDS:
                tokens[token] += 1
        return tokens

    def idf(self, corpus: Sequence[str]) -> dict[str, float]:
        """Return smoothed inverse-document-frequency weights for a corpus."""

        document_frequency: Counter[str] = Counter()
        for text in corpus:
            document_frequency.update(set(self.tokenize(text)))
        size = len(corpus)
        if size < 1:
            return {}
        return {
            term: math.log(1.0 + size / (1.0 + count))
            for term, count in document_frequency.items()
        }

    def similarity(
        self,
        left: str,
        right: str,
        corpus: Sequence[str] | None = None,
    ) -> float:
        """Return cosine similarity after optional corpus-based IDF weighting."""

        idf_map = self.idf(corpus) if corpus else None
        left_vector = self._vector(left, idf_map)
        right_vector = self._vector(right, idf_map)
        return _cosine(left_vector, right_vector)

    def _vector(
        self,
        text: str,
        idf_map: dict[str, float] | None,
    ) -> dict[str, float]:
        """Build one term-weight vector, falling back to raw term frequency."""

        counts = self.tokenize(text)
        if not counts:
            return {}
        if idf_map is None:
            return {term: float(count) for term, count in counts.items()}
        return {
            term: float(count) * idf_map.get(term, 1.0)
            for term, count in counts.items()
        }


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Return cosine similarity between two sparse term vectors."""

    if not left or not right:
        return 0.0
    dot = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class SentenceTransformerSimilarityService:
    """Cosine similarity backed by a BGE-family embedding model via transformers.

    Uses CLS-pooling + L2-normalisation (the canonical BGE recipe) and runs
    fully offline once the model directory is populated.  Falls back to CPU
    automatically so it never competes with vLLM for GPU memory.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        device: str = "cpu",
        local_files_only: bool = True,
    ) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer  # noqa: PLC0415

        if local_files_only:
            import os  # noqa: PLC0415
            os.environ.setdefault("HF_OFFLINE", "1")
            os.environ.setdefault("HF_LOCAL_FILES_ONLY", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name, local_files_only=local_files_only,
        )
        self._model = AutoModel.from_pretrained(
            model_name, local_files_only=local_files_only,
        )
        self._model.eval()
        self._model.to(device)

    def similarity(
        self,
        left: str,
        right: str,
        corpus: Sequence[str] | None = None,
    ) -> float:
        """Return cosine similarity between two texts via BGE embeddings."""

        _ = corpus
        import torch
        import torch.nn.functional as F  # noqa: PLC0415

        left_vec, right_vec = self._encode_many([left, right])
        score = float(
            F.cosine_similarity(left_vec, right_vec, dim=0).item()
        )
        return min(1.0, max(0.0, score))

    def embed_texts_sync(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch for ingestion or query-time vector retrieval."""

        return [vector.detach().cpu().tolist() for vector in self._encode_many(texts)]

    def _encode_many(self, texts: list[str]):
        """Return L2-normalised CLS embeddings for a batch."""

        import torch
        import torch.nn.functional as F  # noqa: PLC0415

        if not texts:
            return []
        inputs = self._tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        )
        if self._model.device.type != "cpu":
            inputs = {k: v.to(self._model.device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model(**inputs)
        cls_emb = outputs.last_hidden_state[:, 0]
        return F.normalize(cls_emb, p=2, dim=1)


class LocalBgeEmbeddingProvider:
    """Async adapter that reuses the process-wide local BGE model."""

    def __init__(
        self,
        service: SentenceTransformerSimilarityService,
        *,
        expected_dimensions: int = 512,
    ) -> None:
        self._service = service
        self._expected_dimensions = expected_dimensions

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await asyncio.to_thread(self._service.embed_texts_sync, texts)
        if any(len(vector) != self._expected_dimensions for vector in vectors):
            raise ValueError(
                "local BGE output dimension does not match the configured vector schema"
            )
        return vectors


import os
import threading

_SIMILARITY_SERVICE: SimilarityService | None = None
_SIMILARITY_LOCK = threading.Lock()


def create_similarity_service() -> SimilarityService:
    """Return the appropriate similarity service (loaded once, never reloaded).

    When ``EMBEDDING_MODEL_ENABLED=true`` the BGE-family model is loaded on
    the first call and cached for the lifetime of the process.  Subsequent
    calls return the same instance — no per-request weight loading.
    """

    global _SIMILARITY_SERVICE  # noqa: PLW0603
    if _SIMILARITY_SERVICE is not None:
        return _SIMILARITY_SERVICE

    import logging  # noqa: PLC0415
    _LOGGER = logging.getLogger(__name__)

    with _SIMILARITY_LOCK:
        if _SIMILARITY_SERVICE is not None:
            return _SIMILARITY_SERVICE

        if os.environ.get("EMBEDDING_MODEL_ENABLED", "").strip().lower() != "true":
            _LOGGER.info("Similarity service: TF-IDF (default)")
            _SIMILARITY_SERVICE = TfidfSimilarityService()
            return _SIMILARITY_SERVICE
        try:
            service = SentenceTransformerSimilarityService(
                os.environ.get(
                    "EMBEDDING_MODEL_NAME",
                    "BAAI/bge-small-zh-v1.5",
                ),
                local_files_only=True,
            )
            _ = service.similarity("warmup", "warmup")
            _LOGGER.info("Similarity service: BGE embedding model loaded (once)")
            _SIMILARITY_SERVICE = service
            return service
        except Exception:
            _LOGGER.warning(
                "Embedding model failed to load — falling back to TF-IDF",
                exc_info=True,
            )
            _SIMILARITY_SERVICE = TfidfSimilarityService()
            return _SIMILARITY_SERVICE


def create_embedding_provider(*, expected_dimensions: int = 512) -> EmbeddingProvider | None:
    """Return a local BGE provider when the shared similarity service has BGE."""

    service = create_similarity_service()
    if isinstance(service, SentenceTransformerSimilarityService):
        return LocalBgeEmbeddingProvider(
            service,
            expected_dimensions=expected_dimensions,
        )
    return None


__all__ = [
    "EmbeddingProvider",
    "LocalBgeEmbeddingProvider",
    "SentenceTransformerSimilarityService",
    "SimilarityService",
    "TfidfSimilarityService",
    "UnavailableEmbeddingProvider",
    "create_embedding_provider",
    "create_similarity_service",
]
