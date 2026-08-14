"""Contracts for language-model requests and responses."""

from enum import StrEnum

from pydantic import Field

from schemas.common import ContractModel


class LLMMessageRole(StrEnum):
    """Roles accepted by chat-style local or remote language models."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(ContractModel):
    """One chat message sent to a language model."""

    role: LLMMessageRole
    content: str = Field(min_length=1)


class LLMRequest(ContractModel):
    """Model-agnostic request consumed by any LLMClient implementation."""

    messages: list[LLMMessage] = Field(min_length=1)
    model_name: str = Field(default="local-psych-support", min_length=1)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=800, ge=1)
    response_format: str | None = None


class LLMUsage(ContractModel):
    """Optional token accounting returned by a model provider."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class LLMResponse(ContractModel):
    """Normalized text response returned by an LLMClient."""

    content: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    finish_reason: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)