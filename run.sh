#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
LOCKFILE="/tmp/velox.lock"

cleanup() {
    rm -f "$LOCKFILE"
    exit 0
}

trap cleanup EXIT INT TERM

if [ -f "$LOCKFILE" ]; then
    OLD_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date): Stopping existing exitBotauto process $OLD_PID"
        kill -9 "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f "$LOCKFILE"
fi

echo $$ > "$LOCKFILE"

while true; do
    echo "$(date): Starting exitBotauto..."
    python -m src.main
    EXIT_CODE=$?
    echo "$(date): Bot exited with code $EXIT_CODE. Restarting in 5s..."
    sleep 5
done
