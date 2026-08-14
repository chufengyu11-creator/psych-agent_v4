"""Unit tests for rolling summarizer implementations."""

from collections.abc import Mapping

import pytest

from agents.rolling_summarizer import FakeRollingSummarizer, RollingSummarizer
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.summary import RollingSummary
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
    """Structured client returning a valid rolling summary."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Return a model-shaped summary while checking adapter metadata."""

        assert "INPUT_JSON" in prompt
        assert output_model is RollingSummary
        assert model_name is None
        assert metadata == {"agent": "rolling_summarizer"}
        return output_model.model_validate(
            {
                "session_id": "session_fixture_work_stress",
                "summary_version": 99,
                "covered_from": "msg_001",
                "covered_to": "msg_003",
                "current_problem": "client generated summary",
                "source_message_ids": ["msg_001"],
            }
        )


class InvalidSourceStructuredClient:
    """Structured client returning a summary with an unknown source ID."""

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
                "session_id": "session_fixture_work_stress",
                "summary_version": 99,
                "covered_from": "msg_001",
                "covered_to": "msg_003",
                "current_problem": "invalid source summary",
                "source_message_ids": ["msg_not_in_input"],
            }
        )


@pytest.mark.asyncio
async def test_model_backed_summarizer_returns_client_summary_with_local_version() -> None:
    """Model-backed summarizer should enforce local session and coverage fields."""

    result = await RollingSummarizer(SuccessfulStructuredClient()).summarize(
        rolling_summarizer_input()
    )

    assert result.current_problem == "client generated summary"
    assert result.summary_version == 1
    assert result.covered_to == MessageId("msg_005")


@pytest.mark.asyncio
async def test_model_backed_summarizer_falls_back_on_unknown_source_ids() -> None:
    """Summaries citing messages outside the payload should not be accepted."""

    result = await RollingSummarizer(InvalidSourceStructuredClient()).summarize(
        rolling_summarizer_input()
    )

    assert result.current_problem != "invalid source summary"
    assert MessageId("msg_not_in_input") not in result.source_message_ids