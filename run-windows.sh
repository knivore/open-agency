#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FE_DIR="${AGENCY_FE_DIR:-"${ROOT_DIR}/../agency-fe"}"
RUN_DIR="${AGENCY_RUN_DIR:-/tmp/open-agency-run}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_INTERNAL_URL="${AGENCY_INTERNAL_API_BASE_URL:-http://127.0.0.1:${BACKEND_PORT}}"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-agency}"

pause_on_error() {
  local exit_code="$1"
  local line_no="$2"

  echo
  echo "run-windows.sh failed on line ${line_no} with exit code ${exit_code}."
  echo "Run it from Git Bash with './run-windows.sh start' to see the full output."
  if [ -n "${RUN_DIR:-}" ]; then
    echo "Logs directory: ${RUN_DIR}"
  fi
  if [ -t 0 ] && [ "${AGENCY_NO_PAUSE_ON_ERROR:-false}" != "true" ]; then
    echo
    read -r -p "Press Enter to close..."
  fi
  exit "${exit_code}"
}

trap 'pause_on_error "$?" "$LINENO"' ERR

usage() {
  cat <<'EOF'
Usage:
  ./run-windows.sh start
  ./run-windows.sh stop
  ./run-windows.sh status

start:
  Starts Postgres, Redis, and supporting containers; writes ../agency-fe/.env.local
  LAN proxy settings; applies migrations; runs agent setup; starts FastAPI on the
  host; and starts the frontend on 0.0.0.0.

Environment:
  AGENCY_REQUIRE_FRONTEND=true  Fail startup when the sibling frontend cannot be started.
EOF
}

run_powershell() {
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "$1" | tr -d '\r'
  elif command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -Command "$1" | tr -d '\r'
  else
    return 127
  fi
}

ensure_run_dir() {
  if mkdir -p "${RUN_DIR}" 2>/dev/null; then
    return 0
  fi

  local fallback="${AGENCY_RUN_DIR_FALLBACK:-/tmp/open-agency-run}"
  echo "Could not create ${RUN_DIR}; using ${fallback} for logs and PID files." >&2
  RUN_DIR="${fallback}"
  mkdir -p "${RUN_DIR}"
}

detect_lan_host() {
  if [ -n "${LAN_HOST:-}" ]; then
    printf '%s\n' "${LAN_HOST}"
    return 0
  fi

  local ps_command=""
  local ip=""
  ps_command='$configs = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.IPv4DefaultGateway -and $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*WSL*" }; $ip = $configs | ForEach-Object { $_.IPv4Address | Select-Object -First 1 -ExpandProperty IPAddress } | Where-Object { $_ -notlike "127.*" -and $_ -notlike "169.254.*" } | Select-Object -First 1; if (-not $ip) { $ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*WSL*" } | Sort-Object InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress }; if ($ip) { $ip }'
  ip="$(run_powershell "${ps_command}" 2>/dev/null | sed -n '1p' || true)"

  if [ -z "${ip}" ] && command -v ipconfig >/dev/null 2>&1; then
    ip="$(
      ipconfig 2>/dev/null |
        tr -d '\r' |
        awk '
          /adapter vEthernet/ || /adapter .*WSL/ { skip=1 }
          /adapter / && !/adapter vEthernet/ && !/adapter .*WSL/ { skip=0 }
          !skip && /IPv4 Address/ {
            sub(/.*: /, "", $0)
            if ($0 !~ /^127\./ && $0 !~ /^169\.254\./) {
              print $0
              exit
            }
          }
        '
    )"
  fi

  if [ -z "${ip}" ]; then
    echo "Unable to detect LAN IP. Re-run with LAN_HOST=<your-ip> ./run-windows.sh start" >&2
    return 1
  fi

  printf '%s\n' "${ip}"
}

