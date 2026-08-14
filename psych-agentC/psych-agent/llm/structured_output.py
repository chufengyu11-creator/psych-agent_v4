"""Utilities for turning model text into typed Pydantic objects."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from llm.exceptions import LLMStructuredOutputError

ModelT = TypeVar("ModelT", bound=BaseModel)

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, object]:
    """Extract a JSON object from raw model text."""

    candidates = [text.strip()]
    candidates.extend(match.strip() for match in _FENCED_JSON_RE.findall(text))

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    msg = "model output did not contain a valid JSON object"
    raise LLMStructuredOutputError(msg)


def parse_structured_output(text: str, output_model: type[ModelT]) -> ModelT:
    """Parse model text into the requested Pydantic model."""

    data = extract_json_object(text)
    try:
        return output_model.model_validate(data)
    except ValidationError as exc:
        msg = f"model output did not match {output_model.__name__}"
        raise LLMStructuredOutputError(msg) from exc
