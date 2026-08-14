"""Strict contract tests for fake and model-backed session finalizers."""

from collections.abc import Mapping

import pytest

from agents.session_finalizer import FakeSessionFinalizer, SessionFinalizer
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.intervention import InterventionRecord
from schemas.memory import MemorySourceType, MemoryType
from schemas.messages import MessageRole
from schemas.risk import RiskLevel
from schemas.state import RiskState
from schemas.summary import SessionFinalizerInput, SessionFinalizerResult
from tests.fixtures.interventions import (
    evaluated_poor_fit_intervention,
    pending_reflective_intervention,
)
from tests.fixtures.messages import make_message, work_stress_dialogue
from tests.fixtures.states import empty_state


def _payload(
    *,
    interventions: list[InterventionRecord] | None = None,
) -> SessionFinalizerInput:
    messages = work_stress_dialogue()
    return SessionFinalizerInput(
        session_id=messages[0].session_id,
        messages=messages,
        final_state=empty_state(messages[0].session_id),
        interventions=interventions or [],
    )


def _valid_model_data() -> dict[str, object]:
    return {
        "session_summary": {
            "value": "The user requested one small step at a time.",
            "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
        },
        "provisional_memories": [
            {
                "candidate_type": "interaction_preference",
                "content": "The user prefers one small step at a time.",
                "source_type": "explicit_user_statement",
                "confidence": 0.9,
                "requires_user_confirmation": False,
                "sensitivity": "low",
                "recommended_operation": "CREATE",
                "evidence": [
                    {"message_id": "msg_005", "quote": "每次一个小步骤"},
                    {"message_id": "msg_005", "quote": "每次一个小步骤"},
                ],
            }
        ],
    }


