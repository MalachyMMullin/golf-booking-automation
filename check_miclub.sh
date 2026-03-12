#!/bin/bash
# Monitor MiClub connectivity — checks every 30 min, logs results
# Run: ~/golf-booking-automation/check_miclub.sh &

LOG="$HOME/golfbot_logs/miclub_monitor.log"
URL="https://macquarielinks.miclub.com.au/security/login.msp"
mkdir -p "$HOME/golfbot_logs"

echo "$(date '+%Y-%m-%d %H:%M:%S') — MiClub monitor started" >> "$LOG"

while true; do
    HTTP_CODE=$(curl -s --connect-timeout 10 -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null)
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    if [ "$HTTP_CODE" != "000" ]; then
        echo "$TIMESTAMP — UNBLOCKED! HTTP $HTTP_CODE" >> "$LOG"
        # Send a macOS notification
        osascript -e "display notification \"MiClub is reachable! HTTP $HTTP_CODE\" with title \"Golf Bot\" sound name \"Glass\"" 2>/dev/null
        echo "$TIMESTAMP — Monitor complete, site is back." >> "$LOG"
        exit 0
    else
        echo "$TIMESTAMP — Still blocked (HTTP $HTTP_CODE)" >> "$LOG"
    fi

    sleep 1800  # 30 minutes
done
