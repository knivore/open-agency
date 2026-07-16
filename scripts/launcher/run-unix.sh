#!/usr/bin/env bash

set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${LAUNCHER_DIR}/../.." && pwd)"
COMMON_HOST_BIN_PATHS="/opt/homebrew/bin:/opt/homebrew/opt/npm/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"
if [ -n "${AGENCY_NODE_BIN_DIR:-}" ]; then
  export PATH="${AGENCY_NODE_BIN_DIR}:${COMMON_HOST_BIN_PATHS}:${PATH}"
else
  export PATH="${COMMON_HOST_BIN_PATHS}:${PATH}"
fi

FE_DIR="${AGENCY_FE_DIR:-"${ROOT_DIR}/../open-agency-fe"}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-open-agency}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
NGROK_PID_FILE="${ROOT_DIR}/.logs/agency-ngrok.pid"
NGROK_LOG_FILE="${ROOT_DIR}/.logs/agency-ngrok.log"
NGROK_API_URL="${AGENCY_NGROK_API_URL:-http://127.0.0.1:4040}"
CLOUDFLARED_PID_FILE="${ROOT_DIR}/.logs/agency-cloudflared.pid"
CLOUDFLARED_LOG_FILE="${ROOT_DIR}/.logs/agency-cloudflared.log"
ONECLI_GATEWAY_CA_HOST_PATH_DEFAULT="${ROOT_DIR}/certs/onecli-gateway-ca.pem"
ONECLI_BACKEND_CA_HOST_PATH_DEFAULT="${ROOT_DIR}/.data/onecli/worker-ca-plus-onecli.pem"
. "${LAUNCHER_DIR}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh [start|restart|stop|status|logs|doctor|bootstrap] [tunnel option]

Commands:
  start      Start Open Agency in the background. This is the default.
  restart    Stop and start Agency.
  stop       Stop the frontend, backend, Docker services, and public tunnel.
  status     Show Docker services, backend health, port listeners, and frontend env.
  logs       Stream backend and launcher-managed frontend logs.
  doctor     Check local prerequisites without changing the environment.
  bootstrap  Create local env files and install backend/frontend dependencies.

Start runs the full local setup in the background:
  - creates .env from .env.example when needed
  - optionally clones ../open-agency-fe after an interactive opt-in when the repo is absent
  - writes ../open-agency-fe/.env.local LAN proxy settings when open-agency-fe is present
  - starts Postgres, Redis, OneCLI, Langfuse, and the FastAPI backend in Docker
  - the backend entrypoint applies migrations automatically
  - runs startup onboarding sync checks inside the backend container
  - starts the frontend in the background when open-agency-fe is present

Tunnel options:
  -local, --local              Force local-only startup with no public tunnel.
  -cloudflare, --cloudflare    Start a Cloudflare Tunnel.
  -ngrok, --ngrok              Start an ngrok tunnel.
  --tunnel-provider=<name>     Explicitly set none, cloudflare, ngrok, or auto.
  --domain <hostname>          Use a reserved ngrok/Cloudflare hostname for this launch.

Environment overrides:
  FRONTEND_PORT / BACKEND_PORT      Change local ports. Defaults to 3000 / 8000.
  AGENCY_FE_DIR                     Frontend repo path. Defaults to ../open-agency-fe.
  AGENCY_FRONTEND_ENABLED=false     Run backend/runtime only.
  AGENCY_PUBLIC_TUNNEL_PROVIDER     auto, none, ngrok, or cloudflare.
  AGENCY_TUNNEL_CUSTOM_DOMAIN       Reserved ngrok/Cloudflare hostname.
  AGENCY_NGROK_BIN / AGENCY_CLOUDFLARE_TUNNEL_BIN
                                    Verified tunnel executable paths when not on PATH.
  AGENCY_TUNNEL_AUTO_INSTALL        Allow automatic provider installation via Homebrew on macOS.
  AGENCY_OPEN_BROWSER=false         Do not open the browser automatically.

Advanced overrides are documented in docs/runbook.md.
EOF
}

checksum_file() {
  local file="$1"

  if command_exists shasum; then
    shasum -a 256 "${file}" | awk '{print $1}'
    return 0
  fi
  if command_exists sha256sum; then
    sha256sum "${file}" | awk '{print $1}'
    return 0
  fi
  cksum "${file}" | awk '{print $1 ":" $2}'
}

env_file_value() {
  local file="$1"
  local key="$2"

  if [ ! -f "${file}" ]; then
    return 0
  fi
  grep -E "^${key}=" "${file}" | tail -n 1 | cut -d= -f2- || true
}

ngrok_config_file() {
  if [ "$(uname -s)" = "Darwin" ]; then
    printf '%s\n' "${HOME}/Library/Application Support/ngrok/ngrok.yml"
    return 0
  fi
  printf '%s\n' "${HOME}/.config/ngrok/ngrok.yml"
}

ngrok_enabled_mode() {
  if [ "$(public_tunnel_provider_mode)" = "ngrok" ]; then
    printf '%s\n' "true"
    return 0
  fi
  printf '%s\n' "false"
}

ngrok_bin_path() {
  if [ -n "${AGENCY_NGROK_BIN:-}" ] && [ -x "${AGENCY_NGROK_BIN}" ]; then
    printf '%s\n' "${AGENCY_NGROK_BIN}"
    return 0
  fi
  if command -v ngrok >/dev/null 2>&1; then
    command -v ngrok
    return 0
  fi
  if [ -x "${HOME}/.local/bin/ngrok" ]; then
    printf '%s\n' "${HOME}/.local/bin/ngrok"
    return 0
  fi
  return 1
}

ngrok_has_authtoken() {
  local config_file=""

  config_file="$(ngrok_config_file)"
  [ -f "${config_file}" ] && grep -Eq '^[[:space:]]*authtoken:[[:space:]]*[^[:space:]]+' "${config_file}"
}

cloudflare_enabled_mode() {
  if [ "$(public_tunnel_provider_mode)" = "cloudflare" ]; then
    printf '%s\n' "true"
    return 0
  fi
  printf '%s\n' "false"
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
  echo "Set AGENCY_FE_DIR=/path/to/open-agency-fe if you want the launcher to start the frontend."
}

