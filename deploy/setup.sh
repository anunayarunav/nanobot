#!/usr/bin/env bash
# setup.sh — Bootstrap the nanobot master/worker architecture on a VPS
# Run as: deploy user on the VPS
#
# Usage: ./setup.sh <master_telegram_token> <your_telegram_id> <llm_api_key>
#
# Example:
#   ./setup.sh "7123456789:AAH..." "123456789" "sk-ant-api03-..."

set -euo pipefail

MASTER_TOKEN="${1:?Usage: ./setup.sh <master_telegram_token> <your_telegram_id> <llm_api_key>}"
OWNER_ID="${2:?Missing your Telegram user ID}"
API_KEY="${3:?Missing LLM API key (e.g. Anthropic, OpenRouter)}"

BOTS_DIR="/home/deploy/bots"
MASTER_DIR="${BOTS_DIR}/master"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================"
echo "  Nanobot Master/Worker Setup"
echo "================================================"
echo ""
echo "Bots directory:   ${BOTS_DIR}"
echo "Master bot token: ${MASTER_TOKEN:0:10}..."
echo "Owner Telegram ID: ${OWNER_ID}"
echo "API key:          ${API_KEY:0:10}..."
echo ""

# ─── Step 1: Check prerequisites ───────────────────────────────────────────

echo "[1/8] Checking prerequisites..."

# Check nanobot is installed
if ! command -v nanobot &>/dev/null; then
  echo "  nanobot not found. Installing..."
  pip install --user nanobot-ai
fi

NANOBOT_BIN=$(command -v nanobot)
echo "  nanobot: ${NANOBOT_BIN}"

# Check python3
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found"
  exit 1
fi
echo "  python3: $(command -v python3)"

# Check systemctl
if ! command -v systemctl &>/dev/null; then
  echo "ERROR: systemctl not found (is this a systemd-based system?)"
  exit 1
fi
echo "  systemctl: OK"

# ─── Step 2: Create directory structure ────────────────────────────────────

echo "[2/8] Creating directory structure..."

mkdir -p "${MASTER_DIR}/.nanobot/workspace/memory"
mkdir -p "${MASTER_DIR}/.nanobot/workspace/skills"
mkdir -p "${MASTER_DIR}/.nanobot/sessions"

# Create audit log and port registry
touch "${BOTS_DIR}/audit.log"
touch "${BOTS_DIR}/ports.txt"

echo "  ${BOTS_DIR}/ created"

# ─── Step 3: Write master config ──────────────────────────────────────────

echo "[3/8] Writing master bot config..."

cat > "${MASTER_DIR}/.nanobot/config.json" << JSONEOF
{
  "agents": {
    "defaults": {
      "workspace": "${MASTER_DIR}/.nanobot/workspace",
      "model": "anthropic/claude-sonnet-4-5-20250929",
      "maxTokens": 8192,
      "temperature": 0.7,
      "maxToolIterations": 25
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "${MASTER_TOKEN}",
      "allowFrom": ["${OWNER_ID}"]
    }
  },
  "providers": {
    "anthropic": {
      "apiKey": "${API_KEY}"
    }
  },
  "gateway": {
    "port": 18790
  },
  "tools": {
    "restrictToWorkspace": false
  }
}
JSONEOF

chmod 600 "${MASTER_DIR}/.nanobot/config.json"
echo "  config.json written (mode 600)"

# ─── Step 4: Write master workspace files ─────────────────────────────────

echo "[4/8] Writing master workspace files..."

cat > "${MASTER_DIR}/.nanobot/workspace/AGENTS.md" << 'MDEOF'
# Master Bot Instructions

You are the **master management bot**. Your job is to manage nanobot project instances.

## Your Capabilities

- Create new project bots (each with its own Telegram bot, workspace, and config)
- Grant/revoke access for friends to specific projects
- Start, stop, restart project services
- Monitor health and view logs of all projects
- Update project configurations (model, instructions, etc.)

## Important

- You have elevated system access. Be careful with commands.
- Always confirm destructive actions (destroy, revoke) before proceeding.
- Log all permission changes to the audit log.
- Refer to the `manager` skill for detailed operation procedures.

## Quick Reference

- Projects live at: /home/deploy/bots/{name}/
- Services: nanobot@{name} (systemd template)
- Ports registry: /home/deploy/bots/ports.txt
- Audit log: /home/deploy/bots/audit.log
MDEOF

cat > "${MASTER_DIR}/.nanobot/workspace/SOUL.md" << 'MDEOF'
# Soul

I am the master nanobot — a secure, efficient system administrator.

