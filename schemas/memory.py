"""Memory retrieval, curation, and policy contracts."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel, MemoryId, MessageId, SessionId, SourceReference, UserId
from schemas.state import SessionState


class MemoryOperation(StrEnum):
    """Allowed operations proposed by MemoryCurator and approved by MemoryPolicy."""

    CREATE = "CREATE"
    REINFORCE = "REINFORCE"
    MERGE = "MERGE"
    SUPERSEDE = "SUPERSEDE"
    MARK_CONFLICT = "MARK_CONFLICT"
    EXPIRE = "EXPIRE"
    DELETE = "DELETE"


class MemoryType(StrEnum):
    """First-version categories for long-term memory."""

    INTERACTION_PREFERENCE = "interaction_preference"
    ACTIVE_GOAL = "active_goal"
    UNFINISHED_TOPIC = "unfinished_topic"
    SEMANTIC = "semantic_memory"
    EPISODIC = "episodic_memory"
    STRATEGY_OUTCOME = "strategy_outcome"


class MemorySensitivity(StrEnum):
    """Sensitivity level used by MemoryPolicy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MemorySourceType(StrEnum):
    """Where a memory candidate came from."""

    EXPLICIT_USER_STATEMENT = "explicit_user_statement"
    SESSION_SUMMARY = "session_summary"
    REPEATED_OBSERVATION = "repeated_observation"
    MODEL_INFERENCE = "model_inference"


class MemoryQueryIntent(StrEnum):
    """Deterministic intent for user questions about durable memory."""

    NONE = "none"
    EXISTENCE = "existence"
    LIST = "list"
    RECALL = "recall"


class LongTermMemory(ContractModel):
    """A user-authorized memory available for retrieval."""

    id: MemoryId
    memory_type: MemoryType
    content: str
    sensitivity: MemorySensitivity = MemorySensitivity.LOW
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    source: list[SourceReference] = Field(default_factory=list)


class RetrievedMemories(ContractModel):
    """Relevant cross-session context returned by MemoryRetriever."""

    active_memories: list[LongTermMemory] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )
    semantic_memories: list[LongTermMemory] = Field(default_factory=list)
    episodic_memories: list[LongTermMemory] = Field(default_factory=list)
    active_goal_memories: list[LongTermMemory] = Field(default_factory=list)
    interaction_preference_memories: list[LongTermMemory] = Field(default_factory=list)
    pending_confirmation_memories: list[LongTermMemory] = Field(default_factory=list)
    active_goals: list[str] = Field(default_factory=list)
    interaction_preferences: list[str] = Field(default_factory=list)
    pending_confirmation_count: int = Field(default=0, ge=0)
    previous_session_summary: str | None = None


class MemoryRetrieverInput(ContractModel):
    """Input consumed by MemoryRetriever."""

    user_id: UserId
    query: str
    session_state: SessionState
    limit: int = Field(default=8, ge=1)


class MemoryCandidate(ContractModel):
    """A proposed memory operation that still needs policy review."""

    candidate_type: MemoryType
    content: str
    source_message_ids: list[MessageId]
    source_type: MemorySourceType
    confidence: float = Field(ge=0.0, le=1.0)
    requires_user_confirmation: bool
    sensitivity: MemorySensitivity
    recommended_operation: MemoryOperation


class MemoryPolicyInput(ContractModel):
    """Input consumed by MemoryPolicy before any durable memory write."""

    candidate: MemoryCandidate
    existing_memories: list[LongTermMemory] = Field(default_factory=list)
    user_memory_enabled: bool
    session_id: SessionId | None = None


class MemoryPolicyDecision(ContractModel):
    """Decision produced by MemoryPolicy for one candidate memory."""

    allowed: bool
    operation: MemoryOperation | None = None
    reason_codes: list[str] = Field(default_factory=list)
    requires_user_confirmation: bool = False
    target_memory_id: MemoryId | None = None
    sanitized_content: str | None = None


class MemoryConflict(ContractModel):
    """Represents a detected conflict between a candidate and existing memory."""

    candidate: MemoryCandidate
    existing_memory_id: MemoryId
    reason: str
    recommended_operation: MemoryOperation = MemoryOperation.MARK_CONFLICT


class MemoryWriteResult(ContractModel):
    """Result returned after an approved memory operation is persisted."""

    memory_id: MemoryId | None = None
    operation: MemoryOperation
    applied: bool
    reason_codes: list[str] = Field(default_factory=list)


class PendingMemoryResolution(ContractModel):
    """Result of a user's atomic decision on one pending memory write."""

    memory_id: MemoryId
    confirmed: bool
    status: str
    superseded_memory_id: MemoryId | None = None
