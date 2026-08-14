"""Session lifecycle and history API routes."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.dependencies import get_application_runtime, get_session_closer
from runtime.application import ApplicationRuntime
from runtime.sqlalchemy_session_closer import SqlAlchemySessionCloser
from schemas.common import SessionId, UserId
from schemas.messages import Message
from storage.repositories.errors import (
    SessionNotFoundError,
    SessionOwnershipError,
    SessionUnavailableError,
    UserUnavailableError,
)
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository

router = APIRouter()


class SessionCloseRequest(BaseModel):
    """External request to finalize and close one owned session."""

    user_id: str
    session_id: str
    reason: str | None = None


class SessionCloseResponse(BaseModel):
    """Public, aggregate-only result of session finalization."""

    session_id: str
    candidate_memory_count: int
    memory_write_count: int
    status: Literal["closed"]


class SessionListItemResponse(BaseModel):
    """User-visible session metadata for history navigation."""

    session_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    current_state_version: int
    current_summary_version: int
    next_message_sequence: int


class SessionListResponse(BaseModel):
    """History page for one user's sessions."""

    sessions: list[SessionListItemResponse]


class SessionMessagesResponse(BaseModel):
    """Messages for one owned session."""

    session_id: str
    messages: list[Message]


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
    user_id: str = Query(min_length=1),
    limit: int = Query(default=30, ge=1, le=100),
) -> SessionListResponse:
    """Return newest sessions for one user."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(UserId(user_id))
        rows = await SqlAlchemySessionRepository(session).list_for_user(
            UserId(user_id),
            limit=limit,
        )
    return SessionListResponse(
        sessions=[SessionListItemResponse(**row.__dict__) for row in rows]
    )


@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
async def list_session_messages(
    session_id: str,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
    user_id: str = Query(min_length=1),
    limit: int = Query(default=200, ge=1, le=500),
) -> SessionMessagesResponse:
    """Return messages for an owned session, including archived sessions."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    owner_id = UserId(user_id)
    owned_session_id = SessionId(session_id)
    async with runtime.session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(owner_id)
        session_repository = SqlAlchemySessionRepository(session)
        try:
            await session_repository.assert_owned_by(owner_id, owned_session_id)
        except SessionNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session was not found for this user.",
            ) from error
        messages = await SqlAlchemyMessageRepository(session).list_for_session(
            owner_id,
            owned_session_id,
            limit=limit,
        )
    return SessionMessagesResponse(session_id=session_id, messages=messages)


@router.post("/close", response_model=SessionCloseResponse)
async def close_session(
    request: SessionCloseRequest,
    closer: Annotated[SqlAlchemySessionCloser, Depends(get_session_closer)],
) -> SessionCloseResponse:
    """Finalize memories and close a SQLAlchemy-backed session."""

    try:
        result = await closer.close_session(
            user_id=UserId(request.user_id),
            session_id=SessionId(request.session_id),
            reason=request.reason,
        )
    except (SessionNotFoundError, SessionOwnershipError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session was not found for this user.",
        ) from error
    except UserUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not available.",
        ) from error
    except SessionUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session cannot be closed.",
        ) from error
    return SessionCloseResponse(
        session_id=result.session_id,
        candidate_memory_count=result.candidate_memory_count,
        memory_write_count=result.memory_write_count,
        status="closed",
    )


def _requires_sqlalchemy_runtime() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Session history requires a SQLAlchemy runtime mode.",
    )


__all__ = [
    "SessionCloseRequest",
    "SessionCloseResponse",
    "SessionListItemResponse",
    "SessionListResponse",
    "SessionMessagesResponse",
    "router",
]