class ScriptedClient:
    """Current structured-client test double returning configured model data."""

    def __init__(self, data: dict[str, object], *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.prompt = ""
        self.model_name: str | None = None

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        self.prompt = prompt
        self.model_name = model_name
        assert output_model.__name__ == "_GroundedSessionFinalizerDraft"
        assert metadata == {"agent": "session_finalizer"}
        if self.fail:
            raise RuntimeError("session finalizer unavailable")
        return output_model.model_validate(self.data)


async def _finalize_model(
    data: dict[str, object],
    *,
    payload: SessionFinalizerInput | None = None,
) -> SessionFinalizerResult:
    return await SessionFinalizer(ScriptedClient(data)).finalize(payload or _payload())


@pytest.mark.asyncio
async def test_fake_returns_grounded_structured_result() -> None:
    payload = _payload(interventions=[evaluated_poor_fit_intervention()])

    result = await FakeSessionFinalizer().finalize(payload)

    assert result.session_id == payload.session_id
    assert result.session_summary
    assert result.source_message_ids
    assert all(item.source_message_ids for item in result.action_items)
    assert all(item.source_message_ids for item in result.candidate_memories)
    assert all(item.source_message_ids for item in result.strategy_outcomes)


@pytest.mark.asyncio
async def test_fake_extracts_only_explicit_user_preference_candidate() -> None:
    payload = _payload()

    result = await FakeSessionFinalizer().finalize(payload)

    assert len(result.candidate_memories) == 1
    candidate = result.candidate_memories[0]
    assert candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
    assert candidate.source_message_ids == [MessageId("msg_005")]


@pytest.mark.asyncio
async def test_fake_ignores_assistant_only_preference() -> None:
    session_id = SessionId("assistant-only-session")
    assistant = make_message(
        "assistant_preference",
        MessageRole.ASSISTANT,
        "以后我每次只给你一个小步骤。",
        1,
        session_id,
    )
    payload = SessionFinalizerInput(
        session_id=session_id,
        messages=[assistant],
        final_state=empty_state(session_id),
    )

    result = await FakeSessionFinalizer().finalize(payload)

    assert result.candidate_memories == []


@pytest.mark.asyncio
async def test_fake_pending_intervention_has_no_strategy_outcome() -> None:
    result = await FakeSessionFinalizer().finalize(
        _payload(interventions=[pending_reflective_intervention()])
    )

    assert result.strategy_outcomes == []


@pytest.mark.asyncio
async def test_fake_evaluated_intervention_uses_recorded_feedback() -> None:
    intervention = evaluated_poor_fit_intervention()

    result = await FakeSessionFinalizer().finalize(
        _payload(interventions=[intervention])
    )

    assert len(result.strategy_outcomes) == 1
    outcome = result.strategy_outcomes[0]
    assert outcome.strategy == intervention.strategy
    assert intervention.objective_progress is not None
    assert intervention.strategy_fit is not None
    assert intervention.objective_progress in outcome.outcome
    assert intervention.strategy_fit in outcome.outcome
    assert outcome.source_message_ids == [intervention.assistant_message_id]


@pytest.mark.asyncio
async def test_fake_risk_events_come_only_from_final_state() -> None:
    payload = _payload()
    low_result = await FakeSessionFinalizer().finalize(payload)
    risk_payload = payload.model_copy(
        update={
            "final_state": payload.final_state.model_copy(
                update={
                    "risk_state": RiskState(
                        level=RiskLevel.HIGH,
                        categories=["self_harm"],
                        reason_codes=["explicit_statement"],
                    )
                }
            )
        }
    )

    risk_result = await FakeSessionFinalizer().finalize(risk_payload)

    assert low_result.risk_events == []
    assert len(risk_result.risk_events) == 1
    assert "self_harm" in risk_result.risk_events[0]
    assert "violence" not in risk_result.risk_events[0]


@pytest.mark.asyncio
async def test_model_success_uses_current_client_and_local_session_id() -> None:
    client = ScriptedClient(_valid_model_data())
    payload = _payload()

    result = await SessionFinalizer(
        client,
        model_name="finalizer-model",
    ).finalize(payload)

    assert result.session_id == payload.session_id
    assert client.model_name == "finalizer-model"
    assert "INPUT_JSON" in client.prompt
    for field in ("messages", "final_state", "interventions", "rolling_summary"):
        assert field in client.prompt
    assert result.source_message_ids == [MessageId("msg_005")]
    assert result.candidate_memories[0].source_message_ids == [MessageId("msg_005")]


@pytest.mark.asyncio
async def test_missing_client_uses_fake_fallback() -> None:
    payload = _payload()
    assert await SessionFinalizer().finalize(payload) == await FakeSessionFinalizer().finalize(
        payload
    )


@pytest.mark.asyncio
async def test_client_exception_uses_fake_fallback() -> None:
    payload = _payload()
    result = await SessionFinalizer(
        ScriptedClient(_valid_model_data(), fail=True)
    ).finalize(payload)

    assert result == await FakeSessionFinalizer().finalize(payload)


@pytest.mark.asyncio
async def test_unknown_top_level_source_uses_fake_fallback() -> None:
    data = _valid_model_data()
    data["source_message_ids"] = ["unknown_message"]

    result = await _finalize_model(data)

    assert MessageId("unknown_message") not in result.source_message_ids
    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_field", ["action_items", "candidate_memories"])
async def test_unknown_nested_source_uses_fake_fallback(nested_field: str) -> None:
    data = _valid_model_data()
    if nested_field == "action_items":
        data[nested_field] = [
            {
                "content": "Write one sentence.",
                "evidence": [{"message_id": "unknown", "quote": "missing"}],
            }
        ]
    else:
        candidate = dict(data["provisional_memories"][0])  # type: ignore[index]
        candidate["evidence"] = [{"message_id": "unknown", "quote": "missing"}]
        data["provisional_memories"] = [candidate]

    result = await _finalize_model(data)

    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_unknown_strategy_outcome_source_uses_fake_fallback() -> None:
    data = _valid_model_data()
    data["strategy_outcomes"] = [
        {
            "strategy": "reflective_listening",
            "outcome": "poor",
            "source_message_ids": ["unknown"],
        }
    ]

    result = await _finalize_model(
        data,
        payload=_payload(interventions=[evaluated_poor_fit_intervention()]),
    )

    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_summary", "goal_updates"])
async def test_top_level_facts_without_sources_use_fake_fallback(field: str) -> None:
    data: dict[str, object] = {
        "session_id": "wrong",
        "session_summary": "",
        "source_message_ids": [],
    }
    data[field] = "unsupported fact" if field == "session_summary" else ["new goal"]

    result = await _finalize_model(data)

    assert result.session_summary != "unsupported fact"
    assert result.goal_updates != ["new goal"]


@pytest.mark.asyncio
async def test_candidate_without_sources_uses_fake_fallback() -> None:
    data = _valid_model_data()
    candidate = dict(data["provisional_memories"][0])  # type: ignore[index]
    candidate["evidence"] = []
    data["provisional_memories"] = [candidate]

    result = await _finalize_model(data)

    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_action_item_without_sources_uses_fake_fallback() -> None:
    data = _valid_model_data()
    data["action_items"] = [
        {"content": "Write one sentence.", "source_message_ids": []}
    ]

    result = await _finalize_model(data)

    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_pending_intervention_cannot_produce_outcome() -> None:
    data = _valid_model_data()
    data["strategy_outcomes"] = [
        {
            "strategy": "reflective_listening",
            "outcome": "good",
            "source_message_ids": ["msg_002"],
        }
    ]

    result = await _finalize_model(
        data,
        payload=_payload(interventions=[pending_reflective_intervention()]),
    )

    assert result.strategy_outcomes == []
    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_nonexistent_strategy_cannot_produce_outcome() -> None:
    data = _valid_model_data()
    data["strategy_outcomes"] = [
        {
            "strategy": "invented_strategy",
            "outcome": "good",
            "source_message_ids": ["msg_002"],
        }
    ]

    result = await _finalize_model(
        data,
        payload=_payload(interventions=[evaluated_poor_fit_intervention()]),
    )

    assert all(outcome.strategy != "invented_strategy" for outcome in result.strategy_outcomes)


@pytest.mark.asyncio
async def test_model_evaluated_outcome_uses_recorded_feedback_and_source() -> None:
    intervention = evaluated_poor_fit_intervention()
    data = _valid_model_data()
    data["strategy_outcomes"] = [
        {
            "intervention_id": intervention.intervention_id,
            "strategy": intervention.strategy,
            "outcome": "not_achieved; poor; negative",
            "evidence": [
                {"message_id": "msg_003", "quote": "不想继续分析情绪"},
                {"message_id": "msg_003", "quote": "不想继续分析情绪"},
            ],
        }
    ]

    result = await _finalize_model(
        data,
        payload=_payload(interventions=[intervention]),
    )

    assert result.session_summary == data["session_summary"]["value"]  # type: ignore[index]
    assert result.strategy_outcomes[0].source_message_ids == [MessageId("msg_003")]


@pytest.mark.asyncio
async def test_low_risk_state_rejects_model_risk_event() -> None:
    data = _valid_model_data()
    data["risk_events"] = ["high risk"]

    result = await _finalize_model(data)

    assert result.risk_events == []
    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_model_cannot_add_risk_category_absent_from_final_state() -> None:
    payload = _payload()
    payload = payload.model_copy(
        update={
            "final_state": payload.final_state.model_copy(
                update={
                    "risk_state": RiskState(
                        level=RiskLevel.HIGH,
                        categories=["self_harm"],
                        reason_codes=["explicit_statement"],
                    )
                }
            )
        }
    )
    data = _valid_model_data()
    data["risk_events"] = [
        "risk_level=high; self_harm; violence; explicit_statement"
    ]

    result = await _finalize_model(data, payload=payload)

    assert all("violence" not in event for event in result.risk_events)
    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
async def test_unsupported_diagnosis_or_success_claim_uses_fake_fallback() -> None:
    data = _valid_model_data()
    data["session_summary"] = "The user has an anxiety disorder and fully recovered."

    result = await _finalize_model(data)

    assert result.session_summary != data["session_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "claim"),
    [
        ("session_summary", "The user has an anxiety disorder."),
        ("goal_updates", "The user has a personality disorder."),
        ("action_items", "The user has a hidden motive."),
        ("candidate_memories", "The user has fully recovered."),
    ],
)
async def test_each_unsupported_claim_category_in_any_checked_field_uses_fallback(
    field: str,
    claim: str,
) -> None:
    data = _valid_model_data()
    if field == "session_summary":
        data[field] = claim
    elif field == "goal_updates":
        data[field] = [claim]
    elif field == "action_items":
        data[field] = [{"content": claim, "source_message_ids": ["msg_005"]}]
    else:
        candidate = dict(data["provisional_memories"][0])  # type: ignore[index]
        candidate["content"] = claim
        data["provisional_memories"] = [candidate]

    payload = _payload()
    result = await _finalize_model(data, payload=payload)
    fallback = await FakeSessionFinalizer().finalize(payload)

    assert result == fallback
    assert claim not in str(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_assistant_source_cannot_masquerade_as_explicit_user_memory() -> None:
    data = _valid_model_data()
    candidate = dict(data["provisional_memories"][0])  # type: ignore[index]
    candidate["evidence"] = [{"message_id": "msg_004", "quote": "低压力的小步骤"}]
    data["provisional_memories"] = [candidate]

    result = await _finalize_model(data)

    assert result.session_summary != data["session_summary"]
    assert result.candidate_memories[0].source_message_ids == [MessageId("msg_005")]
