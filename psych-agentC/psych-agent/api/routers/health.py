"""Health-check API router."""

from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def health_check() -> dict[str, str]:
    """Return a minimal liveness response."""

    return {"status": "ok"}
