"""Experiment scaffolding: create new bot project directories with boilerplate.

Each experiment is a terminal-mode micro-agent: a Python script that reads a
JSON envelope from stdin and writes JSONL frames to stdout.  Nanobot handles
Telegram, Stripe payments, and credit gating — the script handles business logic.
"""

import json
from pathlib import Path

from loguru import logger


# Default config template (camelCase for disk format)
_CONFIG_TEMPLATE = {
    "channels": {
        "telegram": {
            "enabled": True,
            "token": "FILL_IN_YOUR_BOT_TOKEN",
            "allowFrom": [],
        }
    },
    "terminal": {
        "enabled": True,
        "command": "python bot.py",
        "timeout": 120,
        "protocol": "rich",
        "providers": {
            "anthropic": {
                "apiKeys": ["FILL_IN_YOUR_API_KEY"],
                "models": ["claude-sonnet-4-5-20250929"],
            }
        },
    },
    "payments": {
        "enabled": True,
        "stripeApiKey": "FILL_IN_STRIPE_KEY",
        "stripeWebhookSecret": "FILL_IN_WEBHOOK_SECRET",
        "freeCredits": 3,
        "creditPacks": [
            {"credits": 25, "priceCents": 499, "label": "$4.99 for 25 answers"},
            {"credits": 60, "priceCents": 999, "label": "$9.99 for 60 answers"},
            {"credits": 150, "priceCents": 1999, "label": "$19.99 for 150 answers"},
        ],
        "webhookPort": 8080,
    },
    "extensions": [
        {
            "classPath": "nanobot.extensions.credits.CreditExtension",
            "enabled": True,
        }
    ],
    "commands": {"allowed": []},
}

_BOT_TEMPLATE = '''\
#!/usr/bin/env python3
"""Terminal micro-agent for nanobot.

Reads a JSON envelope from stdin, processes the user's message,
and writes JSONL frames to stdout.

See: docs/TERMINAL_PROTOCOL.md in the nanobot repo for the full spec.
"""

import json
import sys

import anthropic


def emit(frame_type: str, **kwargs) -> None:
    """Write a JSONL frame to stdout."""
    print(json.dumps({"type": frame_type, **kwargs}), flush=True)


def main() -> None:
    envelope = json.loads(sys.stdin.readline())
    text = envelope["text"]
    providers = envelope.get("providers", {})

    # Handle /start command
    if text.strip() == "/start":
        emit(
            "message",
            text=(
                "Welcome! I\\'m your AI assistant. "
                "Send me a message and I\\'ll help you out."
            ),
        )
        return

    # Get API key from envelope
    api_key = providers.get("anthropic", {}).get("api_keys", [None])[0]
    if not api_key:
        emit("error", text="No API key configured.", code="NO_API_KEY")
        sys.exit(1)

    model = providers.get("anthropic", {}).get("models", ["claude-sonnet-4-5-20250929"])[0]

    emit("progress", text="Thinking...")

    # ---- Customize the system prompt and logic below ----

    system_prompt = (
        "You are a helpful AI assistant. "
        "Keep responses concise and mobile-friendly."
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": text}],
    )

    answer = response.content[0].text
    emit("message", text=answer)


if __name__ == "__main__":
    main()
'''

_RUN_SCRIPT = """\
#!/bin/bash
# Launch this bot experiment using nanobot.
# Sets HOME to this directory so nanobot finds .nanobot/config.json here.
cd "$(dirname "$0")"
HOME="$(pwd)" exec nanobot gateway
"""


