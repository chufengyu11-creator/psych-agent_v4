# Linux API Deployment

This guide prepares a single FastAPI API process managed by systemd:

```text
systemd -> FastAPI ApplicationRuntime -> PostgreSQL + Redis + OpenAI-compatible LLM
```

The LLM may be DeepSeek or a locally served Qwen/vLLM endpoint. Provider changes
are environment-only. PostgreSQL, Redis, and Qwen/vLLM must already be installed,
secured, and running; this repository does not provision those system services.

## Prerequisites and filesystem layout

Use the latest Python 3.11.x and a dedicated, non-login Linux user such as
`psych-agent`. Example paths in the provided templates are:

```text
/opt/psych-agent/current              application checkout
/opt/psych-agent/venv                 Python virtual environment
/etc/psych-agent/psych-agent.env      secrets and deployment settings
/etc/systemd/system/psych-agent-api.service
```

Adjust every placeholder path consistently before installation. Do not run the
API as root. The application checkout and virtual environment may be managed by
the deployment account while the service runs as `psych-agent`.

PostgreSQL, Redis, and LLM ports should listen only on loopback or a protected
private network. Never expose PostgreSQL, Redis, or vLLM directly to the public
Internet.

## Environment configuration

Copy `deploy/env/psych-agent.env.example` to the protected path and replace all
placeholders:

```bash
sudo install -d -m 0750 -o root -g psych-agent /etc/psych-agent
sudo install -m 0600 -o root -g psych-agent \
  deploy/env/psych-agent.env.example /etc/psych-agent/psych-agent.env
sudo editor /etc/psych-agent/psych-agent.env
```

Keep the file at mode `600`. Never commit it. Database passwords containing URL
reserved characters must be percent-encoded inside `DATABASE_URL`. The served
model ID must exactly match `MAIN_MODEL_NAME`, `STRUCTURED_MODEL_NAME`, and
`SAFETY_MODEL_NAME`.

For local Qwen/vLLM, keep `LLM_THINKING_MODE=omit`. To use DeepSeek, change only
the generic `LLM_*` variables and store its real API key in the protected env
file or a secret manager. Do not put secrets in the systemd unit.

## Install and migrate

Create the Python 3.11 environment and install the project:

```bash
python3.11 -m venv /opt/psych-agent/venv
/opt/psych-agent/venv/bin/python -m pip install --upgrade .
```

Load the protected variables without printing them, then migrate once as an
explicit deployment action:

```bash
set -a
source /etc/psych-agent/psych-agent.env
set +a
/opt/psych-agent/venv/bin/alembic upgrade head
/opt/psych-agent/venv/bin/alembic current
```

Formal PostgreSQL initialization must use Alembic. Do not substitute
`Base.metadata.create_all()`. The systemd unit intentionally does not run a
migration, so an automatic process restart cannot repeatedly execute schema
changes.

## Preflight

From the application checkout, with the protected environment loaded:

```bash
/opt/psych-agent/venv/bin/python scripts/preflight_linux.py
/opt/psych-agent/venv/bin/python scripts/preflight_linux.py --json
```

The preflight checks, in order:

1. Python is at least 3.11;
2. shared `Settings` loads successfully;
3. the configured PostgreSQL connection, Alembic head, core tables, and key
   schema objects through `check_database_connection`;
4. Redis `PING` through `check_redis_connection`;
5. LLM `GET /models` and the configured structured model name through
   `check_llm_endpoint --models-only`;
6. the project and Alembic resource directories are readable and the process
   temporary directory accepts a create/delete probe.

It does not call an Agent or chat completion and does not write database rows or
Redis keys. Output contains status and exit-code fields only, not passwords,
keys, complete URLs, model lists, or raw exceptions. A required failure returns
non-zero.

## Install and operate systemd

Review `deploy/systemd/psych-agent-api.service`, replace paths and the
user/group if needed, then install it:

```bash
sudo install -m 0644 deploy/systemd/psych-agent-api.service \
  /etc/systemd/system/psych-agent-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now psych-agent-api.service
```

Common operations:

```bash
sudo systemctl status psych-agent-api.service
sudo systemctl restart psych-agent-api.service
sudo systemctl stop psych-agent-api.service
sudo journalctl -u psych-agent-api.service -n 200 --no-pager
sudo journalctl -u psych-agent-api.service -f
```

The unit runs preflight before each service start and restarts only after
unexpected failure. It never performs Alembic migration. Shutdown allows the
FastAPI lifespan to close Redis, LLM HTTP, and SQLAlchemy resources.

## Conservative deploy or update flow

After reviewing the defaults at the top of the script:

```bash
chmod +x deploy/scripts/deploy_or_start.sh
APP_DIR=/opt/psych-agent/current \
VENV_DIR=/opt/psych-agent/venv \
ENV_FILE=/etc/psych-agent/psych-agent.env \
deploy/scripts/deploy_or_start.sh
```

The script stops on the first failure and performs this sequence:

```text
validate env file -> install/update package -> Alembic upgrade/current
-> preflight -> start/restart systemd -> liveness -> readiness
```

It does not create PostgreSQL users/databases, install PostgreSQL/Redis/Qwen,
downgrade or delete a database, remove user data, or run business smoke tests.
When rolling application code back, do not blindly run `alembic downgrade`.
Review database compatibility and backups as a separate operational change.

## Acceptance checklist

Run these in a Linux staging/acceptance environment using Python 3.11:

```bash
python --version
python -m pip check
ruff check .
mypy .
pytest

alembic upgrade head
alembic current

python scripts/check_database_connection.py
python scripts/check_redis_connection.py
python scripts/check_llm_endpoint.py
python scripts/preflight_linux.py

python scripts/smoke_model_assisted_database_corpus.py --database postgres --ascii
python scripts/smoke_session_close_memory.py --database postgres --ascii

curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

PostgreSQL business smokes must run only on a dedicated staging/acceptance
database with artificial data. Do not casually run them against a production
database containing real users. The Windows test environment cannot replace
real Linux, systemd, PostgreSQL, Redis, and Qwen/vLLM acceptance.

## Current boundaries

Workers and a Redis-backed TaskQueue are not implemented. `NoopTaskQueue`
remains active. B-5B sequence/version concurrency control is also still
deferred. This deployment asset starts only the existing API Runtime; it does
not claim worker or concurrency-control readiness.
