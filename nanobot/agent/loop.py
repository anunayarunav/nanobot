"""Agent loop: the core processing engine."""

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.commands import CommandContext, CommandResult, build_command_registry
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

        # Command framework
        allowed = config.commands.allowed if config else None
        self.command_registry = build_command_registry(config=config, allowed=allowed)

        # Debug levels per session (runtime state, not persisted)
        self.debug_levels: dict[str, str] = {}

        # Cancellation events per session (for /stop)
        self.cancel_events: dict[str, asyncio.Event] = {}

        self.extensions = extensions or ExtensionManager()

        self._running = False
        self._processing_task: asyncio.Task[None] | None = None
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
        self, channel: str, chat_id: str, session_key: str,
    ) -> "Callable[[str, dict[str, Any]], Awaitable[None]]":
        """Create a callback that respects debug level for progress messages."""
        from collections.abc import Callable, Awaitable

        async def _notify(name: str, args: dict[str, Any]) -> None:
            level = self.debug_levels.get(session_key, "moderate")
            if level == "none":
                return

            # Heartbeat — periodic "still running" update (all + moderate)
            if args.get("_heartbeat"):
                elapsed = args["elapsed"]
                mins, secs = divmod(elapsed, 60)
                label = f"{mins}m{secs}s" if mins else f"{secs}s"
                await self.bus.publish_outbound(OutboundMessage(
                    channel=channel, chat_id=chat_id,
                    content=f"⏳ Still running... ({label})",
                ))
                return

            # "all" mode: show every tool call with args
            if level == "all":
                args_parts = []
                for k, v in args.items():
                    v_str = str(v)
                    if len(v_str) > 80:
                        v_str = v_str[:80] + "..."
                    args_parts.append(f"{k}={v_str}")
                args_display = ", ".join(args_parts)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=channel, chat_id=chat_id,
                    content=f"🔧 `{name}({args_display})`",
                ))
                return

            # "moderate" mode: only slow tools
            if name not in _SLOW_TOOLS:
                return
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

    async def _dispatch_command(self, msg: InboundMessage) -> OutboundMessage | None:
        """Dispatch a slash command and handle side-effects."""
        ctx = CommandContext(
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=msg.session_key,
            raw_args="",
            agent_loop=self,
        )
        result = await self.command_registry.dispatch(msg.content, ctx)
        if not result:
            return None

        # Side-effects: model switch
        if result.new_provider and result.new_model:
            self.provider = result.new_provider
            self.model = result.new_model
            self.subagents.provider = result.new_provider
            self.subagents.model = result.new_model

        # Side-effects: retry re-queue
        if result.requeue_message:
            await self.bus.publish_inbound(InboundMessage(
                channel=msg.channel,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                content=result.requeue_message,
            ))

        if result.message:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=result.message,
            )
        return None

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus.

        Messages are processed as asyncio tasks so that interrupt commands
        (/stop) can be handled while a tool loop is running.
        """
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            # Clean up completed processing task
            if self._processing_task and self._processing_task.done():
                try:
                    self._processing_task.result()
                except Exception:
                    pass  # already logged in _process_and_respond
                self._processing_task = None

            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                # Commands (interrupt or not) dispatch immediately — never block
                if self.command_registry.is_interrupt(msg.content) or \
                   self.command_registry.is_command(msg.content):
                    response = await self._dispatch_command(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                    continue

                # Regular message — wait for previous processing, then start new task
                if self._processing_task and not self._processing_task.done():
                    await self._processing_task
                    self._processing_task = None

                cancel_event = asyncio.Event()
                self.cancel_events[msg.session_key] = cancel_event
                self._processing_task = asyncio.create_task(
                    self._process_and_respond(msg, cancel_event)
                )
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Sorry, I encountered an error: {str(e)}"
                ))

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_and_respond(
        self, msg: InboundMessage, cancel_event: asyncio.Event,
    ) -> None:
        """Process a message and publish the response. Wraps _process_message for task use."""
        try:
            response = await self._process_message(msg, cancel_event)
            if response:
                await self.bus.publish_outbound(response)
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I encountered an error: {str(e)}"
            ))
        finally:
            self.cancel_events.pop(msg.session_key, None)

    async def _process_message(
        self, msg: InboundMessage, cancel_event: asyncio.Event | None = None,
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            cancel_event: Optional cancellation event for /stop support.

        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        if msg.channel == "system":
            return await self._process_system_message(msg)

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

        _maybe_nudge_tool_use(messages)

        # Agent loop
        pre_loop_len = len(messages)
        final_content = await run_tool_loop(
            provider=self.provider,
            tools=self.tools,
            messages=messages,
            model=self.model,
            max_iterations=self.max_iterations,
            on_tool_call=self._make_progress_callback(msg.channel, msg.chat_id, msg.session_key),
            cancel_event=cancel_event,
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
            on_tool_call=self._make_progress_callback(origin_channel, origin_chat_id, session_key),
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

        # Handle commands from CLI too
        if self.command_registry.is_command(content):
            response = await self._dispatch_command(msg)
            return response.content if response else ""

        response = await self._process_message(msg)
        return response.content if response else ""
