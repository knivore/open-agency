#!/bin/sh

set -eu

wait_for_tcp() {
  host="$1"
  port="$2"
  label="$3"

  echo "Waiting for ${label} on ${host}:${port}..."
  python - "$host" "$port" "$label" <<'PY'
import socket
import sys
import time

host = sys.argv[1]
port = int(sys.argv[2])
label = sys.argv[3]

deadline = time.time() + 120
last_error = None

while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"{label} is reachable")
            raise SystemExit(0)
    except OSError as exc:
        last_error = exc
        time.sleep(1)

raise SystemExit(f"Timed out waiting for {label}: {last_error}")
PY
}

DB_HOST="${DATABASE_HOST:-postgres}"
DB_PORT="${DATABASE_PORT:-5432}"
REDIS_HOST_VALUE="${REDIS_HOST:-redis}"
REDIS_PORT_VALUE="${REDIS_PORT:-6379}"

wait_for_tcp "$DB_HOST" "$DB_PORT" "Postgres"
wait_for_tcp "$REDIS_HOST_VALUE" "$REDIS_PORT_VALUE" "Redis"

echo "Applying Alembic migrations..."
python -m alembic upgrade head

echo "Starting backend..."
if [ "${BACKEND_RELOAD:-false}" = "true" ]; then
  exec python -m uvicorn app.api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload --reload-dir /app/app
fi

exec python -m uvicorn app.api.main:create_app --factory --host 0.0.0.0 --port 8000
