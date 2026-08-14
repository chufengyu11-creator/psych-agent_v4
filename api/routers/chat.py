"""Chat API router for processing user turns."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from api.request_models import ChatTurnRequest
from api.response_models import ChatTurnResponse
from app.dependencies import get_runtime_orchestrator
from runtime.application import TurnHandler
from schemas.common import SessionId, UserId
from services.timing import bind_new_trace, reset_trace, timing_span
from storage.repositories.errors import (
    SessionNotFoundError,
    SessionOwnershipError,
    SessionUnavailableError,
    UserUnavailableError,
)

router = APIRouter()


@router.post("/turn", response_model=ChatTurnResponse)
async def create_chat_turn(
    request: ChatTurnRequest,
    orchestrator: Annotated[TurnHandler, Depends(get_runtime_orchestrator)],
) -> ChatTurnResponse:
    """Process one user message through the orchestrator."""

    trace_token = bind_new_trace()
    try:
        with timing_span("http.chat_turn"):
            try:
                result = await orchestrator.handle_turn(
                    user_id=UserId(request.user_id),
                    session_id=SessionId(request.session_id),
                    text=request.message,
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
                    detail="Session is not active.",
                ) from error
            return ChatTurnResponse(**result.model_dump())
    finally:
        reset_trace(trace_token)