frontend_git_url() {
  local configured="${AGENCY_FE_GIT_URL:-}"
  local origin_url=""

  if [ -n "${configured}" ]; then
    printf '%s\n' "${configured}"
    return 0
  fi
  if ! command_exists git; then
    return 1
  fi

  origin_url="$(git -C "${ROOT_DIR}" remote get-url origin 2>/dev/null || true)"
  if [ -z "${origin_url}" ]; then
    return 1
  fi
  origin_url="$(printf '%s\n' "${origin_url}" | sed -E 's#([/:])open-agency(\.git)?$#\1open-agency-fe.git#')"
  if [ "${origin_url}" = "$(git -C "${ROOT_DIR}" remote get-url origin 2>/dev/null || true)" ]; then
    return 1
  fi
  printf '%s\n' "${origin_url}"
}

clone_frontend_repo() {
  local repo_url="$1"
  local target_parent=""

  if [ -d "${FE_DIR}" ]; then
    return 0
  fi
  if ! command_exists git; then
    echo "git is required to clone open-agency-fe." >&2
    return 1
  fi

  target_parent="$(dirname "${FE_DIR}")"
  mkdir -p "${target_parent}"
  echo "Cloning open-agency-fe into ${FE_DIR}..."
  git clone "${repo_url}" "${FE_DIR}"
}

resolve_frontend_workspace_choice() {
  local answer=""
  local repo_url=""

  if frontend_available || [ "$(frontend_mode)" = "false" ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    return 0
  fi

  printf 'Use open-agency-fe frontend too? [Y/n] '
  read -r answer || true
  case "${answer}" in
    n|N|no|NO|No)
      export AGENCY_FRONTEND_ENABLED="false"
      return 0
      ;;
  esac

  repo_url="$(frontend_git_url || true)"
  if [ -z "${repo_url}" ]; then
    echo "Could not infer open-agency-fe clone URL. Set AGENCY_FE_GIT_URL or AGENCY_FE_DIR to enable the frontend." >&2
    export AGENCY_FRONTEND_ENABLED="false"
    return 0
  fi
  if ! clone_frontend_repo "${repo_url}"; then
    echo "Frontend clone failed; continuing with backend only." >&2
    export AGENCY_FRONTEND_ENABLED="false"
    return 0
  fi
}

python_version_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

python_version_text() {
  "$1" - <<'PY' 2>/dev/null || true
import sys
print(".".join(str(part) for part in sys.version_info[:3]))
PY
}

find_python_for_venv() {
  local candidate=""

  for candidate in "${PYTHON:-}" "${ROOT_DIR}/.venv/bin/python" python3.12 python3 python; do
    if [ -z "${candidate}" ]; then
      continue
    fi
    if [ -x "${candidate}" ] || command_exists "${candidate}"; then
      if python_version_ok "${candidate}"; then
        command -v "${candidate}" 2>/dev/null || printf '%s\n' "${candidate}"
        return 0
      fi
    fi
  done

  return 1
}

ensure_env_file() {
  if [ ! -f "${ROOT_DIR}/.env" ] && [ -f "${ROOT_DIR}/.env.example" ]; then
    echo "Creating .env from .env.example..."
    cp "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"
  fi
}

print_env_guidance() {
  local env_file="${ROOT_DIR}/.env"
  local openai_key=""
  local internal_key=""
  local codex_host_home="${CODEX_HOST_HOME:-"${HOME}/.codex"}"

  openai_key="$(env_file_value "${env_file}" "OPENAI_API_KEY")"
  internal_key="$(env_file_value "${env_file}" "AGENCY_INTERNAL_API_KEY")"

  echo
  echo "Local configuration:"
  if [ -n "${openai_key}" ]; then
    echo "  OPENAI_API_KEY: set"
  else
    echo "  OPENAI_API_KEY: not set; OpenAI API-key model profiles will need credentials before use."
  fi
  if [ -f "${codex_host_home}/auth.json" ]; then
    echo "  Codex OAuth: present at ${codex_host_home}/auth.json"
  else
    echo "  Codex OAuth: not found at ${codex_host_home}/auth.json; Codex-backed agents need 'codex login'."
  fi
  if frontend_available && [ -z "${internal_key}" ]; then
    echo "  AGENCY_INTERNAL_API_KEY: empty; local dev auth can run, but BFF identity delegation should use a shared key."
  fi
  echo
}


brew_bin_path() {
  local candidate=""

  if command_exists brew; then
    command -v brew
    return 0
  fi
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

refresh_brew_path() {
  local brew_bin="$1"
  local brew_prefix=""

  brew_prefix="$("${brew_bin}" --prefix 2>/dev/null || true)"
  if [ -n "${brew_prefix}" ] && [ -d "${brew_prefix}/bin" ]; then
    export PATH="${brew_prefix}/bin:${PATH}"
  fi
}

install_tunnel_provider() {
  local provider="$1"
  local brew_bin=""

  tunnel_auto_install_enabled || return 1
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Automatic tunnel installation is only configured for macOS via Homebrew." >&2
    return 1
  fi

  brew_bin="$(brew_bin_path || true)"
  if [ -z "${brew_bin}" ]; then
    echo "Homebrew is required to install ${provider} automatically. Install Homebrew or set its AGENCY_*_BIN path manually." >&2
    return 1
  fi

  case "${provider}" in
    ngrok)
      echo "Installing ngrok with Homebrew..." >&2
      "${brew_bin}" install --cask ngrok
      refresh_brew_path "${brew_bin}"
      ;;
    cloudflare)
      echo "Installing cloudflared with Homebrew..." >&2
      "${brew_bin}" install cloudflared
      refresh_brew_path "${brew_bin}"
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_ngrok_installed() {
  local existing=""

  existing="$(ngrok_bin_path || true)"
  if [ -n "${existing}" ]; then
    printf '%s\n' "${existing}"
    return 0
  fi

  if ! install_tunnel_provider ngrok; then
    return 1
  fi

  existing="$(ngrok_bin_path || true)"
  if [ -n "${existing}" ]; then
    printf '%s\n' "${existing}"
    return 0
  fi
  echo "ngrok is not installed. Install Homebrew or set AGENCY_NGROK_BIN to a verified executable." >&2
  return 1
}

