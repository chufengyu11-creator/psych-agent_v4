"""Correlation-aware timing spans for request diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from time import perf_counter_ns, time_ns
from typing import TypeVar
from uuid import uuid4

_LOGGER = logging.getLogger("uvicorn.error")
_TRACE_ID: ContextVar[str | None] = ContextVar(
    "psych_agent_timing_trace_id",
    default=None,
)
T = TypeVar("T")


def bind_new_trace() -> Token[str | None]:
    """Bind a new opaque correlation ID to the current request context."""

    return _TRACE_ID.set(uuid4().hex)


def reset_trace(token: Token[str | None]) -> None:
    """Restore the previous correlation context."""

    _TRACE_ID.reset(token)


def current_trace_id() -> str | None:
    """Return the current request correlation ID, if one is bound."""

    return _TRACE_ID.get()


def _safe_fields(fields: dict[str, object]) -> dict[str, object]:
    """Keep timing metadata limited to JSON scalar values."""

    return {
        key: value
        for key, value in fields.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }


@contextmanager
def timing_span(stage: str, **fields: object) -> Iterator[None]:
    """Emit one structured timing event without logging user content."""

    started_perf_ns = perf_counter_ns()
    started_epoch_ms = time_ns() // 1_000_000
    event: dict[str, object] = {
        "trace_id": current_trace_id() or "unbound",
        "stage": stage,
        "started_epoch_ms": started_epoch_ms,
        **_safe_fields(fields),
    }
    try:
        yield
    except BaseException as error:
        event["status"] = "error"
        event["error_type"] = type(error).__name__
        raise
    else:
        event["status"] = "ok"
    finally:
        event["finished_epoch_ms"] = time_ns() // 1_000_000
        event["duration_ms"] = round(
            (perf_counter_ns() - started_perf_ns) / 1_000_000,
            3,
        )
        _LOGGER.info(
            "PSYCH_TIMING %s",
            json.dumps(
                event,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


async def timed_awaitable(
    stage: str,
    awaitable: Awaitable[T],
    **fields: object,
) -> T:
    """Await an operation inside a named timing span."""

    with timing_span(stage, **fields):
        return await awaitable


__all__ = [
    "bind_new_trace",
    "current_trace_id",
    "reset_trace",
    "timed_awaitable",
    "timing_span",
]
