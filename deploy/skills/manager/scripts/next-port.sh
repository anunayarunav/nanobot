#!/usr/bin/env bash
# next-port.sh — Find the next available port for a new project
# Ports start at 18791 (master uses 18790)

set -uo pipefail

PORTS_FILE="/home/deploy/bots/ports.txt"
BASE_PORT=18791

if [ ! -f "$PORTS_FILE" ]; then
  echo "$BASE_PORT"
  exit 0
fi

# Find the highest port in use
max_port=$BASE_PORT
while IFS=: read -r _ port; do
  port=$(echo "$port" | tr -d '[:space:]')
  if [ -n "$port" ] && [ "$port" -gt "$max_port" ] 2>/dev/null; then
    max_port="$port"
  fi
done < "$PORTS_FILE"

echo $((max_port + 1))
