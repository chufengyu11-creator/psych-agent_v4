"""Output guard implementations."""

from schemas.safety import GuardDecision, GuardInput, GuardResult


class FakeOutputGuard:
    """Rule-based output guard used until the safety model is integrated."""

    async def run(self, payload: GuardInput) -> GuardResult:
        """Alias for review so the class satisfies Agent protocol."""

        return await self.review(payload)

    async def review(self, payload: GuardInput) -> GuardResult:
        """Block obvious boundary violations and allow ordinary support text."""

        text = payload.draft.text
        blocked_terms = ["诊断你是", "药物剂量", "保证治好"]
        violations = [term for term in blocked_terms if term in text]
        if violations:
            return GuardResult(decision=GuardDecision.BLOCK, violations=violations)
        return GuardResult(decision=GuardDecision.ALLOW)
