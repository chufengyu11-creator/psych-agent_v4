"""Unit tests for privacy-safe model execution metadata."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, ValidationError

from llm.exceptions import (
    LLMError,
    LLMHTTPError,
    LLMRetryExhaustedError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from services.model_execution import (
    ModelExecutionEvent,
    ModelExecutionFailure,
    ModelExecutionStatus,
    model_exception_status,
)


class _RequiredValue(BaseModel):
    value: int


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as captured:
        _RequiredValue.model_validate({})
    return captured.value


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (LLMTimeoutError("secret"), ModelExecutionStatus.FALLBACK_CLIENT_ERROR, "llm_timeout"),
        (LLMHTTPError(500, "secret"), ModelExecutionStatus.FALLBACK_CLIENT_ERROR, "llm_http_error"),
        (
            LLMRetryExhaustedError(2, RuntimeError("secret")),
            ModelExecutionStatus.FALLBACK_CLIENT_ERROR,
            "llm_retry_exhausted",
        ),
        (
            LLMStructuredOutputError("secret"),
            ModelExecutionStatus.FALLBACK_STRUCTURED_OUTPUT_ERROR,
            "llm_structured_output_error",
        ),
        (LLMError("secret"), ModelExecutionStatus.FALLBACK_CLIENT_ERROR, "llm_error"),
        (
            RuntimeError("secret"),
            ModelExecutionStatus.FALLBACK_UNEXPECTED_ERROR,
            "llm_unexpected_error",
        ),
    ],
)
def test_model_exception_status_is_stable(
    error: Exception,
    status: ModelExecutionStatus,
    reason: str,
) -> None:
    assert model_exception_status(error) == (status, (reason,))


def test_validation_error_has_schema_status() -> None:
    assert model_exception_status(_validation_error()) == (
        ModelExecutionStatus.FALLBACK_SCHEMA_VALIDATION_ERROR,
        ("llm_schema_validation_error",),
    )


def test_execution_event_is_frozen_and_uses_immutable_reason_codes() -> None:
    event = ModelExecutionEvent(
        agent="feedback_evaluator",
        status=ModelExecutionStatus.MODEL_SUCCESS,
        model_name=None,
    )
    assert event.reason_codes == ()
    assert isinstance(event.reason_codes, tuple)
    with pytest.raises(FrozenInstanceError):
        event.agent = "changed"  # type: ignore[misc]


def test_execution_failure_does_not_expose_original_error_message() -> None:
    event = ModelExecutionEvent(
        agent="feedback_evaluator",
        status=ModelExecutionStatus.FALLBACK_CLIENT_ERROR,
        model_name="model",
        reason_codes=("llm_timeout",),
    )
    failure = ModelExecutionFailure(event)
    assert "private provider message" not in str(failure)
    assert failure.reason_codes == ("llm_timeout",)