ensure_ngrok_auth() {
  local ngrok_bin="$1"
  local token="${AGENCY_NGROK_AUTHTOKEN:-}"

  if [ -n "${token}" ]; then
    "${ngrok_bin}" config add-authtoken "${token}" >/dev/null
    return 0
  fi
  if ngrok_has_authtoken; then
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "ngrok requested, but no authtoken is configured. Continuing without ngrok." >&2
    disable_public_tunnel
    return 0
  fi

  printf 'Paste ngrok authtoken (leave blank to continue without ngrok): '
  read -r token || true
  if [ -z "${token}" ]; then
    echo "No ngrok authtoken provided. Continuing without ngrok."
    disable_public_tunnel
    return 0
  fi

  "${ngrok_bin}" config add-authtoken "${token}" >/dev/null
}

disable_public_tunnel() {
  export AGENCY_PUBLIC_TUNNEL_PROVIDER="none"
  unset AGENCY_TUNNEL_CUSTOM_DOMAIN
}

cloudflared_bin_path() {
  if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_BIN:-}" ] && [ -x "${AGENCY_CLOUDFLARE_TUNNEL_BIN}" ]; then
    printf '%s\n' "${AGENCY_CLOUDFLARE_TUNNEL_BIN}"
    return 0
  fi
  if command -v cloudflared >/dev/null 2>&1; then
    command -v cloudflared
    return 0
  fi
  if [ -x "${HOME}/.local/bin/cloudflared" ]; then
    printf '%s\n' "${HOME}/.local/bin/cloudflared"
    return 0
  fi
  return 1
}

ensure_cloudflared_installed() {
  local existing=""

  existing="$(cloudflared_bin_path || true)"
  if [ -n "${existing}" ]; then
    printf '%s\n' "${existing}"
    return 0
  fi

  if ! install_tunnel_provider cloudflare; then
    return 1
  fi

  existing="$(cloudflared_bin_path || true)"
  if [ -n "${existing}" ]; then
    printf '%s\n' "${existing}"
    return 0
  fi
  echo "cloudflared is not installed. Install Homebrew or set AGENCY_CLOUDFLARE_TUNNEL_BIN to a verified executable." >&2
  return 1
}

ensure_host_backend_env_files() {
  ensure_env_file
  mkdir -p "${ROOT_DIR}/.logs"
}

ensure_host_python_env() {
  if [ "${AGENCY_SKIP_BACKEND_INSTALL:-false}" = "true" ]; then
    return 0
  fi

  local python_for_venv=""
  local venv_python="${ROOT_DIR}/.venv/bin/python"
  local requirements_hash=""
  local requirements_stamp="${ROOT_DIR}/.venv/.agency-requirements.sha256"
  local playwright_stamp="${ROOT_DIR}/.venv/.agency-playwright-installed"

  if [ ! -x "${venv_python}" ]; then
    python_for_venv="$(find_python_for_venv || true)"
    if [ -z "${python_for_venv}" ]; then
      echo "Python 3.12+ was not found. Install Python 3.12, then rerun ./run.sh start." >&2
      return 1
    fi
    echo "Creating backend virtualenv with ${python_for_venv}..."
    "${python_for_venv}" -m venv "${ROOT_DIR}/.venv"
  fi

  if ! python_version_ok "${venv_python}"; then
    echo "Backend virtualenv uses Python $(python_version_text "${venv_python}"), but Agency requires Python 3.12+." >&2
    echo "Remove .venv or recreate it with Python 3.12, then rerun ./run.sh start." >&2
    return 1
  fi

  "${venv_python}" -m pip install --upgrade pip >/dev/null

  requirements_hash="$(checksum_file "${ROOT_DIR}/requirements.txt")"
  if [ ! -f "${requirements_stamp}" ] || [ "$(cat "${requirements_stamp}")" != "${requirements_hash}" ]; then
    echo "Installing backend Python dependencies..."
    "${venv_python}" -m pip install -r "${ROOT_DIR}/requirements.txt"
    printf '%s\n' "${requirements_hash}" >"${requirements_stamp}"
  fi

  if [ "${AGENCY_SKIP_PLAYWRIGHT_INSTALL:-false}" != "true" ] && [ ! -f "${playwright_stamp}" ]; then
    echo "Installing Playwright browsers..."
    "${venv_python}" -m playwright install
    date -u +"%Y-%m-%dT%H:%M:%SZ" >"${playwright_stamp}"
  fi
}

ensure_frontend_deps() {
  if ! frontend_available; then
    return 0
  fi
  if [ "${AGENCY_SKIP_FRONTEND_INSTALL:-false}" = "true" ]; then
    return 0
  fi
  if ! command_exists npm; then
    echo "npm is required to install/start the frontend at ${FE_DIR}." >&2
    echo "Install Node.js/npm, set AGENCY_FRONTEND_ENABLED=false, or remove the frontend requirement." >&2
    return 1
  fi

  local dependency_file="${FE_DIR}/package.json"
  local dependency_hash=""
  local dependency_stamp="${FE_DIR}/node_modules/.agency-deps.sha256"

  if [ -f "${FE_DIR}/package-lock.json" ]; then
    dependency_file="${FE_DIR}/package-lock.json"
  fi

  dependency_hash="$(checksum_file "${dependency_file}")"
  if [ ! -d "${FE_DIR}/node_modules" ] || [ ! -f "${dependency_stamp}" ] || [ "$(cat "${dependency_stamp}" 2>/dev/null || true)" != "${dependency_hash}" ]; then
    echo "Installing frontend dependencies in ${FE_DIR}..."
    (cd "${FE_DIR}" && npm install)
    mkdir -p "${FE_DIR}/node_modules"
    printf '%s\n' "${dependency_hash}" >"${dependency_stamp}"
  fi
}

