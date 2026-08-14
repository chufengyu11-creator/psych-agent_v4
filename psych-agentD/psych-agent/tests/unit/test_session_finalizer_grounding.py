"""Grounding, strict-mode, and execution tests for SessionFinalizer."""

import logging
from collections.abc import Mapping
from typing import cast

import pytest

from agents.session_finalizer import FakeSessionFinalizer, SessionFinalizer
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from llm.structured_output import ModelT
from schemas.common import MessageId
from schemas.intervention import InterventionRecord
from schemas.messages import Message
from schemas.risk import RiskLevel
from schemas.state import RiskState
from schemas.summary import SessionFinalizerInput, SessionFinalizerResult
from services.model_execution import ModelExecutionFailure, ModelExecutionStatus
from tests.fixtures.interventions import (
    evaluated_poor_fit_intervention,
    pending_reflective_intervention,
)
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import empty_state


class RecordingFallbackFinalizer(FakeSessionFinalizer):
    """Fallback double recording invocations."""

    def __init__(self) -> None:
        self.finalize_calls = 0

    async def finalize(self, payload: SessionFinalizerInput) -> SessionFinalizerResult:
        self.finalize_calls += 1
        return await super().finalize(payload)


class DraftClient:
    """Typed structured client returning one configured private draft."""

    def __init__(self, draft: dict[str, object]) -> None:
        self._draft = draft
        self.output_model_name = ""
        self.model_name: str | None = None
        self.metadata: Mapping[str, object] | None = None
        self.prompt = ""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        self.output_model_name = output_model.__name__
        self.model_name = model_name
        self.metadata = metadata
        self.prompt = prompt
        return output_model.model_validate(self._draft)


