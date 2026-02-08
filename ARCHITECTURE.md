# Architecture

nanobot is a lightweight AI assistant framework (~3,500 lines of Python) that connects LLMs to chat platforms (Telegram, Discord, WhatsApp, Feishu) with tool use, memory, and skills. It is built around an async message bus that decouples channels from the agent core.

## Runtime Flow

What happens when a user sends a Telegram message:

1. **Channel receives message** — `TelegramChannel.start()` polls for updates, calls `BaseChannel._handle_message()` which checks ACL (`is_allowed()`), creates an `InboundMessage`, publishes to the `MessageBus` inbound queue.

2. **Agent consumes message** — `AgentLoop.run()` blocks on `bus.consume_inbound()`. When a message arrives, it calls `_process_message()`.

3. **Context is built** — `ContextBuilder.build_messages()` assembles: system prompt (from `AGENTS.md`, `SOUL.md`, `USER.md`, memory, skills) + conversation history (from `Session`) + the new user message.

4. **Tool context is set** — `ToolRegistry.set_context()` updates all context-aware tools (message, spawn, cron) with the current channel and chat ID.

5. **Agent loop runs** — Calls `provider.chat()` with messages + tool definitions. If the response contains tool calls, executes them via `ToolRegistry.execute()`, appends results, and loops. Repeats until the LLM produces a final text response or `max_iterations` is hit.

6. **Response is saved and sent** — Final content is saved to `Session` (JSONL on disk), then published as an `OutboundMessage` to the bus.

7. **Channel delivers response** — `ChannelManager._dispatch_outbound()` picks up the message and routes it to the correct channel's `.send()` method.

```
Telegram/Discord/...       MessageBus         AgentLoop
        |                      |                  |
  _handle_message() -----> inbound queue ----> _process_message()
                                                  |
                                            ContextBuilder
                                            + ToolRegistry
                                            + provider.chat()
                                                  |
                           outbound queue <---- OutboundMessage
        |                      |
    .send() <--------- _dispatch_outbound()
```

## Package Map

| Package | Purpose | Key Files |
|---------|---------|-----------|
| `agent/` | Core agent loop, context building, memory, skills | `loop.py`, `engine.py`, `commands.py`, `context.py`, `memory.py`, `skills.py`, `subagent.py` |
| `agent/tools/` | Tool base class, registry, built-in tools | `base.py`, `registry.py`, `filesystem.py`, `shell.py`, `web.py`, `message.py`, `spawn.py`, `cron.py` |
| `extensions/` | Extension system: lifecycle hooks + built-in extensions | `base.py`, `manager.py`, `compaction.py` |
| `bus/` | Async message bus decoupling channels from agent | `queue.py`, `events.py` |
| `channels/` | Chat platform adapters | `base.py`, `manager.py`, `telegram.py`, `discord.py`, `whatsapp.py`, `feishu.py` |
| `config/` | Pydantic config schema + JSON loader | `schema.py`, `loader.py` |
| `providers/` | LLM provider abstraction | `base.py`, `litellm_provider.py`, `anthropic_oauth.py`, `factory.py` |
| `session/` | JSONL conversation persistence | `manager.py` |
| `cron/` | Scheduled task execution | `service.py`, `types.py` |
| `heartbeat/` | Periodic agent wake-up | `service.py` |
| `cli/` | Typer CLI commands | `commands.py` |
| `utils/` | Path helpers, date formatting | `helpers.py` |

## Key Abstractions

### Tool (`agent/tools/base.py`)
Abstract base class. Each tool defines `name`, `description`, `parameters` (JSON Schema), and `async execute(**kwargs) -> str`. All tools return strings — errors are returned as `"Error: ..."` strings, never raised. Parameter validation uses the JSON Schema definition.

### ContextAwareTool (`agent/tools/base.py`)
Subclass of `Tool` for tools that need per-message context (channel, chat_id). Implements `set_context(channel, chat_id)`. The registry automatically calls this on all context-aware tools before each message. Used by `MessageTool`, `SpawnTool`, `CronTool`.

### ToolRegistry (`agent/tools/registry.py`)
Dict-based tool registry. `register(tool)`, `unregister(name)`, `get(name)`, `execute(name, args)`, `get_definitions()` (returns OpenAI-format tool schemas). Also provides `set_context()` which iterates all `ContextAwareTool` instances.

