#!/usr/bin/env bash
# install.sh — set up a Claude Discord Bot systemd service for a project
#
# Usage:
#   ./install.sh <project_dir> <service_name>
#
# Example:
#   ./install.sh /home/pi/flint_and_flag flint-bot
#   ./install.sh /home/pi/rosie rosie-bot
#
# Prerequisites:
#   - <project_dir>/config/.env must contain:
#       DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, TMUX_SESSION, CLAUDE_PROJECTS_DIR
#   - /home/pi/venv must have discord.py and python-dotenv installed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_PY="$SCRIPT_DIR/bot.py"
VENV_PYTHON="/home/pi/venv/bin/python3"

usage() {
    echo "Usage: $0 <project_dir> <service_name>"
    echo "  project_dir   — absolute path to the Claude project directory"
    echo "  service_name  — systemd service name (e.g., flint-bot, rosie-bot)"
    exit 1
}

[[ $# -ne 2 ]] && usage

PROJECT_DIR="$1"
SERVICE_NAME="$2"
ENV_FILE="$PROJECT_DIR/config/.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Validate
[[ -d "$PROJECT_DIR" ]] || { echo "Error: $PROJECT_DIR is not a directory"; exit 1; }
[[ -f "$ENV_FILE" ]]    || { echo "Error: $ENV_FILE not found"; exit 1; }
[[ -f "$BOT_PY" ]]      || { echo "Error: bot.py not found at $BOT_PY"; exit 1; }
[[ -f "$VENV_PYTHON" ]] || { echo "Error: Python venv not found at $VENV_PYTHON"; exit 1; }

# Warn about missing required vars
for var in DISCORD_BOT_TOKEN DISCORD_CHANNEL_ID TMUX_SESSION CLAUDE_PROJECTS_DIR; do
    if ! grep -qE "^(export )?${var}=" "$ENV_FILE"; then
        echo "Warning: $var not found in $ENV_FILE"
    fi
done

echo "Installing: $SERVICE_NAME"
echo "  Project dir : $PROJECT_DIR"
echo "  Env file    : $ENV_FILE"
echo "  Bot script  : $BOT_PY"
echo ""

cat > /tmp/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Claude Discord Bot — ${SERVICE_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_PYTHON} ${BOT_PY} --env-file ${ENV_FILE}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Generated service file:"
echo "---"
cat /tmp/${SERVICE_NAME}.service
echo "---"
echo ""
read -p "Install to $SERVICE_FILE and enable? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

sudo cp /tmp/${SERVICE_NAME}.service "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "Done."
echo "  Status : systemctl status $SERVICE_NAME"
echo "  Logs   : journalctl -u $SERVICE_NAME -f"
