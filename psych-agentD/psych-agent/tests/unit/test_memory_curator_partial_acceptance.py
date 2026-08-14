"""Partial-acceptance tests for MemoryCurator."""

from collections.abc import Mapping

import pytest

from agents.memory_curator import FakeMemoryCurator, MemoryCurator, MemoryCuratorInput
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.memory import MemoryCandidate
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import SessionFinalizerResult

SESSION_ID = SessionId("memory-curator-partial")


def _message(message_id: str, content: str, sequence: int) -> Message:
    return Message(
        id=MessageId(message_id),
        session_id=SESSION_ID,
        role=MessageRole.USER,
        content=content,
        sequence_number=sequence,
    )


def _payload() -> MemoryCuratorInput:
    return MemoryCuratorInput(
        session_id=SESSION_ID,
        messages=[
            _message("msg_pref", "Please remember: I prefer one step at a time.", 1),
            _message("msg_goal", "I will draft one message to my manager this week.", 2),
            _message("msg_pref_two", "I prefer one step at a time for planning.", 3),
        ],
        final_state=SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="fixture",
        ),
    )


def _candidate(
    *,
    content: str,
    message_id: str,
    quote: str,
    candidate_type: str = "interaction_preference",
    requires_user_confirmation: bool = False,
    operation: str = "CREATE",
) -> dict[str, object]:
    return {
        "candidate_type": candidate_type,
        "content": content,
        "source_type": "explicit_user_statement",
        "confidence": 0.9,
        "sensitivity": "low",
        "requires_user_confirmation": requires_user_confirmation,
        "recommended_operation": operation,
        "evidence": [{"message_id": message_id, "quote": quote}],
        "provisional_candidate_indexes": [],
    }


class RawClient:
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self.candidates = candidates

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
        return output_model.model_validate({"candidates": self.candidates})


class RecordingFallbackMemoryCurator(FakeMemoryCurator):
    def __init__(self) -> None:
        self.curate_calls = 0

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        self.curate_calls += 1
        return await super().curate(payload)


@pytest.mark.asyncio
async def test_one_invalid_candidate_does_not_fallback_or_drop_valid_candidate() -> None:
    fallback = RecordingFallbackMemoryCurator()
    audit = await MemoryCurator(
        RawClient(
            [
                _candidate(
                    content="User preference: one step at a time.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                ),
                _candidate(
                    content="User active goal: draft one message to my manager this week.",
                    message_id="msg_goal",
                    quote="I will draft one message to my manager this week.",
                    candidate_type="active_goal",
                    requires_user_confirmation=True,
                ),
                _candidate(
                    content="User preference: one step at a time.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                    operation="REINFORCE",
                ),
            ]
        ),
        fallback=fallback,
        strict_model=True,
    )._curate_with_audit(_payload())

    assert fallback.curate_calls == 0
    assert len(audit.validation_results) == 3
    assert len(audit.candidates) == 2
    assert audit.validation_results[2].accepted is False
    assert audit.validation_results[2].reason_codes == ("non_create_operation_not_allowed",)


@pytest.mark.asyncio
async def test_all_candidates_rejected_returns_empty_model_success_without_fallback() -> None:
    fallback = RecordingFallbackMemoryCurator()
    audit = await MemoryCurator(
        RawClient(
            [
                _candidate(
                    content="The user has an anxiety disorder.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                )
            ]
        ),
        fallback=fallback,
        strict_model=True,
    )._curate_with_audit(_payload())

    assert fallback.curate_calls == 0
    assert audit.candidates == []
    assert audit.validation_results[0].accepted is False
    assert "unsupported_diagnosis" in audit.validation_results[0].reason_codes


@pytest.mark.asyncio
async def test_duplicate_valid_candidates_merge_sources_in_order() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _candidate(
                    content="User preference: one step at a time.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                ),
                _candidate(
                    content="User preference: one step at a time.",
                    message_id="msg_pref_two",
                    quote="I prefer one step at a time for planning.",
                ),
            ]
        )
    )._curate_with_audit(_payload())

    assert len(audit.candidates) == 1
    assert audit.candidates[0].source_message_ids == [
        MessageId("msg_pref"),
        MessageId("msg_pref_two"),
    ]
