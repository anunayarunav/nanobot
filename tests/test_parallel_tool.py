"""Tests for the parallel (fire-wait-resume) tool."""

import asyncio
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.parallel import ParallelTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class EchoTool(Tool):
    """Returns whatever 'text' argument it receives."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        }

    async def execute(self, text: str, **kwargs: Any) -> str:
        return text


class SlowTool(Tool):
    """Sleeps briefly then returns a fixed string."""

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "slow tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        await asyncio.sleep(0.05)
        return "done"


class FailTool(Tool):
    """Always returns an error string."""

    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "always fails"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        return "Error: intentional failure"


def _make_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    parallel = ParallelTool(registry=reg)
    reg.register(parallel)
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_tasks() -> None:
    reg = _make_registry(EchoTool())
    result = await reg.execute("parallel", {"tasks": []})
    assert "Error" in result
    assert "empty" in result


@pytest.mark.asyncio
async def test_over_limit() -> None:
    reg = _make_registry(EchoTool())
    tasks = [{"tool": "echo", "arguments": {"text": "x"}}] * 11
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "Error" in result
    assert "10" in result


@pytest.mark.asyncio
async def test_denied_tool_parallel() -> None:
    reg = _make_registry(EchoTool())
    tasks = [{"tool": "parallel", "arguments": {}}]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "Error" in result
    assert "cannot be used inside parallel" in result


@pytest.mark.asyncio
async def test_denied_tool_message() -> None:
    reg = _make_registry(EchoTool())
    tasks = [{"tool": "message", "arguments": {}}]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "Error" in result
    assert "cannot be used inside parallel" in result


@pytest.mark.asyncio
async def test_denied_tool_spawn() -> None:
    reg = _make_registry(EchoTool())
    tasks = [{"tool": "spawn", "arguments": {}}]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "Error" in result
    assert "cannot be used inside parallel" in result


@pytest.mark.asyncio
async def test_unknown_tool() -> None:
    reg = _make_registry(EchoTool())
    tasks = [{"tool": "nonexistent", "arguments": {}}]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_happy_path() -> None:
    reg = _make_registry(EchoTool())
    tasks = [
        {"tool": "echo", "arguments": {"text": "alpha"}},
        {"tool": "echo", "arguments": {"text": "beta"}},
        {"tool": "echo", "arguments": {"text": "gamma"}},
    ]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "[1]" in result
    assert "[2]" in result
    assert "[3]" in result
    assert "alpha" in result
    assert "beta" in result
    assert "gamma" in result


@pytest.mark.asyncio
async def test_partial_failure() -> None:
    reg = _make_registry(EchoTool(), FailTool())
    tasks = [
        {"tool": "echo", "arguments": {"text": "ok"}},
        {"tool": "fail", "arguments": {}},
        {"tool": "echo", "arguments": {"text": "also ok"}},
    ]
    result = await reg.execute("parallel", {"tasks": tasks})
    assert "ok" in result
    assert "also ok" in result
    assert "intentional failure" in result


@pytest.mark.asyncio
async def test_concurrent_execution() -> None:
    """Verify tasks actually run concurrently, not sequentially."""
    reg = _make_registry(SlowTool())
    tasks = [{"tool": "slow", "arguments": {}}] * 5

    start = asyncio.get_event_loop().time()
    result = await reg.execute("parallel", {"tasks": tasks})
    elapsed = asyncio.get_event_loop().time() - start

    # 5 tasks at 50ms each: sequential would take ~250ms, parallel < 150ms
    assert elapsed < 0.2
    assert result.count("done") == 5


@pytest.mark.asyncio
async def test_arguments_passed_correctly() -> None:
    reg = _make_registry(EchoTool())
    tasks = [
        {"tool": "echo", "arguments": {"text": "first"}},
        {"tool": "echo", "arguments": {"text": "second"}},
    ]
    result = await reg.execute("parallel", {"tasks": tasks})
    # Results should be in order
    first_pos = result.index("first")
    second_pos = result.index("second")
    assert first_pos < second_pos
