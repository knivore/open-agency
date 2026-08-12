#!/usr/bin/env bash

set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${LAUNCHER_DIR}/../.." && pwd)"
FE_DIR="${AGENCY_FE_DIR:-"${ROOT_DIR}/../open-agency-fe"}"
RUN_DIR="${AGENCY_RUN_DIR:-/tmp/open-agency-run}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_INTERNAL_URL="${AGENCY_INTERNAL_API_BASE_URL:-http://127.0.0.1:${BACKEND_PORT}}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-open-agency}"
NGROK_API_URL="${AGENCY_NGROK_API_URL:-http://127.0.0.1:4040}"
ONECLI_GATEWAY_CA_HOST_PATH_DEFAULT="${ROOT_DIR}/certs/onecli-gateway-ca.pem"
ONECLI_BACKEND_CA_HOST_PATH_DEFAULT="${ROOT_DIR}/.data/onecli/worker-ca-plus-onecli.pem"
. "${LAUNCHER_DIR}/common.sh"

if [ -n "${AGENCY_RUN_LOG:-}" ]; then
  # The CMD entrypoint sets a Windows path; tee keeps first-run builds visible
  # while preserving the same diagnostic log that previous launchers produced.
  launcher_log="$(cygpath -u "${AGENCY_RUN_LOG}")"
  mkdir -p "$(dirname "${launcher_log}")"
  exec > >(tee "${launcher_log}") 2>&1
fi

pause_on_error() {
  local exit_code="$1"
  local line_no="$2"
  local failed_command="${3:-unknown}"

  echo
  echo "run-windows.sh failed on line ${line_no} with exit code ${exit_code}."
  echo "Failed command: ${failed_command}"
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

trap 'exit_code=$?; line_no=$LINENO; failed_command=$BASH_COMMAND; pause_on_error "$exit_code" "$line_no" "$failed_command"' ERR

usage() {
  cat <<'EOF'
Usage:
  ./run-windows.sh <command> [tunnel option]

Commands:
  start      Start Open Agency in the background. This is the default.
  restart    Stop and start Agency.
  stop       Stop the frontend, backend, Docker services, and public tunnel.
  status     Show Docker services, backend health, port listeners, and frontend env.
  logs       Stream backend and launcher-managed frontend logs.
  tunnel-reload  Restart only the selected public tunnel without interrupting Agency.

start:
  Starts all services including the FastAPI backend inside Docker so no
  terminal needs to stay open. The backend entrypoint applies migrations
  automatically. Startup onboarding sync checks run inside the backend container.
  If ../open-agency-fe is present, also writes frontend LAN proxy settings and starts
  the frontend in the background.

Tunnel options:
  -local, --local              Force local-only startup with no public tunnel.
  -cloudflare, --cloudflare    Start a Cloudflare Tunnel.
  -ngrok, --ngrok              Start an ngrok tunnel.
  --tunnel-provider=<name>     Explicitly set none, cloudflare, ngrok, or auto.
  --domain <hostname>          Use a reserved ngrok/Cloudflare hostname for this launch.

Common environment overrides:
  FRONTEND_PORT / BACKEND_PORT      Change local ports. Defaults to 3000 / 8000.
  AGENCY_FE_DIR                     Frontend repo path. Defaults to ../open-agency-fe.
  AGENCY_FRONTEND_ENABLED=false     Run backend/runtime only.
  AGENCY_FRONTEND_RUNTIME           Use auto, native, or container. Defaults to auto.
  AGENCY_NGROK_BIN / AGENCY_CLOUDFLARE_TUNNEL_BIN
                                    Verified tunnel executable paths when not on PATH.
  AGENCY_TUNNEL_AUTO_INSTALL       Install a selected provider with WinGet when missing.
  AGENCY_NGROK_AUTHTOKEN            ngrok agent token, when required by the account.
  AGENCY_CLOUDFLARE_TUNNEL_TOKEN    Managed Cloudflare Tunnel token.
  AGENCY_OPEN_BROWSER=false         Do not open the browser automatically.
EOF
}

frontend_mode() {
  printf '%s\n' "${AGENCY_FRONTEND_ENABLED:-auto}"
}

frontend_available() {
  local mode=""
  mode="$(frontend_mode)"

  if [ "${mode}" = "false" ]; then
    return 1
  fi
  [ -d "${FE_DIR}" ] && [ -f "${FE_DIR}/package.json" ]
}

frontend_required() {
  [ "$(frontend_mode)" = "true" ]
}

explain_frontend_skip() {
  if [ "$(frontend_mode)" = "false" ]; then
    echo "Frontend disabled with AGENCY_FRONTEND_ENABLED=false."
    return 0
  fi

  if frontend_required; then
    echo "Frontend repo not found at ${FE_DIR}. Set AGENCY_FE_DIR or use AGENCY_FRONTEND_ENABLED=auto." >&2
    return 1
  fi

  echo "Frontend repo not found at ${FE_DIR}; starting backend/runtime only."
}

ngrok_pid_file() {
  printf '%s\n' "${RUN_DIR}/agency-ngrok.pid"
}

ngrok_log_file() {
  printf '%s\n' "${RUN_DIR}/agency-ngrok.log"
}

cloudflared_pid_file() {
  printf '%s\n' "${RUN_DIR}/agency-cloudflared.pid"
}

cloudflared_log_file() {
  printf '%s\n' "${RUN_DIR}/agency-cloudflared.log"
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

resolve_configured_binary() {
  local configured="$1"
  local unix_path=""

  [ -n "${configured}" ] || return 1
  if [ -f "${configured}" ]; then
    printf '%s\n' "${configured}"
    return 0
  fi

  if command -v cygpath >/dev/null 2>&1; then
    unix_path="$(cygpath -u "${configured}" 2>/dev/null || true)"
    if [ -f "${unix_path}" ]; then
      printf '%s\n' "${unix_path}"
      return 0
    fi
  fi

  return 1
}

ngrok_bin_path() {
  local configured=""
  local candidate=""

  configured="$(resolve_configured_binary "${AGENCY_NGROK_BIN:-}" || true)"
  if [ -n "${configured}" ]; then
    printf '%s\n' "${configured}"
    return 0
  fi

  for candidate in ngrok ngrok.exe; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done

  return 1
}

cloudflared_bin_path() {
  local configured=""
  local candidate=""

  configured="$(resolve_configured_binary "${AGENCY_CLOUDFLARE_TUNNEL_BIN:-}" || true)"
  if [ -n "${configured}" ]; then
    printf '%s\n' "${configured}"
    return 0
  fi

  for candidate in cloudflared cloudflared.exe; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done

  return 1
}

powershell_command_path() {
  local command_name="$1"
  local windows_path=""

  windows_path="$(run_powershell "\$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User'); (Get-Command '${command_name}.exe' -ErrorAction SilentlyContinue).Source" 2>/dev/null | sed -n '1p' || true)"
  resolve_configured_binary "${windows_path}"
}

winget_bin_path() {
  if command_exists winget; then
    command -v winget
    return 0
  fi
  powershell_command_path winget
}

install_tunnel_provider() {
  local provider="$1"
  local package_id=""
  local winget_bin=""

  tunnel_auto_install_enabled || return 1
  winget_bin="$(winget_bin_path || true)"
  if [ -z "${winget_bin}" ]; then
    echo "WinGet is not available; install ${provider} or set its AGENCY_*_BIN path manually." >&2
    return 1
  fi

  case "${provider}" in
    ngrok)
      package_id="Ngrok.Ngrok"
      ;;
    cloudflare)
      package_id="Cloudflare.cloudflared"
      ;;
    *)
      return 1
      ;;
  esac

  echo "Installing ${provider} with WinGet (${package_id})..."
  "${winget_bin}" install \
    --id "${package_id}" \
    --exact \
    --source winget \
    --accept-source-agreements \
    --accept-package-agreements \
    --silent
}

