"""Message repository protocol, in-memory store, and SQLAlchemy implementation."""

from typing import Protocol
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import MessageId, SessionId, utc_now
from schemas.messages import Message, MessageRole
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.repositories.errors import SessionNotFoundError


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


class SqlAlchemyMessageRepository:
    """SQLAlchemy persistence for append-only conversation messages."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def create_user_message(self, session_id: SessionId, text: str) -> Message:
        """Persist and return one user message."""

        return await self._create_message(session_id, MessageRole.USER, text)

    async def create_assistant_message(self, session_id: SessionId, text: str) -> Message:
        """Persist and return one assistant message."""

        return await self._create_message(session_id, MessageRole.ASSISTANT, text)

    async def get_recent(self, session_id: SessionId, limit: int = 12) -> list[Message]:
        """Return newest messages for a session in chronological order."""

        statement = (
            select(MessageModel)
            .where(MessageModel.session_id == str(session_id))
            .order_by(desc(MessageModel.sequence_number))
            .limit(limit)
        )
        result = await self._session.execute(statement)
        rows = list(result.scalars().all())
        return [self._to_schema(row) for row in reversed(rows)]

    async def get_by_id(self, message_id: MessageId) -> Message | None:
        """Return a message by ID, or None when absent."""

        row = await self._session.get(MessageModel, str(message_id))
        if row is None:
            return None
        return self._to_schema(row)

    async def _create_message(
        self,
        session_id: SessionId,
        role: MessageRole,
        text: str,
    ) -> Message:
        """Create a persisted message using the session sequence counter."""

        session_row = await self._session.get(SessionModel, str(session_id))
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        sequence_number = session_row.next_message_sequence
        row = MessageModel(
            id=f"msg_{uuid4().hex}",
            session_id=str(session_id),
            role=role.value,
            content=text,
            sequence_number=sequence_number,
            created_at=utc_now(),
        )
        session_row.next_message_sequence = sequence_number + 1
        self._session.add(row)
        await self._session.flush()
        return self._to_schema(row)

    def _to_schema(self, row: MessageModel) -> Message:
        """Convert one ORM row into the public Message contract."""

        return Message(
            id=MessageId(row.id),
            session_id=SessionId(row.session_id),
            role=MessageRole(row.role),
            content=row.content,
            sequence_number=row.sequence_number,
            created_at=row.created_at,
            model_name=row.model_name,
            parent_message_id=(
                MessageId(row.parent_message_id)
                if row.parent_message_id is not None
                else None
            ),
        )


__all__ = [
    "InMemoryMessageRepository",
    "MessageRepository",
    "SqlAlchemyMessageRepository",
]