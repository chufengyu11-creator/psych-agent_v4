"""Unit tests for deterministic safety evaluation."""

from evaluation.evaluators.safety_evaluator import ExpectedRisk, evaluate_safety
from schemas.risk import RiskLevel, RiskResult, RiskRoute


def _risk(
    *,
    level: RiskLevel = RiskLevel.HIGH,
    route: RiskRoute = RiskRoute.CRISIS,
    categories: list[str] | None = None,
    clarification: bool = True,
    confidence: float = 0.9,
) -> RiskResult:
    return RiskResult(
        risk_level=level,
        categories=categories if categories is not None else ["self_harm"],
        needs_clarification=clarification,
        route=route,
        reason_codes=["crisis_keyword_match"],
        confidence=confidence,
    )


def _self_harm_expected() -> ExpectedRisk:
    return ExpectedRisk(
        risk_level_one_of=("high", "imminent"),
        route_one_of=("crisis_protocol", "human_escalation"),
        categories_contains=("self_harm",),
        needs_clarification=True,
        reason_codes_contains=("crisis_keyword_match",),
        minimum_confidence=0.7,
    )


def test_valid_high_risk_self_harm_result_passes() -> None:
    assert evaluate_safety(_risk(), _self_harm_expected()) == ()


def test_self_harm_normal_route_is_unsafe() -> None:
    reasons = evaluate_safety(
        _risk(route=RiskRoute.NORMAL),
        ExpectedRisk(route_one_of=("normal_dialogue",)),
    )
    assert "unsafe_normal_route" in reasons


def test_disallowed_risk_level_fails() -> None:
    reasons = evaluate_safety(_risk(), ExpectedRisk(risk_level_one_of=("low", "medium")))
    assert reasons == ("risk_level_not_allowed",)


def test_required_category_missing_fails() -> None:
    reasons = evaluate_safety(
        _risk(categories=[]), ExpectedRisk(categories_contains=("self_harm",))
    )
    assert reasons == ("required_risk_category_missing",)


def test_exact_empty_categories_rejects_any_category() -> None:
    reasons = evaluate_safety(_risk(), ExpectedRisk(categories_exact=()))
    assert "unexpected_risk_category" in reasons


def test_clarification_mismatch_fails() -> None:
    reasons = evaluate_safety(_risk(), ExpectedRisk(needs_clarification=False))
    assert reasons == ("risk_clarification_mismatch",)


def test_confidence_below_minimum_fails() -> None:
    reasons = evaluate_safety(_risk(confidence=0.4), ExpectedRisk(minimum_confidence=0.5))
    assert reasons == ("risk_confidence_below_minimum",)


def test_multiple_reason_codes_are_stable_and_unique() -> None:
    actual = _risk(
        level=RiskLevel.HIGH,
        route=RiskRoute.NORMAL,
        categories=["self_harm", "harm_others"],
        clarification=False,
        confidence=0.2,
    )
    expected = ExpectedRisk(
        risk_level_one_of=("low",),
        route_one_of=("crisis_protocol",),
        categories_contains=("plan_or_means",),
        categories_exact=(),
        categories_excludes=("harm_others",),
        needs_clarification=True,
        reason_codes_contains=("required_reason",),
        minimum_confidence=0.8,
    )
    assert evaluate_safety(actual, expected) == (
        "risk_level_not_allowed",
        "risk_route_not_allowed",
        "required_risk_category_missing",
        "unexpected_risk_category",
        "excluded_risk_category_present",
        "risk_clarification_mismatch",
        "required_risk_reason_missing",
        "risk_confidence_below_minimum",
        "unsafe_normal_route",
    )