ensure_tunnel_binary() {
  local provider="$1"
  local tunnel_bin=""

  case "${provider}" in
    ngrok)
      tunnel_bin="$(ngrok_bin_path || true)"
      if [ -z "${tunnel_bin}" ]; then
        tunnel_bin="$(powershell_command_path ngrok || true)"
      fi
      ;;
    cloudflare)
      tunnel_bin="$(cloudflared_bin_path || true)"
      if [ -z "${tunnel_bin}" ]; then
        tunnel_bin="$(powershell_command_path cloudflared || true)"
      fi
      ;;
  esac

  if [ -n "${tunnel_bin}" ]; then
    printf '%s\n' "${tunnel_bin}"
    return 0
  fi

  if ! install_tunnel_provider "${provider}"; then
    return 1
  fi

  case "${provider}" in
    ngrok)
      tunnel_bin="$(ngrok_bin_path || true)"
      [ -n "${tunnel_bin}" ] || tunnel_bin="$(powershell_command_path ngrok || true)"
      ;;
    cloudflare)
      tunnel_bin="$(cloudflared_bin_path || true)"
      [ -n "${tunnel_bin}" ] || tunnel_bin="$(powershell_command_path cloudflared || true)"
      ;;
  esac

  if [ -n "${tunnel_bin}" ]; then
    printf '%s\n' "${tunnel_bin}"
    return 0
  fi

  echo "${provider} installation completed, but its executable is not available yet." >&2
  return 1
}

ngrok_config_file() {
  local configured="${LOCALAPPDATA:-}"
  local windows_path=""

  if [ -z "${configured}" ]; then
    windows_path="$(run_powershell '[Environment]::GetEnvironmentVariable("LOCALAPPDATA","User")' 2>/dev/null | sed -n '1p' || true)"
    configured="${windows_path}"
  fi
  if [ -n "${configured}" ]; then
    configured="$(cygpath -u "${configured}" 2>/dev/null || printf '%s\n' "${configured}")"
    printf '%s\n' "${configured}/ngrok/ngrok.yml"
  fi
}

ngrok_has_authtoken() {
  local config_file=""

  config_file="$(ngrok_config_file)"
  [ -f "${config_file}" ] && grep -Eq '^[[:space:]]*authtoken:[[:space:]]*[^[:space:]]+' "${config_file}"
}

