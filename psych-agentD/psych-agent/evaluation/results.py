"""Strict internal result types shared by deterministic evaluators."""

from dataclasses import dataclass


def stable_reason_codes(reason_codes: list[str]) -> tuple[str, ...]:
    """Deduplicate reason codes without changing their first-seen order."""

    return tuple(dict.fromkeys(reason_codes))


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    suite: str
    case_id: str
    passed: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    case_results: tuple[EvaluationCaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.case_results)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.case_results)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def is_success(self) -> bool:
        return self.failed == 0


__all__ = ["EvaluationCaseResult", "EvaluationReport", "stable_reason_codes"]
