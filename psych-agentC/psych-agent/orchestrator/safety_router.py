"""Safety routing for non-normal risk results."""

from schemas.risk import RiskResult, RiskRoute


class SafetyRouter:
    """Returns controlled responses for elevated-risk routes."""

    async def handle(self, risk: RiskResult) -> str:
        """Map a risk route to a deterministic safety response."""

        if risk.route == RiskRoute.CRISIS:
            return (
                "我很在意你现在的安全。请先远离可能伤害自己的物品，"
                "并尽快联系身边可信任的人或当地紧急服务。"
            )
        if risk.route == RiskRoute.HUMAN:
            return "这个情况需要真人支持。我建议你尽快联系专业人员或本地紧急支持渠道。"
        return "我想先确认一下你的安全状况，再继续往下聊。"