configure_ngrok_auth() {
  local ngrok_bin="$1"
  local token="${AGENCY_NGROK_AUTHTOKEN:-}"

  if [ -z "${token}" ] && ! ngrok_has_authtoken && [ -t 0 ]; then
    printf 'Enter your ngrok authtoken (leave blank to disable ngrok): '
    read -r -s token || true
    printf '\n'
  fi
  if [ -z "${token}" ]; then
    if ! ngrok_has_authtoken; then
      echo "ngrok requires an authtoken. Set AGENCY_NGROK_AUTHTOKEN before restarting." >&2
      disable_public_tunnel
    fi
    return 0
  fi

  if ! "${ngrok_bin}" config add-authtoken "${token}" >/dev/null 2>"$(ngrok_log_file)"; then
    echo "Unable to configure the ngrok authtoken. Check $(ngrok_log_file)." >&2
    disable_public_tunnel
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
  local generated_env="${RUN_DIR}/open-agency-fe.env.local"
  local generated_env_win=""
  local backend_internal_url="${BACKEND_INTERNAL_URL}"

  if ! frontend_available; then
    explain_frontend_skip
    return 1
  fi

  env_file_win="$(cygpath -w "${env_file}")"
  ensure_run_dir
  generated_env_win="$(cygpath -w "${generated_env}")"
  if [ -f "${env_file}" ]; then
    env_source_win="${env_file_win}"
  elif [ -f "${FE_DIR}/.env" ]; then
    env_source_win="$(cygpath -w "${FE_DIR}/.env")"
  elif [ -f "${FE_DIR}/.env.example" ]; then
    env_source_win="$(cygpath -w "${FE_DIR}/.env.example")"
  fi

  ENV_FILE_WIN="${generated_env_win}" \
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
    if ($source -and (Test-Path -LiteralPath $source)) {
      Copy-Item -LiteralPath $source -Destination $envFile -Force
    } elseif (-not (Test-Path -LiteralPath $envFile)) {
      New-Item -ItemType File -Path $envFile -Force | Out-Null
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
      AGENCY_FE_ENABLE_BACKEND_REWRITE = "true"
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

  export AGENCY_FRONTEND_CONTAINER_ENV_FILE="${generated_env_win}"
  export NEXT_ALLOWED_DEV_ORIGINS="${lan_host},localhost,127.0.0.1"

  if GENERATED_ENV_WIN="${generated_env_win}" ENV_FILE_WIN="${env_file_win}" run_powershell '
    Copy-Item -LiteralPath $env:GENERATED_ENV_WIN -Destination $env:ENV_FILE_WIN -Force
  ' >/dev/null 2>&1; then
    export AGENCY_FRONTEND_HOST_WRITABLE="true"
    return 0
  fi

  # A read-only sibling workspace is normal inside Codex. The container
  # frontend consumes this managed env file and stores build state in volumes.
  export AGENCY_FRONTEND_HOST_WRITABLE="false"
  echo "Frontend source is read-only; using the automatic Docker frontend runtime."
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

public_tunnel_url() {
  local provider=""
  local custom_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"
  provider="$(public_tunnel_provider_mode)"

  case "${provider}" in
    ngrok)
      if [ -n "${custom_domain}" ]; then
        printf 'https://%s\n' "${custom_domain#https://}"
        return 0
      fi
      if command -v curl >/dev/null 2>&1; then
        curl -fsS "${NGROK_API_URL}/api/tunnels" 2>/dev/null | python -c 'import json,sys; data=json.load(sys.stdin); print(next((t["public_url"] for t in data.get("tunnels", []) if t.get("public_url","").startswith("https://")), ""))' 2>/dev/null | sed -n '1p'
      fi
      ;;
    cloudflare)
      if [ -n "${custom_domain}" ]; then
        printf 'https://%s\n' "${custom_domain#https://}"
        return 0
      fi
      if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL:-}" ]; then
        printf '%s\n' "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL}"
      else
        sed -nE 's/.*(https:\/\/[-a-zA-Z0-9]+\.trycloudflare\.com).*/\1/p' "$(cloudflared_log_file)" 2>/dev/null | tail -n 1
      fi
      ;;
  esac
}

disable_public_tunnel() {
  export AGENCY_PUBLIC_TUNNEL_PROVIDER="none"
  unset AGENCY_TUNNEL_CUSTOM_DOMAIN
}

stop_ngrok() {
  stop_pid_file "ngrok" "$(ngrok_pid_file)"
}

stop_cloudflared() {
  stop_pid_file "cloudflared" "$(cloudflared_pid_file)"
}

stop_public_tunnel() {
  stop_ngrok || true
  stop_cloudflared || true
}

reload_public_tunnel() {
  local public_url=""

  load_dotenv_preserving_cli_tunnel_overrides
  apply_saved_or_detected_tunnel_preference
  stop_public_tunnel
  start_public_tunnel

  public_url="$(public_tunnel_url || true)"
  if [ -n "${public_url}" ]; then
    record_public_endpoint_if_present "${public_url}" docker compose exec backend python
  else
    clear_public_endpoint docker compose exec backend python
  fi

  case "$(public_tunnel_provider_mode)" in
    none)
      return 0
      ;;
    ngrok|cloudflare)
      [ -n "${public_url}" ]
      ;;
    *)
      return 1
      ;;
  esac
}

