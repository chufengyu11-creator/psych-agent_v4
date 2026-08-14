"""Retry policy primitives for model-provider calls."""

import asyncio
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class RetryPolicy:
    """Small retry policy shared by model-backed agents."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        """Validate retry settings at construction time."""

        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        if self.base_delay_seconds < 0:
            msg = "base_delay_seconds cannot be negative"
            raise ValueError(msg)

    def should_retry_status(self, status_code: int) -> bool:
        """Return whether an HTTP status should be retried."""

        return status_code in self.retryable_status_codes

    def should_retry_exception(self, error: Exception) -> bool:
        """Return whether a transport exception should be retried."""

        return isinstance(error, (httpx.TimeoutException, httpx.TransportError))

    async def sleep_before_retry(self, attempt_index: int) -> None:
        """Sleep with exponential backoff before the next attempt."""

        delay = self.base_delay_seconds * (2 ** max(attempt_index - 1, 0))
        if delay > 0:
            await asyncio.sleep(delay)