bootstrap_local() {
  ensure_host_backend_env_files
  resolve_frontend_workspace_choice
  ensure_host_python_env
  ensure_frontend_deps
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

upsert_env_default() {
  local file="$1"
  local key="$2"
  local value="$3"
  local current=""

  if grep -q "^${key}=" "${file}"; then
    current="$(grep "^${key}=" "${file}" | tail -n 1 | cut -d= -f2-)"
    if [ -n "${current}" ]; then
      return 0
    fi
  fi

  upsert_env_value "${file}" "${key}" "${value}"
}

remove_env_value() {
  local file="$1"
  local key="$2"

  if grep -q "^${key}=" "${file}"; then
    sed -i.bak "/^${key}=/d" "${file}"
  fi
}

configure_frontend_env() {
  local lan_host="$1"
  local env_file="${FE_DIR}/.env.local"

  if ! frontend_available; then
    explain_frontend_skip
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
  upsert_env_value "${env_file}" "AUTH_TRUST_HOST" "true"
  upsert_env_value "${env_file}" "NEXT_ALLOWED_DEV_ORIGINS" "${lan_host},localhost,127.0.0.1"
  upsert_env_default "${env_file}" "AUTH_SECRET" "replace-me-in-local-dev"
  upsert_env_default "${env_file}" "NEXTAUTH_SECRET" "replace-me-in-local-dev"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_APP_ENV" "local"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED" "true"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_ENABLE_MOCK_FALLBACKS" "false"
  upsert_env_default "${env_file}" "DEV_AUTH_EMAIL" "dev@example.com"
  upsert_env_default "${env_file}" "DEV_AUTH_PASSWORD" "change-me"
  upsert_env_default "${env_file}" "DEV_AUTH_NAME" "Dev User"
  upsert_env_default "${env_file}" "DEV_AUTH_USER_ID" "dev-user"
  upsert_env_value "${env_file}" "AGENCY_FE_ENABLE_BACKEND_REWRITE" "true"
  upsert_env_value "${env_file}" "NEXT_PUBLIC_AGENCY_API_BASE_URL" "/backend"
  upsert_env_value "${env_file}" "LOCAL_BACKEND" "/backend"
  upsert_env_value "${env_file}" "AGENCY_INTERNAL_API_BASE_URL" "http://127.0.0.1:${BACKEND_PORT}"

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

sync_onecli_gateway_ca_to_host() {
  local env_file="${ROOT_DIR}/.env"
  local host_ca_path="${AGENCY_ONECLI_GATEWAY_CA_HOST_PATH:-${ONECLI_GATEWAY_CA_HOST_PATH_DEFAULT}}"
  local backend_ca_path="${AGENCY_BACKEND_ONECLI_GATEWAY_CA_HOST_PATH:-${ONECLI_BACKEND_CA_HOST_PATH_DEFAULT}}"
  local backend_ca_container_path=""
  local backend_ca_tmp=""
  local python_bin=""
  local certifi_bundle=""
  local host_ca_dir=""
  local host_ca_tmp=""

  host_ca_dir="$(dirname "${host_ca_path}")"
  host_ca_tmp="${host_ca_path}.tmp"
  mkdir -p "${host_ca_dir}"

  # Host-side CLI helpers and connector smoke tests talk to OneCLI through the
  # local MITM gateway, so they must trust the gateway CA generated inside the
  # OneCLI container instead of relying on system roots alone.
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
  mkdir -p "$(dirname "${backend_ca_path}")"
  rm -f "${backend_ca_tmp}"

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
  if [ -n "${certifi_bundle}" ] && [ -f "${certifi_bundle}" ]; then
    cat "${certifi_bundle}" >>"${backend_ca_tmp}"
    printf '\n' >>"${backend_ca_tmp}"
  elif [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    cat /etc/ssl/certs/ca-certificates.crt >>"${backend_ca_tmp}"
    printf '\n' >>"${backend_ca_tmp}"
  fi

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

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

process_alive() {
  local pid="$1"

  [ -n "${pid}" ] && kill -0 "${pid}" >/dev/null 2>&1
}

backend_healthy() {
  curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1
}

frontend_reachable() {
  curl -fsS --max-time 2 "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1
}

open_browser_url() {
  local url="$1"

  if [ "${AGENCY_OPEN_BROWSER:-true}" != "true" ]; then
    return 0
  fi
  if command_exists open; then
    open "${url}" >/dev/null 2>&1 || true
    return 0
  fi
  if command_exists xdg-open; then
    xdg-open "${url}" >/dev/null 2>&1 || true
  fi
}

next_dev_lock_file() {
  printf '%s\n' "${FE_DIR}/.next/dev/lock"
}

frontend_log_has_duplicate_server_error() {
  local log_file="$1"

  [ -f "${log_file}" ] && grep -q "Another next dev server is already running" "${log_file}"
}

print_cloudflared_log_tail() {
  local log_file="$1"

  if [ -f "${log_file}" ]; then
    echo "Last cloudflared log lines:" >&2
    tail -n 40 "${log_file}" >&2 || true
  fi
}

cloudflared_log_has_startup_error() {
  local log_file="$1"

  [ -f "${log_file}" ] && grep -Eq 'failed to unmarshal quick Tunnel|Error unmarshaling QuickTunnel response|failed to serve tunnel connection|Serve tunnel error|Connection terminated|no more connections active and exiting|Error while waiting for tunnel to start|failed to connect to' "${log_file}"
}

ngrok_public_url() {
  local python_bin=""

  if ! command_exists curl; then
    return 1
  fi

  python_bin="$(host_python 2>/dev/null || true)"
  if [ -z "${python_bin}" ]; then
    python_bin="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
  fi
  if [ -z "${python_bin}" ]; then
    return 1
  fi

  # Use `-c` so Python can read the ngrok API response from stdin; a heredoc would consume stdin for the script itself.
  curl -fsS "${NGROK_API_URL}/api/tunnels" 2>/dev/null | "${python_bin}" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

for tunnel in payload.get("tunnels") or []:
    if not isinstance(tunnel, dict):
        continue
    public_url = str(tunnel.get("public_url") or "")
    if public_url.startswith("https://"):
        print(public_url.rstrip("/"))
        raise SystemExit(0)

raise SystemExit(1)
' 2>/dev/null
}

cloudflare_quick_tunnel_url() {
  local log_file="${CLOUDFLARED_LOG_FILE}"

  if [ ! -f "${log_file}" ]; then
    return 1
  fi

  # cloudflared can report the quick-tunnel URL either as plain text or inside
  # structured log lines, so we accept the common URL form directly from the log.
  sed -nE 's/.*(https:\/\/[-a-zA-Z0-9]+\.trycloudflare\.com).*/\1/p' "${log_file}" | tail -n 1
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
      ngrok_public_url
      ;;
    cloudflare)
      if [ -n "${custom_domain}" ]; then
        printf 'https://%s\n' "${custom_domain#https://}"
        return 0
      fi
      if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL:-}" ]; then
        printf '%s\n' "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL%/}"
      else
        cloudflare_quick_tunnel_url
      fi
      ;;
    *)
      return 1
      ;;
  esac
}

stop_ngrok() {
  local ngrok_pid=""

  if [ -f "${NGROK_PID_FILE}" ]; then
    ngrok_pid="$(cat "${NGROK_PID_FILE}" 2>/dev/null || true)"
    if process_alive "${ngrok_pid}"; then
      kill "${ngrok_pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${NGROK_PID_FILE}"
  fi
}

stop_cloudflared() {
  local cloudflared_pid=""

  if [ -f "${CLOUDFLARED_PID_FILE}" ]; then
    cloudflared_pid="$(cat "${CLOUDFLARED_PID_FILE}" 2>/dev/null || true)"
    if process_alive "${cloudflared_pid}"; then
      kill "${cloudflared_pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${CLOUDFLARED_PID_FILE}"
  fi
}

start_ngrok_tunnel() {
  local mode=""
  local ngrok_bin=""
  local public_url=""
  local discovered_url=""
  local attempts=0
  local ngrok_pid=""
  local custom_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"

  mode="$(ngrok_enabled_mode)"
  if [ "${mode}" != "true" ]; then
    return 0
  fi

  ngrok_bin="$(ensure_ngrok_installed || true)"
  if [ -z "${ngrok_bin}" ]; then
    echo "ngrok could not be installed; continuing without a public tunnel." >&2
    disable_public_tunnel
    return 0
  fi

  ensure_ngrok_auth "${ngrok_bin}"
  if [ "$(ngrok_enabled_mode)" != "true" ]; then
    return 0
  fi

  mkdir -p "${ROOT_DIR}/.logs"
  stop_ngrok
  : >"${NGROK_LOG_FILE}"
  # ngrok is optional launcher glue for exposing the whole backend, not a channel-specific service.
  if [ -n "${custom_domain}" ]; then
    nohup "${ngrok_bin}" http --url "${custom_domain#https://}" "http://127.0.0.1:${BACKEND_PORT}" >"${NGROK_LOG_FILE}" 2>&1 </dev/null &
  else
    nohup "${ngrok_bin}" http "http://127.0.0.1:${BACKEND_PORT}" >"${NGROK_LOG_FILE}" 2>&1 </dev/null &
  fi
  ngrok_pid="$!"
  printf '%s\n' "${ngrok_pid}" >"${NGROK_PID_FILE}"

  while [ "${attempts}" -lt 20 ]; do
    if [ -n "${custom_domain}" ]; then
      if process_alive "${ngrok_pid}"; then
        public_url="$(public_tunnel_url || true)"
      else
        public_url=""
      fi
    else
      discovered_url="$(ngrok_public_url || true)"
      public_url="$(public_tunnel_url || true)"
    fi
    if [ -n "${public_url}" ]; then
      public_url="$(public_tunnel_url || true)"
      echo "ngrok tunnel: ${public_url}"
      print_chat_endpoints "${public_url}"
      return 0
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  echo "ngrok did not report a public URL. See ${NGROK_LOG_FILE}." >&2
  tail -n 40 "${NGROK_LOG_FILE}" >&2 || true
  stop_ngrok || true
  disable_public_tunnel
  return 0
}

start_cloudflare_tunnel() {
  local mode=""
  local cloudflared_bin=""
  local public_url=""
  local attempts=0
  local cloudflared_pid=""
  local tunnel_token="${AGENCY_CLOUDFLARE_TUNNEL_TOKEN:-}"
  local custom_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"

  mode="$(cloudflare_enabled_mode)"
  if [ "${mode}" != "true" ]; then
    return 0
  fi

  cloudflared_bin="$(ensure_cloudflared_installed || true)"
  if [ -z "${cloudflared_bin}" ]; then
    echo "cloudflared could not be installed; continuing without a public tunnel." >&2
    disable_public_tunnel
    return 0
  fi

  mkdir -p "${ROOT_DIR}/.logs"
  stop_cloudflared
  : >"${CLOUDFLARED_LOG_FILE}"
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
    # Capture stdout/stderr directly so the launcher can read the public URL if
    # cloudflared prints it outside its structured logfile path.
    nohup "${cloudflared_bin}" tunnel --no-autoupdate run --token "${tunnel_token}" >"${CLOUDFLARED_LOG_FILE}" 2>&1 </dev/null &
  else
    # Quick tunnels are appropriate for local development and avoid requiring account setup during ad hoc laptop runs.
    nohup "${cloudflared_bin}" tunnel --no-autoupdate --url "http://127.0.0.1:${BACKEND_PORT}" >"${CLOUDFLARED_LOG_FILE}" 2>&1 </dev/null &
  fi
  cloudflared_pid="$!"
  printf '%s\n' "$!" >"${CLOUDFLARED_PID_FILE}"

  if [ -n "${custom_domain}" ]; then
    sleep 2
    if process_alive "${cloudflared_pid}"; then
      public_url="$(public_tunnel_url)"
      echo "Cloudflare tunnel: ${public_url}"
      print_chat_endpoints "${public_url}"
      return 0
    fi
  fi

  while [ "${attempts}" -lt 20 ]; do
    if ! process_alive "${cloudflared_pid}"; then
      echo "cloudflared exited before it reported a public URL. See ${CLOUDFLARED_LOG_FILE}." >&2
      print_cloudflared_log_tail "${CLOUDFLARED_LOG_FILE}"
      stop_cloudflared || true
      disable_public_tunnel
      return 0
    fi
    public_url="$(public_tunnel_url || true)"
    if [ -n "${public_url}" ]; then
      echo "Cloudflare tunnel: ${public_url}"
      print_chat_endpoints "${public_url}"
      return 0
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  if cloudflared_log_has_startup_error "${CLOUDFLARED_LOG_FILE}"; then
    echo "cloudflared failed to start a quick tunnel. See ${CLOUDFLARED_LOG_FILE}." >&2
    print_cloudflared_log_tail "${CLOUDFLARED_LOG_FILE}"
    stop_cloudflared || true
    disable_public_tunnel
    return 0
  fi

  echo "cloudflared did not report a public URL. See ${CLOUDFLARED_LOG_FILE}." >&2
  print_cloudflared_log_tail "${CLOUDFLARED_LOG_FILE}"
  stop_cloudflared || true
  disable_public_tunnel
  return 0
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
    *)
      return 0
      ;;
  esac
}

