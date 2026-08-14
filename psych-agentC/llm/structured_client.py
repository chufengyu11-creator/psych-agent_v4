"""Structured-output adapter built on top of the base LLMClient protocol."""

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel

from llm.client import LLMClientProtocol
from llm.structured_output import ModelT, parse_structured_output
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest


class StructuredLLMClientProtocol(Protocol):
    """Protocol consumed by model-backed agents that need typed JSON output."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Generate text and parse it into the requested Pydantic model."""


class StructuredLLMClient:
    """Adapter from LLMClient.generate() to generate_structured()."""

    def __init__(
        self,
        llm_client: LLMClientProtocol,
        *,
        default_model_name: str = "local-psych-support",
        temperature: float = 0.1,
        max_tokens: int = 800,
    ) -> None:
        """Configure the underlying client and default generation options."""

        self._llm_client = llm_client
        self._default_model_name = default_model_name
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Ask the base client for JSON text, then validate it as output_model."""

        request = LLMRequest(
            messages=[
                LLMMessage(
                    role=LLMMessageRole.USER,
                    content=self._with_json_instruction(prompt, output_model, metadata),
                )
            ],
            model_name=model_name or self._default_model_name,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            response_format="json_object",
        )
        response = await self._llm_client.generate(request)
        return parse_structured_output(response.content, output_model)

    def _with_json_instruction(
        self,
        prompt: str,
        output_model: type[BaseModel],
        metadata: Mapping[str, object] | None,
    ) -> str:
        """Append a compact schema reminder to reduce malformed model output."""

        metadata_lines = ""
        if metadata:
            metadata_lines = "\n".join(f"- {key}: {value}" for key, value in metadata.items())
        schema_json = output_model.model_json_schema()
        return (
            f"{prompt.strip()}\n\n"
            "Return exactly one JSON object and no extra prose.\n"
            f"Output model: {output_model.__name__}\n"
            f"JSON schema: {schema_json}\n"
            f"Metadata:\n{metadata_lines or '- none'}\n"
        )