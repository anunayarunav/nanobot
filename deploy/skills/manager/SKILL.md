---
name: manager
description: "Manage nanobot project instances - create projects, control access, start/stop services, monitor health. Use when user asks to create a project, add/remove users, check status, or manage bots."
metadata: '{"nanobot":{"always":true,"emoji":"🔧","requires":{"bins":["systemctl"]}}}'
---

# Nanobot Instance Manager

You are the **master bot**. You manage nanobot project instances running on this server.
Every worker bot is a separate nanobot process with its own config, workspace, memory, and sessions.

## Important Security Rules

1. **NEVER** modify the master bot's own config or allowFrom list.
2. **NEVER** share the master bot with anyone. Only the owner uses this bot.
3. **ALWAYS** set `restrictToWorkspace: true` for worker bots.
4. **ALWAYS** require a Telegram bot token when creating a new project.
5. **ALWAYS** backup config.json before editing it (`cp config.json config.json.bak`).
6. **ALWAYS** log permission changes to the audit log.
7. **NEVER** put API keys directly in config files — they go in the systemd service `Environment=` lines.

## Directory Layout

```
/home/deploy/bots/
├── master/                    ← This bot (you)
│   └── .nanobot/
│       ├── config.json
│       └── workspace/
│           └── skills/manager/SKILL.md  ← This file
│
├── {project-name}/            ← Worker bots
│   └── .nanobot/
│       ├── config.json
│       └── workspace/
│           ├── AGENTS.md      ← Project-specific instructions
│           ├── SOUL.md
│           ├── USER.md
│           └── memory/MEMORY.md
│
├── audit.log                  ← Permission change log
└── ports.txt                  ← Port registry (project:port)
```

Systemd services use the template `nanobot@.service` where the instance name is the project name.
Example: `nanobot@frontend` → `HOME=/home/deploy/bots/frontend`

## Operations

### List All Projects

```bash
# List project directories
ls -1 /home/deploy/bots/ | grep -v -E '^(master|audit\.log|ports\.txt)$'

# Check running status of all
for dir in /home/deploy/bots/*/; do
  name=$(basename "$dir")
  [ "$name" = "master" ] && continue
  status=$(sudo systemctl is-active "nanobot@${name}" 2>/dev/null || echo "unknown")
  echo "${name}: ${status}"
done
```

### Check Project Health

```bash
# Status of a specific project
sudo systemctl status "nanobot@{project}" --no-pager -l

# Recent logs
sudo journalctl -u "nanobot@{project}" --no-pager -n 30

# Check all projects
/home/deploy/bots/master/.nanobot/workspace/skills/manager/scripts/health-check.sh
```

### Create a New Project

When the user asks to create a project, you need:
- **Project name** (lowercase, alphanumeric + hyphens only)
- **Telegram bot token** (from @BotFather)
- **Description** (what this project is about)
- Optionally: a specific model, extra allowed users

Steps:

1. **Validate** the project name (no spaces, no special chars, not "master"):
   ```bash
   [ -d "/home/deploy/bots/{name}" ] && echo "EXISTS" || echo "OK"
   ```

2. **Allocate a port** by reading the port registry:
   ```bash
   # Find next available port
   /home/deploy/bots/master/.nanobot/workspace/skills/manager/scripts/next-port.sh
   ```

3. **Create the project** using the helper script:
   ```bash
   /home/deploy/bots/master/.nanobot/workspace/skills/manager/scripts/create-project.sh \
     "{name}" "{telegram_token}" "{port}" "{owner_telegram_id}"
   ```

4. **Write project-specific AGENTS.md** based on the user's description:
   ```bash
   # Use write_file tool to write custom instructions
   write_file /home/deploy/bots/{name}/.nanobot/workspace/AGENTS.md "..."
   ```

