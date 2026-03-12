#!/bin/bash
# Golf booking — FRIDAY run → books the following Sunday (override target)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG_DIR="$HOME/golfbot_logs"
LOG_FILE="$LOG_DIR/cron_sun_$(date +%Y-%m-%d_%H-%M-%S).log"

mkdir -p "$LOG_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export GOLFBOT_RUN_ROOT="$LOG_DIR"
export PYTHONUNBUFFERED=1

# Load secrets (Discord bot token etc.)
set -a; source "$SCRIPT_DIR/.env"; set +a

# Compute the Sunday 9 days from now (next-next Sunday from Friday)
NEXT_SUNDAY=$(/opt/homebrew/bin/python3 -c "
from datetime import datetime, timedelta
try:
    import zoneinfo; tz = zoneinfo.ZoneInfo('Australia/Sydney')
except: tz = None
now = datetime.now(tz)
days_ahead = (6 - now.weekday()) % 7 or 7  # 6=Sunday; next Sunday
target = now + timedelta(days=days_ahead + 7)  # +7 = next-next Sunday (9 days from Fri)
print(target.strftime('%-d %b'))
")
export OVERRIDE_TARGET_DAY=Sun
export OVERRIDE_TARGET_DATE="$NEXT_SUNDAY"

cd "$SCRIPT_DIR"
git pull origin main >> "$LOG_FILE" 2>&1

source "$VENV/bin/activate"
echo "Target: Sun $OVERRIDE_TARGET_DATE" >> "$LOG_FILE"
python3 -u booking_script_parallel.py >> "$LOG_FILE" 2>&1
