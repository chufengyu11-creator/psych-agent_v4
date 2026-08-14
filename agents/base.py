"""Shared Agent protocols.

This file defines call signatures only. Concrete agents can be fake, local LLM,
or remote LLM implementations as long as they satisfy these protocols.
"""

from typing import Protocol, TypeVar

InputT = TypeVar("InputT", contravariant=True)
OutputT = TypeVar("OutputT", covariant=True)


class Agent(Protocol[InputT, OutputT]):
    """Generic async agent interface used by contract tests."""

    async def run(self, payload: InputT) -> OutputT:
        """Process a typed payload and return a typed result."""


class TaskQueue(Protocol):
    """Minimal async task queue interface used after a user turn."""

    async def enqueue(self, task_name: str, user_id: str, session_id: str) -> None:
        """Schedule background work without blocking the user response."""