## Personality

- Professional and precise
- Security-conscious — I always verify before destructive actions
- Concise in status reports, thorough in explanations when asked

## Values

- Security first — never compromise access controls
- Reliability — services should stay running
- Transparency — always log what I do
MDEOF

cat > "${MASTER_DIR}/.nanobot/workspace/USER.md" << MDEOF
# User

## Owner

- Telegram ID: ${OWNER_ID}
- Role: System administrator (only user of this master bot)
MDEOF

cat > "${MASTER_DIR}/.nanobot/workspace/memory/MEMORY.md" << 'MDEOF'
# Long-term Memory

## System Info

- All project bots are at /home/deploy/bots/
- Systemd template: nanobot@{project}.service
- Port range starts at 18791 (master uses 18790)
- Master bot's config is NOT to be modified via commands

## Projects

(Auto-updated as projects are created)
MDEOF

echo "  Workspace files written"

# ─── Step 5: Install manager skill ────────────────────────────────────────

echo "[5/8] Installing manager skill..."

SKILL_SRC="${DEPLOY_DIR}/skills/manager"
SKILL_DST="${MASTER_DIR}/.nanobot/workspace/skills/manager"

if [ -d "$SKILL_SRC" ]; then
  cp -r "$SKILL_SRC" "$SKILL_DST"
  chmod +x "${SKILL_DST}/scripts/"*.sh 2>/dev/null || true
  echo "  Manager skill installed"
else
  echo "  WARNING: Manager skill source not found at ${SKILL_SRC}"
  echo "  You'll need to copy it manually"
fi

# ─── Step 6: Install systemd services ─────────────────────────────────────

echo "[6/8] Installing systemd services..."

# Update nanobot binary path in service files
NANOBOT_PATH=$(command -v nanobot)

# Master service
sudo cp "${DEPLOY_DIR}/templates/nanobot-master.service" /etc/systemd/system/nanobot-master.service
sudo sed -i "s|/home/deploy/.local/bin/nanobot|${NANOBOT_PATH}|g" /etc/systemd/system/nanobot-master.service
echo "  nanobot-master.service installed"

# Worker template service
sudo cp "${DEPLOY_DIR}/templates/nanobot@.service" /etc/systemd/system/nanobot@.service
sudo sed -i "s|/home/deploy/.local/bin/nanobot|${NANOBOT_PATH}|g" /etc/systemd/system/nanobot@.service
echo "  nanobot@.service template installed"

# ─── Step 7: Install sudoers ──────────────────────────────────────────────

echo "[7/8] Installing sudoers config..."

sudo cp "${DEPLOY_DIR}/sudoers.d/nanobot-deploy" /etc/sudoers.d/nanobot-deploy
sudo chmod 440 /etc/sudoers.d/nanobot-deploy

# Validate sudoers syntax
if sudo visudo -c -f /etc/sudoers.d/nanobot-deploy; then
  echo "  sudoers config installed and validated"
else
  echo "  ERROR: sudoers syntax invalid! Removing..."
  sudo rm /etc/sudoers.d/nanobot-deploy
  exit 1
fi

# ─── Step 8: Start master bot ─────────────────────────────────────────────

echo "[8/8] Starting master bot..."

sudo systemctl daemon-reload
sudo systemctl enable --now nanobot-master

sleep 2

if sudo systemctl is-active --quiet nanobot-master; then
  echo "  Master bot is RUNNING"
else
  echo "  WARNING: Master bot failed to start. Check logs:"
  echo "  sudo journalctl -u nanobot-master --no-pager -n 20"
fi

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "================================================"
echo "  Setup Complete!"
echo "================================================"
echo ""
echo "Master bot:     nanobot-master (systemd service)"
echo "Config:         ${MASTER_DIR}/.nanobot/config.json"
echo "Workspace:      ${MASTER_DIR}/.nanobot/workspace/"
echo "Audit log:      ${BOTS_DIR}/audit.log"
echo ""
echo "Next steps:"
echo "  1. Open Telegram and message your master bot"
echo "  2. Say: 'Create a new project called frontend"
echo "     with bot token 1234:ABC...'"
echo "  3. The master bot will set everything up for you"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status nanobot-master     # Master status"
echo "  sudo journalctl -u nanobot-master -f     # Master logs"
echo "  sudo systemctl status nanobot@frontend   # Worker status"
echo "  cat ${BOTS_DIR}/audit.log                # Audit trail"
echo ""
echo "$(date -Iseconds) SETUP master owner=${OWNER_ID}" >> "${BOTS_DIR}/audit.log"
