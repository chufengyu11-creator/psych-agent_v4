"""FastAPI lifecycle and runtime dependency integration tests."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_runtime_orchestrator
from app.main import create_app
from runtime.application import ApplicationRuntime
from runtime.factory import build_in_memory_orchestrator


def _settings() -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode="in_memory",
        _env_file=None,
    )


def test_lifespan_builds_once_stores_runtime_and_closes_on_shutdown() -> None:
    """Startup should own one runtime for all requests and close it once."""

    built: list[ApplicationRuntime] = []

    async def builder(settings: Settings) -> ApplicationRuntime:
        runtime = ApplicationRuntime(
            settings=settings,
            mode="in_memory",
            orchestrator=build_in_memory_orchestrator(),
        )
        built.append(runtime)
        return runtime

    app = create_app(runtime_builder=builder, settings_provider=_settings)
    with TestClient(app) as client:
        assert app.state.runtime is built[0]
        first = client.post(
            "/chat/turn",
            json={
                "user_id": "api-runtime-user",
                "session_id": "api-runtime-session",
                "message": "I want one artificial planning step.",
            },
        )
        second = client.post(
            "/chat/turn",
            json={
                "user_id": "api-runtime-user",
                "session_id": "api-runtime-session",
                "message": "I want to continue the artificial test.",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(built) == 1
        assert not built[0].closed

    assert built[0].closed
    assert getattr(app.state, "runtime", None) is None


def test_fastapi_dependency_override_remains_available() -> None:
    """Tests may replace the route handler without replacing production wiring."""

    async def builder(settings: Settings) -> ApplicationRuntime:
        return ApplicationRuntime(
            settings=settings,
            mode="in_memory",
            orchestrator=build_in_memory_orchestrator(),
        )

    override = build_in_memory_orchestrator()
    app = create_app(runtime_builder=builder, settings_provider=_settings)
    app.dependency_overrides[get_runtime_orchestrator] = lambda: override
    with TestClient(app) as client:
        response = client.post(
            "/chat/turn",
            json={
                "user_id": "override-user",
                "session_id": "override-session",
                "message": "This is an artificial override test.",
            },
        )
    assert response.status_code == 200
    app.dependency_overrides.clear()


def test_startup_configuration_failure_is_not_replaced_by_fake_runtime() -> None:
    """A failed selected runtime should abort startup without a hidden fallback."""

    async def failing_builder(settings: Settings) -> ApplicationRuntime:
        _ = settings
        raise RuntimeError("safe startup failure")

    app = create_app(runtime_builder=failing_builder, settings_provider=_settings)
    with pytest.raises(RuntimeError, match="safe startup failure"), TestClient(app):
        pass
