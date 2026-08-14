"""Static safety tests for copyable Linux deployment assets."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_systemd_unit_is_non_root_and_does_not_repeat_migrations() -> None:
    """Automatic restart may preflight but must not rerun Alembic."""

    content = (PROJECT_ROOT / "deploy/systemd/psych-agent-api.service").read_text()

    assert "User=psych-agent" in content
    assert "WorkingDirectory=/opt/psych-agent/current" in content
    assert "EnvironmentFile=/etc/psych-agent/psych-agent.env" in content
    assert "ExecStartPre=/opt/psych-agent/venv/bin/python scripts/preflight_linux.py" in content
    assert "uvicorn app.main:app" in content
    assert "Restart=on-failure" in content
    assert "alembic" not in content.casefold()
    assert "password" not in content.casefold()


def test_deployment_script_has_safe_order_and_no_destructive_database_commands() -> None:
    """The explicit deploy path should stop on failure and migrate only upward."""

    content = (PROJECT_ROOT / "deploy/scripts/deploy_or_start.sh").read_text()
    install_at = content.index('"${PYTHON}" -m pip install --upgrade .')
    migrate_at = content.index('"${ALEMBIC}" upgrade head')
    preflight_at = content.index('"${PYTHON}" scripts/preflight_linux.py')
    systemd_at = content.index('systemctl daemon-reload')
    live_at = content.index('wait_for_health "/health/live"')
    ready_at = content.index('wait_for_health "/health/ready"')

    assert content.startswith("#!/usr/bin/env bash\nset -euo pipefail")
    assert install_at < migrate_at < preflight_at < systemd_at < live_at < ready_at
    assert "alembic downgrade" not in content
    assert "dropdb" not in content
    assert "DROP DATABASE" not in content
    assert "Base.metadata" not in content


def test_linux_environment_template_contains_placeholders_only() -> None:
    """The committed template must describe production without real credentials."""

    content = (PROJECT_ROOT / "deploy/env/psych-agent.env.example").read_text()

    assert "APP_ENV=production" in content
    assert "APP_RUNTIME_MODE=sqlalchemy_model" in content
    assert "REDIS_REQUIRED=true" in content
    assert "LLM_THINKING_MODE=omit" in content
    assert "replace-me" in content
    assert "replace-with-served-model" in content