stop_public_tunnel() {
  stop_ngrok || true
  stop_cloudflared || true
}

print_public_tunnel_summary() {
  local provider=""
  local public_url=""

  provider="$(public_tunnel_provider_mode)"
  case "${provider}" in
    ngrok|cloudflare)
      public_url="$(public_tunnel_url || true)"
      if [ -n "${public_url}" ]; then
        echo "Public backend URL: ${public_url}"
      fi
      ;;
  esac
}

show_public_tunnel_status() {
  local provider=""
  local tunnel_pid_file=""
  local tunnel_log=""
  local tunnel_name=""
  local tunnel_pid=""
  local public_url=""

  provider="$(public_tunnel_provider_mode)"
  case "${provider}" in
    ngrok)
      tunnel_name="ngrok"
      tunnel_pid_file="${NGROK_PID_FILE}"
      tunnel_log="${NGROK_LOG_FILE}"
      ;;
    cloudflare)
      tunnel_name="cloudflared"
      tunnel_pid_file="${CLOUDFLARED_PID_FILE}"
      tunnel_log="${CLOUDFLARED_LOG_FILE}"
      ;;
    *)
      echo "Public tunnel: disabled"
      return 0
      ;;
  esac

  echo "Public tunnel provider: ${provider}"
  if [ -f "${tunnel_pid_file}" ]; then
    tunnel_pid="$(cat "${tunnel_pid_file}" 2>/dev/null || true)"
    if process_alive "${tunnel_pid}"; then
      echo "Launcher ${tunnel_name} PID: ${tunnel_pid}"
      public_url="$(public_tunnel_url || true)"
      if [ -n "${public_url}" ]; then
        echo "Public backend URL: ${public_url}"
      else
        echo "Public backend URL: unavailable; check ${tunnel_log}"
      fi
    else
      echo "Launcher ${tunnel_name} PID file is stale."
    fi
  else
    echo "Launcher ${tunnel_name}: not running"
  fi
}

