"""Provider-agnostic exceptions raised by the shared LLM client layer."""


class LLMError(Exception):
    """Base exception for LLM client failures."""

    retryable = False


class LLMConfigurationError(LLMError):
    """Raised when a model or client is not configured correctly."""


class LLMConnectionError(LLMError):
    """Raised when the configured provider cannot be reached."""

    retryable = True


class LLMTimeoutError(LLMError):
    """Raised when a provider connection or response times out."""

    retryable = True


class LLMHTTPError(LLMError):
    """Raised when a provider returns an unsuccessful HTTP response."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        """Store only a status code and a safe provider-independent summary."""

        super().__init__(f"LLM provider returned HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.retryable = retryable


class LLMBadRequestError(LLMHTTPError):
    """Raised for malformed or unsupported provider requests."""


class LLMAuthenticationError(LLMHTTPError):
    """Raised when the provider rejects the configured API key."""


class LLMPermissionError(LLMHTTPError):
    """Raised when the API key lacks permission for the requested operation."""


class LLMNotFoundError(LLMHTTPError):
    """Raised when an endpoint or served model name does not exist."""


class LLMRateLimitError(LLMHTTPError):
    """Raised when the provider rate-limits a request or reports exhausted quota."""


class LLMServerError(LLMHTTPError):
    """Raised when the provider reports a server-side failure."""


class LLMProtocolError(LLMError):
    """Raised when a successful response violates the OpenAI-compatible contract."""


class LLMOutputTruncatedError(LLMProtocolError):
    """Raised when finish_reason=length indicates an incomplete final response."""


class LLMStructuredOutputError(LLMError):
    """Raised when provider text cannot be parsed into the requested schema."""


class LLMRetryExhaustedError(LLMError):
    """Raised after all retry attempts have failed."""

    def __init__(self, attempts: int, last_error: LLMError) -> None:
        """Store retry count and the final sanitized domain error."""

        super().__init__(f"LLM request failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error
