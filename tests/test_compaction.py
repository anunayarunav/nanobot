"""Tests for the session compaction extension."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nanobot.extensions.base import ExtensionContext
from nanobot.extensions.compaction import (
    CompactionExtension,
    estimate_tokens,
    estimate_messages_tokens,
)
from nanobot.session.manager import Session


def _ctx(workspace: str) -> ExtensionContext:
    return ExtensionContext(
        channel="test", chat_id="123", session_key="test:123", workspace=workspace,
    )


def _make_message(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content, "timestamp": "2026-01-01T00:00:00"}


def _session_with_tokens(token_count: int, msg_count: int = 20) -> Session:
    """Create a session whose messages total approximately `token_count` tokens."""
    # Each message gets equal share of tokens; content length = tokens * 4 chars
    tokens_per_msg = token_count // msg_count
    chars_per_msg = tokens_per_msg * 4  # CHARS_PER_TOKEN = 4

    session = Session(key="test:123")
    for i in range(msg_count):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"msg{i} " + "x" * max(0, chars_per_msg - 6)
        session.add_message(role, content)
    return session


# ===========================================================================
# Token estimation
# ===========================================================================


def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world!") == 3  # 12 chars / 4
    assert estimate_tokens("a" * 400) == 100


def test_estimate_messages_tokens():
    msgs = [
        {"role": "user", "content": "a" * 40},   # 10 + 4 overhead = 14
        {"role": "assistant", "content": "b" * 80},  # 20 + 4 = 24
    ]
    total = estimate_messages_tokens(msgs)
    assert total == 38


def test_estimate_messages_tokens_multimodal():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                {"type": "text", "text": "a" * 40},
            ],
        }
    ]
    total = estimate_messages_tokens(msgs)
    # 10 tokens from text + 4 overhead
    assert total == 14


# ===========================================================================
# transform_history — summary injection
# ===========================================================================


def test_transform_history_no_summary():
    ext = CompactionExtension()
    session = Session(key="test:123")
    history = [{"role": "user", "content": "hi"}]
    result = asyncio.get_event_loop().run_until_complete(
        ext.transform_history(history, session, _ctx("/tmp"))
    )
    assert result == history  # unchanged


def test_transform_history_with_summary():
    ext = CompactionExtension()
    session = Session(key="test:123")
    session.metadata["compaction_summary"] = "We discussed Python packaging."
    history = [{"role": "user", "content": "hi"}]
    result = asyncio.get_event_loop().run_until_complete(
        ext.transform_history(history, session, _ctx("/tmp"))
    )
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert "Context from earlier conversation" in result[0]["content"]
    assert "Python packaging" in result[0]["content"]
    assert result[1] == history[0]  # original preserved


# ===========================================================================
# pre_session_save — archiving
# ===========================================================================


def test_no_compaction_when_under_threshold():
    ext = CompactionExtension()
    ext.max_tokens = 200_000
    session = _session_with_tokens(100_000, msg_count=10)
    original_count = len(session.messages)

    asyncio.get_event_loop().run_until_complete(
        ext.pre_session_save(session, _ctx("/tmp"))
    )

    assert len(session.messages) == original_count  # no change
    assert "compaction_summary" not in session.metadata


def test_compaction_triggers_when_over_threshold():
    tmp = tempfile.mkdtemp()
    try:
        ext = CompactionExtension()
        ext.max_tokens = 1000  # Low threshold for testing
        # Create ~2000 tokens worth of messages
        session = _session_with_tokens(2000, msg_count=10)
        original_count = len(session.messages)

        asyncio.get_event_loop().run_until_complete(
            ext.pre_session_save(session, _ctx(tmp))
        )

        # Session should be smaller now
        assert len(session.messages) < original_count
        assert "compaction_summary" in session.metadata
        assert "archive_path" in session.metadata
        assert session.metadata["archived_count"] > 0

        # Archive file should exist and contain valid JSONL
        archive_path = Path(session.metadata["archive_path"])
        assert archive_path.exists()
        lines = archive_path.read_text().strip().split("\n")
        assert len(lines) == session.metadata["archived_count"]
        for line in lines:
            data = json.loads(line)
            assert "role" in data
            assert "content" in data
    finally:
        shutil.rmtree(tmp)


def test_compaction_keeps_60_percent_of_budget():
    """After compaction, kept messages should be around 60% of max_tokens."""
    tmp = tempfile.mkdtemp()
    try:
        ext = CompactionExtension()
        ext.max_tokens = 1000
        session = _session_with_tokens(3000, msg_count=30)

        asyncio.get_event_loop().run_until_complete(
            ext.pre_session_save(session, _ctx(tmp))
        )

        kept_tokens = estimate_messages_tokens(session.messages)
        # Kept should be <= 60% of max_tokens (with some slack for message boundaries)
        assert kept_tokens <= ext.max_tokens * 0.7  # 70% allows for rounding
    finally:
        shutil.rmtree(tmp)


def test_compaction_archive_appends():
    """Multiple compactions should append to the same archive file."""
    tmp = tempfile.mkdtemp()
    try:
        ext = CompactionExtension()
        ext.max_tokens = 500
        ctx = _ctx(tmp)

        # First compaction
        session = _session_with_tokens(1500, msg_count=10)
        asyncio.get_event_loop().run_until_complete(ext.pre_session_save(session, ctx))
        first_archived = session.metadata["archived_count"]
        archive_path = Path(session.metadata["archive_path"])
        first_lines = len(archive_path.read_text().strip().split("\n"))
        assert first_lines == first_archived

        # Add more messages to trigger a second compaction
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            session.add_message(role, "x" * 400)  # ~100 tokens each

        asyncio.get_event_loop().run_until_complete(ext.pre_session_save(session, ctx))

        total_archived = session.metadata["archived_count"]
        assert total_archived > first_archived
        second_lines = len(archive_path.read_text().strip().split("\n"))
        assert second_lines == total_archived  # all archived lines in one file
    finally:
        shutil.rmtree(tmp)


# ===========================================================================
# Summary generation
# ===========================================================================


def test_build_summary_extracts_topics():
    messages = [
        _make_message("user", "How do I install Python?"),
        _make_message("assistant", "Use pyenv or your system package manager."),
        _make_message("user", "What about virtual environments?"),
        _make_message("assistant", "Use python -m venv."),
    ]
    summary = CompactionExtension._build_summary(messages, None)
    assert "install Python" in summary
    assert "virtual environments" in summary


def test_build_summary_includes_last_exchange():
    messages = [
        _make_message("user", "first question"),
        _make_message("assistant", "first answer"),
        _make_message("user", "second question"),
        _make_message("assistant", "second answer"),
    ]
    summary = CompactionExtension._build_summary(messages, None)
    assert "second question" in summary
    assert "second answer" in summary


def test_build_summary_carries_forward_previous():
    prev = "We discussed deployment on VPS."
    messages = [
        _make_message("user", "How about Docker?"),
        _make_message("assistant", "Docker is also an option."),
    ]
    summary = CompactionExtension._build_summary(messages, prev)
    assert "deployment on VPS" in summary
    assert "Docker" in summary


def test_build_summary_capped_at_1000_chars():
    messages = [
        _make_message("user", "x" * 500),
        _make_message("assistant", "y" * 500),
    ] * 10
    summary = CompactionExtension._build_summary(messages, "prev " * 200)
    assert len(summary) <= 1000


# ===========================================================================
# End-to-end: compaction → next message gets summary in history
# ===========================================================================


def test_end_to_end_compact_then_history():
    """Compact a session, then verify transform_history injects the summary."""
    tmp = tempfile.mkdtemp()
    try:
        ext = CompactionExtension()
        ext.max_tokens = 500
        ctx = _ctx(tmp)

        session = _session_with_tokens(1500, msg_count=10)
        asyncio.get_event_loop().run_until_complete(ext.pre_session_save(session, ctx))

        assert "compaction_summary" in session.metadata
        assert len(session.messages) < 10

        # Now simulate the next message: get_history → transform_history
        history = [{"role": m["role"], "content": m["content"]} for m in session.messages[-5:]]
        result = asyncio.get_event_loop().run_until_complete(
            ext.transform_history(history, session, ctx)
        )

        # Should have summary prepended
        assert len(result) == len(history) + 1
        assert "Context from earlier conversation" in result[0]["content"]
    finally:
        shutil.rmtree(tmp)
