#!/bin/bash
cd /opt/velox-app

# Fetch latest
git fetch origin main -q 2>/dev/null

# Check if behind
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date): New commits detected, deploying..."
    
    # Stash any runtime data changes so pull never conflicts
    git stash -q 2>/dev/null
    git pull origin main -q
    
    # Reinstall deps if requirements changed
    if git diff "$LOCAL" "$REMOTE" -- requirements.txt | grep -q .; then
        echo "$(date): requirements.txt changed, installing deps..."
        .venv/bin/pip install -r requirements.txt -q
    fi
    
    systemctl restart velox
    echo "$(date): Velox restarted with $(git log --oneline -1)"
else
    exit 0
fi
