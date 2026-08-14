"""Fake language-model client for tests and local development."""

from schemas.llm import LLMRequest, LLMResponse


class FakeLLMClient:
    """Deterministic LLMClient that records requests and returns safe text."""

    def __init__(
        self,
        response_text: str | None = None,
        model_name: str = "fake-local-model",
    ) -> None:
        """Create a fake client with an optional fixed response."""

        self._response_text = response_text
        self._model_name = model_name
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Return deterministic text based on the rendered strategy context."""

        self.requests.append(request)
        text = self._response_text or self._choose_response(request)
        return LLMResponse(
            content=text,
            model_name=self._model_name,
            finish_reason="stop",
        )

    def _choose_response(self, request: LLMRequest) -> str:
        """Choose a canned response that keeps integration tests meaningful."""

        prompt = "\n".join(message.content for message in request.messages)
        if '"primary_strategy":"clarification"' in prompt:
            return (
                "\u6211\u542c\u89c1\u4f60\u4e0d\u592a\u60f3"
                "\u6cbf\u7528\u521a\u624d\u7684\u65b9\u5411\u3002"
                "\u6211\u4eec\u5148\u6821\u51c6\u4e00\u4e0b\uff1a"
                "\u4f60\u66f4\u60f3\u8981\u5177\u4f53\u6b65\u9aa4\u3001"
                "\u60c5\u7eea\u68b3\u7406\uff0c\u8fd8\u662f\u53ea\u9700\u8981"
                "\u6211\u966a\u4f60\u628a\u8bdd\u8bf4\u5b8c\uff1f"
            )
        if '"primary_strategy":"collaborative_problem_solving"' in prompt:
            return (
                "\u542c\u8d77\u6765\u4f60\u60f3\u8981\u66f4\u5177\u4f53"
                "\u4e00\u70b9\u7684\u4e0b\u4e00\u6b65\u3002"
                "\u6211\u4eec\u53ef\u4ee5\u5148\u9009\u4e00\u4e2a\u5f88\u5c0f\u3001"
                "\u538b\u529b\u6700\u4f4e\u7684\u884c\u52a8\u6765\u8bd5\u8bd5\u3002"
            )
        return (
            "\u6211\u542c\u5230\u4f60\u73b0\u5728\u6709\u4e9b\u4e0d\u5bb9\u6613\u3002"
            "\u6211\u4eec\u53ef\u4ee5\u5148\u628a\u6700\u56f0\u6270\u4f60\u7684\u90e8\u5206"
            "\u6162\u6162\u8bf4\u6e05\u695a\u3002"
        )