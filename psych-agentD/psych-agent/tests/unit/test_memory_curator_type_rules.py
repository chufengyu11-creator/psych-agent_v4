"""Type-specific candidate rules for MemoryCurator."""

from collections.abc import Mapping

import pytest

from agents.memory_curator import MemoryCurator, MemoryCuratorInput
from llm.structured_output import ModelT
from schemas.common import InterventionId, MessageId, SessionId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import SessionFinalizerResult, StrategyOutcome

SESSION_ID = SessionId("memory-curator-type-rules")


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
            _message("msg_pref", MessageRole.USER, "I prefer one step at a time.", 1),
            _message("msg_goal", MessageRole.USER, "I will draft a manager message.", 2),
            _message("msg_topic", MessageRole.USER, "Next time I want to continue work stress.", 3),
            _message("msg_assistant", MessageRole.ASSISTANT, "Let us try one step.", 4),
            _message("msg_feedback", MessageRole.USER, "That helped a little, mixed fit.", 5),
            _message("msg_sensitive", MessageRole.USER, "I want to talk about trauma history.", 6),
        ],
        final_state=SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="fixture",
            strategy_outcomes=[
                StrategyOutcome(
                    strategy="small_step_support",
                    outcome="mixed fit",
                    source_message_ids=[MessageId("msg_feedback")],
                )
            ],
        ),
        interventions=[
            InterventionRecord(
                intervention_id=InterventionId("int_001"),
                session_id=SESSION_ID,
                assistant_message_id=MessageId("msg_assistant"),
                strategy="small_step_support",
                objective="offer one step",
                status=InterventionStatus.EVALUATED,
                observed_response="mixed fit",
                explicit_feedback="mixed",
                strategy_fit="mixed",
                objective_progress="partial",
            )
        ],
    )


def _raw(
    *,
    candidate_type: str,
    content: str,
    message_id: str,
    quote: str,
    source_type: str = "explicit_user_statement",
    sensitivity: str = "low",
    confirmation: bool = False,
) -> dict[str, object]:
    return {
        "candidate_type": candidate_type,
        "content": content,
        "source_type": source_type,
        "confidence": 0.9,
        "sensitivity": sensitivity,
        "requires_user_confirmation": confirmation,
        "recommended_operation": "CREATE",
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


@pytest.mark.asyncio
async def test_accepts_valid_active_goal_and_requires_confirmation() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="active_goal",
                    content="User active goal: draft a manager message.",
                    message_id="msg_goal",
                    quote="I will draft a manager message.",
                    confirmation=True,
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert len(audit.candidates) == 1
    assert audit.candidates[0].requires_user_confirmation is True


@pytest.mark.asyncio
async def test_active_goal_without_confirmation_is_rejected() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="active_goal",
                    content="User active goal: draft a manager message.",
                    message_id="msg_goal",
                    quote="I will draft a manager message.",
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert "confirmation_required" in audit.validation_results[0].reason_codes


@pytest.mark.asyncio
async def test_accepts_unfinished_topic_with_continue_semantics() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="unfinished_topic",
                    content="Unfinished topic: continue work stress next time.",
                    message_id="msg_topic",
                    quote="Next time I want to continue work stress.",
                    confirmation=True,
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert len(audit.candidates) == 1


@pytest.mark.asyncio
async def test_accepts_strategy_outcome_only_with_evaluated_support() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="strategy_outcome",
                    content="Strategy outcome: small_step_support had mixed fit.",
                    message_id="msg_feedback",
                    quote="That helped a little, mixed fit.",
                    source_type="session_summary",
                    confirmation=True,
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert len(audit.candidates) == 1


@pytest.mark.asyncio
async def test_rejects_repeated_observation_and_model_inference_source_types() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="interaction_preference",
                    content="User preference: one step at a time.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                    source_type="repeated_observation",
                ),
                _raw(
                    candidate_type="semantic_memory",
                    content="I prefer one step at a time.",
                    message_id="msg_pref",
                    quote="I prefer one step at a time.",
                    source_type="model_inference",
                ),
            ]
        )
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert (
        "single_session_repeated_observation_not_allowed"
        in audit.validation_results[0].reason_codes
    )
    assert "model_inference_not_allowed" in audit.validation_results[1].reason_codes


@pytest.mark.asyncio
async def test_high_sensitivity_requires_confirmation() -> None:
    audit = await MemoryCurator(
        RawClient(
            [
                _raw(
                    candidate_type="episodic_memory",
                    content="I want to talk about trauma history.",
                    message_id="msg_sensitive",
                    quote="I want to talk about trauma history.",
                    sensitivity="high",
                )
            ]
        )
    )._curate_with_audit(_payload())

    assert audit.candidates == []
    assert "high_sensitivity_requires_confirmation" in audit.validation_results[0].reason_codes
