#!/usr/bin/env bash

# Shared launcher helpers keep tunnel/onboarding behavior consistent across the
# macOS/Linux and Windows shell entrypoints while leaving platform bootstrapping
# details inside the caller scripts.

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

saved_tunnel_preference_path() {
  local configured="${AGENCY_TUNNEL_PREFERENCE_PATH:-}"

  if [ -z "${configured}" ]; then
    printf '%s\n' "${ROOT_DIR}/.agency/tunnel-preference.json"
    return 0
  fi

  case "${configured}" in
    /*)
      printf '%s\n' "${configured}"
      ;;
    [A-Za-z]:[\\/]*)
      if command_exists cygpath; then
        cygpath -u "${configured}"
      else
        printf '%s\n' "${configured}"
      fi
      ;;
    ~/*)
      printf '%s/%s\n' "${HOME}" "${configured#~/}"
      ;;
    *)
      # Relative values in .env are workspace-relative, independent of the
      # directory from which the launcher was invoked.
      printf '%s/%s\n' "${ROOT_DIR}" "${configured#./}"
      ;;
  esac
}

load_dotenv() {
  if [ -f "${ROOT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${ROOT_DIR}/.env"
    set +a
  fi
}

load_dotenv_preserving_cli_tunnel_overrides() {
  local cli_tunnel_override="${AGENCY_TUNNEL_CLI_OVERRIDE:-false}"
  local cli_tunnel_provider="${AGENCY_PUBLIC_TUNNEL_PROVIDER:-}"
  local cli_tunnel_domain="${AGENCY_TUNNEL_CUSTOM_DOMAIN:-}"
  local cli_tunnel_domain_set="${AGENCY_TUNNEL_CUSTOM_DOMAIN+x}"

  load_dotenv

  if [ "${cli_tunnel_override}" = "true" ]; then
    if [ -n "${cli_tunnel_provider}" ]; then
      export AGENCY_PUBLIC_TUNNEL_PROVIDER="${cli_tunnel_provider}"
    fi
    if [ "${cli_tunnel_domain_set}" = "x" ]; then
      export AGENCY_TUNNEL_CUSTOM_DOMAIN="${cli_tunnel_domain}"
    fi
    export AGENCY_TUNNEL_CLI_OVERRIDE="true"
  fi
}

tunnel_auto_install_enabled() {
  # Automatic installation is opt-out so fresh starts and saved setup
  # preferences behave the same on macOS and Windows.
  [ "${AGENCY_TUNNEL_AUTO_INSTALL:-true}" = "true" ]
}

detected_tunnel_provider() {
  if [ -n "${AGENCY_CLOUDFLARE_TUNNEL_TOKEN:-}" ] || command_exists cloudflared || [ -x "${HOME}/.local/bin/cloudflared" ]; then
    printf '%s\n' "cloudflare"
    return 0
  fi
  if [ -n "${AGENCY_NGROK_AUTHTOKEN:-}" ] || command_exists ngrok || [ -x "${HOME}/.local/bin/ngrok" ]; then
    printf '%s\n' "ngrok"
    return 0
  fi
  # Default fresh starts should try a public tunnel. Platform-specific launchers
  # degrade to local-only if the selected provider cannot be installed or run.
  printf '%s\n' "${AGENCY_DEFAULT_TUNNEL_PROVIDER:-cloudflare}"
}

apply_saved_or_detected_tunnel_preference() {
  local preference_file=""
  local python_bin=""
  local saved_provider=""
  local saved_domain=""
  local configured_provider="${AGENCY_PUBLIC_TUNNEL_PROVIDER:-auto}"

  if [ "${AGENCY_TUNNEL_CLI_OVERRIDE:-false}" = "true" ]; then
    export AGENCY_TUNNEL_PREFERENCE_SOURCE="cli"
  elif [ "${AGENCY_IGNORE_SAVED_TUNNEL_PREFERENCE:-false}" != "true" ]; then
    preference_file="$(saved_tunnel_preference_path)"
    if [ -f "${preference_file}" ]; then
      python_bin="$(_setup_status_python)"
      if [ -n "${python_bin}" ]; then
        saved_provider="$("${python_bin}" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
print(str(payload.get("provider") or "auto").strip().lower())
' "${preference_file}" 2>/dev/null || true)"
        saved_domain="$("${python_bin}" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
print(str(payload.get("custom_domain") or "").strip().lower())
' "${preference_file}" 2>/dev/null || true)"
      fi
    fi
  fi

  if [ "${AGENCY_TUNNEL_CLI_OVERRIDE:-false}" != "true" ]; then
    case "${saved_provider}" in
      auto|none|ngrok|cloudflare)
        configured_provider="${saved_provider}"
        export AGENCY_TUNNEL_CUSTOM_DOMAIN="${saved_domain}"
        export AGENCY_TUNNEL_PREFERENCE_SOURCE="browser"
        ;;
      *)
        saved_provider=""
        export AGENCY_TUNNEL_PREFERENCE_SOURCE="launcher"
        ;;
    esac
  fi

  if [ -z "${saved_provider}" ] && [ "${AGENCY_TUNNEL_CLI_OVERRIDE:-false}" != "true" ]; then
    export AGENCY_TUNNEL_PREFERENCE_SOURCE="launcher"
  fi

  if [ "${configured_provider}" = "auto" ]; then
    configured_provider="$(detected_tunnel_provider)"
  fi

  export AGENCY_PUBLIC_TUNNEL_PROVIDER="${configured_provider}"
}

public_tunnel_provider_mode() {
  printf '%s\n' "${AGENCY_PUBLIC_TUNNEL_PROVIDER:-auto}"
}

normalize_tunnel_provider() {
  case "$1" in
    none|local)
      printf '%s\n' "none"
      ;;
    ngrok)
      printf '%s\n' "ngrok"
      ;;
    cloudflare)
      printf '%s\n' "cloudflare"
      ;;
    auto)
      printf '%s\n' "auto"
      ;;
    *)
      return 1
      ;;
  esac
}

normalize_tunnel_domain() {
  local domain="$1"

  domain="${domain#https://}"
  domain="${domain#http://}"
  domain="${domain%%/*}"
  domain="${domain%%:*}"
  printf '%s\n' "${domain}"
}

parse_cli() {
  COMMAND="${1:-start}"
  shift $(( $# > 0 ? 1 : 0 ))

  local tunnel_provider_override=""
  local arg=""
  while [ "$#" -gt 0 ]; do
    arg="$1"
    case "${arg}" in
      -local|--local)
        tunnel_provider_override="none"
        ;;
      -cloudflare|--cloudflare)
        tunnel_provider_override="cloudflare"
        ;;
      -ngrok|--ngrok)
        tunnel_provider_override="ngrok"
        ;;
      --tunnel-provider=*)
        tunnel_provider_override="$(normalize_tunnel_provider "${arg#*=}")" || {
          echo "Unknown tunnel provider: ${arg#*=}" >&2
          usage >&2
          return 2
        }
        ;;
      --tunnel-provider)
        shift
        if [ "$#" -eq 0 ]; then
          echo "Missing value for --tunnel-provider" >&2
          usage >&2
          return 2
        fi
        tunnel_provider_override="$(normalize_tunnel_provider "$1")" || {
          echo "Unknown tunnel provider: $1" >&2
          usage >&2
          return 2
        }
        ;;
      --domain=*)
        export AGENCY_TUNNEL_CUSTOM_DOMAIN
        AGENCY_TUNNEL_CUSTOM_DOMAIN="$(normalize_tunnel_domain "${arg#*=}")"
        export AGENCY_TUNNEL_CLI_OVERRIDE="true"
        ;;
      --domain|--tunnel-domain)
        shift
        if [ "$#" -eq 0 ]; then
          echo "Missing value for ${arg}" >&2
          usage >&2
          return 2
        fi
        export AGENCY_TUNNEL_CUSTOM_DOMAIN
        AGENCY_TUNNEL_CUSTOM_DOMAIN="$(normalize_tunnel_domain "$1")"
        export AGENCY_TUNNEL_CLI_OVERRIDE="true"
        ;;
      *)
        echo "Unexpected extra argument: ${arg}" >&2
        usage >&2
        return 2
        ;;
    esac
    shift
  done

  if [ -n "${tunnel_provider_override}" ]; then
    export AGENCY_PUBLIC_TUNNEL_PROVIDER="${tunnel_provider_override}"
    export AGENCY_TUNNEL_CLI_OVERRIDE="true"
  fi
}

print_chat_endpoints() {
  local base_url="$1"

  echo "Discord endpoint: ${base_url}/integrations/conversations/adapters/discord/webhook"
  echo "Telegram endpoint: ${base_url}/integrations/conversations/adapters/telegram/webhook"
  echo "Telegram credential-scoped endpoint: ${base_url}/integrations/conversations/adapters/telegram/webhook?credential_id=<installation_id>"
  echo "WhatsApp endpoint: ${base_url}/integrations/conversations/adapters/whatsapp/webhook"
  echo "Telegram webhook auto-registration: enabled when AGENCY_PUBLIC_WEBHOOK_BASE_URL is provided."
}

_setup_status_python() {
  local python_bin=""

  if command_exists host_python; then
    python_bin="$(host_python 2>/dev/null || true)"
  fi
  if [ -z "${python_bin}" ]; then
    python_bin="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
  fi
  printf '%s\n' "${python_bin}"
}

setup_next_path() {
  local python_bin=""

  if ! command_exists curl; then
    printf '%s\n' "/setup"
    return 0
  fi

  python_bin="$(_setup_status_python)"
  if [ -z "${python_bin}" ]; then
    printf '%s\n' "/setup"
    return 0
  fi

  curl -fsS --max-time 2 "http://127.0.0.1:${BACKEND_PORT}/setup/status" 2>/dev/null | "${python_bin}" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    print("/setup")
    raise SystemExit(0)

path = str(payload.get("next_path") or "").strip()
if not path.startswith("/"):
    path = "/setup"
print(path)
' 2>/dev/null || printf '%s\n' "/setup"
}

startup_url_for_frontend() {
  printf 'http://localhost:%s%s\n' "${FRONTEND_PORT}" "$(setup_next_path)"
}

agency_frontend_reachable() {
  command_exists curl &&
    curl -fsS --max-time 2 "http://127.0.0.1:${FRONTEND_PORT}/login" 2>/dev/null |
      grep '<title>Agency' >/dev/null
}

record_public_endpoint_if_present() {
  local public_url="$1"
  shift

  if [ -z "${public_url}" ]; then
    return 0
  fi

  "$@" -m app.cli public-endpoint record \
    --provider "$(public_tunnel_provider_mode)" \
    --url "${public_url}" >/dev/null || true
}

run_startup_onboarding_sync() {
  local backend_only_hint="$1"
  local operator_hint="$2"
  shift 2

  if [ "${AGENCY_AUTO_SETUP_AGENTS:-true}" != "true" ]; then
    return 0
  fi

  echo "Running startup onboarding sync checks..."
  set +e
  "$@" main-agent --non-interactive
  local main_status=$?
  local recommended_status=0
  if [ "${main_status}" -eq 0 ]; then
    "$@" recommended-agents
    recommended_status=$?
  fi
  set -e

  if [ "${main_status}" -eq 0 ] && [ "${recommended_status}" -eq 0 ]; then
    echo "Startup onboarding sync checks complete."
    return 0
  fi

  echo "Warning: headless startup sync did not complete." >&2
  echo "Normal local onboarding can continue in the frontend at /setup." >&2
  echo "For backend-only runs, use: ${backend_only_hint}" >&2
  echo "For the operator path, ${operator_hint}" >&2
  if [ "${AGENCY_AUTO_SETUP_AGENTS_STRICT:-false}" = "true" ]; then
    if [ "${main_status}" -ne 0 ]; then
      return "${main_status}"
    fi
    return "${recommended_status}"
  fi
  echo "Continuing startup because AGENCY_AUTO_SETUP_AGENTS_STRICT is not true." >&2
  return 0
}