### LLMProvider (`providers/base.py`)
Abstract base. Single method: `async chat(messages, tools, model, max_tokens, temperature) -> LLMResponse`. `LLMResponse` contains `content`, `tool_calls`, `finish_reason`, `usage`. Two implementations:

- **LiteLLMProvider** — Wraps [litellm](https://github.com/BerriAI/litellm) for OpenRouter, Anthropic API, OpenAI, Gemini, DeepSeek, Groq, and many others. Handles model name prefixing and gateway detection.
- **AnthropicOAuthProvider** — Proxies through `claude -p` CLI for Anthropic OAuth tokens (`sk-ant-oat01-...`). These tokens require cryptographic verification only the official CLI provides. The CLI acts as its own agent — nanobot's tools are bypassed; the CLI uses its own internal bash/file tools.

### MessageBus (`bus/queue.py`)
Two `asyncio.Queue`s: inbound (channels → agent) and outbound (agent → channels). Channels publish inbound messages; the agent loop consumes them. Outbound messages are dispatched to channel-specific subscribers.

### BaseChannel (`channels/base.py`)
Abstract base for chat platforms. Implements `start()`, `stop()`, `send()`. Includes ACL via `is_allowed()` checking `allow_from` lists in config. Four implementations: Telegram (polling), Discord (WebSocket gateway), WhatsApp (Node.js bridge), Feishu (WebSocket).

### Session (`session/manager.py`)
Conversation state stored as JSONL files in `~/.nanobot/sessions/`. Session key = `channel:chat_id`. `get_history(max_messages=50)` returns recent messages in OpenAI format. `add_message(role, content, **kwargs)` supports arbitrary extra fields.

### SubagentManager (`agent/subagent.py`)
Spawns background asyncio tasks with isolated tool registries (no message/spawn/cron tools — subagents can't send messages or spawn further subagents). Results are announced back via the bus as system messages to the origin chat.

### Config (`config/schema.py`)
Pydantic `BaseSettings` with nested models for agents, channels, providers, tools. `get_provider(model)` does keyword-based model-to-provider matching. Environment variable override via `NANOBOT_` prefix.

## Provider System

### Provider Selection at Startup
`providers/factory.py:make_provider()` selects the provider:
1. If OAuth token is available AND model is Anthropic-compatible → `AnthropicOAuthProvider`
2. Otherwise → `LiteLLMProvider` with the matched API key

OAuth tokens can come from config (`providers.anthropic.oauthAccessToken`) or the `CLAUDE_CODE_OAUTH_TOKEN` environment variable (used by worker bots via shared secrets).

### Runtime Model Switching
The `/model` command in `agent/commands.py` hot-swaps `agent.provider` and `agent.model` at runtime. Model aliases (like `cc`, `opus`, `gemini`) map to specific model IDs and provider modes. Aliases can be configured in `agents.modelAliases` or fall back to built-in defaults.

### OAuth Flow
```
User message → AgentLoop → AnthropicOAuthProvider
    → builds text prompt from messages
    → spawns: claude -p <prompt> --model <model> --output-format json --dangerously-skip-permissions
    → env: CLAUDE_CODE_OAUTH_TOKEN=<token>
    → parses JSON result → LLMResponse
```

The `--dangerously-skip-permissions` flag is required for headless operation. The CLI must inherit the full `os.environ` (not a minimal env) because Node.js needs PATH.

## Tool System

### Adding a Tool

1. Create a class inheriting `Tool` (or `ContextAwareTool` if it needs channel/chat context)
2. Implement: `name`, `description`, `parameters` (JSON Schema dict), `async execute(**kwargs) -> str`
3. Register in `AgentLoop._register_default_tools()`

### Built-in Tools

| Tool | File | Description |
|------|------|-------------|
| `read_file` | `filesystem.py` | Read file contents |
| `write_file` | `filesystem.py` | Write/create files |
| `edit_file` | `filesystem.py` | Search-and-replace edits |
| `list_dir` | `filesystem.py` | List directory contents |
| `exec` | `shell.py` | Execute shell commands (with safety guards) |
| `web_search` | `web.py` | Brave Search API |
| `web_fetch` | `web.py` | Fetch and extract web page content |
| `message` | `message.py` | Send messages to users on any channel |
| `spawn` | `spawn.py` | Spawn background subagent tasks |
| `cron` | `cron.py` | Schedule recurring tasks |

### Shell Safety
`ExecTool` (`shell.py`) has layered guards:
- **Deny patterns** — Blocks `rm -rf`, `dd`, `shutdown`, fork bombs, etc.
- **Workspace restriction** — When `restrictToWorkspace: true`, all file paths in commands must be within the workspace directory
- **Git clone whitelist** — `tools.exec.allowedGitRepos` config lists allowed repo URL patterns (e.g., `github.com/user/*`). URLs are normalized before matching.

## Extension System

Extensions are lifecycle hooks that plug into the agent message pipeline without modifying the core loop. They are loaded from config and run in pipeline order.

### Extension Base Class (`extensions/base.py`)

`Extension` provides four async hook methods (all no-ops by default — override what you need):

| Hook | Signature | When |
|------|-----------|------|
| `transform_history` | `(history, session, ctx) -> history` | After `session.get_history()`, before `build_messages()`. Modify conversation history. |
| `transform_messages` | `(messages, ctx) -> messages` | After `build_messages()`, before LLM call. Modify full message array. |
| `transform_response` | `(content, ctx) -> content` | After tool loop, before session save. Modify the response. |
| `pre_session_save` | `(session, ctx) -> None` | Before `sessions.save()`. Mutate session in place (e.g., trim, update metadata). |

`ExtensionContext` carries `channel`, `chat_id`, `session_key`, and `workspace` to all hooks.

### Extension Manager (`extensions/manager.py`)

`ExtensionManager` loads extensions from config (dynamic import via `class_path`), calls `on_load(options)` on each, and runs hooks in pipeline order (each extension's output is the next one's input).

### Built-in Extensions

- **CompactionExtension** (`extensions/compaction.py`) — Archives old session messages to JSONL files, injects a summary of archived context into future conversations. Configurable `max_active_messages` threshold.

### Config

```json
{
  "extensions": [
    {
      "classPath": "nanobot.extensions.compaction.CompactionExtension",
      "enabled": true,
      "options": { "maxActiveMessages": 30, "archiveDir": "sessions/archives" }
    }
  ]
}
```

### Adding an Extension

1. Create a class inheriting `Extension` from `extensions/base.py`
2. Set `name` class attribute
3. Override `on_load(config)` to read options
4. Override the hooks you need
5. Add to config `extensions` list with `classPath`, `enabled`, `options`

## Skills System

Skills extend agent capabilities dynamically. Located at `{workspace}/skills/{name}/SKILL.md` (per-workspace) or `nanobot/skills/{name}/SKILL.md` (bundled).

### SKILL.md Format
```markdown
---
name: my-skill
description: What this skill does
always: false
requires: [some-cli-tool]
---

# Instructions for the agent when using this skill
...
```

- `always: true` — Preloaded into every system prompt
- `always: false` — Summary shown in system prompt; agent uses `read_file` to load full content on demand
- `requires` — External dependencies checked at load time

## Configuration

Config lives at `~/.nanobot/config.json` in **camelCase**. `config/loader.py` auto-converts to **snake_case** for Pydantic. Saving converts back.

```
JSON (camelCase) → loader.py convert_keys() → Pydantic (snake_case) → runtime
```

Environment variable overrides use `NANOBOT_` prefix with `__` for nesting:
```bash
NANOBOT_PROVIDERS__GEMINI__API_KEY=your-key
```

## Deployment

See `deploy/README.md` for the master/worker VPS architecture. Key concepts:

- **HOME trick** — Each bot instance gets `HOME=/home/deploy/bots/{project}`, isolating config, workspace, and sessions
- **Shared secrets** — `EnvironmentFile=-/home/deploy/shared/secrets.env` in the systemd template provides shared OAuth tokens to all workers
- **Master bot** — Manages worker lifecycle via Telegram commands. Has `ProtectSystem=false` for broad filesystem access.
- **Worker bots** — Sandboxed with `restrictToWorkspace: true` and `ReadWritePaths=/home/deploy/bots/{project}`
