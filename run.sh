#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:${PATH}"

FE_DIR="${AGENCY_FE_DIR:-"${ROOT_DIR}/../agency-fe"}"
RUN_DIR="${AGENCY_RUN_DIR:-"${ROOT_DIR}/.run"}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_INTERNAL_URL="${AGENCY_INTERNAL_API_BASE_URL:-http://127.0.0.1:${BACKEND_PORT}}"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-agency}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh start
  ./run.sh stop
  ./run.sh status

start:
  Starts Postgres, Redis, and supporting containers; writes ../agency-fe/.env.local
  LAN proxy settings; applies migrations; runs agent setup; starts FastAPI on the
  host; and starts the frontend on 0.0.0.0.

Environment overrides:
  LAN_HOST                       LAN IP/hostname to advertise.
  FRONTEND_PORT                  Frontend port. Defaults to 3000.
  BACKEND_PORT                   Host backend port. Defaults to 8000.
  AGENCY_FE_DIR                  Frontend repo path. Defaults to ../agency-fe.
  AGENCY_INTERNAL_API_BASE_URL   Backend URL used by the frontend server.
  CODEX_HOST_HOME                Host Codex home to sync into the Docker volume.
  AGENCY_SYNC_CODEX_OAUTH        Sync host Codex OAuth files. Defaults to true.
  AGENCY_HOST_BUILD_RUNTIME_IMAGE
                                 Build the backend runtime image. Defaults to true.
EOF
}

detect_lan_host() {
  if [ -n "${LAN_HOST:-}" ]; then
    printf '%s\n' "${LAN_HOST}"
    return 0
  fi

  local ip=""
  ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -z "${ip}" ]; then
    ip="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [ -z "${ip}" ]; then
    local interface=""
    interface="$(route get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
    if [ -n "${interface}" ]; then
      ip="$(ipconfig getifaddr "${interface}" 2>/dev/null || true)"
    fi
  fi
  if [ -z "${ip}" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi

  if [ -z "${ip}" ]; then
    echo "Unable to detect LAN IP. Re-run with LAN_HOST=<your-ip> ./run.sh start" >&2
    return 1
  fi

  printf '%s\n' "${ip}"
}

upsert_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"

  if grep -q "^${key}=" "${file}"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "${file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${file}"
  fi
}

remove_env_value() {
  local file="$1"
  local key="$2"

  if grep -q "^${key}=" "${file}"; then
    sed -i.bak "/^${key}=/d" "${file}"
  fi
}

configure_frontend_lan_env() {
  local lan_host="$1"
  local env_file="${FE_DIR}/.env.local"

  if [ ! -d "${FE_DIR}" ]; then
    echo "Frontend repo not found at ${FE_DIR}. Set AGENCY_FE_DIR to override." >&2
    return 1
  fi

  if [ ! -f "${env_file}" ]; then
    if [ -f "${FE_DIR}/.env" ]; then
      cp "${FE_DIR}/.env" "${env_file}"
    elif [ -f "${FE_DIR}/.env.example" ]; then
      cp "${FE_DIR}/.env.example" "${env_file}"
    else
      touch "${env_file}"
    fi
  fi

  remove_env_value "${env_file}" "NEXTAUTH_URL"
  remove_env_value "${env_file}" "AUTH_URL"
  remove_env_value "${env_file}" "AUTH_TRUST_HOST"
  remove_env_value "${env_file}" "AUTH_SECRET"
  remove_env_value "${env_file}" "NEXTAUTH_SECRET"
  remove_env_value "${env_file}" "NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED"
  remove_env_value "${env_file}" "DEV_AUTH_EMAIL"
  remove_env_value "${env_file}" "DEV_AUTH_PASSWORD"
  remove_env_value "${env_file}" "DEV_AUTH_NAME"
  remove_env_value "${env_file}" "DEV_AUTH_USER_ID"
  upsert_env_value "${env_file}" "NEXT_ALLOWED_DEV_ORIGINS" "${lan_host},localhost,127.0.0.1"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_APP_ENV" "local"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_AGENCY_API_BASE_URL" "/backend"
  upsert_env_value "${env_file}" "LOCAL_BACKEND" "/backend"
  upsert_env_value "${env_file}" "AGENCY_INTERNAL_API_BASE_URL" "${BACKEND_INTERNAL_URL}"

  rm -f "${env_file}.bak"
}

host_python() {
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "${PYTHON}"
    return 0
  fi
  if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  command -v python
}

pid_is_running() {
  local pid_file="$1"

  [ -f "${pid_file}" ] || return 1
  local pid=""
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  [ -n "${pid}" ] || return 1
  kill -0 "${pid}" >/dev/null 2>&1
}

