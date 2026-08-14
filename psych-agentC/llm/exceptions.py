"""Exceptions raised by the shared LLM client layer."""


class LLMError(Exception):
    """Base exception for LLM client failures."""


class LLMConfigurationError(LLMError):
    """Raised when a model or client is not configured correctly."""


class LLMHTTPError(LLMError):
    """Raised when a provider returns an unsuccessful HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the provider status code and diagnostic message."""

        super().__init__(f"LLM provider returned HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class LLMTimeoutError(LLMError):
    """Raised when a provider request times out."""


class LLMStructuredOutputError(LLMError):
    """Raised when provider text cannot be parsed into the requested schema."""


class LLMRetryExhaustedError(LLMError):
    """Raised after all retry attempts have failed."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        """Store retry count and the final underlying error."""

        super().__init__(f"LLM request failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error