cleanup_stale_frontend_lock() {
  local lock_file=""
  local lock_pid=""
  local lock_port=""

  lock_file="$(next_dev_lock_file)"
  if [ ! -f "${lock_file}" ]; then
    return 0
  fi

  lock_pid="$(sed -n 's/.*"pid":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "${lock_file}" | head -n 1)"
  lock_port="$(sed -n 's/.*"port":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "${lock_file}" | head -n 1)"

  if [ -n "${lock_pid}" ] && process_alive "${lock_pid}"; then
    return 1
  fi
  if [ -n "${lock_port}" ] && port_in_use "${lock_port}"; then
    return 1
  fi
  if [ -z "${lock_port}" ] && port_in_use "${FRONTEND_PORT}"; then
    return 1
  fi

  echo "Removing stale Next dev lock at ${lock_file}."
  rm -f "${lock_file}"
}

print_frontend_log_tail() {
  local log_file="$1"

  if [ -f "${log_file}" ]; then
    echo "Last frontend log lines:" >&2
    tail -n 40 "${log_file}" >&2 || true
  fi
}

wait_for_frontend() {
  local frontend_pid="$1"
  local log_file="$2"
  local attempts=0
  local stable_checks=0

  # Background mode starts npm in the background; verify Next survived before reporting usable URLs.
  while [ "${attempts}" -lt 45 ]; do
    if frontend_reachable; then
      stable_checks=$((stable_checks + 1))
      if [ "${stable_checks}" -ge 3 ]; then
        return 0
      fi
    else
      stable_checks=0
    fi
    if ! process_alive "${frontend_pid}"; then
      echo "Frontend exited before it became reachable. See ${log_file}." >&2
      print_frontend_log_tail "${log_file}"
      return 1
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  echo "Frontend did not become reachable at http://localhost:${FRONTEND_PORT} after 45s. See ${log_file}." >&2
  print_frontend_log_tail "${log_file}"
  return 1
}