stop_pid_file() {
  local name="$1"
  local pid_file="$2"

  if pid_is_running "${pid_file}"; then
    local pid=""
    pid="$(cat "${pid_file}")"
    echo "Stopping ${name} process ${pid}..."
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 1
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "${pid_file}"
}

kill_port() {
  local port="$1"

  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"${port}" | xargs -r kill >/dev/null 2>&1 || true
  fi
}

sync_codex_oauth_to_volume() {
  if [ "${AGENCY_SYNC_CODEX_OAUTH:-true}" != "true" ]; then
    return 0
  fi

  local codex_host_home="${CODEX_HOST_HOME:-"${HOME}/.codex"}"

  if [ ! -f "${codex_host_home}/auth.json" ]; then
    echo "Host Codex auth not found at ${codex_host_home}/auth.json; run codex login or set CODEX_HOST_HOME." >&2
    return 0
  fi

  echo "Syncing host Codex OAuth into Docker Codex volume..."
  docker compose run --rm --no-deps -v "${codex_host_home}:/host-codex:ro" backend sh -lc '
    mkdir -p /codex && chmod 700 /codex
    cp /host-codex/auth.json /codex/auth.json
    chmod 600 /codex/auth.json
    if [ -f /host-codex/config.toml ]; then
      cp /host-codex/config.toml /codex/config.toml
      chmod 600 /codex/config.toml
    fi
  '
}

export_host_backend_env() {
  export AGENCY_BACKEND_RUN_MODE="${AGENCY_BACKEND_RUN_MODE:-host}"
  export APP_ENV="${APP_ENV:-development}"
  export REQUIRE_DATABASE="${REQUIRE_DATABASE:-true}"
  export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/agency}"
  export REDIS_HOST="${REDIS_HOST:-localhost}"
  export REDIS_PORT="${REDIS_PORT:-6379}"
  export INTEGRATIONS_RUNTIME_ENABLED="${INTEGRATIONS_RUNTIME_ENABLED:-true}"
  export EXECUTION_ISOLATION_ENABLED="${EXECUTION_ISOLATION_ENABLED:-true}"
  export EXECUTION_RUNTIME_DATABASE_URL="${EXECUTION_RUNTIME_DATABASE_URL:-postgresql://postgres:postgres@postgres:5432/agency}"
  export EXECUTION_RUNTIME_BASE_IMAGE="${EXECUTION_RUNTIME_BASE_IMAGE:-agency-backend:latest}"
  export EXECUTION_CONTAINER_NETWORK="${EXECUTION_CONTAINER_NETWORK:-${COMPOSE_PROJECT}_default}"
  export EXECUTION_CONTAINER_WORKDIR="${EXECUTION_CONTAINER_WORKDIR:-/app}"
  export EXECUTION_CODEX_CLI_CWD="${EXECUTION_CODEX_CLI_CWD:-${EXECUTION_CONTAINER_WORKDIR}}"
  export CODEX_CLI_CWD="${CODEX_CLI_CWD:-${ROOT_DIR}}"
  export CODEX_HOME_VOLUME="${CODEX_HOME_VOLUME:-agency_codex_home}"
  export AGENCY_BACKEND_WORKSPACE="${AGENCY_BACKEND_WORKSPACE:-/workspace/agency}"
  export AGENCY_BACKEND_HOST_WORKSPACE="${AGENCY_BACKEND_HOST_WORKSPACE:-${ROOT_DIR}}"
  export AGENCY_FRONTEND_WORKSPACE="${AGENCY_FRONTEND_WORKSPACE:-/workspace/agency-fe}"
  export AGENCY_FRONTEND_HOST_WORKSPACE="${AGENCY_FRONTEND_HOST_WORKSPACE:-${FE_DIR}}"
}