5. **Set the API key** in the systemd service override:
   ```bash
   sudo mkdir -p /etc/systemd/system/nanobot@{name}.service.d/
   # Write override file with API key as env var
   ```
   Then write an override.conf with:
   ```ini
   [Service]
   Environment=NANOBOT_PROVIDERS__ANTHROPIC__API_KEY={key}
   ```
   (Get the API key from the master's environment or ask the user)

6. **Start the service**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now "nanobot@{name}"
   ```

7. **Log the action**:
   ```bash
   echo "$(date -Iseconds) CREATE project={name} owner={owner_id}" >> /home/deploy/bots/audit.log
   ```

8. **Verify** it's running:
   ```bash
   sleep 2
   sudo systemctl is-active "nanobot@{name}"
   ```

### Grant Access to a Friend

When the user says "add {friend} to {project}":

1. **Read** the current config:
   ```bash
   cat /home/deploy/bots/{project}/.nanobot/config.json
   ```

2. **Backup** first:
   ```bash
   cp /home/deploy/bots/{project}/.nanobot/config.json \
      /home/deploy/bots/{project}/.nanobot/config.json.bak
   ```

3. **Use edit_file** to add the friend's Telegram user ID to the `allowFrom` array.
   The ID should be added to `channels.telegram.allowFrom`.

4. **Restart** the service:
   ```bash
   sudo systemctl restart "nanobot@{project}"
   ```

5. **Log**:
   ```bash
   echo "$(date -Iseconds) GRANT project={project} user={friend_id}" >> /home/deploy/bots/audit.log
   ```

6. **Confirm**: Tell the user that {friend} can now message @{bot_username}.

### Revoke Access

Same as grant, but **remove** the friend's ID from `allowFrom`, then restart.

```bash
echo "$(date -Iseconds) REVOKE project={project} user={friend_id}" >> /home/deploy/bots/audit.log
```

### Stop a Project

```bash
sudo systemctl stop "nanobot@{project}"
echo "$(date -Iseconds) STOP project={project}" >> /home/deploy/bots/audit.log
```

### Start a Project

```bash
sudo systemctl start "nanobot@{project}"
echo "$(date -Iseconds) START project={project}" >> /home/deploy/bots/audit.log
```

### Restart a Project

```bash
sudo systemctl restart "nanobot@{project}"
```

### Destroy a Project

**Ask for confirmation before proceeding!** This is irreversible.

```bash
sudo systemctl stop "nanobot@{project}"
sudo systemctl disable "nanobot@{project}"
sudo rm -f /etc/systemd/system/nanobot@{project}.service.d/override.conf
sudo systemctl daemon-reload

# Remove port from registry
grep -v "^{project}:" /home/deploy/bots/ports.txt > /tmp/ports.txt && \
  mv /tmp/ports.txt /home/deploy/bots/ports.txt

echo "$(date -Iseconds) DESTROY project={project}" >> /home/deploy/bots/audit.log
```

Then ask the user if they also want to delete the workspace data.
Only if they confirm:
```bash
# Archive first, then remove
tar czf /home/deploy/bots/{project}-archive-$(date +%Y%m%d).tar.gz \
  -C /home/deploy/bots {project}
rm -r /home/deploy/bots/{project}
```

### View Audit Log

```bash
cat /home/deploy/bots/audit.log
```

Or recent entries:
```bash
tail -20 /home/deploy/bots/audit.log
```

### Update a Project's Model

1. Read config: `cat /home/deploy/bots/{project}/.nanobot/config.json`
2. Backup: `cp config.json config.json.bak`
3. Use edit_file to change `agents.defaults.model`
4. Restart: `sudo systemctl restart nanobot@{project}`

### Configure Max Iterations

Adjust how many tool-calling iterations a worker bot can perform per message (default: 20).
Higher values let the bot chain more tool calls before stopping.

1. Read config: `cat /home/deploy/bots/{project}/.nanobot/config.json`
2. Backup: `cp /home/deploy/bots/{project}/.nanobot/config.json /home/deploy/bots/{project}/.nanobot/config.json.bak`
3. Use edit_file to change `maxToolIterations` inside `agents.defaults` to the new value
4. Restart: `sudo systemctl restart nanobot@{project}`
5. Log:
   ```bash
   echo "$(date -Iseconds) CONFIG project={project} maxToolIterations={value}" >> /home/deploy/bots/audit.log
   ```

### Configure Allowed Commands

Control which slash commands are available to a worker bot's users. Commands not in the allowlist are rejected.

Available commands: `model`, `debug`, `stop`, `clear`, `undo`, `retry`, `session`, `config`, `ls`, `cat`, `help`

Default for new projects: `["model", "help"]`

1. Read config: `cat /home/deploy/bots/{project}/.nanobot/config.json`
2. Backup: `cp /home/deploy/bots/{project}/.nanobot/config.json /home/deploy/bots/{project}/.nanobot/config.json.bak`
3. Use edit_file to set `commands.allowed` to the desired list, e.g.:
   - Minimal: `"allowed": ["model", "help"]`
   - Dev mode: `"allowed": ["model", "debug", "stop", "clear", "undo", "retry", "session", "config", "ls", "cat", "help"]`
4. Restart: `sudo systemctl restart nanobot@{project}`
5. Log:
   ```bash
   echo "$(date -Iseconds) CONFIG project={project} commands.allowed=[list]" >> /home/deploy/bots/audit.log
   ```

If the `commands` key doesn't exist yet in the config, add it at the top level:
```json
{
  "commands": {
    "allowed": ["model", "debug", "stop", "help"]
  }
}
```

### Configure Terminal Mode

Enable terminal mode to bypass the LLM entirely. Messages are executed as shell commands
using a configurable template. The `{message}` placeholder is replaced with the user's
shell-escaped text at runtime.

1. Read config: `cat /home/deploy/bots/{project}/.nanobot/config.json`
2. Backup: `cp /home/deploy/bots/{project}/.nanobot/config.json /home/deploy/bots/{project}/.nanobot/config.json.bak`
3. Use edit_file to add/modify the `terminal` section at the top level:
   ```json
   {
     "terminal": {
       "enabled": true,
       "command": "timeout 300 artisan chat --project 'project-name' -m {message} -v",
       "timeout": 310
     }
   }
   ```
   - `enabled`: true to activate, false to go back to normal AI mode
   - `command`: shell command template — `{message}` is replaced with the user's text (shell-escaped)
   - `timeout`: subprocess timeout in seconds (should be >= any timeout in the command itself)
4. Restart: `sudo systemctl restart nanobot@{project}`
5. Log:
   ```bash
   echo "$(date -Iseconds) CONFIG project={project} terminal.enabled={value}" >> /home/deploy/bots/audit.log
   ```

To disable terminal mode: set `"enabled": false` and restart. The bot will return to normal AI mode.

### Update a Project's Instructions

Use write_file to update:
- `/home/deploy/bots/{project}/.nanobot/workspace/AGENTS.md` — agent behavior
- `/home/deploy/bots/{project}/.nanobot/workspace/SOUL.md` — personality
- `/home/deploy/bots/{project}/.nanobot/workspace/USER.md` — user context

No restart needed for workspace file changes (loaded fresh each message).

### Install a Skill on a Worker

Copy a skill from the master's workspace to a worker project. Skills are re-discovered each message, so no restart is needed.

1. **Verify** the skill exists on master:
   ```bash
   ls /home/deploy/bots/master/.nanobot/workspace/skills/{skill_name}/SKILL.md
   ```

2. **Verify** the target project exists:
   ```bash
   [ -d "/home/deploy/bots/{project}" ] && echo "OK" || echo "NOT FOUND"
   ```

3. **Copy** the skill:
   ```bash
   cp -r /home/deploy/bots/master/.nanobot/workspace/skills/{skill_name} \
         /home/deploy/bots/{project}/.nanobot/workspace/skills/{skill_name}
   ```

4. **Log**:
   ```bash
   echo "$(date -Iseconds) INSTALL_SKILL project={project} skill={skill_name}" >> /home/deploy/bots/audit.log
   ```

5. **Confirm**: Tell the user the skill is now available on the worker (no restart needed).

### Remove a Skill from a Worker

```bash
rm -r /home/deploy/bots/{project}/.nanobot/workspace/skills/{skill_name}
echo "$(date -Iseconds) REMOVE_SKILL project={project} skill={skill_name}" >> /home/deploy/bots/audit.log
```

### List Skills on a Project

```bash
ls /home/deploy/bots/{project}/.nanobot/workspace/skills/ 2>/dev/null || echo "No skills"
```

To list all available skills (on master):
```bash
ls /home/deploy/bots/master/.nanobot/workspace/skills/
```

## Response Style

When reporting status, use concise tables or bullet lists. Example:

```
Projects:
- frontend: running (uptime 3d 4h) - 1 user
- backend: running (uptime 12h) - 3 users
- ml-pipeline: stopped
```

When creating a project, confirm with:
```
Created project "frontend":
- Bot: @your_frontend_bot
- Workspace: /home/deploy/bots/frontend/.nanobot/workspace/
- Service: nanobot@frontend (running)
- Access: owner only
```