launch_frontend_background() {
  local log_file="$1"
  local python_bin=""

  python_bin="$(host_python 2>/dev/null || true)"
  if [ -n "${python_bin}" ]; then
    "${python_bin}" - "${FE_DIR}" "${FRONTEND_PORT}" "${log_file}" <<'PY'
import subprocess
import sys

fe_dir, port, log_file = sys.argv[1:4]
log = open(log_file, "ab", buffering=0)
# Keep background frontend out of the launcher process group so terminal/tool cleanup does not stop Next.
process = subprocess.Popen(
    ["npm", "run", "dev:lan", "--", "-p", port],
    cwd=fe_dir,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
    return 0
  fi

  (
    cd "${FE_DIR}"
    exec nohup npm run dev:lan -- -p "${FRONTEND_PORT}" \
      >"${log_file}" 2>&1 </dev/null
  ) &
  printf '%s\n' "$!"
}

start_background() {
  local lan_host=""
  local public_url=""
  local has_frontend="false"
  local startup_url=""

  ensure_host_backend_env_files
  load_dotenv_preserving_cli_tunnel_overrides
  resolve_frontend_workspace_choice

  if frontend_available; then
    has_frontend="true"
    lan_host="$(detect_lan_host)"
    configure_frontend_env "${lan_host}"
  else
    explain_frontend_skip
  fi
  apply_saved_or_detected_tunnel_preference

  print_env_guidance

  docker compose stop backend >/dev/null 2>&1 || true
  if port_in_use "${BACKEND_PORT}"; then
    echo "Stopping stale backend listener on port ${BACKEND_PORT}..."
    stop_backend_port
  fi

  echo "Starting Postgres, Redis, Neo4j, OneCLI, and Langfuse..."
  docker compose --profile onecli up -d \
    postgres redis neo4j onecli \
    langfuse-postgres langfuse-redis langfuse-clickhouse langfuse-minio langfuse-worker langfuse-web
  sync_onecli_gateway_ca_to_host || true

  if [ "${AGENCY_BUILD_RUNTIME_IMAGE:-true}" = "true" ]; then
    echo "Building backend and graph-projector images..."
    docker compose build backend graph-projector
  fi
  sync_codex_oauth_to_volume

  start_public_tunnel
  public_url="$(public_tunnel_url || true)"
  if [ -n "${public_url}" ]; then
    echo "Export AGENCY_PUBLIC_WEBHOOK_BASE_URL as ${public_url}"
    export AGENCY_PUBLIC_WEBHOOK_BASE_URL="${public_url}"
  fi

  echo "Starting backend container..."
  docker compose up -d backend

  echo "Waiting for backend to become healthy (migrations run inside container)..."
  local attempts=0
  while [ "${attempts}" -lt 90 ]; do
    if backend_healthy; then
      echo "Backend healthy at http://127.0.0.1:${BACKEND_PORT}."
      break
    fi
    sleep 2
    attempts=$((attempts + 1))
  done
  if ! backend_healthy; then
    echo "Backend did not become healthy after 180s. Check: docker compose logs backend" >&2
    return 1
  fi

  # The projector depends on completed backend migrations and a healthy Neo4j.
  # Start it only after the backend health gate so a normal launcher start brings
  # up the full graph stack without racing database initialization.
  echo "Starting graph projector..."
  docker compose up -d graph-projector

  record_public_endpoint_if_present "${public_url}" docker compose exec -T backend python
  run_startup_onboarding_sync \
    "docker compose exec -T backend python scripts/setup.py local-onboarding" \
    "re-run with MAIN_AGENT_BOOTSTRAP_* configured and: ./run.sh start" \
    docker compose exec backend python scripts/setup.py

  if [ "${has_frontend}" = "true" ]; then
    if agency_frontend_reachable; then
      echo "Agency frontend already reachable at http://localhost:${FRONTEND_PORT}; reusing it."
    elif port_in_use "${FRONTEND_PORT}"; then
      echo "Port ${FRONTEND_PORT} is already in use by a non-Agency frontend process." >&2
      echo "Stop that process or set FRONTEND_PORT before starting Agency." >&2
      return 1
    else
      ensure_frontend_deps
      mkdir -p "${ROOT_DIR}/.logs"
      if [ -f "${ROOT_DIR}/.logs/agency-frontend.pid" ]; then
        echo "Stopping stale launcher-managed frontend process..."
        stop_frontend
      fi
      echo "Starting frontend in background..."
      local frontend_log="${ROOT_DIR}/.logs/agency-frontend.log"
      local frontend_pid=""
      cleanup_stale_frontend_lock || true
      : >"${frontend_log}"
      frontend_pid="$(launch_frontend_background "${frontend_log}")"
      printf '%s\n' "${frontend_pid}" >"${ROOT_DIR}/.logs/agency-frontend.pid"
      if ! wait_for_frontend "${frontend_pid}" "${frontend_log}"; then
        if frontend_log_has_duplicate_server_error "${frontend_log}" && cleanup_stale_frontend_lock; then
          echo "Retrying frontend after removing stale Next dev lock..."
          : >"${frontend_log}"
          frontend_pid="$(launch_frontend_background "${frontend_log}")"
          printf '%s\n' "${frontend_pid}" >"${ROOT_DIR}/.logs/agency-frontend.pid"
          if ! wait_for_frontend "${frontend_pid}" "${frontend_log}"; then
            rm -f "${ROOT_DIR}/.logs/agency-frontend.pid"
            return 1
          fi
        else
          rm -f "${ROOT_DIR}/.logs/agency-frontend.pid"
          return 1
        fi
      fi
      disown "${frontend_pid}" 2>/dev/null || true
      echo "Frontend started (PID ${frontend_pid}, log: ${frontend_log})."
    fi
  fi

  echo
  if [ "${has_frontend}" = "true" ]; then
    echo "Frontend URL: http://${lan_host}:${FRONTEND_PORT}"
    echo "Local frontend URL: http://localhost:${FRONTEND_PORT}"
    echo "Backend health through frontend proxy: http://${lan_host}:${FRONTEND_PORT}/backend/health"
    startup_url="$(startup_url_for_frontend)"
    echo "Recommended startup URL: ${startup_url}"
  else
    echo "Frontend URL: skipped because open-agency-fe was not found."
  fi
  echo "Backend URL: http://127.0.0.1:${BACKEND_PORT}"
  print_public_tunnel_summary
  echo "Langfuse URL: http://localhost:3001"
  echo
  echo "To stream logs: ./run.sh logs"
  if [ "${has_frontend}" = "true" ]; then
    open_browser_url "${startup_url}"
  fi
  echo "To stop: ./run.sh stop"
}

stop_port() {
  local port="$1"
  local pids=""

  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi

  pids="$(lsof -ti tcp:"${port}" 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    kill ${pids} >/dev/null 2>&1 || true
  fi
}

port_has_docker_listener() {
  local port="$1"
  local pid=""
  local command_name=""

  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  for pid in $(lsof -ti tcp:"${port}" 2>/dev/null || true); do
    command_name="$(ps -p "${pid}" -o comm= 2>/dev/null || true)"
    case "${command_name}" in
      *Docker*|*docker*|*com.docker*)
        return 0
        ;;
    esac
  done

  return 1
}

stop_backend_port() {
  if ! port_in_use "${BACKEND_PORT}"; then
    return 0
  fi

  if port_has_docker_listener "${BACKEND_PORT}"; then
    echo "Backend port ${BACKEND_PORT} is owned by Docker; refusing to kill Docker Desktop's port proxy." >&2
    echo "Try stopping the backend with: docker compose stop backend" >&2
    return 1
  fi

  stop_port "${BACKEND_PORT}"
}

stop_frontend() {
  local pid_file="${ROOT_DIR}/.logs/agency-frontend.pid"
  local frontend_pid=""
  local attempts=0

  if [ -f "${pid_file}" ]; then
    frontend_pid="$(cat "${pid_file}" 2>/dev/null || true)"
    if process_alive "${frontend_pid}"; then
      kill "${frontend_pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${pid_file}"
  fi

  stop_port "${FRONTEND_PORT}"

  while port_in_use "${FRONTEND_PORT}" && [ "${attempts}" -lt 20 ]; do
    sleep 0.5
    attempts=$((attempts + 1))
  done
}

stop_all() {
  if frontend_available || port_in_use "${FRONTEND_PORT}"; then
    echo "Stopping Agency frontend on port ${FRONTEND_PORT}..."
    stop_frontend
  fi

  echo "Stopping Agency Docker services..."
  docker compose down || true

  echo "Stopping any stale backend listener on port ${BACKEND_PORT}..."
  stop_backend_port || true

  echo "Stopping public tunnel..."
  stop_public_tunnel
}

