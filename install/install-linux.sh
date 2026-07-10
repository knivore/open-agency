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
ASSUME_YES="${AGENCY_INSTALL_ASSUME_YES:-false}"

usage() {
  cat <<'EOF'
Usage:
  ./install/install-linux.sh [--no-start] [--backend-only] [--install-dir <path>] [--tunnel-provider <auto|local|ngrok|cloudflare>]

Options:
  --no-start            Clone and bootstrap, but do not run ./agency start.
  --backend-only        Skip cloning open-agency-fe and set AGENCY_FRONTEND_ENABLED=false on first start.
  --install-dir <path>  Override the install root. Defaults to ~/OpenAgency.
  --ngrok               Start the first launch with an ngrok tunnel.
  --cloudflare          Start the first launch with a Cloudflare Tunnel.
  --tunnel-provider     Explicitly set auto, local, ngrok, or cloudflare for first launch.
  -y, --yes             Install missing distro packages without prompting.
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
    -y|--yes)
      ASSUME_YES="true"
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

  if [ "${ASSUME_YES}" = "true" ]; then
    return 0
  fi
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

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

sudo_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
    return 0
  fi
  if ! command_exists sudo; then
    echo "sudo is required to install missing packages. Install prerequisites manually or rerun as root." >&2
    return 1
  fi
  sudo "$@"
}

missing_commands() {
  local missing=()

  command_exists git || missing+=(git)
  command_exists curl || missing+=(curl)
  command_exists unzip || missing+=(unzip)
  command_exists tar || missing+=(tar)
  command_exists python3 || missing+=(python3)
  command_exists node || missing+=(node)
  command_exists npm || missing+=(npm)

  printf '%s\n' "${missing[@]}"
}

install_packages() {
  local commands=("$@")
  local packages=()
  local command_name=""

  if [ "${#commands[@]}" -eq 0 ]; then
    return 0
  fi

  if command_exists apt-get; then
    for command_name in "${commands[@]}"; do
      case "${command_name}" in
        node)
          packages+=(nodejs)
          ;;
        *)
          packages+=("${command_name}")
          ;;
      esac
    done
    sudo_cmd apt-get update
    sudo_cmd apt-get install -y "${packages[@]}" python3-venv python3-pip ca-certificates
    return 0
  fi
  if command_exists dnf; then
    for command_name in "${commands[@]}"; do
      case "${command_name}" in
        node)
          packages+=(nodejs)
          ;;
        *)
          packages+=("${command_name}")
          ;;
      esac
    done
    sudo_cmd dnf install -y "${packages[@]}" python3-pip python3-devel ca-certificates
    return 0
  fi
  if command_exists yum; then
    for command_name in "${commands[@]}"; do
      case "${command_name}" in
        node)
          packages+=(nodejs)
          ;;
        *)
          packages+=("${command_name}")
          ;;
      esac
    done
    sudo_cmd yum install -y "${packages[@]}" python3-pip python3-devel ca-certificates
    return 0
  fi
  if command_exists pacman; then
    for command_name in "${commands[@]}"; do
      case "${command_name}" in
        python3)
          packages+=(python)
          ;;
        node)
          packages+=(nodejs)
          ;;
        *)
          packages+=("${command_name}")
          ;;
      esac
    done
    sudo_cmd pacman -Sy --needed --noconfirm "${packages[@]}" python-pip ca-certificates
    return 0
  fi

  echo "Unsupported Linux package manager. Install these commands manually and rerun:" >&2
  printf '  %s\n' "${commands[@]}" >&2
  return 1
}

install_venv_support() {
  if command_exists apt-get; then
    sudo_cmd apt-get update
    sudo_cmd apt-get install -y python3-venv python3-pip
    return 0
  fi
  if command_exists dnf; then
    sudo_cmd dnf install -y python3-pip python3-devel
    return 0
  fi
  if command_exists yum; then
    sudo_cmd yum install -y python3-pip python3-devel
    return 0
  fi
  if command_exists pacman; then
    sudo_cmd pacman -Sy --needed --noconfirm python python-pip
    return 0
  fi

  echo "Unsupported Linux package manager. Install Python venv support manually and rerun." >&2
  return 1
}

ensure_prerequisites() {
  local missing=()
  local package=""

  mapfile -t missing < <(missing_commands)
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "Missing required commands: ${missing[*]}"
    if ! confirm "Install missing packages with the system package manager?" "y"; then
      echo "Install the missing packages and rerun this installer." >&2
      return 1
    fi
    install_packages "${missing[@]}"
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "python3 venv support is missing."
    if ! confirm "Install python3 venv support with the system package manager?" "y"; then
      echo "Install python3-venv or your distro equivalent and rerun this installer." >&2
      return 1
    fi
    install_venv_support
  fi

  for package in git curl unzip tar python3 node npm; do
    if ! command_exists "${package}"; then
      echo "Required command still missing after package install: ${package}" >&2
      return 1
    fi
  done
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

is_wsl() {
  grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null
}

ensure_docker_ready() {
  if ! command_exists docker; then
    echo "Docker is required before first start." >&2
    if is_wsl; then
      echo "Install Docker Desktop for Windows and enable WSL integration for this distro, then rerun this installer." >&2
    else
      echo "Install Docker Engine or Docker Desktop for Linux, then rerun this installer." >&2
    fi
    return 1
  fi

  if docker info >/dev/null 2>&1; then
    return 0
  fi

  if is_wsl; then
    echo "Docker is installed but not reachable from WSL." >&2
    echo "Start Docker Desktop for Windows and enable Settings > Resources > WSL integration for this distro." >&2
    return 1
  fi

  if command_exists systemctl; then
    echo "Docker is installed but not running. Trying to start Docker..."
    sudo_cmd systemctl start docker || true
    if wait_for_docker; then
      return 0
    fi
  fi

  echo "Docker is installed but not ready. Start Docker and rerun this installer." >&2
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

linux_name() {
  if [ -r /etc/os-release ]; then
    . /etc/os-release
    printf '%s\n' "${PRETTY_NAME:-Linux}"
    return 0
  fi
  printf '%s\n' "Linux"
}

if [ "$(uname -s)" != "Linux" ]; then
  echo "install-linux.sh only supports Linux and WSL." >&2
  exit 1
fi

echo "Installing Open Agency into ${INSTALL_ROOT}"
if is_wsl; then
  echo "Detected WSL: $(linux_name)"
else
  echo "Detected Linux: $(linux_name)"
fi

ensure_prerequisites

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