tunnel_supervisor_pid_file() {
  printf '%s\n' "${RUN_DIR}/agency-tunnel-supervisor.pid"
}

start_tunnel_supervisor() {
  local pid_file=""
  pid_file="$(tunnel_supervisor_pid_file)"
  if pid_is_running "${pid_file}"; then
    return 0
  fi
  rm -f "${pid_file}"
  nohup "${LAUNCHER_DIR}/tunnel-supervisor.sh" "${LAUNCHER_DIR}/run-windows.sh" \
    >"${RUN_DIR}/agency-tunnel-supervisor.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"${pid_file}"
}

start_ngrok_tunnel() {
  if [ "$(public_tunnel_provider_mode)" != "ngrok" ]; then
    return 0
  fi

  local ngrok_bin=""
  local custom_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"
  ngrok_bin="$(ensure_tunnel_binary ngrok || true)"
  if [ -z "${ngrok_bin}" ]; then
    echo "ngrok is selected but no executable was found. Install ngrok, set AGENCY_NGROK_BIN, or enable WinGet auto-install." >&2
    disable_public_tunnel
    return 0
  fi

  mkdir -p "${RUN_DIR}"
  : >"$(ngrok_log_file)"
  configure_ngrok_auth "${ngrok_bin}"
  if [ "$(public_tunnel_provider_mode)" != "ngrok" ]; then
    return 0
  fi
  stop_ngrok
  if [ -n "${custom_domain}" ]; then
    nohup "${ngrok_bin}" http --url "${custom_domain#https://}" "http://127.0.0.1:${BACKEND_PORT}" >"$(ngrok_log_file)" 2>&1 </dev/null &
  else
    nohup "${ngrok_bin}" http "http://127.0.0.1:${BACKEND_PORT}" >"$(ngrok_log_file)" 2>&1 </dev/null &
  fi
  echo "$!" >"$(ngrok_pid_file)"

  local attempts=0
  local public_url=""
  while [ "${attempts}" -lt 20 ]; do
    if command -v curl >/dev/null 2>&1 && curl -fsS "${NGROK_API_URL}/api/tunnels" >/dev/null 2>&1; then
      public_url="$(public_tunnel_url || true)"
      if [ -n "${public_url}" ]; then
        echo "ngrok tunnel: ${public_url}"
        return 0
      fi
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  echo "ngrok did not report a public URL. Check $(ngrok_log_file)." >&2
  tail -n 40 "$(ngrok_log_file)" >&2 || true
  disable_public_tunnel
}

start_cloudflare_tunnel() {
  if [ "$(public_tunnel_provider_mode)" != "cloudflare" ]; then
    return 0
  fi

  local cloudflared_bin=""
  local tunnel_token="${AGENCY_CLOUDFLARE_TUNNEL_TOKEN:-}"
  local custom_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"
  cloudflared_bin="$(ensure_tunnel_binary cloudflare || true)"
  if [ -z "${cloudflared_bin}" ]; then
    echo "Cloudflare is selected but cloudflared was not found. Install it, set AGENCY_CLOUDFLARE_TUNNEL_BIN, or enable WinGet auto-install." >&2
    disable_public_tunnel
    return 0
  fi

  mkdir -p "${RUN_DIR}"
  : >"$(cloudflared_log_file)"
  stop_cloudflared
  if [ -n "${custom_domain}" ] && [ -z "${tunnel_token}" ]; then
    echo "Cloudflare custom domains require AGENCY_CLOUDFLARE_TUNNEL_TOKEN for a managed tunnel." >&2
    disable_public_tunnel
    return 0
  fi
  if [ -n "${tunnel_token}" ]; then
    if [ -z "${custom_domain}" ] && [ -z "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL:-}" ]; then
      echo "AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL is required when using AGENCY_CLOUDFLARE_TUNNEL_TOKEN." >&2
      disable_public_tunnel
      return 0
    fi
    nohup "${cloudflared_bin}" tunnel --no-autoupdate run --token "${tunnel_token}" >"$(cloudflared_log_file)" 2>&1 </dev/null &
  else
    nohup "${cloudflared_bin}" tunnel --no-autoupdate --url "http://127.0.0.1:${BACKEND_PORT}" >"$(cloudflared_log_file)" 2>&1 </dev/null &
  fi
  echo "$!" >"$(cloudflared_pid_file)"

  if [ -n "${custom_domain}" ]; then
    sleep 2
    if pid_is_running "$(cloudflared_pid_file)"; then
      public_url="$(public_tunnel_url)"
      echo "Cloudflare tunnel: ${public_url}"
      return 0
    fi
  fi

  local attempts=0
  local public_url=""
  while [ "${attempts}" -lt 20 ]; do
    if ! pid_is_running "$(cloudflared_pid_file)"; then
      echo "cloudflared exited before it reported a public URL. Check $(cloudflared_log_file)." >&2
      stop_cloudflared || true
      disable_public_tunnel
      return 0
    fi
    public_url="$(public_tunnel_url || true)"
    if [ -n "${public_url}" ]; then
      echo "Cloudflare tunnel: ${public_url}"
      return 0
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  echo "cloudflared did not report a public URL. Check $(cloudflared_log_file)." >&2
  stop_cloudflared || true
  disable_public_tunnel
}

