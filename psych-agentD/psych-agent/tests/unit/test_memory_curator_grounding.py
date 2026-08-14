"""Grounding and candidate-local rejection tests for MemoryCurator."""

from collections.abc import Mapping

import pytest

from agents.memory_curator import MemoryCurator, MemoryCuratorInput
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.memory import MemoryType
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import SessionFinalizerResult

SESSION_ID = SessionId("memory-curator-grounding")


def _message(
    message_id: str,
    role: MessageRole,
    content: str,
    sequence: int,
) -> Message:
    return Message(
        id=MessageId(message_id),
        session_id=SESSION_ID,
        role=role,
        content=content,
        sequence_number=sequence,
    )


def _payload() -> MemoryCuratorInput:
    return MemoryCuratorInput(
        session_id=SESSION_ID,
        messages=[
            _message(
                "msg_user",
                MessageRole.USER,
                "Please remember that I prefer one small step at a time.",
                1,
            ),
            _message("msg_assistant", MessageRole.ASSISTANT, "I can offer one step.", 2),
        ],
        final_state=SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="fixture",
        ),
    )


def _raw(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "candidate_type": "interaction_preference",
        "content": "User preference: one small step at a time.",
        "source_type": "explicit_user_statement",
        "confidence": 0.92,
        "sensitivity": "low",
        "requires_user_confirmation": False,
        "recommended_operation": "CREATE",
        "evidence": [
            {
                "message_id": "msg_user",
                "quote": "I prefer one small step at a time.",
            }
        ],
        "provisional_candidate_indexes": [],
    }
    data.update(updates)
    return data


class RawClient:
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self.candidates = candidates
        self.output_model_name = ""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = prompt, model_name
        assert metadata == {"agent": "memory_curator"}
        self.output_model_name = output_model.__name__
        return output_model.model_validate({"candidates": self.candidates})


@pytest.mark.asyncio
async def test_private_raw_draft_does_not_include_public_source_ids() -> None:
    client = RawClient([_raw()])

    result = await MemoryCurator(client).curate(_payload())

    assert client.output_model_name == "_RawMemoryCandidateBatch"
    assert result[0].candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert result[0].source_message_ids == [MessageId("msg_user")]


@pytest.mark.asyncio
async def test_unknown_source_id_rejects_only_candidate() -> None:
    audit = await MemoryCurator(
        RawClient([_raw(evidence=[{"message_id": "missing", "quote": "x"}])])
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert audit.validation_results[0].reason_codes == (
        "unknown_source_message_id",
        "assistant_only_explicit_memory",
        "candidate_content_not_supported_by_evidence",
    )


@pytest.mark.asyncio
async def test_quote_must_exist_in_the_cited_message() -> None:
    audit = await MemoryCurator(
        RawClient([_raw(evidence=[{"message_id": "msg_user", "quote": "not present"}])])
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert "evidence_quote_not_found" in audit.validation_results[0].reason_codes


@pytest.mark.asyncio
async def test_assistant_only_explicit_memory_is_rejected() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    evidence=[
                        {"message_id": "msg_assistant", "quote": "I can offer one step."}
                    ]
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert "assistant_only_explicit_memory" in audit.validation_results[0].reason_codes


@pytest.mark.asyncio
async def test_content_must_be_supported_by_evidence() -> None:
    audit = await MemoryCurator(
        RawClient([_raw(content="User preference: always use complete plans.")])
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert "candidate_content_not_supported_by_evidence" in audit.validation_results[0].reason_codes
