# CLAUDE.md

Actionable reference for AI assistants working on this codebase.

## Project Identity

- **What:** Lightweight AI assistant framework (~6k lines Python) connecting LLMs to chat platforms with tools, memory, and skills
- **Entry point:** `nanobot gateway` → `cli/commands.py:gateway()` → `AgentLoop.run()`
- **Core loop:** `agent/loop.py:_process_message()` — context build → LLM call → tool execution loop → response

## Conventions

- Python 3.11+. Use `str | None` not `Optional[str]`. Use `list[str]` not `List[str]`.
- `loguru` for logging, never `logging` stdlib.
- Tools return error strings (`"Error: ..."`) — never raise exceptions from `execute()`.
- Async everywhere — all tool `execute()` methods, all provider `chat()` methods, all bus operations.
- Imports at top of file. No `import os` inside method bodies.
- JSON config uses **camelCase** (`oauthAccessToken`). Python uses **snake_case** (`oauth_access_token`). `config/loader.py` converts automatically.
- Pydantic `BaseModel` for config, `BaseSettings` for root `Config` class.

## Key Patterns

### Adding a Tool
1. Create class inheriting `Tool` (or `ContextAwareTool` if it needs channel/chat context) in `agent/tools/`
2. Implement: `name` (property), `description` (property), `parameters` (JSON Schema dict property), `async execute(**kwargs) -> str`
3. Register in `AgentLoop._register_default_tools()`
4. For subagent access: also register in `SubagentManager._run_subagent()` (subagents don't get message/spawn/cron tools)

### Adding a Provider
1. Subclass `LLMProvider` from `providers/base.py`
2. Implement `async chat(messages, tools, model, max_tokens, temperature) -> LLMResponse` and `get_default_model() -> str`
3. Wire into `providers/factory.py:make_provider()`

### Adding a Channel
1. Subclass `BaseChannel` from `channels/base.py`
2. Implement `start()`, `stop()`, `send(OutboundMessage)`
3. Add config model to `config/schema.py`
4. Register in `channels/manager.py`

### ContextAwareTool
Tools that need per-message channel/chat_id (MessageTool, SpawnTool, CronTool) inherit `ContextAwareTool` and implement `set_context(channel, chat_id)`. The registry calls `set_context()` on all such tools before each message.

## Important Files

| File | What it does |
|------|-------------|
| `agent/loop.py` | Core agent loop, message processing, /model command |
| `agent/context.py` | System prompt assembly from bootstrap files + memory + skills |
| `agent/engine.py` | Shared tool execution loop (used by agent + subagent) |
| `agent/commands.py` | Slash command handling (/model, future commands) |
| `agent/tools/base.py` | `Tool` and `ContextAwareTool` abstract base classes |
| `agent/tools/registry.py` | Tool registry with `set_context()` broadcast |
| `agent/tools/shell.py` | ExecTool with deny patterns, workspace restriction, git clone whitelist |
| `agent/subagent.py` | Background task execution with isolated tool registry |
| `providers/base.py` | `LLMProvider` ABC, `LLMResponse`, `ToolCallRequest` |
| `providers/factory.py` | `make_provider()` — unified provider creation |
| `providers/litellm_provider.py` | LiteLLM wrapper for OpenRouter, Gemini, Anthropic API, etc. |
| `providers/anthropic_oauth.py` | Claude CLI proxy for OAuth tokens |
| `config/schema.py` | Pydantic config: providers, channels, tools, agents |
| `config/loader.py` | JSON load/save with camelCase ↔ snake_case conversion |
| `session/manager.py` | JSONL conversation persistence |
| `channels/base.py` | `BaseChannel` ABC with ACL (`is_allowed()`) |
| `bus/queue.py` | Async message bus (inbound + outbound queues) |

## Gotchas

- **OAuth tokens can't hit the API directly.** They require cryptographic verification only the official Claude CLI provides. That's why `AnthropicOAuthProvider` proxies through `claude -p`.
- **`--dangerously-skip-permissions`** is required for headless Claude CLI operation (it blocks tool use without human approval otherwise).
- **Full env inheritance** — Claude CLI subprocess must inherit `os.environ` (not a minimal env) because Node.js needs PATH, HOME, etc.
- **OAuth model compatibility** — OAuth provider is only used when the model name contains "anthropic" or "claude". This prevents the shared `CLAUDE_CODE_OAUTH_TOKEN` env var from hijacking non-Anthropic models.
- **Session keys** are `channel:chat_id` strings. Colons in chat IDs could cause collisions (known limitation).
- **Config camelCase/snake_case** — VPS config files use camelCase. If you edit config programmatically, use `config/loader.py` save/load which handles conversion.
- **Telegram polling** gets transient `httpx.ReadError` — this auto-recovers, not a bug.

## What NOT to Do

- Don't raise exceptions from tool `execute()` — return `"Error: ..."` strings instead.
- Don't use `Optional[X]` — use `X | None`.
- Don't import inside function bodies — keep imports at module top.
- Don't hardcode model IDs — use config `model_aliases` or the `/model` command system.
- Don't add context-aware tools without inheriting `ContextAwareTool` — otherwise `set_context()` won't be called.
- Don't use `logging` — use `loguru.logger`.
- Don't send OAuth tokens to raw HTTP API endpoints — they won't work, use the CLI proxy.
- Don't add tools to subagents that can send messages or spawn further subagents.

## Testing

```bash
pytest tests/           # Run all tests
nanobot agent -m "Hi"   # Quick local smoke test
nanobot gateway         # Full gateway with channels
```

## Deployment

- VPS: `deploy@clawd-bot.tail250fd7.ts.net`
- Each bot instance: `HOME=/home/deploy/bots/{project}` (isolates config, workspace, sessions)
- Shared secrets: `/home/deploy/shared/secrets.env` loaded by systemd template
- Master service: `nanobot-master.service` — manages worker lifecycle
- Worker template: `nanobot@.service` — sandboxed with `ProtectSystem=strict`
- See `deploy/README.md` for full deployment docs
