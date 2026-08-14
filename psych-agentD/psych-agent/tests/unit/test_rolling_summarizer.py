"""Unit tests for rolling summarizer implementations."""

from collections.abc import Mapping

import pytest

from agents.rolling_summarizer import FakeRollingSummarizer, RollingSummarizer
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.summary import RollingSummarizerInput, RollingSummary
from services.model_execution import ModelExecutionFailure, ModelExecutionStatus
from tests.fixtures.interventions import pending_reflective_intervention
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import empty_state
from tests.fixtures.summaries import rolling_summarizer_input


@pytest.mark.asyncio
async def test_fake_rolling_summarizer_returns_grounded_summary() -> None:
    """Fallback summarizer should produce a typed summary from fixture inputs."""

    result = await FakeRollingSummarizer().summarize(rolling_summarizer_input())

    assert result.session_id == SessionId("session_fixture_work_stress")
    assert result.summary_version == 1
    assert result.covered_from == MessageId("msg_001")
    assert result.covered_to == MessageId("msg_005")
    assert result.current_problem is not None
    assert result.important_user_statements
    assert "reflective_listening" in result.strategies_attempted
    assert MessageId("msg_001") in result.source_message_ids


class SuccessfulStructuredClient:
    """Structured client returning a valid private grounded draft."""

    def __init__(self) -> None:
        self.output_model_name = ""
        self.prompt = ""
        self.model_name = ""
        self.metadata: Mapping[str, object] | None = None

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a model-shaped draft while recording adapter metadata."""

        self.prompt = prompt
        self.output_model_name = output_model.__name__
        self.model_name = model_name or ""
        self.metadata = metadata
        return output_model.model_validate(
            {
                "current_problem": {
                    "value": "用户和直属领导沟通紧张，开会前焦虑。",
                    "evidence": [
                        {
                            "message_id": "msg_001",
                            "quote": "和直属领导沟通很紧张",
                        }
                    ],
                },
                "important_user_statements": [
                    {
                        "value": "用户想知道下一步具体怎么做。",
                        "evidence": [
                            {
                                "message_id": "msg_003",
                                "quote": "下一步具体怎么做",
                            }
                        ],
                    }
                ],
                "strategies_attempted": [
                    {
                        "strategy": "reflective_listening",
                        "evidence": [
                            {
                                "message_id": "msg_002",
                                "quote": "听起来这段沟通关系",
                            }
                        ],
                    }
                ],
                "strategy_responses": [
                    {
                        "strategy": "reflective_listening",
                        "response": "用户明确要求切换到具体行动建议。",
                        "evidence": [
                            {
                                "message_id": "msg_003",
                                "quote": "不想继续分析情绪",
                            }
                        ],
                    }
                ],
            }
        )


class InvalidSourceStructuredClient:
    """Structured client returning a draft with an unknown source ID."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return an invalid source citation to force fallback behavior."""

        _ = (prompt, model_name, metadata)
        return output_model.model_validate(
            {
                "current_problem": {
                    "value": "invalid source summary",
                    "evidence": [
                        {"message_id": "msg_not_in_input", "quote": "missing"}
                    ],
                }
            }
        )


@pytest.mark.asyncio
async def test_model_backed_summarizer_returns_client_summary_with_local_version() -> None:
    """Model-backed summarizer should enforce local session and coverage fields."""

    client = SuccessfulStructuredClient()
    result = await RollingSummarizer(client).summarize(rolling_summarizer_input())

    assert client.output_model_name == "_GroundedRollingSummaryDraft"
    assert client.model_name == ""
    assert client.metadata == {"agent": "rolling_summarizer"}
    assert "INPUT_JSON" in client.prompt
    assert result.current_problem == "用户和直属领导沟通紧张，开会前焦虑。"
    assert result.session_id == SessionId("session_fixture_work_stress")
    assert result.summary_version == 1
    assert result.covered_to == MessageId("msg_005")
    assert result.source_message_ids == [
        MessageId("msg_001"),
        MessageId("msg_003"),
        MessageId("msg_002"),
    ]
    assert any(
        response == "reflective_listening: 用户明确要求切换到具体行动建议。"
        for response in result.strategy_responses
    )


