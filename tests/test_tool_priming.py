"""Tests for tool priming — injecting a tool_call example when session history has none."""

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with a few files for list_dir to find."""
    (tmp_path / "AGENTS.md").write_text("# Agents")
    (tmp_path / "notes.txt").write_text("some notes")
    (tmp_path / "subdir").mkdir()
    return tmp_path


@pytest.fixture
def builder(workspace: Path) -> ContextBuilder:
    return ContextBuilder(workspace)


def _build_messages_with_history(
    builder: ContextBuilder,
    history: list[dict[str, Any]],
    current: str = "hello",
) -> list[dict[str, Any]]:
    """Helper: build messages with given history and a current user message."""
    return builder.build_messages(
        history=history,
        current_message=current,
        channel="test",
        chat_id="123",
    )


# ===========================================================================
# Priming triggers
# ===========================================================================


def test_priming_skipped_for_fresh_conversation(builder: ContextBuilder):
    """No priming when there's no history (brand new conversation)."""
    messages = _build_messages_with_history(builder, history=[])
    result = builder.prime_tool_usage(messages)

    # Should be unchanged — system + current user = 2 messages only
    assert len(result) == len(messages)
    assert not any(m.get("tool_calls") for m in result)


def test_priming_skipped_for_short_history(builder: ContextBuilder):
    """No priming when history is just 1 exchange (system + 1 user + 1 assistant + current = 4,
    but we want at least 2 history messages = 4 total, so 1 history message = 3 total → skip)."""
    messages = _build_messages_with_history(
        builder,
        history=[{"role": "user", "content": "hi"}],
    )
    # system + 1 history + current = 3 messages — under threshold
    result = builder.prime_tool_usage(messages)
    assert len(result) == len(messages)


def test_priming_fires_with_text_only_history(builder: ContextBuilder):
    """Priming fires when history has multiple messages but no tool_calls."""
    history = [
        {"role": "user", "content": "What can you do?"},
        {"role": "assistant", "content": "I can help with many things."},
        {"role": "user", "content": "Tell me more."},
        {"role": "assistant", "content": "I have access to tools."},
    ]
    messages = _build_messages_with_history(builder, history=history)
    original_len = len(messages)

    result = builder.prime_tool_usage(messages)

    # Should have 2 extra messages (tool_call + tool_response)
    assert len(result) == original_len + 2

    # Find the priming pair
    prime_assistant = next(m for m in result if m.get("tool_calls"))
    prime_tool = next(m for m in result if m.get("role") == "tool")

    assert prime_assistant["role"] == "assistant"
    assert prime_assistant["tool_calls"][0]["function"]["name"] == "list_dir"
    assert prime_tool["tool_call_id"] == "prime_1"
    assert prime_tool["name"] == "list_dir"


def test_priming_skipped_when_tool_calls_exist(builder: ContextBuilder):
    """No priming when assistant messages already have tool_calls."""
    history = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "tc_1",
                "type": "function",
                "function": {"name": "list_dir", "arguments": '{"path": "."}'},
            }],
        },
        {"role": "tool", "tool_call_id": "tc_1", "name": "list_dir", "content": "file1.txt"},
        {"role": "assistant", "content": "Here are the files."},
    ]
    messages = _build_messages_with_history(builder, history=history)
    original_len = len(messages)

    result = builder.prime_tool_usage(messages)

    # Should be unchanged
    assert len(result) == original_len


# ===========================================================================
# Priming content
# ===========================================================================


def test_priming_uses_real_workspace_listing(builder: ContextBuilder, workspace: Path):
    """The priming tool_response should contain actual workspace contents."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's new?"},
        {"role": "assistant", "content": "not much"},
    ]
    messages = _build_messages_with_history(builder, history=history)
    result = builder.prime_tool_usage(messages)

    tool_msg = next(m for m in result if m.get("role") == "tool")
    content = tool_msg["content"]

    # Should list the files we created in the workspace fixture
    assert "AGENTS.md" in content
    assert "notes.txt" in content
    assert "subdir" in content


def test_priming_inserted_before_last_user_message(builder: ContextBuilder):
    """The priming pair should appear just before the final user message."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's new?"},
        {"role": "assistant", "content": "not much"},
    ]
    messages = _build_messages_with_history(builder, history=history, current="do something")
    result = builder.prime_tool_usage(messages)

    # Last message should be the current user message
    assert result[-1]["role"] == "user"
    assert result[-1]["content"] == "do something"

    # Second-to-last should be the tool response
    assert result[-2]["role"] == "tool"
    assert result[-2]["name"] == "list_dir"

    # Third-to-last should be the assistant with tool_calls
    assert result[-3]["role"] == "assistant"
    assert result[-3].get("tool_calls")


def test_priming_tool_call_has_valid_structure(builder: ContextBuilder):
    """The injected tool_call should match OpenAI tool_call format."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "sure"},
    ]
    messages = _build_messages_with_history(builder, history=history)
    result = builder.prime_tool_usage(messages)

    tc_msg = next(m for m in result if m.get("tool_calls"))
    tc = tc_msg["tool_calls"][0]

    assert "id" in tc
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "list_dir"

    # Arguments should be valid JSON
    args = json.loads(tc["function"]["arguments"])
    assert "path" in args


def test_priming_workspace_path_matches(builder: ContextBuilder, workspace: Path):
    """The list_dir argument should point to the actual workspace."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "sure"},
    ]
    messages = _build_messages_with_history(builder, history=history)
    result = builder.prime_tool_usage(messages)

    tc_msg = next(m for m in result if m.get("tool_calls"))
    args = json.loads(tc_msg["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == str(workspace.resolve())


# ===========================================================================
# Edge cases
# ===========================================================================


def test_priming_handles_unreadable_workspace(tmp_path: Path):
    """If workspace listing fails, priming still works with a fallback."""
    # Point to a non-existent directory
    fake_workspace = tmp_path / "nonexistent"
    builder = ContextBuilder(fake_workspace)

    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "sure"},
    ]
    messages = builder.build_messages(
        history=history, current_message="test", channel="test", chat_id="123",
    )
    original_len = len(messages)
    result = builder.prime_tool_usage(messages)

    # Should still inject priming (with fallback content)
    assert len(result) == original_len + 2
    tool_msg = next(m for m in result if m.get("role") == "tool")
    assert "workspace" in tool_msg["content"].lower() or tool_msg["content"]


def test_priming_is_idempotent(builder: ContextBuilder):
    """Calling prime_tool_usage twice doesn't double-inject."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "sure"},
    ]
    messages = _build_messages_with_history(builder, history=history)

    result = builder.prime_tool_usage(messages)
    first_len = len(result)

    # Second call — should detect the existing tool_calls and skip
    result2 = builder.prime_tool_usage(result)
    assert len(result2) == first_len
