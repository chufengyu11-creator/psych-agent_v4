"""FastAPI liveness and readiness route behavior."""

from fastapi.testclient import TestClient

from api.health_models import (
    ComponentHealth,
    ReadinessComponents,
    ReadinessResponse,
)
from app.config import Settings
from app.dependencies import get_health_checker
from app.main import create_app
from runtime.application import ApplicationRuntime
from runtime.factory import build_in_memory_orchestrator

SECRET = "health-route-secret-that-must-not-leak"


class StaticRuntimeHealthChecker:
    """Injected readiness result for HTTP status and serialization tests."""

    def __init__(self, result: ReadinessResponse) -> None:
        self.result = result
        self.calls = 0

    async def check_readiness(
        self,
        runtime: ApplicationRuntime,
    ) -> ReadinessResponse:
        _ = runtime
        self.calls += 1
        return self.result


def _settings() -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode="in_memory",
        redis_url=None,
        llm_api_key=SECRET,
        _env_file=None,
    )


async def _builder(settings: Settings) -> ApplicationRuntime:
    return ApplicationRuntime(
        settings=settings,
        mode="in_memory",
        orchestrator=build_in_memory_orchestrator(),
    )


def test_legacy_health_and_live_are_process_only() -> None:
    """Liveness routes must not invoke the readiness checker or resources."""

    exploding = StaticRuntimeHealthChecker(_not_ready_result())
    app = create_app(runtime_builder=_builder, settings_provider=_settings)
    app.dependency_overrides[get_health_checker] = lambda: exploding
    with TestClient(app) as client:
        legacy = client.get("/health")
        live = client.get("/health/live")

    assert legacy.status_code == 200
    assert legacy.json() == {"status": "ok"}
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert exploding.calls == 0


def test_in_memory_ready_requires_no_external_services() -> None:
    """Default test Runtime should report ready without external connections."""

    app = create_app(runtime_builder=_builder, settings_provider=_settings)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["runtime_mode"] == "in_memory"
    assert payload["components"]["database"]["status"] == "not_required"
    assert payload["components"]["redis"]["status"] == "not_configured"
    assert payload["components"]["llm"]["status"] == "not_required"
    assert SECRET not in response.text


def test_required_failure_returns_503_and_all_component_statuses() -> None:
    """Readiness should preserve every component result with no traceback."""

    checker = StaticRuntimeHealthChecker(_not_ready_result())
    app = create_app(runtime_builder=_builder, settings_provider=_settings)
    app.dependency_overrides[get_health_checker] = lambda: checker
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert set(payload["components"]) == {"database", "redis", "llm"}
    assert payload["components"]["database"]["status"] == "unavailable"
    assert payload["components"]["redis"]["status"] == "timeout"
    assert payload["components"]["llm"]["status"] == "authentication_failed"
    assert "traceback" not in response.text.casefold()
    assert SECRET not in response.text
    assert checker.calls == 1


def test_readiness_schema_is_present_in_openapi() -> None:
    """Health response contracts should be visible without changing chat schemas."""

    app = create_app(runtime_builder=_builder, settings_provider=_settings)
    schema = app.openapi()

    assert "/health/live" in schema["paths"]
    assert "/health/ready" in schema["paths"]
    assert "ReadinessResponse" in schema["components"]["schemas"]


def _not_ready_result() -> ReadinessResponse:
    return ReadinessResponse(
        status="not_ready",
        runtime_mode="sqlalchemy_model",
        components=ReadinessComponents(
            database=ComponentHealth(
                required=True,
                status="unavailable",
                error_code="database_unavailable",
            ),
            redis=ComponentHealth(
                required=True,
                status="timeout",
                error_code="redis_timeout",
            ),
            llm=ComponentHealth(
                required=True,
                status="authentication_failed",
                error_code="llm_authentication",
            ),
        ),
    )
