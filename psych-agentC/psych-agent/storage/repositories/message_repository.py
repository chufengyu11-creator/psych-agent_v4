"""Message repository protocol and in-memory implementation."""

from typing import Protocol

from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole


class MessageRepository(Protocol):
    """Persistence boundary for append-only conversation messages."""

    async def create_user_message(self, session_id: SessionId, text: str) -> Message:
        """Persist and return one user message."""

    async def create_assistant_message(self, session_id: SessionId, text: str) -> Message:
        """Persist and return one assistant message."""

    async def get_recent(self, session_id: SessionId, limit: int = 12) -> list[Message]:
        """Return recent messages for context construction."""

    async def get_by_id(self, message_id: MessageId) -> Message | None:
        """Return a message by ID, or None if it does not exist."""


class InMemoryMessageRepository:
    """Volatile message repository for tests and local fake pipelines."""

    def __init__(self) -> None:
        """Create an empty in-memory message store."""

        self._messages: list[Message] = []

    async def create_user_message(self, session_id: SessionId, text: str) -> Message:
        """Append a user message to the in-memory store."""

        return await self._create_message(
            session_id=session_id,
            role=MessageRole.USER,
            text=text,
        )

    async def create_assistant_message(self, session_id: SessionId, text: str) -> Message:
        """Append an assistant message to the in-memory store."""

        return await self._create_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            text=text,
        )

    async def get_recent(self, session_id: SessionId, limit: int = 12) -> list[Message]:
        """Return the newest messages for a session."""

        session_messages = [
            message for message in self._messages if message.session_id == session_id
        ]
        return session_messages[-limit:]

    async def get_by_id(self, message_id: MessageId) -> Message | None:
        """Return the first matching message ID from the store."""

        for message in self._messages:
            if message.id == message_id:
                return message
        return None

    async def _create_message(
        self,
        session_id: SessionId,
        role: MessageRole,
        text: str,
    ) -> Message:
        """Create a typed message with a monotonic sequence number."""

        session_messages = [
            message for message in self._messages if message.session_id == session_id
        ]
        message = Message(
            id=MessageId(f"msg_{len(self._messages) + 1}"),
            session_id=session_id,
            role=role,
            content=text,
            sequence_number=len(session_messages) + 1,
        )
        self._messages.append(message)
        return message
