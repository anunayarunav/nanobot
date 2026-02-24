"""Slash command framework: registry, dispatch, and built-in handlers."""

import os
import subprocess
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.schema import Config, ModelAlias
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import make_provider


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CommandContext:
    """Context passed to every command handler."""
    channel: str
    chat_id: str
    session_key: str
    raw_args: str
    agent_loop: Any  # AgentLoop (forward ref avoids circular import)


@dataclass
class CommandResult:
    """Return value from a command handler."""
    message: str
    new_model: str | None = None
    new_provider: LLMProvider | None = None
    requeue_message: str | None = None


# Handler signature
Handler = Callable[[CommandContext], Awaitable[CommandResult]]

# Interrupt commands that can fire while a message is being processed
_INTERRUPT_COMMANDS = {"stop"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class CommandRegistry:
    """Registry-based slash command dispatcher with allowlist enforcement."""

    def __init__(self, config: Config | None = None, allowed: list[str] | None = None):
        self.config = config
        self._handlers: dict[str, Handler] = {}
        self._descriptions: dict[str, str] = {}
        self._allowed = allowed  # None = all allowed

    def register(self, name: str, handler: Handler, description: str = "") -> None:
        self._handlers[name] = handler
        self._descriptions[name] = description

    def is_command(self, text: str) -> bool:
        text = text.strip()
        if not text.startswith("/"):
            return False
        name = text.split()[0][1:]
        return name in self._handlers and self._is_allowed(name)

    def is_interrupt(self, text: str) -> bool:
        text = text.strip()
        if not text.startswith("/"):
            return False
        name = text.split()[0][1:]
        # Interrupt commands bypass the allowlist — they're safety controls
        return name in _INTERRUPT_COMMANDS and name in self._handlers

    async def dispatch(self, text: str, ctx: CommandContext) -> CommandResult | None:
        text = text.strip()
        if not text.startswith("/"):
            return None
        parts = text.split(None, 1)
        name = parts[0][1:]
        if name not in self._handlers:
            return None
        if not self._is_allowed(name):
            return CommandResult(message=f"Command `/{name}` is not enabled for this bot.")
        ctx.raw_args = parts[1].strip() if len(parts) > 1 else ""
        logger.info(f"Command: /{name} args={ctx.raw_args!r}")
        return await self._handlers[name](ctx)

    def _is_allowed(self, name: str) -> bool:
        if self._allowed is None:
            return True
        return name in self._allowed

    def get_help_text(self) -> str:
        lines = ["Available commands:"]
        for name in sorted(self._descriptions):
            if self._is_allowed(name):
                lines.append(f"  `/{name}` — {self._descriptions[name]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# /model handler (migrated from old CommandHandler)
# ---------------------------------------------------------------------------

_DEFAULT_ALIASES: dict[str, ModelAlias] = {
    "cc": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "claude-code": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "opus": ModelAlias(model="anthropic/claude-opus-4-6", mode="oauth"),
    "sonnet": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "gemini": ModelAlias(model="gemini/gemini-2.5-pro-preview-06-05", mode="api"),
    "flash": ModelAlias(model="gemini/gemini-2.5-flash-preview-05-20", mode="api"),
    "claude": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="api"),
}


def _get_aliases(config: Config) -> dict[str, ModelAlias]:
    aliases = dict(_DEFAULT_ALIASES)
    aliases.update(config.agents.model_aliases)
    return aliases


def _model_status(config: Config, current_model: str) -> str:
    lines = [f"Current model: `{current_model}`", ""]
    p = config.providers
    lines.append("Available shortcuts:")
    has_oauth = p.anthropic.oauth_access_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if has_oauth:
        lines.append("  `/model opus` — Claude Opus 4.6 (OAuth/CLI)")
        lines.append("  `/model sonnet` — Claude Sonnet 4.5 (OAuth/CLI)")
        lines.append("  `/model cc` — alias for sonnet via Claude Code")
    if p.anthropic.api_key:
        lines.append("  `/model claude` — Claude Sonnet 4.5 (API key)")
    if p.gemini.api_key:
        lines.append("  `/model gemini` — Gemini 2.5 Pro")
        lines.append("  `/model flash` — Gemini 2.5 Flash")
    lines.append("")
    lines.append("Or use an explicit model: `/model anthropic/claude-opus-4-6`")
    return "\n".join(lines)


async def handle_model(ctx: CommandContext) -> CommandResult:
    """Show or switch LLM model."""
    config = ctx.agent_loop.config
    if not config:
        return CommandResult(message="No config available.")

    if not ctx.raw_args:
        return CommandResult(message=_model_status(config, ctx.agent_loop.model))

    target = ctx.raw_args.strip()
    aliases = _get_aliases(config)

    if target.lower() in aliases:
        alias = aliases[target.lower()]
        model, mode = alias.model, alias.mode
    else:
        model = target
        has_oauth = (
            config.providers.anthropic.oauth_access_token
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        mode = "oauth" if has_oauth and "anthropic" in model.lower() else "api"

    try:
        provider = make_provider(config, model, mode)
        label = "Claude Code (OAuth)" if mode == "oauth" else "API key"
        logger.info(f"Switched to model: {model} (mode: {mode})")
        return CommandResult(
            message=f"Switched to `{model}` via {label}",
            new_model=model,
            new_provider=provider,
        )
    except Exception as e:
        logger.error(f"Failed to switch model: {e}")
        return CommandResult(message=f"Failed to switch model: {e}")


# ---------------------------------------------------------------------------
# /debug handler
# ---------------------------------------------------------------------------

async def handle_debug(ctx: CommandContext) -> CommandResult:
    """Set tool call visibility: all|moderate|none."""
    level = ctx.raw_args.lower() if ctx.raw_args else ""
    if level not in ("all", "moderate", "none", ""):
        return CommandResult(message="Usage: `/debug all|moderate|none`")

    loop = ctx.agent_loop
    if not level:
        current = loop.debug_levels.get(ctx.session_key, "moderate")
        return CommandResult(message=f"Debug level: `{current}`\nOptions: `all`, `moderate`, `none`")

    loop.debug_levels[ctx.session_key] = level
    return CommandResult(message=f"Debug level set to `{level}`")


# ---------------------------------------------------------------------------
# /stop handler
# ---------------------------------------------------------------------------

async def handle_stop(ctx: CommandContext) -> CommandResult:
    """Cancel current tool loop."""
    loop = ctx.agent_loop
    event = loop.cancel_events.get(ctx.session_key)
    if event and not event.is_set():
        event.set()
        return CommandResult(message="Cancelling current operation...")
    return CommandResult(message="Nothing running to cancel.")


# ---------------------------------------------------------------------------
# /clear handler
# ---------------------------------------------------------------------------

async def handle_clear(ctx: CommandContext) -> CommandResult:
    """Wipe session history."""
    loop = ctx.agent_loop
    session = loop.sessions.get_or_create(ctx.session_key)
    msg_count = len(session.messages)
    session.clear()
    loop.sessions.save(session)
    return CommandResult(message=f"Session cleared ({msg_count} messages removed).")


# ---------------------------------------------------------------------------
# /undo handler
# ---------------------------------------------------------------------------

async def handle_undo(ctx: CommandContext) -> CommandResult:
    """Remove last user+assistant exchange."""
    loop = ctx.agent_loop
    session = loop.sessions.get_or_create(ctx.session_key)
    if not session.messages:
        return CommandResult(message="Session is empty, nothing to undo.")

    removed = 0
    # Pop assistant + tool messages
    while session.messages and session.messages[-1]["role"] in ("assistant", "tool"):
        session.messages.pop()
        removed += 1
    # Pop user message
    if session.messages and session.messages[-1]["role"] == "user":
        session.messages.pop()
        removed += 1

    loop.sessions.save(session)
    return CommandResult(message=f"Undone last exchange ({removed} messages removed).")


# ---------------------------------------------------------------------------
# /retry handler
# ---------------------------------------------------------------------------

async def handle_retry(ctx: CommandContext) -> CommandResult:
    """Undo + re-send last user message."""
    loop = ctx.agent_loop
    session = loop.sessions.get_or_create(ctx.session_key)
    if not session.messages:
        return CommandResult(message="Session is empty, nothing to retry.")

    # Pop assistant + tool messages
    while session.messages and session.messages[-1]["role"] in ("assistant", "tool"):
        session.messages.pop()
    # Pop and capture user message
    last_user_content = None
    if session.messages and session.messages[-1]["role"] == "user":
        last_user_content = session.messages.pop()["content"]

    loop.sessions.save(session)

    if last_user_content:
        return CommandResult(message="Retrying last message...", requeue_message=last_user_content)
    return CommandResult(message="Could not find a user message to retry.")


# ---------------------------------------------------------------------------
# /session handler
# ---------------------------------------------------------------------------

async def handle_session(ctx: CommandContext) -> CommandResult:
    """Show session stats."""
    from nanobot.extensions.compaction import estimate_messages_tokens

    loop = ctx.agent_loop
    session = loop.sessions.get_or_create(ctx.session_key)
    msg_count = len(session.messages)
    token_est = estimate_messages_tokens(
        [{"role": m["role"], "content": m.get("content", "")} for m in session.messages]
    )
    archived = session.metadata.get("archived_count", 0)

    lines = [
        f"Session: `{ctx.session_key}`",
        f"Messages: {msg_count}" + (f" (+ {archived} archived)" if archived else ""),
        f"Estimated tokens: ~{token_est:,}",
        f"Created: {session.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"Updated: {session.updated_at.strftime('%Y-%m-%d %H:%M')}",
    ]
    return CommandResult(message="\n".join(lines))


# ---------------------------------------------------------------------------
# /config handler
# ---------------------------------------------------------------------------

async def handle_config(ctx: CommandContext) -> CommandResult:
    """Show current bot settings."""
    loop = ctx.agent_loop
    debug = loop.debug_levels.get(ctx.session_key, "moderate")
    lines = [
        f"Model: `{loop.model}`",
        f"Max iterations: {loop.max_iterations}",
        f"Debug level: `{debug}`",
        f"Workspace: `{loop.workspace}`",
        f"Restrict to workspace: {loop.restrict_to_workspace}",
    ]
    return CommandResult(message="\n".join(lines))


# ---------------------------------------------------------------------------
# /ls handler
# ---------------------------------------------------------------------------

async def handle_ls(ctx: CommandContext) -> CommandResult:
    """List directory contents."""
    loop = ctx.agent_loop
    target = ctx.raw_args or str(loop.workspace)
    target_path = Path(target).expanduser().resolve()

    if loop.restrict_to_workspace:
        ws = loop.workspace.resolve()
        if not str(target_path).startswith(str(ws)):
            return CommandResult(message=f"Error: path outside workspace (`{ws}`)")

    if not target_path.is_dir():
        return CommandResult(message=f"Error: not a directory: `{target_path}`")

    try:
        result = subprocess.run(
            ["ls", "-la", str(target_path)],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout[:2000]
        return CommandResult(message=f"```\n{output}\n```")
    except Exception as e:
        return CommandResult(message=f"Error: {e}")


# ---------------------------------------------------------------------------
# /cat handler
# ---------------------------------------------------------------------------

async def handle_cat(ctx: CommandContext) -> CommandResult:
    """Display file contents."""
    loop = ctx.agent_loop
    if not ctx.raw_args:
        return CommandResult(message="Usage: `/cat <path>`")

    target = Path(ctx.raw_args).expanduser().resolve()

    if loop.restrict_to_workspace:
        ws = loop.workspace.resolve()
        if not str(target).startswith(str(ws)):
            return CommandResult(message=f"Error: path outside workspace (`{ws}`)")

    if not target.is_file():
        return CommandResult(message=f"Error: not a file: `{target}`")

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 3000:
            content = content[:3000] + "\n...(truncated)"
        return CommandResult(message=f"```\n{content}\n```")
    except Exception as e:
        return CommandResult(message=f"Error: {e}")


# ---------------------------------------------------------------------------
# /help handler
# ---------------------------------------------------------------------------

async def handle_help(ctx: CommandContext) -> CommandResult:
    """List available commands."""
    loop = ctx.agent_loop
    return CommandResult(message=loop.command_registry.get_help_text())


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------

def build_command_registry(config: Config | None = None, allowed: list[str] | None = None) -> CommandRegistry:
    """Create and populate the default command registry."""
    reg = CommandRegistry(config=config, allowed=allowed)
    reg.register("model", handle_model, "Show/switch LLM model")
    reg.register("debug", handle_debug, "Set tool call visibility: all|moderate|none")
    reg.register("stop", handle_stop, "Cancel current tool loop")
    reg.register("clear", handle_clear, "Wipe session history")
    reg.register("undo", handle_undo, "Remove last user+assistant exchange")
    reg.register("retry", handle_retry, "Undo + re-send last user message")
    reg.register("session", handle_session, "Show session stats")
    reg.register("config", handle_config, "Show current bot settings")
    reg.register("ls", handle_ls, "List directory contents")
    reg.register("cat", handle_cat, "Display file contents")
    reg.register("help", handle_help, "List available commands")
    return reg
