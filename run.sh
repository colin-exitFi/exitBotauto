#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
    echo "$(date): Starting exitBotauto..."
    python -m src.main
    EXIT_CODE=$?
    echo "$(date): Bot exited with code $EXIT_CODE. Restarting in 5s..."
    sleep 5
done
