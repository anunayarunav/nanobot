"""Tests for tool call persistence — get_history() preserving tool_calls and tool result fields."""

from typing import Any

import pytest

from nanobot.session.manager import Session


@pytest.fixture
def session() -> Session:
    return Session(key="test:123")


# ===========================================================================
# get_history preserves tool-related fields
# ===========================================================================


def test_get_history_preserves_tool_calls(session: Session):
    """Assistant messages with tool_calls should survive get_history()."""
    session.add_message("user", "list files")
    session.add_message(
        "assistant",
        "",
        tool_calls=[{
            "id": "tc_1",
            "type": "function",
            "function": {"name": "list_dir", "arguments": '{"path": "."}'},
        }],
    )
    session.add_message(
        "tool",
        "file1.txt\nfile2.txt",
        tool_call_id="tc_1",
        name="list_dir",
    )
    session.add_message("assistant", "Here are the files.")

    history = session.get_history()

    # Assistant with tool_calls
    tc_msg = history[1]
    assert tc_msg["role"] == "assistant"
    assert tc_msg["tool_calls"][0]["function"]["name"] == "list_dir"

    # Tool result
    tool_msg = history[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "tc_1"
    assert tool_msg["name"] == "list_dir"
    assert "file1.txt" in tool_msg["content"]


def test_get_history_plain_messages_unchanged(session: Session):
    """Plain user/assistant messages should not gain extra fields."""
    session.add_message("user", "hello")
    session.add_message("assistant", "hi there")

    history = session.get_history()

    assert len(history) == 2
    assert set(history[0].keys()) == {"role", "content"}
    assert set(history[1].keys()) == {"role", "content"}


def test_get_history_mixed_messages(session: Session):
    """Mix of plain and tool messages should all be preserved correctly."""
    session.add_message("user", "do something")
    session.add_message(
        "assistant",
        "Let me check.",
        tool_calls=[{
            "id": "tc_1",
            "type": "function",
            "function": {"name": "exec", "arguments": '{"command": "ls"}'},
        }],
    )
    session.add_message("tool", "output", tool_call_id="tc_1", name="exec")
    session.add_message("assistant", "Done.")
    session.add_message("user", "thanks")
    session.add_message("assistant", "You're welcome.")

    history = session.get_history()

    assert len(history) == 6
    # tool_calls on assistant[1]
    assert "tool_calls" in history[1]
    # tool result fields on tool[2]
    assert history[2]["tool_call_id"] == "tc_1"
    assert history[2]["name"] == "exec"
    # plain messages don't have extra fields
    assert "tool_calls" not in history[3]
    assert "tool_call_id" not in history[4]


def test_get_history_multiple_tool_calls(session: Session):
    """Multiple tool calls in a single assistant message should all persist."""
    session.add_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": "tc_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            },
            {
                "id": "tc_2",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "b.txt"}'},
            },
        ],
    )
    session.add_message("tool", "content a", tool_call_id="tc_1", name="read_file")
    session.add_message("tool", "content b", tool_call_id="tc_2", name="read_file")

    history = session.get_history()

    assert len(history[0]["tool_calls"]) == 2
    assert history[1]["tool_call_id"] == "tc_1"
    assert history[2]["tool_call_id"] == "tc_2"