configure_frontend_lan_env() {
  local lan_host="$1"
  local env_file="${FE_DIR}/.env.local"
  local env_file_win=""
  local env_source_win=""
  local backend_internal_url="${BACKEND_INTERNAL_URL}"
  local generated_env="${RUN_DIR}/agency-fe.env.local"

  if [ ! -d "${FE_DIR}" ]; then
    echo "Frontend repo not found at ${FE_DIR}. Set AGENCY_FE_DIR to override." >&2
    return 1
  fi

  env_file_win="$(cygpath -w "${env_file}")"
  if [ -f "${FE_DIR}/.env" ]; then
    env_source_win="$(cygpath -w "${FE_DIR}/.env")"
  elif [ -f "${FE_DIR}/.env.example" ]; then
    env_source_win="$(cygpath -w "${FE_DIR}/.env.example")"
  fi

  ENV_FILE_WIN="${env_file_win}" \
  ENV_SOURCE_WIN="${env_source_win}" \
  LAN_HOST_VALUE="${lan_host}" \
  BACKEND_INTERNAL_URL_VALUE="${backend_internal_url}" \
  run_powershell '
    $envFile = $env:ENV_FILE_WIN
    $source = $env:ENV_SOURCE_WIN
    $parent = Split-Path -Parent $envFile
    if (-not (Test-Path -LiteralPath $parent)) {
      throw "Frontend env directory does not exist: $parent"
    }
    if (-not (Test-Path -LiteralPath $envFile)) {
      if ($source -and (Test-Path -LiteralPath $source)) {
        Copy-Item -LiteralPath $source -Destination $envFile
      } else {
        New-Item -ItemType File -Path $envFile -Force | Out-Null
      }
    }

    $remove = @(
      "NEXTAUTH_URL",
      "AUTH_URL",
      "AUTH_TRUST_HOST",
      "AUTH_SECRET",
      "NEXTAUTH_SECRET",
      "NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED",
      "DEV_AUTH_EMAIL",
      "DEV_AUTH_PASSWORD",
      "DEV_AUTH_NAME",
      "DEV_AUTH_USER_ID"
    )
    $set = [ordered]@{
      NEXT_ALLOWED_DEV_ORIGINS = "$($env:LAN_HOST_VALUE),localhost,127.0.0.1"
      NEXT_PUBLIC_APP_ENV = "local"
      NEXT_PUBLIC_AGENCY_API_BASE_URL = "/backend"
      LOCAL_BACKEND = "/backend"
      AGENCY_INTERNAL_API_BASE_URL = $env:BACKEND_INTERNAL_URL_VALUE
    }

    $lines = @()
    if (Test-Path -LiteralPath $envFile) {
      $lines = @(Get-Content -LiteralPath $envFile)
    }
    $keysToReplace = @{}
    foreach ($key in $remove) { $keysToReplace[$key] = $true }
    foreach ($key in $set.Keys) { $keysToReplace[$key] = $true }

    $next = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
      if ($line -match "^\s*([^#=\s]+)=") {
        $key = $Matches[1]
        if ($keysToReplace.ContainsKey($key)) { continue }
      }
      $next.Add($line)
    }
    foreach ($key in $set.Keys) {
      $next.Add("$key=$($set[$key])")
    }
    Set-Content -LiteralPath $envFile -Value $next -Encoding UTF8
  ' >/dev/null && return 0

  echo "Warning: unable to update frontend env file: ${env_file_win}" >&2
  echo "Startup will continue, but the frontend may use stale proxy settings." >&2
  echo "Writing the intended env file to ${generated_env} instead." >&2

  ENV_FILE_WIN="$(cygpath -w "${generated_env}")" \
  ENV_SOURCE_WIN="${env_source_win}" \
  LAN_HOST_VALUE="${lan_host}" \
  BACKEND_INTERNAL_URL_VALUE="${backend_internal_url}" \
  run_powershell '
    $envFile = $env:ENV_FILE_WIN
    $source = $env:ENV_SOURCE_WIN
    $parent = Split-Path -Parent $envFile
    if (-not (Test-Path -LiteralPath $parent)) {
      throw "Frontend env directory does not exist: $parent"
    }
    if (-not (Test-Path -LiteralPath $envFile)) {
      if ($source -and (Test-Path -LiteralPath $source)) {
        Copy-Item -LiteralPath $source -Destination $envFile
      } else {
        New-Item -ItemType File -Path $envFile -Force | Out-Null
      }
    }

    $remove = @(
      "NEXTAUTH_URL",
      "AUTH_URL",
      "AUTH_TRUST_HOST",
      "AUTH_SECRET",
      "NEXTAUTH_SECRET",
      "NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED",
      "DEV_AUTH_EMAIL",
      "DEV_AUTH_PASSWORD",
      "DEV_AUTH_NAME",
      "DEV_AUTH_USER_ID"
    )
    $set = [ordered]@{
      NEXT_ALLOWED_DEV_ORIGINS = "$($env:LAN_HOST_VALUE),localhost,127.0.0.1"
      NEXT_PUBLIC_APP_ENV = "local"
      NEXT_PUBLIC_AGENCY_API_BASE_URL = "/backend"
      LOCAL_BACKEND = "/backend"
      AGENCY_INTERNAL_API_BASE_URL = $env:BACKEND_INTERNAL_URL_VALUE
    }

    $lines = @()
    if (Test-Path -LiteralPath $envFile) {
      $lines = @(Get-Content -LiteralPath $envFile)
    }
    $keysToReplace = @{}
    foreach ($key in $remove) { $keysToReplace[$key] = $true }
    foreach ($key in $set.Keys) { $keysToReplace[$key] = $true }

    $next = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
      if ($line -match "^\s*([^#=\s]+)=") {
        $key = $Matches[1]
        if ($keysToReplace.ContainsKey($key)) { continue }
      }
      $next.Add($line)
    }
    foreach ($key in $set.Keys) {
      $next.Add("$key=$($set[$key])")
    }
    Set-Content -LiteralPath $envFile -Value $next -Encoding UTF8
  ' >/dev/null

  echo "To apply it manually from PowerShell:" >&2
  echo "  Copy-Item \"$(cygpath -w "${generated_env}")\" \"${env_file_win}\" -Force" >&2
  echo "If Windows permissions are the issue, run:" >&2
  echo "  icacls \"${env_file_win}\" /grant \"\$env:USERNAME:F\"" >&2
}

