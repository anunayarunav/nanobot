"""Shared tool execution loop used by agent and subagent."""

import json
from typing import Any

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider


async def run_tool_loop(
    provider: LLMProvider,
    tools: ToolRegistry,
    messages: list[dict[str, Any]],
    model: str,
    max_iterations: int = 20,
    log_prefix: str = "",
) -> str | None:
    """Run the LLM tool-calling loop until a final text response or max iterations.

    Args:
        provider: LLM provider to call.
        tools: Tool registry for execution and definitions.
        messages: Mutable message list (modified in place).
        model: Model identifier.
        max_iterations: Safety cap on iterations.
        log_prefix: Optional prefix for log messages (e.g. "Subagent [abc123]").

    Returns:
        The final text content, or None if max_iterations hit without a text response.
    """
    prefix = f"{log_prefix} " if log_prefix else ""
    empty_retries = 0

    for _ in range(max_iterations):
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
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.info(f"{prefix}Tool call: {tool_call.name}({args_str[:200]})")
            result = await tools.execute(tool_call.name, tool_call.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "content": result,
            })

    return None
