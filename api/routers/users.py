"""User preference API routes."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.dependencies import get_application_runtime
from runtime.application import ApplicationRuntime
from schemas.common import MemoryId, UserId
from storage.models.user import UserModel
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository

router = APIRouter()
MemoryStatusFilter = Literal["active", "pending_confirmation", "all"]


class UserMemorySettingsRequest(BaseModel):
    """Request body for updating long-term memory consent."""

    enabled: bool


class UserMemorySettingsResponse(BaseModel):
    """Public memory setting for one user."""

    user_id: str
    memory_enabled: bool


class PendingMemoryConfirmationRequest(BaseModel):
    """A user's explicit decision on a pending memory write."""

    confirmed: bool


class PendingMemoryConfirmationResponse(BaseModel):
    """The resulting lifecycle state after resolving a pending memory."""

    memory_id: str
    confirmed: bool
    status: str
    superseded_memory_id: str | None = None


class UserMemoryRecordResponse(BaseModel):
    """One long-term memory exposed by the existing V4 management UI."""

    id: str
    memory_type: str
    content: str
    sensitivity: str
    confidence: float
    source_message_ids: list[str]
    source_type: str
    status: str
    requires_user_confirmation: bool
    user_confirmed: bool
    reinforcement_count: int
    created_at: datetime
    updated_at: datetime


class UserMemoryListResponse(BaseModel):
    memories: list[UserMemoryRecordResponse]


class UserMemoryMutationResponse(BaseModel):
    memory_id: str
    status: str


@router.get("/{user_id}/memory", response_model=UserMemorySettingsResponse)
async def get_user_memory_settings(
    user_id: str,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> UserMemorySettingsResponse:
    """Return whether long-term memory writes are enabled for a user."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(UserId(user_id))
        user = await session.get(UserModel, user_id)
        return UserMemorySettingsResponse(
            user_id=user_id,
            memory_enabled=bool(user and user.memory_enabled),
        )


@router.put("/{user_id}/memory", response_model=UserMemorySettingsResponse)
async def update_user_memory_settings(
    user_id: str,
    request: UserMemorySettingsRequest,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> UserMemorySettingsResponse:
    """Update whether long-term memory writes are enabled for a user."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        await SqlAlchemyUserRepository(session).ensure_user(UserId(user_id))
        user = await session.get(UserModel, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User was not found after initialization",
            )
        user.memory_enabled = request.enabled
        await session.commit()
        return UserMemorySettingsResponse(
            user_id=user_id,
            memory_enabled=user.memory_enabled,
        )


@router.get("/{user_id}/memories", response_model=UserMemoryListResponse)
async def list_user_memories(
    user_id: str,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
    status_filter: Annotated[MemoryStatusFilter, Query(alias="status")] = "active",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> UserMemoryListResponse:
    """Keep V4's paged/status-filtered memory panel API."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        owner_id = UserId(user_id)
        await SqlAlchemyUserRepository(session).ensure_user(owner_id)
        records = await SqlAlchemyMemoryRepository(session).list_records(
            owner_id,
            status=None if status_filter == "all" else status_filter,
            limit=limit,
        )
    return UserMemoryListResponse(
        memories=[UserMemoryRecordResponse(**record.__dict__) for record in records]
    )


@router.post(
    "/{user_id}/memories/{memory_id}/confirmation",
    response_model=PendingMemoryConfirmationResponse,
)
async def resolve_pending_memory_confirmation(
    user_id: str,
    memory_id: str,
    request: PendingMemoryConfirmationRequest,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> PendingMemoryConfirmationResponse:
    """Apply a user's confirmation or rejection of one pending memory."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        result = await SqlAlchemyMemoryRepository(
            session
        ).resolve_pending_confirmation(
            UserId(user_id),
            MemoryId(memory_id),
            confirmed=request.confirmed,
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pending memory was not found for this user.",
            )
        await session.commit()
        return PendingMemoryConfirmationResponse(
            memory_id=str(result.memory_id),
            confirmed=result.confirmed,
            status=result.status,
            superseded_memory_id=(
                str(result.superseded_memory_id)
                if result.superseded_memory_id is not None
                else None
            ),
        )


@router.post(
    "/{user_id}/memories/{memory_id}/confirm",
    response_model=PendingMemoryConfirmationResponse,
    include_in_schema=False,
)
async def confirm_pending_memory_legacy(
    user_id: str,
    memory_id: str,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> PendingMemoryConfirmationResponse:
    """Keep cached V4 pages working while they migrate to ``/confirmation``."""

    return await resolve_pending_memory_confirmation(
        user_id,
        memory_id,
        PendingMemoryConfirmationRequest(confirmed=True),
        runtime,
    )


@router.delete("/{user_id}/memories/{memory_id}", response_model=UserMemoryMutationResponse)
async def delete_user_memory(
    user_id: str,
    memory_id: str,
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
) -> UserMemoryMutationResponse:
    """Keep V4's soft-delete endpoint alongside atomic confirmation."""

    if runtime.session_factory is None:
        raise _requires_sqlalchemy_runtime()
    async with runtime.session_factory() as session:
        owner_id = UserId(user_id)
        await SqlAlchemyUserRepository(session).ensure_user(owner_id)
        if not await SqlAlchemyMemoryRepository(session).soft_delete(
            owner_id, MemoryId(memory_id)
        ):
            raise HTTPException(status_code=404, detail="Memory was not found for this user.")
        await session.commit()
    return UserMemoryMutationResponse(memory_id=memory_id, status="deleted")


def _requires_sqlalchemy_runtime() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="User memory settings require a SQLAlchemy runtime mode.",
    )


__all__ = [
    "UserMemorySettingsRequest",
    "UserMemorySettingsResponse",
    "confirm_pending_memory_legacy",
    "PendingMemoryConfirmationRequest",
    "PendingMemoryConfirmationResponse",
    "UserMemoryListResponse",
    "UserMemoryMutationResponse",
    "UserMemoryRecordResponse",
    "router",
]
