"""Session lifecycle API routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_session_closer
from runtime.sqlalchemy_session_closer import SqlAlchemySessionCloser
from schemas.common import SessionId, UserId

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


@router.post("/close", response_model=SessionCloseResponse)
async def close_session(
    request: SessionCloseRequest,
    closer: Annotated[SqlAlchemySessionCloser, Depends(get_session_closer)],
) -> SessionCloseResponse:
    """Finalize memories and close a SQLAlchemy-backed session."""

    result = await closer.close_session(
        user_id=UserId(request.user_id),
        session_id=SessionId(request.session_id),
        reason=request.reason,
    )
    return SessionCloseResponse(
        session_id=result.session_id,
        candidate_memory_count=result.candidate_memory_count,
        memory_write_count=result.memory_write_count,
        status="closed",
    )


__all__ = ["SessionCloseRequest", "SessionCloseResponse", "router"]