load_dotenv() {
  if [ -f "${ROOT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${ROOT_DIR}/.env"
    set +a
  fi
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"

  for _ in $(seq 1 "${attempts}"); do
    if command -v curl >/dev/null 2>&1 && curl -fsS "${url}" >/dev/null 2>&1; then
      echo "${label} is ready."
      return 0
    fi
    sleep 1
  done

  echo "${label} did not respond at ${url}. Check logs in ${RUN_DIR}." >&2
  return 1
}

start_backend() {
  local python_bin="$1"

  if pid_is_running "${RUN_DIR}/backend.pid"; then
    echo "Host backend is already running with PID $(cat "${RUN_DIR}/backend.pid")."
    return 0
  fi

  echo "Starting FastAPI backend on http://127.0.0.1:${BACKEND_PORT}..."
  (
    cd "${ROOT_DIR}"
    export_host_backend_env
    exec "${python_bin}" -m uvicorn app:app --host 0.0.0.0 --port "${BACKEND_PORT}" --reload
  ) >"${RUN_DIR}/backend.log" 2>&1 &
  echo "$!" >"${RUN_DIR}/backend.pid"
  wait_for_http "http://127.0.0.1:${BACKEND_PORT}/health" "Backend"
}

start_frontend() {
  if pid_is_running "${RUN_DIR}/frontend.pid"; then
    echo "Frontend is already running with PID $(cat "${RUN_DIR}/frontend.pid")."
    return 0
  fi

  echo "Starting frontend on http://0.0.0.0:${FRONTEND_PORT}..."
  (
    cd "${FE_DIR}"
    if command -v npm >/dev/null 2>&1; then
      exec npm run dev:lan -- -p "${FRONTEND_PORT}"
    fi
    if [ -x "./node_modules/.bin/next" ]; then
      exec ./node_modules/.bin/next dev -H 0.0.0.0 -p "${FRONTEND_PORT}"
    fi
    echo "npm is not available and ./node_modules/.bin/next was not found." >&2
    exit 127
  ) >"${RUN_DIR}/frontend.log" 2>&1 &
  echo "$!" >"${RUN_DIR}/frontend.pid"
}

run_agent_setup() {
  local python_bin="$1"

  if "${python_bin}" scripts/setup.py all --non-interactive; then
    return 0
  fi

  echo "Warning: agent setup did not complete." >&2
  echo "Configure a model profile in the frontend, or set MAIN_AGENT_BOOTSTRAP_* values in .env and run start again." >&2
}

start_all() {
  local lan_host=""
  local python_bin=""

  mkdir -p "${RUN_DIR}"
  cd "${ROOT_DIR}"

  if [ ! -f .env ]; then
    cp .env.example .env
  fi
  load_dotenv

  lan_host="$(detect_lan_host)"
  python_bin="$(host_python)"
  configure_frontend_lan_env "${lan_host}"
  export_host_backend_env

  echo "Starting Postgres, Redis, and supporting containers..."
  docker compose up -d --build postgres redis langfuse-web

  if [ "${AGENCY_HOST_BUILD_RUNTIME_IMAGE:-true}" = "true" ]; then
    echo "Building backend runtime image for isolated workers..."
    docker compose build backend
  fi
  sync_codex_oauth_to_volume

  echo "Applying Alembic migrations..."
  "${python_bin}" -m alembic upgrade head

  echo "Running agent setup..."
  run_agent_setup "${python_bin}"

  start_backend "${python_bin}"
  start_frontend

  echo
  echo "Agency is starting."
  echo "Frontend: http://${lan_host}:${FRONTEND_PORT}"
  echo "Backend:  http://127.0.0.1:${BACKEND_PORT}"
  echo "Logs:     ${RUN_DIR}"
}

stop_all() {
  mkdir -p "${RUN_DIR}"
  cd "${ROOT_DIR}"

  stop_pid_file "frontend" "${RUN_DIR}/frontend.pid"
  stop_pid_file "backend" "${RUN_DIR}/backend.pid"
  kill_port "${FRONTEND_PORT}"
  kill_port "${BACKEND_PORT}"

  echo "Stopping Agency containers..."
  docker compose down
}

show_pid_status() {
  local name="$1"
  local pid_file="$2"

  if pid_is_running "${pid_file}"; then
    echo "${name}: running (PID $(cat "${pid_file}"))"
  else
    echo "${name}: stopped"
  fi
}

show_status() {
  local env_file="${FE_DIR}/.env.local"

  cd "${ROOT_DIR}"
  show_pid_status "Backend" "${RUN_DIR}/backend.pid"
  show_pid_status "Frontend" "${RUN_DIR}/frontend.pid"

  echo
  echo "Containers:"
  docker compose ps || true

  echo
  echo "Backend health:"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" || true
    echo
  else
    echo "curl is not available for the health check."
  fi

  echo
  echo "Port listeners:"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -i tcp:"${BACKEND_PORT}" -sTCP:LISTEN || true
    lsof -nP -i tcp:"${FRONTEND_PORT}" -sTCP:LISTEN || true
  else
    echo "lsof is not available for the port check."
  fi

  echo
  echo "Frontend LAN env (${env_file}):"
  if [ -f "${env_file}" ]; then
    grep -E '^(NEXT_ALLOWED_DEV_ORIGINS|NEXT_PUBLIC_APP_ENV|NEXT_PUBLIC_AGENCY_API_BASE_URL|LOCAL_BACKEND|AGENCY_INTERNAL_API_BASE_URL)=' "${env_file}" || true
  else
    echo "Missing. Run ./run.sh start to generate it."
  fi

  echo
  echo "Logs:"
  echo "Backend:  ${RUN_DIR}/backend.log"
  echo "Frontend: ${RUN_DIR}/frontend.log"
}

command="${1:-start}"
case "${command}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  status)
    show_status
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
