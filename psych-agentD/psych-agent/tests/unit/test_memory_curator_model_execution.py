"""Model-execution and strict-mode tests for MemoryCurator."""

from collections.abc import Mapping

import pytest

from agents.memory_curator import FakeMemoryCurator, MemoryCurator, MemoryCuratorInput
from llm.exceptions import LLMStructuredOutputError, LLMTimeoutError
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.memory import MemoryCandidate
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import SessionFinalizerResult
from services.model_execution import ModelExecutionFailure

SESSION_ID = SessionId("memory-curator-model-execution")


def _payload() -> MemoryCuratorInput:
    return MemoryCuratorInput(
        session_id=SESSION_ID,
        messages=[
            Message(
                id=MessageId("msg_user"),
                session_id=SESSION_ID,
                role=MessageRole.USER,
                content="Please remember that I prefer one step at a time.",
                sequence_number=1,
            )
        ],
        final_state=SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="fixture",
        ),
    )


class RecordingFallbackMemoryCurator(FakeMemoryCurator):
    def __init__(self) -> None:
        self.curate_calls = 0

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        self.curate_calls += 1
        return await super().curate(payload)


class ExceptionClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = prompt, output_model, model_name, metadata
        raise self.error


class InvalidBatchClient:
    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        _ = prompt, model_name, metadata
        return output_model.model_validate({"candidates": "not-a-list"})


@pytest.mark.asyncio
async def test_non_strict_no_client_uses_fake_fallback_once() -> None:
    fallback = RecordingFallbackMemoryCurator()

    result = await MemoryCurator(fallback=fallback).curate(_payload())

    assert fallback.curate_calls == 1
    assert len(result) == 1


@pytest.mark.asyncio
async def test_strict_no_client_raises_without_fallback() -> None:
    fallback = RecordingFallbackMemoryCurator()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await MemoryCurator(fallback=fallback, strict_model=True).curate(_payload())

    assert fallback.curate_calls == 0
    assert exc_info.value.status.value == "fallback_no_client"


@pytest.mark.asyncio
async def test_strict_client_error_raises_without_fallback() -> None:
    fallback = RecordingFallbackMemoryCurator()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await MemoryCurator(
            ExceptionClient(LLMTimeoutError("timeout")),
            fallback=fallback,
            strict_model=True,
        ).curate(_payload())

    assert fallback.curate_calls == 0
    assert exc_info.value.reason_codes == ("llm_timeout",)


@pytest.mark.asyncio
async def test_non_strict_structured_error_uses_fake_fallback_once() -> None:
    fallback = RecordingFallbackMemoryCurator()

    result = await MemoryCurator(
        ExceptionClient(LLMStructuredOutputError("bad json")),
        fallback=fallback,
    ).curate(_payload())

    assert fallback.curate_calls == 1
    assert len(result) == 1


@pytest.mark.asyncio
async def test_strict_batch_schema_error_raises_without_fallback() -> None:
    fallback = RecordingFallbackMemoryCurator()

    with pytest.raises(ModelExecutionFailure) as exc_info:
        await MemoryCurator(
            InvalidBatchClient(),
            fallback=fallback,
            strict_model=True,
        ).curate(_payload())

    assert fallback.curate_calls == 0
    assert exc_info.value.status.value == "fallback_schema_validation_error"
