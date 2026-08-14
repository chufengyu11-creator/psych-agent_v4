"""Safe, immutable metadata for model execution and fallback decisions."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from llm.exceptions import (
    LLMError,
    LLMHTTPError,
    LLMRetryExhaustedError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)


class ModelExecutionStatus(StrEnum):
    """Stable outcomes for one model-backed agent execution."""

    MODEL_SUCCESS = "model_success"
    FALLBACK_NO_CLIENT = "fallback_no_client"
    FALLBACK_CLIENT_ERROR = "fallback_client_error"
    FALLBACK_STRUCTURED_OUTPUT_ERROR = "fallback_structured_output_error"
    FALLBACK_SCHEMA_VALIDATION_ERROR = "fallback_schema_validation_error"
    FALLBACK_SEMANTIC_VALIDATION_ERROR = "fallback_semantic_validation_error"
    FALLBACK_UNEXPECTED_ERROR = "fallback_unexpected_error"


@dataclass(frozen=True, slots=True)
class ModelExecutionEvent:
    """Privacy-safe immutable metadata for one execution decision."""

    agent: str
    status: ModelExecutionStatus
    model_name: str | None
    reason_codes: tuple[str, ...] = ()


class ModelExecutionFailure(RuntimeError):
    """Strict-mode failure containing stable metadata only."""

    def __init__(self, event: ModelExecutionEvent) -> None:
        self.agent = event.agent
        self.status = event.status
        self.reason_codes = event.reason_codes
        super().__init__(
            "model execution failed: "
            f"agent={event.agent} status={event.status.value} "
            f"reason_codes={','.join(event.reason_codes) or 'none'}"
        )


def model_exception_status(
    error: Exception,
) -> tuple[ModelExecutionStatus, tuple[str, ...]]:
    """Classify an exception without exposing its original message."""

    if isinstance(error, LLMTimeoutError):
        return ModelExecutionStatus.FALLBACK_CLIENT_ERROR, ("llm_timeout",)
    if isinstance(error, LLMHTTPError):
        return ModelExecutionStatus.FALLBACK_CLIENT_ERROR, ("llm_http_error",)
    if isinstance(error, LLMRetryExhaustedError):
        return ModelExecutionStatus.FALLBACK_CLIENT_ERROR, ("llm_retry_exhausted",)
    if isinstance(error, LLMStructuredOutputError):
        return ModelExecutionStatus.FALLBACK_STRUCTURED_OUTPUT_ERROR, (
            "llm_structured_output_error",
        )
    if isinstance(error, ValidationError):
        return ModelExecutionStatus.FALLBACK_SCHEMA_VALIDATION_ERROR, (
            "llm_schema_validation_error",
        )
    if isinstance(error, LLMError):
        return ModelExecutionStatus.FALLBACK_CLIENT_ERROR, ("llm_error",)
    return ModelExecutionStatus.FALLBACK_UNEXPECTED_ERROR, (
        "llm_unexpected_error",
    )


def log_model_execution(
    logger: logging.Logger,
    event: ModelExecutionEvent,
) -> None:
    """Log only stable execution metadata, never model or user content."""

    extra = {
        "agent": event.agent,
        "status": event.status.value,
        "model_name": event.model_name,
        "reason_codes": event.reason_codes,
    }
    if event.status == ModelExecutionStatus.MODEL_SUCCESS:
        logger.info("model_execution", extra=extra)
    else:
        logger.warning("model_execution", extra=extra)


__all__ = [
    "ModelExecutionEvent",
    "ModelExecutionFailure",
    "ModelExecutionStatus",
    "log_model_execution",
    "model_exception_status",
]
