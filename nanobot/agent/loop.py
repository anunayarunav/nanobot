"""Agent loop: the core processing engine."""

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.commands import CommandHandler
from nanobot.agent.context import ContextBuilder
from nanobot.agent.engine import run_tool_loop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.history import HistorySearchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.subagent import SubagentManager
from nanobot.extensions.base import ExtensionContext
from nanobot.extensions.manager import ExtensionManager
from nanobot.session.manager import SessionManager


_SLOW_TOOLS = {"exec", "web_search", "web_fetch", "spawn"}

_TOOL_NUDGE = (
    "[System: You have tools available (file I/O, shell, web search, etc.). "
    "When the user's request requires reading files, running commands, searching "
    "the web, or any action beyond pure conversation, you MUST call the "
    "appropriate tool rather than responding with text only.]"
)


def _maybe_nudge_tool_use(messages: list[dict[str, Any]]) -> None:
    """Insert a system reminder to use tools when history lacks tool_call examples.

    Fires only when there are 3+ history messages (beyond system + current user)
    and none of them carry ``tool_calls``.  This covers legacy sessions saved
    before tool_call persistence and brand-new agents.  Once real tool_calls
    appear in the session, this becomes a no-op.

    Mutates *messages* in place — inserts before the last user message.
    """
    # Need meaningful history: system + at least 2 history msgs + current user
    if len(messages) < 4:
        return

    if any(m.get("tool_calls") for m in messages if m.get("role") == "assistant"):
        return

    # Insert nudge just before the final user message
    messages.insert(-1, {"role": "system", "content": _TOOL_NUDGE})


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        config: "Config | None" = None,
        extensions: "ExtensionManager | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig, Config
        from nanobot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.config = config
        
        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self.commands = CommandHandler(config) if config else None
        self.extensions = extensions or ExtensionManager()

        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            allowed_git_repos=self.exec_config.allowed_git_repos,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # History search tool (for archived conversation recall)
        self.tools.register(HistorySearchTool(workspace=str(self.workspace)))

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    def _make_progress_callback(
        self, channel: str, chat_id: str,
    ) -> "Callable[[str, dict[str, Any]], Awaitable[None]]":
        """Create a callback that sends progress messages for slow tools."""
        from collections.abc import Callable, Awaitable

        async def _notify(name: str, args: dict[str, Any]) -> None:
            if name not in _SLOW_TOOLS:
                return
            # Heartbeat — periodic "still running" update
            if args.get("_heartbeat"):
                elapsed = args["elapsed"]
                mins, secs = divmod(elapsed, 60)
                label = f"{mins}m{secs}s" if mins else f"{secs}s"
                await self.bus.publish_outbound(OutboundMessage(
                    channel=channel, chat_id=chat_id,
                    content=f"⏳ Still running... ({label})",
                ))
                return
            # Initial notification
            if name == "exec":
                detail = str(args.get("command", ""))
            elif name == "web_search":
                detail = str(args.get("query", ""))
            elif name == "web_fetch":
                detail = str(args.get("url", ""))
            else:
                detail = str(args.get("task", ""))
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel, chat_id=chat_id,
                content=f"⏳ `{name}`: {detail}",
            ))

        return _notify

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        # Handle /model command
        if msg.content.strip().startswith("/model") and self.commands:
            status_msg, new_model, new_provider = self.commands.handle_model(
                msg.content.strip(), self.model
            )
            if new_provider and new_model:
                self.provider = new_provider
                self.model = new_model
                self.subagents.provider = new_provider
                self.subagents.model = new_model
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=status_msg)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)

        # Update tool contexts
        self.tools.set_context(msg.channel, msg.chat_id)

        ctx = ExtensionContext(
            channel=msg.channel, chat_id=msg.chat_id,
            session_key=msg.session_key, workspace=str(self.workspace),
        )

        # HOOK: transform_history
        history = session.get_history()
        history = await self.extensions.transform_history(history, session, ctx)

        # HOOK: transform_messages
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        messages = await self.extensions.transform_messages(messages, ctx)

        # Tool-use nudge for legacy/fresh sessions: when history has
        # messages but none carry tool_calls, the model tends to respond
        # text-only.  A short system reminder fixes this cheaply (~30 tokens)
        # and self-disables once real tool_calls appear in the session.
        _maybe_nudge_tool_use(messages)

        # Agent loop
        pre_loop_len = len(messages)
        final_content = await run_tool_loop(
            provider=self.provider,
            tools=self.tools,
            messages=messages,
            model=self.model,
            max_iterations=self.max_iterations,
            on_tool_call=self._make_progress_callback(msg.channel, msg.chat_id),
        )

        if not final_content:
            final_content = "I processed your request but wasn't able to generate a text response. Could you try rephrasing or asking again?"

        # HOOK: transform_response
        final_content = await self.extensions.transform_response(final_content, ctx)

        # Log response preview
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        # Save to session — persist real tool_call structures so the model
        # sees tool usage examples after a process restart.
        session.add_message("user", msg.content)
        for loop_msg in messages[pre_loop_len:]:
            role = loop_msg["role"]
            content = loop_msg.get("content", "")
            extra: dict[str, Any] = {}
            if loop_msg.get("tool_calls"):
                extra["tool_calls"] = loop_msg["tool_calls"]
            if loop_msg.get("tool_call_id"):
                extra["tool_call_id"] = loop_msg["tool_call_id"]
                # Truncate tool results for storage efficiency
                if len(content) > 500:
                    content = content[:500] + "\n...(truncated)"
            if loop_msg.get("name") and role == "tool":
                extra["name"] = loop_msg["name"]
            session.add_message(role, content, **extra)
        session.add_message("assistant", final_content)

        # HOOK: pre_session_save
        await self.extensions.pre_session_save(session, ctx)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)

        # Update tool contexts
        self.tools.set_context(origin_channel, origin_chat_id)

        ctx = ExtensionContext(
            channel=origin_channel, chat_id=origin_chat_id,
            session_key=session_key, workspace=str(self.workspace),
        )

        # HOOK: transform_history
        history = session.get_history()
        history = await self.extensions.transform_history(history, session, ctx)

        # HOOK: transform_messages
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        messages = await self.extensions.transform_messages(messages, ctx)

        _maybe_nudge_tool_use(messages)

        # Agent loop (limited for announce handling)
        pre_loop_len = len(messages)
        final_content = await run_tool_loop(
            provider=self.provider,
            tools=self.tools,
            messages=messages,
            model=self.model,
            max_iterations=self.max_iterations,
            on_tool_call=self._make_progress_callback(origin_channel, origin_chat_id),
        )

        if not final_content:
            final_content = "Background task completed."

        # HOOK: transform_response
        final_content = await self.extensions.transform_response(final_content, ctx)

        # Save to session — persist real tool_call structures
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        for loop_msg in messages[pre_loop_len:]:
            role = loop_msg["role"]
            content = loop_msg.get("content", "")
            extra: dict[str, Any] = {}
            if loop_msg.get("tool_calls"):
                extra["tool_calls"] = loop_msg["tool_calls"]
            if loop_msg.get("tool_call_id"):
                extra["tool_call_id"] = loop_msg["tool_call_id"]
                if len(content) > 500:
                    content = content[:500] + "\n...(truncated)"
            if loop_msg.get("name") and role == "tool":
                extra["name"] = loop_msg["name"]
            session.add_message(role, content, **extra)
        session.add_message("assistant", final_content)

        # HOOK: pre_session_save
        await self.extensions.pre_session_save(session, ctx)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
