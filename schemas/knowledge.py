"""Typed contracts for reviewed public knowledge retrieval."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel


class KnowledgeDocumentStatus(StrEnum):
    """Publication state of a public knowledge document."""

    DRAFT = "draft"
    REVIEWED = "reviewed"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class KnowledgeRetrievalStatus(StrEnum):
    """Non-fatal outcome of one public knowledge lookup."""

    DISABLED = "disabled"
    HIT = "hit"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    FAILED = "failed"


class KnowledgeEvidence(ContractModel):
    """One reviewed public source fragment available to the agents."""

    chunk_id: str
    document_id: str
    title: str
    source_uri: str
    version: str
    content: str
    token_count: int = Field(ge=0)
    retrieval_score: float
    section_path: str | None = None


class RetrievedKnowledge(ContractModel):
    """A bounded public evidence set for one normal-risk turn."""

    evidences: list[KnowledgeEvidence] = Field(default_factory=list)
    retrieval_status: KnowledgeRetrievalStatus = KnowledgeRetrievalStatus.DISABLED


__all__ = [
    "KnowledgeDocumentStatus",
    "KnowledgeEvidence",
    "KnowledgeRetrievalStatus",
    "RetrievedKnowledge",
]
