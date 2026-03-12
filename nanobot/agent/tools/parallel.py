"""Parallel execution tool — fire-wait-resume pattern."""

import asyncio
import json
from typing import Any, TYPE_CHECKING

from loguru import logger

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry


# Tools that must not be called inside parallel
_DENY_LIST = frozenset({"parallel", "message", "spawn"})


class ParallelTool(Tool):
    """Execute multiple tool calls concurrently (fire-wait-resume).

    Launches all sub-tasks at once via asyncio.gather, blocks until every
    task finishes, then returns all results in a single string.
    """

    def __init__(self, registry: "ToolRegistry"):
        self._registry = registry

    @property
    def name(self) -> str:
        return "parallel"

    @property
    def description(self) -> str:
        return (
            "Execute multiple tool calls concurrently (fire-wait-resume). "
            "All tasks run in parallel; the call returns only after every task finishes. "
            "Use this when you need to perform several independent operations at once, "
            "such as reading multiple files or running unrelated commands. "
            "Do NOT include tools that depend on each other's results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": "Name of the tool to call",
                            },
                            "arguments": {
                                "type": "object",
                                "description": "Arguments to pass to the tool",
                            },
                        },
                        "required": ["tool", "arguments"],
                    },
                    "description": "List of independent tool calls to execute concurrently",
                },
            },
            "required": ["tasks"],
        }

    async def execute(self, tasks: list[dict[str, Any]], **kwargs: Any) -> str:
        if not tasks:
            return "Error: tasks list is empty"
        if len(tasks) > 10:
            return "Error: maximum 10 parallel tasks allowed"

        # Validate all tool names upfront — fail fast before firing anything
        for i, task in enumerate(tasks):
            tool_name = task.get("tool", "")
            if tool_name in _DENY_LIST:
                return f"Error: tool '{tool_name}' cannot be used inside parallel"
            if not self._registry.has(tool_name):
                return f"Error: tool '{tool_name}' not found (task {i + 1})"

        # Fire — launch all concurrently
        logger.info(f"Parallel: launching {len(tasks)} tasks")
        coros = [
            self._registry.execute(task["tool"], task.get("arguments", {}))
            for task in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # Format results
        parts: list[str] = []
        for i, (task, result) in enumerate(zip(tasks, results), 1):
            tool_name = task["tool"]
            args_brief = json.dumps(task.get("arguments", {}), ensure_ascii=False)
            if len(args_brief) > 120:
                args_brief = args_brief[:120] + "..."

            if isinstance(result, Exception):
                body = f"Error: {result}"
            else:
                body = result

            parts.append(f"[{i}] {tool_name}({args_brief}):\n{body}")

        return "\n\n---\n\n".join(parts)
