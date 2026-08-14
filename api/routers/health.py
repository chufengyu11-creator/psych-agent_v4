"""Liveness and dependency readiness API routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from api.health_models import LivenessResponse, ReadinessResponse
from app.dependencies import get_application_runtime, get_health_checker
from runtime.application import ApplicationRuntime
from runtime.health import RuntimeHealthChecker

router = APIRouter()


@router.get("", response_model=LivenessResponse)
async def health_check() -> LivenessResponse:
    """Preserve the legacy process-only health endpoint."""

    return LivenessResponse()


@router.get("/live", response_model=LivenessResponse)
async def liveness_check() -> LivenessResponse:
    """Return process liveness without touching any external service."""

    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness_check(
    runtime: Annotated[ApplicationRuntime, Depends(get_application_runtime)],
    checker: Annotated[RuntimeHealthChecker, Depends(get_health_checker)],
) -> JSONResponse:
    """Return fresh component readiness and 503 for required failures."""

    result = await checker.check_readiness(runtime)
    status_code = (
        status.HTTP_200_OK
        if result.status == "ready"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(
        status_code=status_code,
        content=result.model_dump(mode="json"),
    )
