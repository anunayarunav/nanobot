#!/usr/bin/env bash
# create-project.sh — Create a new nanobot worker project
# Usage: create-project.sh <name> <telegram_token> <port> <owner_telegram_id>

set -euo pipefail

NAME="${1:?Usage: create-project.sh <name> <telegram_token> <port> <owner_id>}"
TOKEN="${2:?Missing telegram bot token}"
PORT="${3:?Missing port number}"
OWNER_ID="${4:?Missing owner telegram ID}"

BOTS_DIR="/home/deploy/bots"
PROJECT_DIR="${BOTS_DIR}/${NAME}"
NANOBOT_DIR="${PROJECT_DIR}/.nanobot"
WORKSPACE_DIR="${NANOBOT_DIR}/workspace"

# Validate name
if [[ ! "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "ERROR: Project name must be lowercase alphanumeric with hyphens only"
  exit 1
fi

if [ "$NAME" = "master" ]; then
  echo "ERROR: Cannot create a project named 'master'"
  exit 1
fi

if [ -d "$PROJECT_DIR" ]; then
  echo "ERROR: Project '${NAME}' already exists at ${PROJECT_DIR}"
  exit 1
fi

echo "Creating project '${NAME}'..."

# Create directory structure
mkdir -p "${WORKSPACE_DIR}/memory"
mkdir -p "${WORKSPACE_DIR}/skills"
mkdir -p "${NANOBOT_DIR}/sessions"

# Write config.json
cat > "${NANOBOT_DIR}/config.json" << JSONEOF
{
  "agents": {
    "defaults": {
      "workspace": "${WORKSPACE_DIR}",
      "model": "anthropic/claude-sonnet-4-5-20250929",
      "maxTokens": 8192,
      "temperature": 0.7,
      "maxToolIterations": 20
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "${TOKEN}",
      "allowFrom": ["${OWNER_ID}"]
    }
  },
  "providers": {},
  "gateway": {
    "port": ${PORT}
  },
  "tools": {
    "restrictToWorkspace": true
  },
  "commands": {
    "allowed": ["model", "help"]
  },
  "extensions": []
}
JSONEOF

# Write default AGENTS.md
cat > "${WORKSPACE_DIR}/AGENTS.md" << 'MDEOF'
# Agent Instructions

You are a project assistant. Be concise, accurate, and helpful.

## Guidelines

- Explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
MDEOF

# Write default SOUL.md
cat > "${WORKSPACE_DIR}/SOUL.md" << 'MDEOF'
# Soul

I am a project-focused AI assistant.

## Personality

- Helpful and professional
- Concise and to the point
- Focused on the project at hand

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
MDEOF

# Write default USER.md
cat > "${WORKSPACE_DIR}/USER.md" << 'MDEOF'
# User

Information about project users goes here.
MDEOF

# Write default MEMORY.md
cat > "${WORKSPACE_DIR}/memory/MEMORY.md" << 'MDEOF'
# Long-term Memory

This file stores important information that persists across sessions.
MDEOF

# Register port
echo "${NAME}:${PORT}" >> "${BOTS_DIR}/ports.txt"

# Log
echo "$(date -Iseconds) CREATE project=${NAME} port=${PORT} owner=${OWNER_ID}" >> "${BOTS_DIR}/audit.log"

echo "OK: Project '${NAME}' created at ${PROJECT_DIR}"
echo "Next: Set API key in systemd override, then start with: sudo systemctl enable --now nanobot@${NAME}"
