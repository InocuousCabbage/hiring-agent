#!/bin/bash
# deploy/cron_entry.sh
#
# Sets up a cron job to run the agent every 30 minutes.
# Hiring.cafe sends weekly alerts, but we poll frequently to catch them quickly.
#
# Usage: bash deploy/cron_entry.sh

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Verify flock is available — required to prevent overlapping runs.
FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
if [ -z "${FLOCK_BIN}" ]; then
    echo "ERROR: flock not found on PATH. Install util-linux (Linux) or 'brew install util-linux' (macOS)." >&2
    exit 1
fi

# Check every 30 minutes
CRON_SCHEDULE="*/30 * * * *"
# flock -n on a pidfile: if a previous run is still executing, skip this tick
# rather than racing (double Gmail reads, double PDF renders, duplicate applies).
# -n = non-blocking (exit immediately if the lock is held); -E 0 = exit 0 on
# lock-held so cron doesn't email an error every 30 min for a working system.
LOCK_FILE="/tmp/hiring-agent.lock"
CRON_CMD="cd ${SCRIPT_DIR} && ${FLOCK_BIN} -n -E 0 ${LOCK_FILE} ${SCRIPT_DIR}/.venv/bin/python src/main.py >> /var/log/hiring-agent.log 2>&1"

# Add to crontab (idempotent — removes old entry first)
(crontab -l 2>/dev/null | grep -v "hiring-agent" ; echo "${CRON_SCHEDULE} ${CRON_CMD} # hiring-agent") | crontab -

echo "✓ Cron job installed:"
echo "  Schedule: ${CRON_SCHEDULE}"
echo "  Command: ${CRON_CMD}"
echo ""
echo "View logs: tail -f /var/log/hiring-agent.log"
echo "Remove:    crontab -l | grep -v hiring-agent | crontab -"
