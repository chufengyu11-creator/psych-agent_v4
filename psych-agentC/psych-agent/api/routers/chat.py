"""Chat API router for processing user turns."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api.request_models import ChatTurnRequest
from api.response_models import ChatTurnResponse
from app.dependencies import get_orchestrator
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import SessionId, UserId

router = APIRouter()


@router.post("/turn", response_model=ChatTurnResponse)
async def create_chat_turn(
    request: ChatTurnRequest,
    orchestrator: Annotated[TurnOrchestrator, Depends(get_orchestrator)],
) -> ChatTurnResponse:
    """Process one user message through the orchestrator."""

    result = await orchestrator.handle_turn(
        user_id=UserId(request.user_id),
        session_id=SessionId(request.session_id),
        text=request.message,
    )
    return ChatTurnResponse(**result.model_dump())
