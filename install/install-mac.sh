#!/usr/bin/env bash

set -euo pipefail

BACKEND_REPO_URL="${AGENCY_BACKEND_REPO_URL:-https://github.com/knivore/open-agency.git}"
FRONTEND_REPO_URL="${AGENCY_FRONTEND_REPO_URL:-https://github.com/knivore/open-agency-fe.git}"
INSTALL_ROOT="${AGENCY_INSTALL_DIR:-${HOME}/OpenAgency}"
BACKEND_DIR="${INSTALL_ROOT}/open-agency"
FRONTEND_DIR="${INSTALL_ROOT}/open-agency-fe"
START_AFTER_INSTALL="true"
CLONE_FRONTEND="true"
TUNNEL_PROVIDER="auto"

usage() {
  cat <<'EOF'
Usage:
  ./install/install-mac.sh [--no-start] [--backend-only] [--install-dir <path>] [--tunnel-provider <auto|local|ngrok|cloudflare>]

Options:
  --no-start            Clone and bootstrap, but do not run ./agency start.
  --backend-only        Skip cloning open-agency-fe and set AGENCY_FRONTEND_ENABLED=false on first start.
  --install-dir <path>  Override the install root. Defaults to ~/OpenAgency.
  --ngrok               Start the first launch with an ngrok tunnel.
  --cloudflare          Start the first launch with a Cloudflare Tunnel.
  --tunnel-provider     Explicitly set local, ngrok, or cloudflare for first launch.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-start)
      START_AFTER_INSTALL="false"
      ;;
    --backend-only)
      CLONE_FRONTEND="false"
      ;;
    --install-dir)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Missing value for --install-dir" >&2
        exit 2
      fi
      INSTALL_ROOT="$1"
      BACKEND_DIR="${INSTALL_ROOT}/open-agency"
      FRONTEND_DIR="${INSTALL_ROOT}/open-agency-fe"
      ;;
    --ngrok)
      TUNNEL_PROVIDER="ngrok"
      ;;
    --cloudflare)
      TUNNEL_PROVIDER="cloudflare"
      ;;
    --tunnel-provider)
      shift
      if [ "$#" -eq 0 ]; then
        echo "Missing value for --tunnel-provider" >&2
        exit 2
      fi
      case "$1" in
        auto)
          TUNNEL_PROVIDER="auto"
          ;;
        local|none)
          TUNNEL_PROVIDER="none"
          ;;
        ngrok|cloudflare)
          TUNNEL_PROVIDER="$1"
          ;;
        *)
          echo "Unsupported tunnel provider: $1" >&2
          exit 2
          ;;
      esac
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

confirm() {
  local prompt="$1"
  local default_answer="${2:-y}"
  local reply=""
  local suffix="Y/n"

  if [ "${default_answer}" = "n" ]; then
    suffix="y/N"
  fi

  while true; do
    printf '%s [%s] ' "${prompt}" "${suffix}"
    read -r reply || true
    if [ -z "${reply}" ]; then
      [ "${default_answer}" = "y" ] && return 0
      return 1
    fi
    case "${reply}" in
      y|Y|yes|YES)
        return 0
        ;;
      n|N|no|NO)
        return 1
        ;;
    esac
  done
}

need_command() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi
  echo "Missing required command: $1" >&2
  return 1
}

ensure_homebrew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "Homebrew is required to install missing dependencies in non-interactive mode." >&2
    echo "Install Homebrew from https://brew.sh and rerun this installer." >&2
    return 1
  fi
  if ! confirm "Homebrew is not installed. Install it now?" "y"; then
    echo "Homebrew is required for simple install on macOS." >&2
    return 1
  fi
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

ensure_homebrew_package() {
  local command_name="$1"
  local formula="$2"

  if command -v "${command_name}" >/dev/null 2>&1; then
    return 0
  fi
  if ! ensure_homebrew; then
    return 1
  fi
  echo "Installing ${formula} with Homebrew..."
  brew install "${formula}"
}

wait_for_docker() {
  local attempts=0

  while [ "${attempts}" -lt 30 ]; do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  return 1
}

ensure_docker_ready() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Desktop is required before first start." >&2
    echo "Install it from https://www.docker.com/products/docker-desktop/ and run this installer again." >&2
    return 1
  fi

  if docker info >/dev/null 2>&1; then
    return 0
  fi

  if [ -d "/Applications/Docker.app" ]; then
    echo "Starting Docker Desktop..."
    open -a Docker >/dev/null 2>&1 || true
    if wait_for_docker; then
      return 0
    fi
  fi

  echo "Docker Desktop is installed but not ready." >&2
  echo "Open Docker Desktop, wait until it reports that Docker is running, then rerun this installer." >&2
  return 1
}

sync_repo() {
  local url="$1"
  local dir="$2"

  if [ -d "${dir}/.git" ]; then
    echo "Updating ${dir}..."
    git -C "${dir}" pull --ff-only
    return 0
  fi

  mkdir -p "$(dirname "${dir}")"
  echo "Cloning ${url} into ${dir}..."
  git clone "${url}" "${dir}"
}

start_command_args() {
  case "${TUNNEL_PROVIDER}" in
    none)
      printf '%s\n' "-local"
      ;;
    ngrok)
      printf '%s\n' "-ngrok"
      ;;
    cloudflare)
      printf '%s\n' "-cloudflare"
      ;;
    auto)
      printf '%s\n' ""
      ;;
  esac
}

mac_arch() {
  case "$(uname -m)" in
    arm64)
      printf '%s\n' "Apple Silicon"
      ;;
    x86_64)
      printf '%s\n' "Intel"
      ;;
    *)
      printf '%s\n' "$(uname -m)"
      ;;
  esac
}

if [ "$(uname -s)" != "Darwin" ]; then
  echo "install-mac.sh only supports macOS." >&2
  exit 1
fi

echo "Installing Open Agency into ${INSTALL_ROOT}"
echo "Detected macOS architecture: $(mac_arch)"

ensure_homebrew_package git git
ensure_homebrew_package python3 python
ensure_homebrew_package node node
need_command npm

if ! ensure_docker_ready; then
  exit 1
fi

sync_repo "${BACKEND_REPO_URL}" "${BACKEND_DIR}"

if [ "${CLONE_FRONTEND}" = "true" ]; then
  sync_repo "${FRONTEND_REPO_URL}" "${FRONTEND_DIR}"
else
  echo "Skipping open-agency-fe clone because --backend-only was selected."
fi

cd "${BACKEND_DIR}"

echo "Bootstrapping local dependencies..."
./agency bootstrap
./agency doctor

if [ "${START_AFTER_INSTALL}" != "true" ]; then
  echo
  echo "Install complete."
  if [ "${TUNNEL_PROVIDER}" != "auto" ]; then
    echo "Next step: cd ${BACKEND_DIR} && ./agency start $(start_command_args)"
  else
    echo "Next step: cd ${BACKEND_DIR} && ./agency start"
  fi
  exit 0
fi

echo "Starting Open Agency..."
if [ "${CLONE_FRONTEND}" = "true" ]; then
  if [ "${TUNNEL_PROVIDER}" != "auto" ]; then
    ./agency start "$(start_command_args)"
  else
    ./agency start
  fi
else
  if [ "${TUNNEL_PROVIDER}" != "auto" ]; then
    AGENCY_FRONTEND_ENABLED=false ./agency start "$(start_command_args)"
  else
    AGENCY_FRONTEND_ENABLED=false ./agency start
  fi
fi