host_python() {
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "${PYTHON}"
    return 0
  fi
  if [ -x "${ROOT_DIR}/.venv/Scripts/python.exe" ]; then
    printf '%s\n' "${ROOT_DIR}/.venv/Scripts/python.exe"
    return 0
  fi
  if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  command -v python3
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
    kill -9 "${pid}" >/dev/null 2>&1 || true
  fi
  rm -f "${pid_file}"
}

kill_port() {
  local port="$1"
  run_powershell "\$connections=Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue; \$connections | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1 || true
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
  local run_dir_win=""
  run_dir_win="$(cygpath -w "${RUN_DIR}")"

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
  export LOG_DIR="${LOG_DIR:-${run_dir_win}\\logs}"
  export OBSERVABILITY_JSONL_PATH="${OBSERVABILITY_JSONL_PATH:-${run_dir_win}\\logs\\observability.jsonl}"
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
  local fe_dir_win=""
  local frontend_log_win=""
  local frontend_pid=""

  if pid_is_running "${RUN_DIR}/frontend.pid"; then
    echo "Frontend is already running with PID $(cat "${RUN_DIR}/frontend.pid")."
    return 0
  fi

  echo "Starting frontend on http://0.0.0.0:${FRONTEND_PORT}..."
  fe_dir_win="$(cygpath -w "${FE_DIR}")"
  frontend_log_win="$(cygpath -w "${RUN_DIR}/frontend.log")"

  if ! FE_DIR_WIN="${fe_dir_win}" run_powershell '
    $testPath = Join-Path $env:FE_DIR_WIN ".agency-write-test"
    Set-Content -LiteralPath $testPath -Value "ok" -Encoding UTF8
    Remove-Item -LiteralPath $testPath -Force
  ' >/dev/null; then
    echo "Unable to write to frontend directory: ${fe_dir_win}" >&2
    echo "Next.js needs write access there for .env.local and .next lock/cache files." >&2
    echo "Run this from a normal PowerShell window, then retry:" >&2
    echo "  icacls \"${fe_dir_win}\" /grant \"\$env:USERNAME:(OI)(CI)F\" /T" >&2
    return 1
  fi

  frontend_pid="$(
    FE_DIR_WIN="${fe_dir_win}" \
    FRONTEND_LOG_WIN="${frontend_log_win}" \
    FRONTEND_PORT_VALUE="${FRONTEND_PORT}" \
    run_powershell '
      $fe = $env:FE_DIR_WIN
      $log = $env:FRONTEND_LOG_WIN
      $port = $env:FRONTEND_PORT_VALUE
      $cmd = "npm run dev:lan -- -p $port > `"$log`" 2>&1"
      $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/s", "/c", $cmd) -WorkingDirectory $fe -WindowStyle Hidden -PassThru
      $process.Id
    ' | sed -n '1p'
  )"

  if [ -z "${frontend_pid}" ]; then
    echo "Unable to start frontend. Check ${RUN_DIR}/frontend.log." >&2
    return 1
  fi
  echo "${frontend_pid}" >"${RUN_DIR}/frontend.pid"
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
  local frontend_started="true"

  ensure_run_dir
  cd "${ROOT_DIR}"

  if [ ! -f .env ]; then
    run_powershell "Copy-Item -LiteralPath '$(cygpath -w "${ROOT_DIR}/.env.example")' -Destination '$(cygpath -w "${ROOT_DIR}/.env")'" >/dev/null
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
  if ! start_frontend; then
    frontend_started="false"
    echo "Warning: frontend startup did not complete." >&2
    echo "Backend is running; fix frontend directory permissions and rerun start to launch the frontend." >&2
    if [ "${AGENCY_REQUIRE_FRONTEND:-false}" = "true" ]; then
      return 1
    fi
  fi

  echo
  echo "Agency is starting."
  if [ "${frontend_started}" = "true" ]; then
    echo "Frontend: http://${lan_host}:${FRONTEND_PORT}"
  else
    echo "Frontend: skipped; see warning above"
  fi
  echo "Backend:  http://127.0.0.1:${BACKEND_PORT}"
  echo "Logs:     ${RUN_DIR}"
}

stop_all() {
  ensure_run_dir
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

  ensure_run_dir
  cd "${ROOT_DIR}"
  show_pid_status "Backend" "${RUN_DIR}/backend.pid"
  show_pid_status "Frontend" "${RUN_DIR}/frontend.pid"

  echo
  echo "Containers:"
  docker compose ps || true

  echo
  echo "Backend health:"
  run_powershell "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:${BACKEND_PORT}/health' -UseBasicParsing -TimeoutSec 5).Content } catch { \$_.Exception.Message }" || true

  echo
  echo "Port listeners:"
  run_powershell "Get-NetTCPConnection -LocalPort ${BACKEND_PORT},${FRONTEND_PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize" || true

  echo
  echo "Frontend LAN env (${env_file}):"
  if [ -f "${env_file}" ]; then
    grep -E '^(NEXT_ALLOWED_DEV_ORIGINS|NEXT_PUBLIC_APP_ENV|NEXT_PUBLIC_AGENCY_API_BASE_URL|LOCAL_BACKEND|AGENCY_INTERNAL_API_BASE_URL)=' "${env_file}" || true
  else
    echo "Missing. Run ./run-windows.sh start to generate it."
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
