"""Protocol-level tests for the in-memory memory repository boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.common import MessageId, UserId
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryPolicyDecision,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from storage.repositories.memory_repository import InMemoryMemoryRepository


def _candidate(operation: MemoryOperation = MemoryOperation.CREATE) -> MemoryCandidate:
    """Build one artificial candidate for protocol-level tests."""

    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="Original artificial preference.",
        source_message_ids=[MessageId("artificial-source")],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=operation,
    )


async def test_in_memory_repository_keeps_owner_and_sanitized_boundaries() -> None:
    """The fake implementation should match durable ownership/content semantics."""

    repository = InMemoryMemoryRepository()
    owner_id = UserId("memory-fake-owner")
    other_id = UserId("memory-fake-other")
    created = await repository.create(
        owner_id,
        _candidate(),
        MemoryPolicyDecision(
            allowed=True,
            operation=MemoryOperation.CREATE,
            sanitized_content="Sanitized artificial preference.",
            reason_codes=["repository_test"],
        ),
    )
    assert created.memory_id is not None

    cross_user = await repository.create(
        other_id,
        _candidate(MemoryOperation.REINFORCE),
        MemoryPolicyDecision(
            allowed=True,
            operation=MemoryOperation.REINFORCE,
            target_memory_id=created.memory_id,
            sanitized_content="Foreign replacement content.",
            reason_codes=["repository_test"],
        ),
    )

    owner_memories = await repository.list_active(owner_id)
    assert cross_user.applied is False
    assert cross_user.memory_id is None
    assert await repository.list_active(other_id) == []
    assert len(owner_memories) == 1
    assert owner_memories[0].content == "Sanitized artificial preference."


def test_unknown_memory_operation_is_rejected_by_public_contract() -> None:
    """An unknown operation cannot silently reach Repository as CREATE."""

    with pytest.raises(ValidationError):
        MemoryPolicyDecision.model_validate(
            {
                "allowed": True,
                "operation": "UNKNOWN",
                "reason_codes": ["repository_test"],
            }
        )
