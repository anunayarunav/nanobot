#!/usr/bin/env bash
# health-check.sh — Check health of all nanobot project instances
# Usage: health-check.sh [project-name]

set -uo pipefail

BOTS_DIR="/home/deploy/bots"

check_project() {
  local name="$1"
  local service="nanobot@${name}"
  local config="${BOTS_DIR}/${name}/.nanobot/config.json"

  # Service status
  local active
  active=$(sudo systemctl is-active "$service" 2>/dev/null || echo "inactive")

  # Uptime (if running)
  local uptime="-"
  if [ "$active" = "active" ]; then
    uptime=$(sudo systemctl show "$service" --property=ActiveEnterTimestamp --value 2>/dev/null | xargs -I{} date -d "{}" +%s 2>/dev/null || echo "")
    if [ -n "$uptime" ]; then
      local now
      now=$(date +%s)
      local diff=$((now - uptime))
      local days=$((diff / 86400))
      local hours=$(( (diff % 86400) / 3600 ))
      uptime="${days}d ${hours}h"
    else
      uptime="running"
    fi
  fi

  # User count from allowFrom
  local users=0
  if [ -f "$config" ]; then
    users=$(python3 -c "
import json
with open('${config}') as f:
    c = json.load(f)
af = c.get('channels',{}).get('telegram',{}).get('allowFrom',[])
print(len(af))
" 2>/dev/null || echo "?")
  fi

  # Recent errors
  local errors
  errors=$(sudo journalctl -u "$service" --no-pager -n 50 --since "1 hour ago" -p err 2>/dev/null | grep -c "" || echo "0")

  printf "%-20s %-10s %-12s %-8s %-8s\n" "$name" "$active" "$uptime" "$users" "$errors"
}

# Header
printf "%-20s %-10s %-12s %-8s %-8s\n" "PROJECT" "STATUS" "UPTIME" "USERS" "ERRORS"
printf "%-20s %-10s %-12s %-8s %-8s\n" "-------" "------" "------" "-----" "------"

if [ -n "${1:-}" ]; then
  # Check specific project
  check_project "$1"
else
  # Check all projects
  for dir in "${BOTS_DIR}"/*/; do
    name=$(basename "$dir")
    [ "$name" = "master" ] && continue
    check_project "$name"
  done
fi

# Summary
echo ""
total=$(find "${BOTS_DIR}" -maxdepth 1 -mindepth 1 -type d ! -name master | wc -l)
running=$(for d in "${BOTS_DIR}"/*/; do
  n=$(basename "$d")
  [ "$n" = "master" ] && continue
  sudo systemctl is-active "nanobot@${n}" 2>/dev/null
done | grep -c "^active$" || echo "0")
echo "Total: ${total} projects, ${running} running"
