#!/bin/bash
# deploy/cron_entry.sh
#
# Sets up a cron job to run the agent every 30 minutes.
# Hiring.cafe sends weekly alerts, but we poll frequently to catch them quickly.
#
# Usage: bash deploy/cron_entry.sh

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Check every 30 minutes
CRON_SCHEDULE="*/30 * * * *"
CRON_CMD="cd ${SCRIPT_DIR} && ${SCRIPT_DIR}/.venv/bin/python src/main.py >> /var/log/hiring-agent.log 2>&1"

# Add to crontab (idempotent — removes old entry first)
(crontab -l 2>/dev/null | grep -v "hiring-agent" ; echo "${CRON_SCHEDULE} ${CRON_CMD} # hiring-agent") | crontab -

echo "✓ Cron job installed:"
echo "  Schedule: ${CRON_SCHEDULE}"
echo "  Command: ${CRON_CMD}"
echo ""
echo "View logs: tail -f /var/log/hiring-agent.log"
echo "Remove:    crontab -l | grep -v hiring-agent | crontab -"
