"""Shared tool execution loop used by agent and subagent."""

import asyncio
import json
from collections.abc import Callable, Awaitable
from typing import Any

from loguru import logger

_HEARTBEAT_INTERVAL = 30  # seconds between "still running" notifications

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider


def summarize_tool_actions(messages: list[dict[str, Any]], start_index: int) -> str:
    """Build a compact text summary of tool actions from messages added during the tool loop.

    Args:
        messages: The full messages list (mutated by run_tool_loop).
        start_index: Index where tool loop messages begin.

    Returns:
        A compact summary string, or empty string if no tool calls were made.
    """
    actions: list[dict[str, str]] = []

    for msg in messages[start_index:]:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                name = func.get("name", "?")
                raw_args = func.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed = {}
                else:
                    parsed = raw_args

                arg_parts = []
                for k, v in parsed.items():
                    v_str = str(v)
                    if len(v_str) > 120:
                        v_str = v_str[:120] + "..."
                    arg_parts.append(f"{k}={v_str}")
                args_display = ", ".join(arg_parts)

                actions.append({"id": tc["id"], "name": name, "args": args_display})

        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            result = msg.get("content", "")
            for action in reversed(actions):
                if action.get("id") == tc_id:
                    if len(result) > 200:
                        result = result[:200] + "..."
                    action["result"] = result
                    break

    if not actions:
        return ""

    lines = []
    for a in actions:
        result_part = f" -> {a['result']}" if a.get("result") else ""
        lines.append(f"- {a['name']}({a['args']}){result_part}")

    return "<tool_context>\n" + "\n".join(lines) + "\n</tool_context>"


async def _execute_with_heartbeat(
    tools: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
    on_tool_call: Callable[[str, dict[str, Any]], Awaitable[None]] | None,
) -> str:
    """Execute a tool, sending periodic heartbeat notifications for slow calls."""
    task = asyncio.create_task(tools.execute(name, arguments))
    elapsed = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=_HEARTBEAT_INTERVAL)
        if done:
            return task.result()
        elapsed += _HEARTBEAT_INTERVAL
        if on_tool_call:
            await on_tool_call(name, {"_heartbeat": True, "elapsed": elapsed})


async def run_tool_loop(
    provider: LLMProvider,
    tools: ToolRegistry,
    messages: list[dict[str, Any]],
    model: str,
    max_iterations: int = 20,
    log_prefix: str = "",
    on_tool_call: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> str | None:
    """Run the LLM tool-calling loop until a final text response or max iterations.

    Args:
        provider: LLM provider to call.
        tools: Tool registry for execution and definitions.
        messages: Mutable message list (modified in place).
        model: Model identifier.
        max_iterations: Safety cap on iterations.
        log_prefix: Optional prefix for log messages (e.g. "Subagent [abc123]").
        on_tool_call: Optional async callback fired before each tool execution.
            Receives (tool_name, arguments). Used for progress notifications.
        cancel_event: Optional event set by /stop to cancel the loop.

    Returns:
        The final text content, or None if max_iterations hit without a text response.
    """
    prefix = f"{log_prefix} " if log_prefix else ""
    empty_retries = 0

    for _ in range(max_iterations):
        # Check cancellation before each iteration
        if cancel_event and cancel_event.is_set():
            logger.info(f"{prefix}Tool loop cancelled by user")
            return "[Operation cancelled by user]"

        response = await provider.chat(
            messages=messages,
            tools=tools.get_definitions(),
            model=model,
        )

        if not response.has_tool_calls:
            # Some models return empty content with no tool calls — nudge once
            if not response.content and empty_retries < 1:
                empty_retries += 1
                logger.warning(f"{prefix}Empty response with no tool calls, retrying")
                messages.append({"role": "assistant", "content": ""})
                messages.append({
                    "role": "user",
                    "content": "[System: Your previous response was empty. Please provide a summary of what you did or respond to the user's message.]",
                })
                continue
            return response.content

        # Append assistant message with tool calls
        tool_call_dicts = []
        for tc in response.tool_calls:
            tc_dict: dict[str, Any] = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            # Preserve provider-specific fields (e.g. Gemini thought signatures)
            if tc.provider_specific_fields:
                tc_dict["provider_specific_fields"] = tc.provider_specific_fields
            tool_call_dicts.append(tc_dict)
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": tool_call_dicts,
        })

        # Execute each tool call and append results
        for tool_call in response.tool_calls:
            # Check cancellation before each tool execution
            if cancel_event and cancel_event.is_set():
                logger.info(f"{prefix}Tool loop cancelled before executing {tool_call.name}")
                return "[Operation cancelled by user]"

            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.info(f"{prefix}Tool call: {tool_call.name}({args_str[:200]})")
            if on_tool_call:
                await on_tool_call(tool_call.name, tool_call.arguments)
            result = await _execute_with_heartbeat(
                tools, tool_call.name, tool_call.arguments, on_tool_call,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "content": result,
            })

    return None
