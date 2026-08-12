#!/usr/bin/env bash

# Keep host-level tunnel processes responsive to authenticated browser requests
# without giving the API container permission to execute host commands directly.
set -euo pipefail

LAUNCHER_SCRIPT="${1:?Pass the platform launcher script as the first argument.}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTROL_PATH="${AGENCY_TUNNEL_RUNTIME_CONTROL_PATH:-${ROOT_DIR}/.agency/tunnel-runtime-control.json}"
POLL_SECONDS="${AGENCY_TUNNEL_SUPERVISOR_POLL_SECONDS:-2}"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN="python"
fi

read_requested_id() {
  "${PYTHON_BIN}" - "${CONTROL_PATH}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)

request_id = payload.get("request_id")
if payload.get("state") == "requested" and isinstance(request_id, str) and request_id:
    print(request_id)
PY
}

write_state() {
  local request_id="$1"
  local state="$2"
  local message="$3"

  "${PYTHON_BIN}" - "${CONTROL_PATH}" "${request_id}" "${state}" "${message}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
request_id, state, message = sys.argv[2:]
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)

# A newer browser request supersedes this reload. Do not overwrite its state.
if payload.get("request_id") != request_id:
    raise SystemExit(0)

payload["state"] = state
payload["message"] = message
payload["updated_at"] = datetime.now(timezone.utc).isoformat()
temporary = path.with_suffix(f"{path.suffix}.tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY
}

heartbeat() {
  "${PYTHON_BIN}" - "${CONTROL_PATH}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    payload = {"state": "idle"}

payload["supervisor_updated_at"] = datetime.now(timezone.utc).isoformat()
temporary = path.with_suffix(f"{path.suffix}.tmp")
path.parent.mkdir(parents=True, exist_ok=True)
temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY
}

while true; do
  heartbeat
  request_id="$(read_requested_id || true)"
  if [ -n "${request_id}" ]; then
    write_state "${request_id}" "applying" "Reloading the selected public tunnel."
    if "${LAUNCHER_SCRIPT}" tunnel-reload; then
      write_state "${request_id}" "ready" "The selected public tunnel is running."
    else
      write_state "${request_id}" "failed" "The selected tunnel could not be started. Check launcher logs."
    fi
  fi
  sleep "${POLL_SECONDS}"
done
