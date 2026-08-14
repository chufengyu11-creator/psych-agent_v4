"""Tests for structured LLM output parsing and adapter behavior."""

import pytest

from llm.exceptions import LLMStructuredOutputError
from llm.structured_client import StructuredLLMClient
from llm.structured_output import extract_json_object, parse_structured_output
from schemas.llm import LLMRequest, LLMResponse
from schemas.risk import RiskLevel, RiskResult, RiskRoute


class StubLLMClient:
    """Fake base LLM client that returns queued text responses."""

    def __init__(self, responses: list[str]) -> None:
        """Store text responses for later generate calls."""

        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Record the request and return the next queued response."""

        self.requests.append(request)
        return LLMResponse(
            content=self._responses.pop(0),
            model_name=request.model_name,
        )


def test_extract_json_object_accepts_fenced_json() -> None:
    """Structured parsing should tolerate fenced JSON model output."""

    data = extract_json_object('Here is JSON:\n```json\n{"a": 1}\n```')

    assert data == {"a": 1}


def test_parse_structured_output_rejects_invalid_schema() -> None:
    """Invalid JSON shape should fail before it reaches agent code."""

    with pytest.raises(LLMStructuredOutputError):
        parse_structured_output('{"risk_level": "low"}', RiskResult)


@pytest.mark.asyncio
async def test_structured_llm_client_wraps_base_generate() -> None:
    """Adapter should convert base LLM text into a typed Pydantic model."""

    raw_json = """
    {
      "risk_level": "low",
      "categories": [],
      "needs_clarification": false,
      "route": "normal_dialogue",
      "reason_codes": ["fixture"],
      "confidence": 0.88
    }
    """
    base_client = StubLLMClient([raw_json])
    client = StructuredLLMClient(base_client, default_model_name="fixture-model")

    result = await client.generate_structured("Classify risk", RiskResult)

    assert result.risk_level == RiskLevel.LOW
    assert result.route == RiskRoute.NORMAL
    assert result.confidence == 0.88
    assert len(base_client.requests) == 1
    assert base_client.requests[0].response_format == "json_object"
    assert "Return exactly one JSON object" in base_client.requests[0].messages[0].content