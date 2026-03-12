#!/bin/bash
# Golf booking — THURSDAY run → books the following Saturday (default script logic)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG_DIR="$HOME/golfbot_logs"
LOG_FILE="$LOG_DIR/cron_sat_$(date +%Y-%m-%d_%H-%M-%S).log"

mkdir -p "$LOG_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export GOLFBOT_RUN_ROOT="$LOG_DIR"
export PYTHONUNBUFFERED=1

# Load secrets (Discord bot token etc.)
set -a; source "$SCRIPT_DIR/.env"; set +a

cd "$SCRIPT_DIR"
git pull origin main >> "$LOG_FILE" 2>&1

source "$VENV/bin/activate"
python3 -u booking_script_parallel.py >> "$LOG_FILE" 2>&1
