"""Session compaction extension: archives old messages and injects summaries."""

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.extensions.base import Extension, ExtensionContext
from nanobot.utils.helpers import ensure_dir, safe_filename

# Rough approximation: 1 token ≈ 4 characters for English text.
# Not exact, but good enough for compaction thresholds across providers.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using char/4 heuristic."""
    return len(text) // CHARS_PER_TOKEN


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens across a list of messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # Multimodal content (images + text blocks)
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
        # Small overhead per message for role, metadata
        total += 4
    return total


class CompactionExtension(Extension):
    """Sole context manager: trims history to token budget and archives old messages.

    Config options:
        max_tokens: int = 170000  — total token budget for the model context
        context_headroom: int = 20000  — tokens reserved for system prompt, tools, current message
        archive_dir: str = "sessions/archives" — relative to workspace
    """

    name = "compaction"

    def __init__(self) -> None:
        self.max_tokens: int = 170_000
        self.context_headroom: int = 20_000
        self.archive_dir: str = "sessions/archives"

    async def on_load(self, config: dict[str, Any]) -> None:
        self.max_tokens = config.get("max_tokens", 170_000)
        self.context_headroom = config.get("context_headroom", 20_000)
        self.archive_dir = config.get("archive_dir", "sessions/archives")

    async def transform_history(
        self, history: list[dict[str, Any]], session: Any, ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        """Trim history to token budget and prepend compaction summary."""
        budget = self.max_tokens - self.context_headroom
        history = self._trim_to_budget(history, budget)

        summary = session.metadata.get("compaction_summary")
        if summary:
            summary_msg = {
                "role": "user",
                "content": f"[Context from earlier conversation:\n{summary}]",
            }
            return [summary_msg] + history

        return history

    @staticmethod
    def _trim_to_budget(
        messages: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        """Keep the most recent messages that fit within the token budget."""
        total = estimate_messages_tokens(messages)
        if total <= budget:
            return messages

        accumulated = 0
        start_idx = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            content = msg.get("content", "")
            msg_tokens = estimate_tokens(content) + 4 if isinstance(content, str) else 4
            if accumulated + msg_tokens > budget:
                start_idx = i + 1
                break
            accumulated += msg_tokens
        else:
            start_idx = 0

        # Always include at least the most recent message
        if start_idx >= len(messages) and messages:
            return messages[-1:]

        return messages[start_idx:]

    async def pre_session_save(self, session: Any, ctx: ExtensionContext) -> None:
        """Archive old messages when session token count exceeds threshold."""
        total_tokens = estimate_messages_tokens(session.messages)
        if total_tokens <= self.max_tokens:
            return

        # Find the split point: walk from the end, keeping messages until
        # we've accumulated max_tokens * 0.6 (keep 60%, archive the rest).
        # This leaves headroom so we don't re-compact on the very next message.
        keep_budget = int(self.max_tokens * 0.6)
        kept_tokens = 0
        split_idx = len(session.messages)

        for i in range(len(session.messages) - 1, -1, -1):
            msg = session.messages[i]
            content = msg.get("content", "")
            if isinstance(content, str):
                msg_tokens = estimate_tokens(content) + 4
            else:
                msg_tokens = 4
            if kept_tokens + msg_tokens > keep_budget:
                split_idx = i + 1
                break
            kept_tokens += msg_tokens
        else:
            # All messages fit in budget — shouldn't happen since total > max,
            # but guard against it
            split_idx = 0

        if split_idx == 0:
            return  # Nothing to archive

        to_archive = session.messages[:split_idx]
        kept = session.messages[split_idx:]

        # Append to archive file
        archive_path = self._get_archive_path(ctx.workspace, session.key)
        ensure_dir(archive_path.parent)

        with open(archive_path, "a") as f:
            for msg in to_archive:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        archived_count = len(to_archive)
        prev_archived = session.metadata.get("archived_count", 0)
        archived_tokens = estimate_messages_tokens(to_archive)

        # Build summary from archived messages
        summary = self._build_summary(to_archive, session.metadata.get("compaction_summary"))

        # Update session
        session.messages = kept
        session.metadata["compaction_summary"] = summary
        session.metadata["archive_path"] = str(archive_path)
        session.metadata["archived_count"] = prev_archived + archived_count

        logger.info(
            f"Compacted session {session.key}: archived {archived_count} messages "
            f"(~{archived_tokens} tokens), kept {len(kept)} (~{kept_tokens} tokens)"
        )

    def _get_archive_path(self, workspace: str, session_key: str) -> Path:
        safe_key = safe_filename(session_key.replace(":", "_"))
        return Path(workspace) / self.archive_dir / f"{safe_key}.jsonl"

    @staticmethod
    def _build_summary(messages: list[dict[str, Any]], prev_summary: str | None) -> str:
        """Build a concise summary from archived messages.

        Extracts user messages as topic markers and the last exchange.
        No LLM call — pure string manipulation for speed.
        """
        parts = []

        # Carry forward previous summary context (abbreviated)
        if prev_summary:
            parts.append(prev_summary[:300].rstrip())

        # Extract user messages as topic markers (sample up to 5)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        step = max(1, len(user_msgs) // 5)
        sampled = user_msgs[::step][:5]

        if sampled:
            topics = []
            for m in sampled:
                content = m.get("content", "")
                if isinstance(content, str):
                    line = content.strip().split("\n")[0][:200]
                    if line:
                        topics.append(f"- {line}")
            if topics:
                parts.append("Topics discussed:\n" + "\n".join(topics))

        # Last exchange
        last_user = None
        last_assistant = None
        for m in reversed(messages):
            role = m.get("role")
            if role == "assistant" and last_assistant is None:
                last_assistant = m.get("content", "")
            elif role == "user" and last_user is None:
                last_user = m.get("content", "")
            if last_user and last_assistant:
                break

        if last_user and last_assistant:
            u = last_user[:200] if isinstance(last_user, str) else str(last_user)[:200]
            a = last_assistant[:200] if isinstance(last_assistant, str) else str(last_assistant)[:200]
            parts.append(f"Last archived exchange:\nUser: {u}\nAssistant: {a}")

        summary = "\n\n".join(parts)
        return summary[:1000]