class ExceptionClient:
    """Typed structured client raising one configured exception."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = (prompt, output_model, model_name, metadata)
        raise self._error


def _payload(
    *,
    messages: list[Message] | None = None,
    interventions: list[InterventionRecord] | None = None,
    risk: bool = False,
) -> SessionFinalizerInput:
    items = messages or work_stress_dialogue()
    final_state = empty_state(items[0].session_id)
    if risk:
        final_state = final_state.model_copy(
            update={
                "risk_state": RiskState(
                    level=RiskLevel.HIGH,
                    categories=["self_harm"],
                    reason_codes=["explicit_statement"],
                )
            }
        )
    return SessionFinalizerInput(
        session_id=items[0].session_id,
        messages=items,
        final_state=final_state,
        interventions=interventions or [],
    )


def _valid_draft() -> dict[str, object]:
    return {
        "session_summary": {
            "value": "The user discussed work stress and asked for one small step.",
            "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
        },
        "provisional_memories": [
            {
                "candidate_type": "interaction_preference",
                "content": "The user prefers one small step at a time.",
                "source_type": "explicit_user_statement",
                "confidence": 0.9,
                "sensitivity": "low",
                "requires_user_confirmation": False,
                "recommended_operation": "CREATE",
                "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
            }
        ],
    }


def _last_execution_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    return next(
        record
        for record in reversed(caplog.records)
        if record.name == "agents.session_finalizer"
        and record.getMessage() == "model_execution"
    )


def _record_status(record: logging.LogRecord) -> str:
    return cast("str", record.__dict__["status"])


def _record_reason_codes(record: logging.LogRecord) -> tuple[str, ...]:
    value = record.__dict__["reason_codes"]
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


@pytest.mark.asyncio
async def test_success_uses_private_draft_and_local_sources(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = DraftClient(_valid_draft())

    with caplog.at_level(logging.INFO, logger="agents.session_finalizer"):
        result = await SessionFinalizer(
            client,
            model_name="finalizer-model",
        ).finalize(_payload())

    record = _last_execution_record(caplog)
    assert client.output_model_name == "_GroundedSessionFinalizerDraft"
    assert client.model_name == "finalizer-model"
    assert client.metadata == {"agent": "session_finalizer"}
    assert "INPUT_JSON" in client.prompt
    assert result.source_message_ids == [MessageId("msg_005")]
    assert result.candidate_memories[0].source_message_ids == [MessageId("msg_005")]
    assert _record_status(record) == ModelExecutionStatus.MODEL_SUCCESS.value
    assert _record_reason_codes(record) == ()


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (
            {
                "session_summary": {
                    "value": "Unknown source",
                    "evidence": [{"message_id": "msg_unknown", "quote": "missing"}],
                }
            },
            "unknown_finalizer_source_message",
        ),
        (
            {
                "session_summary": {
                    "value": "Fake quote",
                    "evidence": [{"message_id": "msg_005", "quote": "not present"}],
                }
            },
            "finalizer_evidence_quote_not_found",
        ),
        (
            {
                "session_summary": {
                    "value": "Summary",
                    "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
                },
                "action_items": [
                    {
                        "content": "Assistant-only action",
                        "evidence": [
                            {"message_id": "msg_004", "quote": "低压力的小步骤"}
                        ],
                    }
                ],
            },
            "action_item_requires_user_source",
        ),
        (
            {
                "session_summary": {
                    "value": "Summary",
                    "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
                },
                "strategy_outcomes": [
                    {
                        "intervention_id": "int_001",
                        "strategy": "reflective_listening",
                        "outcome": "success",
                        "evidence": [
                            {"message_id": "msg_003", "quote": "不想继续分析情绪"}
                        ],
                    }
                ],
            },
            "strategy_outcome_feedback_mismatch",
        ),
        (
            {
                "session_summary": {
                    "value": "Summary",
                    "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
                },
                "provisional_memories": [
                    {
                        "candidate_type": "interaction_preference",
                        "content": "The user prefers one small step at a time.",
                        "source_type": "model_inference",
                        "confidence": 0.9,
                        "sensitivity": "low",
                        "requires_user_confirmation": False,
                        "recommended_operation": "CREATE",
                        "evidence": [
                            {"message_id": "msg_005", "quote": "每次一个小步骤"}
                        ],
                    }
                ],
            },
            "provisional_memory_invalid_source_type",
        ),
        (
            {
                "session_summary": {
                    "value": "The user has depression.",
                    "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
                }
            },
            "unsupported_diagnosis",
        ),
    ],
)
@pytest.mark.asyncio
async def test_semantic_failures_log_stable_reason_and_fallback_once(
    draft: dict[str, object],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fallback = RecordingFallbackFinalizer()

    with caplog.at_level(logging.WARNING, logger="agents.session_finalizer"):
        await SessionFinalizer(
            DraftClient(draft),
            fallback=fallback,
        ).finalize(_payload(interventions=[evaluated_poor_fit_intervention()]))

    assert fallback.finalize_calls == 1
    record = _last_execution_record(caplog)
    assert _record_status(record) == (
        ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR.value
    )
    assert reason in _record_reason_codes(record)


@pytest.mark.asyncio
async def test_pending_intervention_cannot_produce_strategy_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    draft = _valid_draft()
    draft["strategy_outcomes"] = [
        {
            "intervention_id": "int_001",
            "strategy": "reflective_listening",
            "outcome": "partial",
            "evidence": [{"message_id": "msg_003", "quote": "不想继续分析情绪"}],
        }
    ]

    with caplog.at_level(logging.WARNING, logger="agents.session_finalizer"):
        await SessionFinalizer(DraftClient(draft)).finalize(
            _payload(interventions=[pending_reflective_intervention()])
        )

    assert "strategy_outcome_from_pending_intervention" in _record_reason_codes(
        _last_execution_record(caplog)
    )


@pytest.mark.asyncio
async def test_low_risk_input_rejects_model_risk_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    draft = _valid_draft()
    draft["risk_events"] = [
        {
            "risk_level": "high",
            "categories": ["self_harm"],
            "reason_codes": ["explicit_statement"],
            "description": "high risk",
            "evidence": [{"message_id": "msg_001", "quote": "沟通很紧张"}],
        }
    ]

    with caplog.at_level(logging.WARNING, logger="agents.session_finalizer"):
        await SessionFinalizer(DraftClient(draft)).finalize(_payload())

    assert "risk_event_without_typed_support" in _record_reason_codes(
        _last_execution_record(caplog)
    )


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (
            LLMTimeoutError("private timeout"),
            ModelExecutionStatus.FALLBACK_CLIENT_ERROR,
            "llm_timeout",
        ),
        (
            LLMStructuredOutputError("private bad json"),
            ModelExecutionStatus.FALLBACK_STRUCTURED_OUTPUT_ERROR,
            "llm_structured_output_error",
        ),
        (
            RuntimeError("raw provider message"),
            ModelExecutionStatus.FALLBACK_UNEXPECTED_ERROR,
            "llm_unexpected_error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_client_errors_are_classified_and_private(
    error: Exception,
    status: ModelExecutionStatus,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fallback = RecordingFallbackFinalizer()

    with caplog.at_level(logging.WARNING, logger="agents.session_finalizer"):
        await SessionFinalizer(
            ExceptionClient(error),
            fallback=fallback,
        ).finalize(_payload())

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    record = _last_execution_record(caplog)
    assert fallback.finalize_calls == 1
    assert _record_status(record) == status.value
    assert _record_reason_codes(record) == (reason,)
    assert "raw provider message" not in rendered
    assert "每次一个小步骤" not in rendered
    assert "INPUT_JSON" not in rendered


@pytest.mark.asyncio
async def test_strict_semantic_failure_raises_without_fallback() -> None:
    fallback = RecordingFallbackFinalizer()
    draft = _valid_draft()
    draft["session_summary"] = {
        "value": "The user has fully recovered.",
        "evidence": [{"message_id": "msg_005", "quote": "每次一个小步骤"}],
    }

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await SessionFinalizer(
            DraftClient(draft),
            fallback=fallback,
            strict_model=True,
        ).finalize(_payload())

    assert fallback.finalize_calls == 0
    assert exc_info.value.status == ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR
    assert exc_info.value.reason_codes == ("unsupported_treatment_outcome",)


@pytest.mark.asyncio
async def test_strict_no_client_raises_without_fallback() -> None:
    fallback = RecordingFallbackFinalizer()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await SessionFinalizer(
            fallback=fallback,
            strict_model=True,
        ).finalize(_payload())

    assert fallback.finalize_calls == 0
    assert exc_info.value.status == ModelExecutionStatus.FALLBACK_NO_CLIENT
