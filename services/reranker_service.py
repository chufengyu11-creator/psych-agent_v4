"""Small deterministic reranker for public knowledge evidence."""

import re

from schemas.knowledge import KnowledgeEvidence


class LexicalReranker:
    """Add bounded query-term coverage to the fused retrieval score."""

    def rerank(self, query: str, candidates: list[KnowledgeEvidence]) -> list[KnowledgeEvidence]:
        terms = _terms(query)
        if not terms:
            return candidates
        ranked = [
            evidence.model_copy(
                update={
                    "retrieval_score": evidence.retrieval_score
                    + sum(term in evidence.content.casefold() for term in terms) / len(terms) / 10
                }
            )
            for evidence in candidates
        ]
        return sorted(ranked, key=lambda item: item.retrieval_score, reverse=True)


def _terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2}", query.casefold())
    return list(dict.fromkeys(words))[:12]


__all__ = ["LexicalReranker"]
