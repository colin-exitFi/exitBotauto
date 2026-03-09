#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
LOCKFILE="/tmp/velox.lock"
CHILD_PID=""

cleanup() {
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            kill -9 "$CHILD_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$LOCKFILE"
    exit 0
}

trap cleanup EXIT INT TERM

if [ -f "$LOCKFILE" ]; then
    OLD_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date): Stopping existing exitBotauto process $OLD_PID"
        OLD_CHILDREN=$(pgrep -P "$OLD_PID" 2>/dev/null || true)
        if [ -n "$OLD_CHILDREN" ]; then
            kill $OLD_CHILDREN 2>/dev/null || true
        fi
        kill "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 10); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            if [ -n "$OLD_CHILDREN" ]; then
                kill -9 $OLD_CHILDREN 2>/dev/null || true
            fi
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
    fi
    rm -f "$LOCKFILE"
fi

echo $$ > "$LOCKFILE"

while true; do
    echo "$(date): Starting exitBotauto..."
    python -m src.main &
    CHILD_PID=$!
    wait "$CHILD_PID"
    EXIT_CODE=$?
    CHILD_PID=""
    echo "$(date): Bot exited with code $EXIT_CODE. Restarting in 5s..."
    sleep 5
done
