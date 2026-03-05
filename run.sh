#!/bin/bash
# exitBotauto - Auto-restart wrapper
# Usage: ./run.sh

cd "$(dirname "$0")"

# Activate venv if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "🤖 exitBotauto starting..."
echo "Dashboard: http://localhost:8421"
echo "Press Ctrl+C to stop"
echo ""

while true; do
    python src/main.py
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -eq 0 ]; then
        echo "Bot exited cleanly."
        break
    fi
    
    echo ""
    echo "⚠️  Bot crashed (exit code $EXIT_CODE). Restarting in 10s..."
    echo "Press Ctrl+C to stop restart."
    sleep 10
done