start_public_tunnel() {
  case "$(public_tunnel_provider_mode)" in
    ngrok)
      start_ngrok_tunnel
      ;;
    cloudflare)
      start_cloudflare_tunnel
      ;;
    none)
      stop_public_tunnel
      ;;
  esac
}

print_public_tunnel_summary() {
  local public_url=""

  public_url="$(public_tunnel_url || true)"
  if [ -n "${public_url}" ]; then
    echo "Public backend URL: ${public_url}"
  else
    echo "Public tunnel: disabled"
  fi
}

sync_codex_oauth_to_volume() {
  if [ "${AGENCY_SYNC_CODEX_OAUTH:-true}" != "true" ]; then
    return 0
  fi

  local codex_host_home="${CODEX_HOST_HOME:-"${HOME}/.codex"}"
  local codex_host_home_docker=""

  if [ -n "${CODEX_HOME_SOURCE:-}" ] && [ -d "${CODEX_HOME_SOURCE}" ]; then
    echo "Host Codex home is already mounted into the backend; OAuth sync is current."
    return 0
  fi

  if [ ! -f "${codex_host_home}/auth.json" ]; then
    echo "Host Codex auth not found at ${codex_host_home}/auth.json; run codex login or set CODEX_HOST_HOME." >&2
    return 0
  fi

  codex_host_home_docker="$(cygpath -w "${codex_host_home}")"
  echo "Syncing host Codex OAuth into Docker Codex volume..."
  MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -v "${codex_host_home_docker}:/host-codex:ro" backend sh -lc '
    set -e
    mkdir -p /codex && chmod 700 /codex
    cp /host-codex/auth.json /codex/auth.json
    chmod 600 /codex/auth.json
    if [ -f /host-codex/config.toml ]; then
      cp /host-codex/config.toml /codex/config.toml
      chmod 600 /codex/config.toml
    fi
  '
}

upsert_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"

  # Git Bash can fail to update timestamps on an existing Windows file even
  # when the file itself is writable. Only create the file when it is absent;
  # callers can decide whether a write failure should be fatal.
  mkdir -p "$(dirname "${file}")" >/dev/null 2>&1 || return 1
  if [ ! -e "${file}" ]; then
    : >"${file}" 2>/dev/null || return 1
  fi
  if grep -qE "^${key}=" "${file}"; then
    sed -i.bak -E "s|^${key}=.*|${key}=${value}|" "${file}" >/dev/null 2>&1 || return 1
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${file}" 2>/dev/null || return 1
  fi
}

ensure_browser_runtime_signing_secret() {
  local env_file="${ROOT_DIR}/.env"
  local browser_secret=""
  local python_bin=""

  browser_secret="$(grep -E '^BROWSER_RUNTIME_SIGNING_SECRET=' "${env_file}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  if [ "${#browser_secret}" -lt 32 ]; then
    python_bin="$(host_python || true)"
    if [ -z "${python_bin}" ]; then
      echo "Python is required to generate BROWSER_RUNTIME_SIGNING_SECRET." >&2
      return 1
    fi
    # Keep the browser capability-signing authority distinct from the key
    # used by trusted frontend routes to delegate backend identity.
    browser_secret="$("${python_bin}" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    if upsert_env_value "${env_file}" "BROWSER_RUNTIME_SIGNING_SECRET" "${browser_secret}"; then
      rm -f "${env_file}.bak" 2>/dev/null || true
      echo "Generated a browser runtime signing secret in .env."
    else
      echo "Warning: .env is not writable; using a generated browser runtime signing secret for this launch only." >&2
    fi
  fi
  export BROWSER_RUNTIME_SIGNING_SECRET="${browser_secret}"
}

