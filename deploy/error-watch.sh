#!/bin/bash
# Streams ERROR/CRITICAL lines to a file, last 100 kept
journalctl -u velox -f --no-pager | grep --line-buffered -iE "ERROR|CRITICAL|fatal|exception|traceback" >> /var/log/velox-errors.log &
# Keep only last 100 lines
while true; do
    sleep 300
    tail -100 /var/log/velox-errors.log > /var/log/velox-errors.tmp 2>/dev/null
    mv /var/log/velox-errors.tmp /var/log/velox-errors.log 2>/dev/null
done
