"""Tests for JSON evaluation dataset fixtures."""

from __future__ import annotations

import json
from pathlib import Path

DATASET_DIR = Path("evaluation/datasets")
DATASET_FILES = [
    "adaptive_loop_cases.json",
    "summary_cases.json",
    "memory_cases.json",
    "safety_cases.json",
]


def test_evaluation_datasets_are_valid_json() -> None:
    """Each evaluation dataset should parse as JSON and expose cases."""

    for filename in DATASET_FILES:
        payload = json.loads((DATASET_DIR / filename).read_text(encoding="utf-8"))

        assert isinstance(payload, dict)
        assert payload["schema_version"] == "0.1"
        assert isinstance(payload["dataset"], str)
        assert isinstance(payload["cases"], list)
        assert payload["cases"]


def test_evaluation_dataset_cases_have_ids_and_descriptions() -> None:
    """Every dataset case should have a stable ID and description."""

    for filename in DATASET_FILES:
        payload = json.loads((DATASET_DIR / filename).read_text(encoding="utf-8"))
        cases = payload["cases"]
        assert isinstance(cases, list)
        for case in cases:
            assert isinstance(case, dict)
            assert isinstance(case["id"], str)
            assert isinstance(case["description"], str)


def test_safety_cases_have_explicit_typed_inputs() -> None:
    payload = json.loads((DATASET_DIR / "safety_cases.json").read_text(encoding="utf-8"))
    for case in payload["cases"]:
        assert set(case["input"]) >= {
            "current_message",
            "recent_messages",
            "current_risk_level",
        }
        assert isinstance(case["input"]["recent_messages"], list)
        assert isinstance(case["expected_risk"], dict)
