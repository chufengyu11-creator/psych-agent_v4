#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/psych-agent/current}"
VENV_DIR="${VENV_DIR:-/opt/psych-agent/venv}"
ENV_FILE="${ENV_FILE:-/etc/psych-agent/psych-agent.env}"
SERVICE_NAME="${SERVICE_NAME:-psych-agent-api.service}"
PYTHON_BOOTSTRAP="${PYTHON_BOOTSTRAP:-python3.11}"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "Application directory does not exist." >&2
  exit 2
fi
if [[ ! -r "${ENV_FILE}" ]]; then
  echo "Environment file is missing or unreadable." >&2
  exit 2
fi

load_environment_file() {
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "${line}" != *"="* || ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "Environment file contains an invalid line." >&2
      exit 2
    fi
    export "${key}=${value}"
  done < "${ENV_FILE}"
}

load_environment_file

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  command -v "${PYTHON_BOOTSTRAP}" >/dev/null
  "${PYTHON_BOOTSTRAP}" -m venv "${VENV_DIR}"
fi

PYTHON="${VENV_DIR}/bin/python"
ALEMBIC="${VENV_DIR}/bin/alembic"

cd "${APP_DIR}"
"${PYTHON}" -m pip install --upgrade .
"${ALEMBIC}" upgrade head
"${ALEMBIC}" current
"${PYTHON}" scripts/preflight_linux.py

if (( EUID == 0 )); then
  PRIVILEGE=()
else
  command -v sudo >/dev/null
  PRIVILEGE=(sudo)
fi

"${PRIVILEGE[@]}" systemctl daemon-reload
if "${PRIVILEGE[@]}" systemctl is-active --quiet "${SERVICE_NAME}"; then
  "${PRIVILEGE[@]}" systemctl restart "${SERVICE_NAME}"
else
  "${PRIVILEGE[@]}" systemctl enable --now "${SERVICE_NAME}"
fi

HEALTH_BASE_URL="http://${HEALTH_HOST}:${APP_PORT}"
wait_for_health() {
  local endpoint="$1"
  local attempt
  for attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 3 \
      "${HEALTH_BASE_URL}${endpoint}" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "Health check failed: ${endpoint}" >&2
  return 1
}

wait_for_health "/health/live"
wait_for_health "/health/ready"
echo "Deployment checks completed successfully."
