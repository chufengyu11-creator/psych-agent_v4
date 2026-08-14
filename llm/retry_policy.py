"""Bounded retry policy for transient model-provider failures."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from llm.exceptions import LLMConnectionError, LLMError, LLMHTTPError, LLMTimeoutError

AsyncSleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """Retry only transient transport failures and selected provider statuses."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 2.0
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({429, 502, 503, 504})
    )
    sleeper: AsyncSleeper = field(default=asyncio.sleep, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate retry settings at construction time."""

        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        if self.base_delay_seconds < 0:
            msg = "base_delay_seconds cannot be negative"
            raise ValueError(msg)
        if self.max_delay_seconds < 0:
            msg = "max_delay_seconds cannot be negative"
            raise ValueError(msg)

    def should_retry_status(self, status_code: int) -> bool:
        """Return whether an HTTP status should be retried."""

        return status_code in self.retryable_status_codes

    def should_retry_exception(self, error: LLMError) -> bool:
        """Return whether a sanitized client-layer exception is transient."""

        if isinstance(error, (LLMConnectionError, LLMTimeoutError)):
            return True
        return isinstance(error, LLMHTTPError) and self.should_retry_status(
            error.status_code
        )

    async def sleep_before_retry(self, attempt_index: int) -> None:
        """Sleep with capped exponential backoff before the next attempt."""

        delay = min(
            self.base_delay_seconds * (2 ** max(attempt_index - 1, 0)),
            self.max_delay_seconds,
        )
        if delay > 0:
            await self.sleeper(delay)
