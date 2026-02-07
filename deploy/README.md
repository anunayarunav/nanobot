# Nanobot Multi-Project Deployment

Master/worker architecture for managing multiple nanobot projects from Telegram.

## Architecture

```
Your Telegram
  |
  +-- @master_bot --> nanobot-master (manages everything)
  |                   allowFrom: [YOU only]
  |                   restrictToWorkspace: false
  |
  +-- @project_a  --> nanobot@project-a (sandboxed worker)
  |                   allowFrom: [YOU, friends...]
  |                   restrictToWorkspace: true
  |
  +-- @project_b  --> nanobot@project-b (sandboxed worker)
                      allowFrom: [YOU]
                      restrictToWorkspace: true
```

## Prerequisites

- A VPS with systemd (Ubuntu 22.04+ / Debian 12+)
- Python 3.11+
- A `deploy` user with sudo access
- nanobot installed: `pip install nanobot-ai`

## Quick Start

### 1. Get your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) on Telegram. It will reply with your numeric ID.

### 2. Create a master Telegram bot

Message [@BotFather](https://t.me/BotFather):
```
/newbot
Name: My Nanobot Master
Username: your_master_bot
```
Save the token it gives you.

### 3. Deploy to VPS

```bash
# From your local machine
scp -r deploy/ deploy@clawd-bot.tail250fd7.ts.net:~/nanobot-deploy/

# SSH into VPS
ssh deploy@clawd-bot.tail250fd7.ts.net

# Run setup
cd ~/nanobot-deploy
chmod +x setup.sh
./setup.sh "BOT_TOKEN" "YOUR_TELEGRAM_ID" "sk-ant-YOUR-API-KEY"
```

### 4. Start using it

Open Telegram, message your master bot:

> "Create a new project called frontend with bot token 7777:AAAA..."

The master bot will:
1. Create the project directory and config
2. Install the systemd service
3. Start the worker bot
4. Report back with status

## File Structure

```
deploy/
  setup.sh                          # One-time VPS bootstrap
  README.md                         # This file
  skills/manager/
    SKILL.md                        # Master bot's management skill
    scripts/
      create-project.sh             # Creates a new worker project
      health-check.sh               # Checks all project health
      next-port.sh                  # Allocates next gateway port
  templates/
    nanobot-master.service          # Systemd service for master bot
    nanobot@.service                # Systemd template for worker bots
  sudoers.d/
    nanobot-deploy                  # Passwordless sudo for service mgmt
```

## On the VPS after setup

```
/home/deploy/bots/
  master/.nanobot/                  # Master bot (system manager)
  frontend/.nanobot/                # Worker: frontend project
  backend/.nanobot/                 # Worker: backend project
  audit.log                         # All permission changes
  ports.txt                         # Port registry
```

## Common Operations

### Via Telegram (master bot)

- "Show all projects" -- lists projects with status
- "Create project X with token Y" -- creates a new worker
- "Add user 12345 to frontend" -- grants access
- "Remove user 12345 from frontend" -- revokes access
- "Stop backend" -- stops a worker
- "Show logs for frontend" -- recent logs
- "Destroy project X" -- removes a worker (asks confirmation)

### Via SSH (manual)

```bash
# Service management
sudo systemctl status nanobot-master
sudo systemctl status nanobot@frontend
sudo journalctl -u nanobot@frontend -f

# View audit log
cat /home/deploy/bots/audit.log

# Manually edit a project's config
nano /home/deploy/bots/frontend/.nanobot/config.json
sudo systemctl restart nanobot@frontend
```

## Security Notes

- The master bot is the crown jewel. Its `allowFrom` ONLY contains your ID.
- Worker bots have `restrictToWorkspace: true` -- they can't escape their directory.
- Worker systemd services have `ProtectSystem=strict` + `ReadWritePaths` limits.
- API keys can be stored as systemd environment overrides instead of config files.
- All permission changes are logged to `audit.log`.
- Sudoers config is tightly scoped to `nanobot@*` services only.
