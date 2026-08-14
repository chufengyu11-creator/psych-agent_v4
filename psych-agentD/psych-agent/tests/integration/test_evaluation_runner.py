"""Integration tests for the offline evaluation runner."""

import asyncio
import json
import shutil
from pathlib import Path

from evaluation.run_eval import cli, format_report, run_evaluation

DATASETS = Path(__file__).resolve().parents[2] / "evaluation" / "datasets"


def _copy_datasets(target: Path) -> None:
    for name in (
        "adaptive_loop_cases.json",
        "summary_cases.json",
        "memory_cases.json",
        "safety_cases.json",
    ):
        shutil.copyfile(DATASETS / name, target / name)


def test_official_datasets_all_pass() -> None:
    report = asyncio.run(run_evaluation())
    assert report.is_success
    assert report.total == report.passed + report.failed
    output = format_report(report)
    assert "total:" in output
    assert "passed:" in output
    assert "failed:" in output
    assert "safety:safety_self_harm_ideation" in output
    assert "safety:safety_normal_work_stress" in output
    assert report.total == 7


def test_failed_case_does_not_stop_remaining_cases_and_returns_one(
    tmp_path: Path, capsys: object
) -> None:
    _copy_datasets(tmp_path)
    path = tmp_path / "adaptive_loop_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["expected"]["feedback"]["explicit_feedback"] = "positive"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = asyncio.run(run_evaluation(tmp_path))
    assert report.failed == 1
    assert report.total == 7
    assert report.case_results[-1].passed
    assert cli(["--dataset-dir", str(tmp_path)]) == 1


def test_corrupt_dataset_returns_two(tmp_path: Path) -> None:
    _copy_datasets(tmp_path)
    (tmp_path / "summary_cases.json").write_text("{broken", encoding="utf-8")
    assert cli(["--dataset-dir", str(tmp_path)]) == 2


def test_failed_safety_case_does_not_stop_other_suites_and_returns_one(
    tmp_path: Path,
) -> None:
    _copy_datasets(tmp_path)
    path = tmp_path / "safety_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["expected_risk"]["route_one_of"] = ["normal_dialogue"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = asyncio.run(run_evaluation(tmp_path))
    assert report.failed == 1
    assert report.case_results[0].passed
    assert report.case_results[-1].passed
    assert cli(["--dataset-dir", str(tmp_path)]) == 1


def test_corrupt_safety_json_returns_two(tmp_path: Path) -> None:
    _copy_datasets(tmp_path)
    (tmp_path / "safety_cases.json").write_text("{broken", encoding="utf-8")
    assert cli(["--dataset-dir", str(tmp_path)]) == 2


def test_invalid_typed_safety_input_returns_two(tmp_path: Path) -> None:
    _copy_datasets(tmp_path)
    path = tmp_path / "safety_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["input"]["current_risk_level"] = "invalid"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert cli(["--dataset-dir", str(tmp_path)]) == 2