sync_onecli_gateway_ca_to_host() {
  if [ "${ONECLI_ENABLED:-false}" != "true" ]; then
    return 0
  fi

  local env_file="${ROOT_DIR}/.env"
  local host_ca_path="${AGENCY_ONECLI_GATEWAY_CA_HOST_PATH:-${ONECLI_GATEWAY_CA_HOST_PATH_DEFAULT}}"
  local backend_ca_path="${AGENCY_BACKEND_ONECLI_GATEWAY_CA_HOST_PATH:-${ONECLI_BACKEND_CA_HOST_PATH_DEFAULT}}"
  local backend_ca_container_path=""
  local backend_ca_tmp=""
  local certifi_bundle=""
  local certifi_bundle_unix=""
  local extra_ca=""
  local host_ca_dir=""
  local host_ca_tmp=""
  local python_bin=""
  local windows_ca_tmp=""

  host_ca_dir="$(dirname "${host_ca_path}")"
  host_ca_tmp="${host_ca_path}.tmp"
  mkdir -p "${host_ca_dir}"

  # The backend container runs Linux and cannot read the Windows certificate
  # stores, so we build an explicit CA bundle before direct-mode connectors run.
  if ! docker compose --profile onecli exec -T onecli sh -lc 'cat /app/data/gateway/ca.pem' >"${host_ca_tmp}" 2>/dev/null; then
    rm -f "${host_ca_tmp}"
    echo "Unable to sync OneCLI gateway CA from the local OneCLI container." >&2
    return 1
  fi

  if [ ! -s "${host_ca_tmp}" ]; then
    rm -f "${host_ca_tmp}"
    echo "Local OneCLI gateway CA export was empty." >&2
    return 1
  fi

  mv "${host_ca_tmp}" "${host_ca_path}"
  backend_ca_tmp="${backend_ca_path}.tmp"
  windows_ca_tmp="${backend_ca_path}.windows.tmp"
  mkdir -p "$(dirname "${backend_ca_path}")"
  rm -f "${backend_ca_tmp}" "${windows_ca_tmp}"

  python_bin="$(host_python || true)"
  if [ -n "${python_bin}" ]; then
    certifi_bundle="$("${python_bin}" - <<'PY' 2>/dev/null || true
try:
    import certifi
except Exception:
    raise SystemExit(0)
print(certifi.where())
PY
)"
  fi
  certifi_bundle_unix="${certifi_bundle}"
  if [ -n "${certifi_bundle_unix}" ] && [ ! -f "${certifi_bundle_unix}" ] && command -v cygpath >/dev/null 2>&1; then
    certifi_bundle_unix="$(cygpath -u "${certifi_bundle_unix}" 2>/dev/null || printf '%s\n' "${certifi_bundle_unix}")"
  fi
  if [ -n "${certifi_bundle_unix}" ] && [ -f "${certifi_bundle_unix}" ]; then
    cat "${certifi_bundle_unix}" >>"${backend_ca_tmp}"
    printf '\n' >>"${backend_ca_tmp}"
  fi

  WINDOWS_CA_WIN="$(cygpath -w "${windows_ca_tmp}")" run_powershell '
    $out = $env:WINDOWS_CA_WIN
    $stores = @(
      "Cert:\LocalMachine\Root",
      "Cert:\CurrentUser\Root",
      "Cert:\LocalMachine\CA",
      "Cert:\CurrentUser\CA"
    )
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($store in $stores) {
      if (-not (Test-Path $store)) { continue }
      Get-ChildItem $store | Where-Object { $_.RawData } | ForEach-Object {
        $lines.Add("-----BEGIN CERTIFICATE-----")
        $lines.Add([Convert]::ToBase64String($_.RawData, "InsertLineBreaks"))
        $lines.Add("-----END CERTIFICATE-----")
        $lines.Add("")
      }
    }
    Set-Content -LiteralPath $out -Value $lines -Encoding ascii
  ' >/dev/null 2>&1 || true

  if [ -s "${windows_ca_tmp}" ]; then
    cat "${windows_ca_tmp}" >>"${backend_ca_tmp}"
    printf '\n' >>"${backend_ca_tmp}"
  fi
  rm -f "${windows_ca_tmp}"

  cat "${host_ca_path}" >>"${backend_ca_tmp}"
  printf '\n' >>"${backend_ca_tmp}"
  for extra_ca in \
    "${ROOT_DIR}/certs/local_cloudflare.cert" \
    "${ROOT_DIR}/.data/onecli/cloudflare-gateway-ca.pem"; do
    if [ -f "${extra_ca}" ]; then
      cat "${extra_ca}" >>"${backend_ca_tmp}"
      printf '\n' >>"${backend_ca_tmp}"
    fi
  done
  mv "${backend_ca_tmp}" "${backend_ca_path}"

  backend_ca_container_path="${backend_ca_path}"
  case "${backend_ca_path}" in
    "${ROOT_DIR}"/*)
      backend_ca_container_path="/workspace/open-agency/${backend_ca_path#"${ROOT_DIR}/"}"
      ;;
  esac
  upsert_env_value "${env_file}" "ONECLI_GATEWAY_CA_BUNDLE_PATH" "${host_ca_path}"
  upsert_env_value "${env_file}" "AGENCY_BACKEND_ONECLI_GATEWAY_CA_BUNDLE_PATH" "${backend_ca_container_path}"
  rm -f "${env_file}.bak"
  export ONECLI_GATEWAY_CA_BUNDLE_PATH="${host_ca_path}"
  export AGENCY_BACKEND_ONECLI_GATEWAY_CA_BUNDLE_PATH="${backend_ca_container_path}"
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

open_browser_url() {
  local url="$1"

  if [ "${AGENCY_OPEN_BROWSER:-true}" != "true" ]; then
    return 0
  fi

  run_powershell "Start-Process '${url}'" >/dev/null 2>&1 || true
}

frontend_port_in_use() {
  run_powershell "if (Get-NetTCPConnection -LocalPort ${FRONTEND_PORT} -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" \
    >/dev/null 2>&1
}

container_frontend_running() {
  [ "$(docker inspect -f '{{.State.Running}}' open-agency-frontend 2>/dev/null || true)" = "true" ]
}

frontend_host_writable() {
  local fe_dir_win=""

  case "${AGENCY_FRONTEND_HOST_WRITABLE:-}" in
    true)
      return 0
      ;;
    false)
      return 1
      ;;
esac
  fe_dir_win="$(cygpath -w "${FE_DIR}")"
  FE_DIR_WIN="${fe_dir_win}" run_powershell '
    $testPath = Join-Path $env:FE_DIR_WIN ".agency-write-test"
    try {
      Set-Content -LiteralPath $testPath -Value "ok" -Encoding UTF8 -ErrorAction Stop
    } finally {
      Remove-Item -LiteralPath $testPath -Force -ErrorAction SilentlyContinue
    }
  ' >/dev/null 2>&1
}

frontend_native_available() {
  frontend_host_writable && command -v npm >/dev/null 2>&1
}

start_container_frontend() {
  local timeout="${AGENCY_FRONTEND_STARTUP_TIMEOUT_SECONDS:-300}"

  echo "Starting frontend in Docker with read-only source and managed build volumes..."
  rm -f "${RUN_DIR}/frontend.pid"
  docker compose --profile container-frontend up -d frontend
  printf '%s\n' "container" >"${RUN_DIR}/frontend.runtime"

  if wait_for_http "http://127.0.0.1:${FRONTEND_PORT}/login" "Frontend" "${timeout}"; then
    return 0
  fi

  docker compose logs --tail 120 frontend >&2 || true
  return 1
}

start_native_frontend() {
  local fe_dir_win=""
  local frontend_log_win=""
  local frontend_pid=""
  local timeout="${AGENCY_FRONTEND_STARTUP_TIMEOUT_SECONDS:-180}"

  fe_dir_win="$(cygpath -w "${FE_DIR}")"
  frontend_log_win="$(cygpath -w "${RUN_DIR}/frontend.log")"

  echo "Starting frontend natively on http://0.0.0.0:${FRONTEND_PORT}..."
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
  printf '%s\n' "native" >"${RUN_DIR}/frontend.runtime"

  if wait_for_http "http://127.0.0.1:${FRONTEND_PORT}/login" "Frontend" "${timeout}"; then
    return 0
  fi

  tail -n 120 "${RUN_DIR}/frontend.log" >&2 || true
  return 1
}

start_frontend() {
  local runtime="${AGENCY_FRONTEND_RUNTIME:-auto}"

  if ! frontend_available; then
    explain_frontend_skip
    return 0
  fi

  if agency_frontend_reachable; then
    echo "Agency frontend already reachable at http://localhost:${FRONTEND_PORT}; reusing it."
    return 0
  fi
  if container_frontend_running; then
    echo "Container frontend is already running; waiting for it to become ready."
    if wait_for_http "http://127.0.0.1:${FRONTEND_PORT}/login" "Frontend" "${AGENCY_FRONTEND_STARTUP_TIMEOUT_SECONDS:-300}"; then
      return 0
    fi
    docker compose logs --tail 120 frontend >&2 || true
    return 1
  fi
  if pid_is_running "${RUN_DIR}/frontend.pid"; then
    echo "Frontend is already running with PID $(cat "${RUN_DIR}/frontend.pid")."
    return 0
  fi
  if frontend_port_in_use; then
    echo "Port ${FRONTEND_PORT} is already in use by a non-Agency frontend process." >&2
    echo "Stop that process or set FRONTEND_PORT before starting Agency." >&2
    return 1
  fi

  case "${runtime}" in
    auto)
      if frontend_native_available; then
        start_native_frontend
      else
        start_container_frontend
      fi
      ;;
    native)
      if ! frontend_host_writable; then
        echo "AGENCY_FRONTEND_RUNTIME=native requires a writable frontend directory." >&2
        return 1
      fi
      if ! command -v npm >/dev/null 2>&1; then
        echo "AGENCY_FRONTEND_RUNTIME=native requires npm on PATH." >&2
        return 1
      fi
      start_native_frontend
      ;;
    container)
      start_container_frontend
      ;;
    *)
      echo "Unknown AGENCY_FRONTEND_RUNTIME value: ${runtime}. Use auto, native, or container." >&2
      return 2
      ;;
  esac
}

start_background() {
  local lan_host=""
  local has_frontend="false"
  local public_url=""
  local startup_url=""

  ensure_run_dir
  cd "${ROOT_DIR}"

  if [ ! -f .env ]; then
    run_powershell "Copy-Item -LiteralPath '$(cygpath -w "${ROOT_DIR}/.env.example")' -Destination '$(cygpath -w "${ROOT_DIR}/.env")'" >/dev/null
  fi
  ensure_browser_runtime_signing_secret
  load_dotenv_preserving_cli_tunnel_overrides
  apply_saved_or_detected_tunnel_preference

  if frontend_available; then
    has_frontend="true"
    lan_host="$(detect_lan_host)"
    configure_frontend_lan_env "${lan_host}"
  else
    explain_frontend_skip
  fi

  echo "Starting Postgres, Redis, Neo4j, OneCLI, and supporting containers..."
  docker compose --profile onecli up -d postgres redis neo4j onecli langfuse-web
  sync_onecli_gateway_ca_to_host || true

  if [ "${AGENCY_HOST_BUILD_RUNTIME_IMAGE:-true}" = "true" ]; then
    echo "Building backend and graph-projector images..."
    docker compose build backend graph-projector
  fi
  sync_codex_oauth_to_volume

  start_public_tunnel
  start_tunnel_supervisor
  public_url="$(public_tunnel_url || true)"
  if [ -n "${public_url}" ]; then
    echo "Export AGENCY_PUBLIC_WEBHOOK_BASE_URL as ${public_url}"
    export AGENCY_PUBLIC_WEBHOOK_BASE_URL="${public_url}"
  fi

  echo "Starting backend container..."
  docker compose up -d backend

  echo "Waiting for backend to become healthy (migrations run inside container)..."
  wait_for_http "http://127.0.0.1:${BACKEND_PORT}/health" "Backend" 90

  # The projector depends on completed backend migrations and a healthy Neo4j.
  # Start it only after the backend health gate so a normal launcher start brings
  # up the full graph stack without racing database initialization.
  echo "Starting graph projector..."
  docker compose up -d graph-projector

  record_public_endpoint_if_present "${public_url}" docker compose exec backend python

  run_startup_onboarding_sync \
    "docker compose exec backend python scripts/setup.py local-onboarding" \
    "set MAIN_AGENT_BOOTSTRAP_* values in .env and run start again." \
    docker compose exec backend python scripts/setup.py

  if [ "${has_frontend}" = "true" ]; then
    start_frontend
  fi

  echo
  if [ "${has_frontend}" = "true" ]; then
    echo "Frontend: http://${lan_host}:${FRONTEND_PORT}"
    startup_url="$(startup_url_for_frontend)"
    echo "Recommended startup URL: ${startup_url}"
    if [ -n "${public_url}" ]; then
      print_chat_endpoints "${public_url}"
    else
      print_chat_endpoints "http://${lan_host}:${BACKEND_PORT}"
    fi
  else
    echo "Frontend: skipped because open-agency-fe was not found."
  fi
  echo "Backend:  http://127.0.0.1:${BACKEND_PORT}"
  print_public_tunnel_summary
  echo "To stream logs: ./run-windows.sh logs"
  if [ "${has_frontend}" = "true" ]; then
    open_browser_url "${startup_url}"
  fi
  echo "To stop: ./run-windows.sh stop"
}

stop_all() {
  ensure_run_dir
  cd "${ROOT_DIR}"

  stop_pid_file "frontend" "${RUN_DIR}/frontend.pid"
  stop_pid_file "tunnel supervisor" "$(tunnel_supervisor_pid_file)"
  stop_public_tunnel

  echo "Stopping Agency containers..."
  docker compose --profile container-frontend down
  rm -f "${RUN_DIR}/frontend.runtime"
  kill_port "${FRONTEND_PORT}"
  kill_port "${BACKEND_PORT}"
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

show_frontend_status() {
  if pid_is_running "${RUN_DIR}/frontend.pid"; then
    echo "Frontend: running natively (PID $(cat "${RUN_DIR}/frontend.pid"))"
  elif container_frontend_running; then
    echo "Frontend: running in Docker (open-agency-frontend)"
  elif agency_frontend_reachable; then
    echo "Frontend: reachable on port ${FRONTEND_PORT} (external process)"
  else
    echo "Frontend: stopped"
  fi
}

show_status() {
  local env_file="${FE_DIR}/.env.local"

  ensure_run_dir
  cd "${ROOT_DIR}"
  load_dotenv_preserving_cli_tunnel_overrides
  apply_saved_or_detected_tunnel_preference
  if container_frontend_running; then
    env_file="${RUN_DIR}/open-agency-fe.env.local"
  fi
  show_frontend_status

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
  if frontend_available; then
    echo "Frontend LAN env (${env_file}):"
    if [ -f "${env_file}" ]; then
      grep -E '^(NEXT_ALLOWED_DEV_ORIGINS|NEXT_PUBLIC_APP_ENV|AGENCY_FE_ENABLE_BACKEND_REWRITE|NEXT_PUBLIC_AGENCY_API_BASE_URL|LOCAL_BACKEND|AGENCY_INTERNAL_API_BASE_URL)=' "${env_file}" || true
    else
      echo "Missing. Run ./run-windows.sh start to generate it."
    fi
  else
    explain_frontend_skip || true
  fi

  echo
  echo "Logs:"
  echo "Backend:  docker compose logs -f backend"
  if container_frontend_running; then
    echo "Frontend: docker compose logs -f frontend"
  else
    echo "Frontend: ${RUN_DIR}/frontend.log"
  fi
  print_public_tunnel_summary
}

stream_logs() {
  local frontend_log="${RUN_DIR}/frontend.log"
  local frontend_tail_pid=""

  ensure_run_dir
  cd "${ROOT_DIR}"

  if container_frontend_running; then
    echo "Streaming backend and Docker frontend logs..."
    docker compose logs -f backend frontend
    return $?
  fi

  if [ -f "${frontend_log}" ]; then
    echo "Streaming frontend log: ${frontend_log}"
    tail -n 80 -F "${frontend_log}" &
    frontend_tail_pid="$!"
  fi

  if [ -n "${frontend_tail_pid}" ]; then
    trap 'kill "${frontend_tail_pid}" >/dev/null 2>&1 || true' EXIT INT TERM
  fi

  echo "Streaming backend logs: docker compose logs -f backend"
  docker compose logs -f backend
}

parse_cli "$@" || exit $?

case "${COMMAND}" in
  start)
    start_background
    ;;
  restart)
    stop_all
    start_background
    ;;
  stop)
    stop_all
    ;;
  status)
    show_status
    ;;
  logs|log)
    stream_logs
    ;;
  tunnel-reload)
    reload_public_tunnel
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
