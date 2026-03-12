#!/bin/bash
# Test run — book Friday 20 Mar (tee sheet already open, no ballot)
# Patches timing gates to run immediately via sed, then restores original.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG_DIR="$HOME/golfbot_logs"
LOG_FILE="$LOG_DIR/test_fri20_$(date +%Y-%m-%d_%H-%M-%S).log"
SCRIPT="$SCRIPT_DIR/booking_script_parallel.py"

mkdir -p "$LOG_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export GOLFBOT_RUN_ROOT="$LOG_DIR"
export PYTHONUNBUFFERED=1

# Load secrets (Discord bot token etc.)
set -a; source "$SCRIPT_DIR/.env"; set +a

# Target: Friday 20 Mar
export OVERRIDE_TARGET_DAY="Fri"
export OVERRIDE_TARGET_DATE="20 Mar"

# Patch timing gates to 00:00 so they pass immediately (backup first)
cp "$SCRIPT" "$SCRIPT.bak_test"
sed -i '' 's/LOGIN_TIME        = (17, 15)/LOGIN_TIME        = (0, 0)/' "$SCRIPT"
sed -i '' 's/QUEUE_JOIN_TIME   = (18, 30)/QUEUE_JOIN_TIME   = (0, 0)/' "$SCRIPT"
sed -i '' 's/BOOKING_OPEN_TIME = (19,  0)/BOOKING_OPEN_TIME = (0, 0)/' "$SCRIPT"
# Zero out stagger between workers for test
sed -i '' 's/LOGIN_STAGGER_SECS = 480/LOGIN_STAGGER_SECS = 0/' "$SCRIPT"

# Ensure we restore on exit
trap 'cp "$SCRIPT.bak_test" "$SCRIPT"; rm -f "$SCRIPT.bak_test"; echo "Restored original script."' EXIT

cd "$SCRIPT_DIR"
source "$VENV/bin/activate"

echo "=== Test run: Fri 20 Mar — started $(date) ===" | tee "$LOG_FILE"
echo "Log: $LOG_FILE"
echo "Timing gates patched to 00:00 (immediate). Target: Fri 20 Mar."

python3 -u booking_script_parallel.py 2>&1 | tee -a "$LOG_FILE"