show_status() {
  local env_file="${FE_DIR}/.env.local"

  cd "${ROOT_DIR}"
  load_dotenv_preserving_cli_tunnel_overrides
  apply_saved_or_detected_tunnel_preference

  echo "Docker services:"
  docker compose ps || true

  echo
  echo "Backend health:"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" || true
    echo
  else
    echo "curl is not available for the health check."
  fi

  if frontend_available; then
    echo
    echo "Frontend listener on port ${FRONTEND_PORT}:"
    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -i tcp:"${FRONTEND_PORT}" -sTCP:LISTEN || true
    else
      echo "lsof is not available for the port check."
    fi
  else
    echo
    explain_frontend_skip || true
  fi

  echo
  echo "Backend listener on port ${BACKEND_PORT}:"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -i tcp:"${BACKEND_PORT}" -sTCP:LISTEN || true
  else
    echo "lsof is not available for the port check."
  fi

  echo
  echo "Codex OAuth:"
  if [ -f "${CODEX_HOST_HOME:-"${HOME}/.codex"}/auth.json" ]; then
    echo "Host Codex auth: present"
  else
    echo "Host Codex auth: missing"
  fi

  echo
  show_public_tunnel_status

  if frontend_available; then
    echo
    echo "Frontend env (${env_file}):"
    if [ -f "${env_file}" ]; then
      grep -E '^(AUTH_URL|NEXTAUTH_URL|AUTH_TRUST_HOST|NEXT_ALLOWED_DEV_ORIGINS|NEXT_PUBLIC_APP_ENV|NEXT_PUBLIC_AGENCY_DEV_AUTH_ENABLED|NEXT_PUBLIC_ENABLE_MOCK_FALLBACKS|AGENCY_FE_ENABLE_BACKEND_REWRITE|NEXT_PUBLIC_AGENCY_API_BASE_URL|LOCAL_BACKEND|AGENCY_INTERNAL_API_BASE_URL|DEV_AUTH_EMAIL|DEV_AUTH_NAME|DEV_AUTH_USER_ID)=' "${env_file}" || true
      if grep -q '^DEV_AUTH_PASSWORD=' "${env_file}"; then
        echo "DEV_AUTH_PASSWORD=<set>"
      else
        echo "DEV_AUTH_PASSWORD=<missing>"
      fi
    else
      echo "Missing. Run ./run.sh start to generate it."
    fi
  fi
}

stream_logs() {
  local frontend_log="${ROOT_DIR}/.logs/agency-frontend.log"
  local frontend_tail_pid=""

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

doctor() {
  local failures=0
  local candidate_python=""

  cd "${ROOT_DIR}"
  load_dotenv_preserving_cli_tunnel_overrides
  apply_saved_or_detected_tunnel_preference

  echo "Agency local setup doctor"
  echo

  if command_exists docker; then
    echo "Docker CLI: present"
    if docker info >/dev/null 2>&1; then
      echo "Docker daemon: running"
    else
      echo "Docker daemon: not reachable. Start Docker Desktop." >&2
      failures=$((failures + 1))
    fi
    if docker compose version >/dev/null 2>&1; then
      echo "Docker Compose: present"
    else
      echo "Docker Compose: missing or not available through 'docker compose'." >&2
      failures=$((failures + 1))
    fi
  else
    echo "Docker CLI: missing. Install Docker Desktop." >&2
    failures=$((failures + 1))
  fi

  candidate_python="$(find_python_for_venv || true)"
  if [ -n "${candidate_python}" ]; then
    echo "Python: $(python_version_text "${candidate_python}") at ${candidate_python}"
  else
    echo "Python: missing compatible Python 3.12+." >&2
    failures=$((failures + 1))
  fi

  if [ -f "${ROOT_DIR}/.env" ]; then
    echo ".env: present"
  elif [ -f "${ROOT_DIR}/.env.example" ]; then
    echo ".env: missing; bootstrap/start will create it from .env.example"
  else
    echo ".env.example: missing." >&2
    failures=$((failures + 1))
  fi

  if frontend_available; then
    echo "Frontend: present at ${FE_DIR}"
    if command_exists npm; then
      echo "npm: present at $(command -v npm)"
    else
      echo "npm: missing; install Node.js/npm or set AGENCY_FRONTEND_ENABLED=false." >&2
      failures=$((failures + 1))
    fi
  else
    explain_frontend_skip || failures=$((failures + 1))
  fi

  if port_in_use "${BACKEND_PORT}" && ! backend_healthy; then
    echo "Backend port ${BACKEND_PORT}: in use by a non-Agency process." >&2
    failures=$((failures + 1))
  else
    echo "Backend port ${BACKEND_PORT}: available or healthy"
  fi

  if frontend_available && port_in_use "${FRONTEND_PORT}"; then
    echo "Frontend port ${FRONTEND_PORT}: currently in use"
  elif frontend_available; then
    echo "Frontend port ${FRONTEND_PORT}: available"
  fi

  case "$(public_tunnel_provider_mode)" in
    ngrok)
      local ngrok_bin=""
      ngrok_bin="$(ngrok_bin_path || true)"
      if [ -n "${ngrok_bin}" ]; then
        echo "ngrok: present at ${ngrok_bin}"
      else
        echo "ngrok: not installed yet; launcher will attempt a Homebrew install when start enables ngrok."
      fi
      if [ -n "${AGENCY_NGROK_AUTHTOKEN:-}" ] || ngrok_has_authtoken; then
        echo "ngrok authtoken: configured"
      else
        echo "ngrok authtoken: missing; launcher will prompt in interactive mode." >&2
      fi
      ;;
    cloudflare)
      local cloudflared_bin=""
      cloudflared_bin="$(cloudflared_bin_path || true)"
      if [ -n "${cloudflared_bin}" ]; then
        echo "cloudflared: present at ${cloudflared_bin}"
      else
        echo "cloudflared: not installed yet; launcher will attempt a Homebrew install when start enables Cloudflare Tunnel."
      fi
      if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
        echo "Cloudflare Tunnel mode: managed token"
        if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL:-}" ]; then
          echo "Cloudflare Tunnel public URL: ${AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL}"
        else
          echo "Cloudflare Tunnel public URL: missing AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL for managed token mode." >&2
        fi
      else
        echo "Cloudflare Tunnel mode: quick tunnel"
      fi
      ;;
    *)
      echo "Public tunnel: disabled"
      ;;
  esac

  print_env_guidance

  if [ "${failures}" -gt 0 ]; then
    echo "Doctor found ${failures} blocking issue(s)." >&2
    return 1
  fi

  echo "Doctor found no blocking setup issues."
}

main() {
  parse_cli "$@" || return $?

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
    doctor)
      doctor
      ;;
    bootstrap)
      bootstrap_local
      print_env_guidance
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      echo "Unknown command: ${COMMAND}" >&2
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