@pytest.mark.asyncio
async def test_model_backed_summarizer_falls_back_on_unknown_source_ids() -> None:
    """Summaries citing messages outside the payload should not be accepted."""

    result = await RollingSummarizer(InvalidSourceStructuredClient()).summarize(
        rolling_summarizer_input()
    )

    assert result.current_problem != "invalid source summary"
    assert MessageId("msg_not_in_input") not in result.source_message_ids


class RecordingFallbackSummarizer(FakeRollingSummarizer):
    """Fallback double that records summarize calls."""

    def __init__(self) -> None:
        self.summarize_calls = 0

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Record a single fallback invocation."""

        self.summarize_calls += 1
        return await super().summarize(payload)


class PendingResponseStructuredClient:
    """Client that invents a response for a pending intervention."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return semantically invalid pending-response evidence."""

        _ = (prompt, model_name, metadata)
        return output_model.model_validate(
            {
                "strategy_responses": [
                    {
                        "strategy": "reflective_listening",
                        "response": "用户喜欢这个回应。",
                        "evidence": [
                            {
                                "message_id": "msg_003",
                                "quote": "下一步具体怎么做",
                            }
                        ],
                    }
                ]
            }
        )


class UnsupportedClaimStructuredClient:
    """Client that returns a grounded but unsupported clinical claim."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a grounded diagnosis claim."""

        _ = (prompt, model_name, metadata)
        return output_model.model_validate(
            {
                "current_problem": {
                    "value": "用户有 depression。",
                    "evidence": [
                        {
                            "message_id": "msg_001",
                            "quote": "沟通很紧张",
                        }
                    ],
                }
            }
        )


class FailingStructuredClient:
    """Client that raises a sanitized model failure."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Raise an unexpected error."""

        _ = (prompt, output_model, model_name, metadata)
        raise RuntimeError("raw provider message with user content")


@pytest.mark.asyncio
async def test_pending_intervention_response_is_semantic_fallback() -> None:
    """Pending interventions may be attempted but cannot produce responses."""

    messages = work_stress_dialogue()
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        uncovered_messages=messages[:3],
        current_state=empty_state(messages[0].session_id),
        interventions=[pending_reflective_intervention()],
    )
    fallback = RecordingFallbackSummarizer()

    result = await RollingSummarizer(
        PendingResponseStructuredClient(),
        fallback=fallback,
    ).summarize(payload)

    assert fallback.summarize_calls == 1
    assert result.strategy_responses == []


@pytest.mark.asyncio
async def test_strict_model_semantic_failure_raises_without_fallback() -> None:
    """Strict mode must not hide semantic failures behind fake success."""

    fallback = RecordingFallbackSummarizer()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await RollingSummarizer(
            UnsupportedClaimStructuredClient(),
            fallback=fallback,
            strict_model=True,
        ).summarize(rolling_summarizer_input())

    assert fallback.summarize_calls == 0
    assert exc_info.value.status == ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR
    assert exc_info.value.reason_codes == ("unsupported_diagnosis",)


@pytest.mark.asyncio
async def test_strict_model_no_client_raises_without_fallback() -> None:
    """A missing strict client is a model execution failure."""

    fallback = RecordingFallbackSummarizer()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await RollingSummarizer(fallback=fallback, strict_model=True).summarize(
            rolling_summarizer_input()
        )

    assert fallback.summarize_calls == 0
    assert exc_info.value.status == ModelExecutionStatus.FALLBACK_NO_CLIENT
    assert exc_info.value.reason_codes == ("llm_client_unavailable",)


@pytest.mark.asyncio
async def test_no_uncovered_messages_strict_is_noop_without_model_or_event() -> None:
    """No new messages is a legitimate no-op even when strict mode is enabled."""

    messages = work_stress_dialogue()
    previous = RollingSummary(
        session_id=messages[0].session_id,
        summary_version=2,
        covered_from=messages[0].id,
        covered_to=messages[2].id,
    )
    fallback = RecordingFallbackSummarizer()
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        previous_summary=previous,
        uncovered_messages=[],
        current_state=empty_state(messages[0].session_id),
    )

    result = await RollingSummarizer(
        FailingStructuredClient(),
        fallback=fallback,
        strict_model=True,
    ).summarize(payload)

    assert result == previous
    assert fallback.summarize_calls == 0