def _readme_template(name: str) -> str:
    return f"""\
# {name}

Telegram bot experiment built on the nanobot framework with Stripe credit-based payments.

## Architecture

This bot uses nanobot's **terminal protocol**: nanobot handles Telegram, credit
gating, and Stripe payments.  `bot.py` is a micro-agent that handles the
business logic — it reads a JSON envelope from stdin and writes JSONL frames
to stdout.

```
User (Telegram) -> nanobot gateway -> credit check -> bot.py (subprocess)
                                                       |
                                                       v
                                                    Claude API
                                                       |
                                                       v
                                                  JSONL stdout -> nanobot -> Telegram
```

## Quick Start

1. Fill in credentials in `.nanobot/config.json`:
   - `channels.telegram.token` — from [@BotFather](https://t.me/BotFather)
   - `terminal.providers.anthropic.apiKeys` — from [Anthropic](https://console.anthropic.com/)
   - `payments.stripeApiKey` — from [Stripe Dashboard](https://dashboard.stripe.com/apikeys)
   - `payments.stripeWebhookSecret` — from Stripe webhook settings

2. Install Python dependencies:
   ```bash
   pip install anthropic
   ```

3. Customize `bot.py` — change the system prompt and logic.

4. Run locally:
   ```bash
   chmod +x run.sh
   ./run.sh
   ```

   Or run directly:
   ```bash
   HOME=$(pwd) nanobot gateway
   ```

## Stripe Webhook Setup

Point your Stripe webhook to: `https://your-domain:8080/webhook/stripe`

Events to listen for: `checkout.session.completed`

For local testing: `stripe listen --forward-to localhost:8080/webhook/stripe`

## Credit System

- {_CONFIG_TEMPLATE['payments']['freeCredits']} free answers on first use
- Credit packs configured in `.nanobot/config.json` under `payments.creditPacks`
- Credits are checked by nanobot BEFORE `bot.py` runs — no credit logic needed in your script
- After `bot.py` responds, nanobot deducts 1 credit automatically

## Terminal Protocol

`bot.py` communicates with nanobot via the terminal protocol:

**Input** (stdin JSON envelope):
```json
{{
  "version": 1,
  "text": "user's message",
  "channel": "telegram",
  "chat_id": "123456789",
  "user_data_dir": "/path/to/users/123456789",
  "providers": {{"anthropic": {{"api_keys": ["sk-ant-..."]}}}}
}}
```

**Output** (stdout JSONL frames):
```json
{{"type": "progress", "text": "Thinking..."}}
{{"type": "message", "text": "Here's your answer"}}
{{"type": "error", "text": "Something went wrong", "code": "ERR"}}
```

See `docs/TERMINAL_PROTOCOL.md` in the nanobot repo for the full spec.

## Deployment (VPS)

```bash
# On the VPS:
sudo systemctl enable nanobot@{name}
sudo systemctl start nanobot@{name}
```

The systemd template uses `HOME=/home/deploy/bots/{name}` for isolation.
"""


def create_experiment(name: str, base_dir: Path) -> Path:
    """Create a new experiment project directory with all boilerplate.

    Creates:
      {base_dir}/{name}/
        .nanobot/config.json      — terminal-mode config with payments
        bot.py                    — micro-agent script (customize this)
        README.md                 — setup/deploy docs
        run.sh                   — HOME=$(pwd) nanobot gateway

    Returns the created project path.
    """
    project = base_dir / name
    if project.exists():
        raise FileExistsError(f"Project already exists: {project}")

    # Create directory structure
    config_dir = project / ".nanobot"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Write config
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(_CONFIG_TEMPLATE, indent=2))

    # Write bot script
    (project / "bot.py").write_text(_BOT_TEMPLATE)

    # Write README and run script
    (project / "README.md").write_text(_readme_template(name))
    run_sh = project / "run.sh"
    run_sh.write_text(_RUN_SCRIPT)
    run_sh.chmod(0o755)

    logger.info(f"Created experiment project: {project}")
    return project


def list_experiments(base_dir: Path) -> list[dict]:
    """List all experiment directories with their config status."""
    if not base_dir.exists():
        return []

    results = []
    for item in sorted(base_dir.iterdir()):
        if not item.is_dir():
            continue
        config_path = item / ".nanobot" / "config.json"
        has_config = config_path.exists()
        configured = False
        if has_config:
            try:
                data = json.loads(config_path.read_text())
                token = data.get("channels", {}).get("telegram", {}).get("token", "")
                configured = bool(token) and token != "FILL_IN_YOUR_BOT_TOKEN"
            except (json.JSONDecodeError, KeyError):
                pass

        results.append({
            "name": item.name,
            "path": str(item),
            "has_config": has_config,
            "has_bot": (item / "bot.py").exists(),
            "configured": configured,
        })

    return results
