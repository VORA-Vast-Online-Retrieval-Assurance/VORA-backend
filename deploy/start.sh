#!/usr/bin/env bash
# Container entrypoint: restore the database, start SearXNG and the backup loop, then VORA.
# On a stop signal VORA shuts down first, then a final snapshot is uploaded.
set -euo pipefail
cd /app
mkdir -p "$VORA_STATE_DIR"

# 1. The disk of a free Space is empty after every restart: bring back the last snapshot (no-op without a token).
python deploy/backup.py restore || echo "restore skipped"

# 2. The Chromium that ships with this Playwright image.
if [ -z "${VORA_BROWSER_BINARY:-}" ] || [ ! -x "${VORA_BROWSER_BINARY}" ]; then
  VORA_BROWSER_BINARY="$(find "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}" -type f -name chrome -path '*chromium-*' ! -path '*headless_shell*' | head -n 1)"
  export VORA_BROWSER_BINARY
fi
echo "browser: ${VORA_BROWSER_BINARY:-not found}"

# 3. SearXNG on loopback only; VORA reaches it at VORA_SEARXNG_URL.
if [ -z "${SEARXNG_SECRET:-}" ]; then
  SEARXNG_SECRET="$(python -c 'import secrets; print(secrets.token_hex(24))')"
fi
export SEARXNG_SECRET SEARXNG_BIND_ADDRESS=127.0.0.1 SEARXNG_PORT=8080
(cd /opt/searxng/src && exec /opt/searxng/venv/bin/python -m searx.webapp >/tmp/searxng.log 2>&1) &
SEARXNG_PID=$!

# 4. Periodic snapshots of the database.
python deploy/backup.py loop &
BACKUP_PID=$!

# 5. VORA in the foreground (as a child, so this script can take the final snapshot after it stops).
python app.py &
VORA_PID=$!

stop() {
  echo "stopping"
  kill -TERM "$VORA_PID" 2>/dev/null || true
  wait "$VORA_PID" 2>/dev/null || true
  kill "$BACKUP_PID" "$SEARXNG_PID" 2>/dev/null || true
  python deploy/backup.py once || echo "final snapshot failed"
  exit 0
}
trap stop TERM INT

STATUS=0
wait "$VORA_PID" || STATUS=$?
kill "$BACKUP_PID" "$SEARXNG_PID" 2>/dev/null || true
python deploy/backup.py once || true
exit "$STATUS"
