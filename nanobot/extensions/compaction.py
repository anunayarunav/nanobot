"""Session compaction extension: archives old messages and injects summaries."""

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.extensions.base import Extension, ExtensionContext
from nanobot.utils.helpers import ensure_dir, safe_filename


class CompactionExtension(Extension):
    """Archives old session messages and prepends a summary to history.

    Config options:
        max_active_messages: int = 30  — messages kept in active session
        archive_dir: str = "sessions/archives" — relative to workspace
    """

    name = "compaction"

    def __init__(self) -> None:
        self.max_active_messages: int = 30
        self.archive_dir: str = "sessions/archives"

    async def on_load(self, config: dict[str, Any]) -> None:
        self.max_active_messages = config.get("max_active_messages", 30)
        self.archive_dir = config.get("archive_dir", "sessions/archives")

    async def transform_history(
        self, history: list[dict[str, Any]], session: Any, ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        """Prepend compaction summary to history if one exists."""
        summary = session.metadata.get("compaction_summary")
        if not summary:
            return history

        summary_msg = {
            "role": "user",
            "content": f"[Context from earlier conversation:\n{summary}]",
        }
        return [summary_msg] + history

    async def pre_session_save(self, session: Any, ctx: ExtensionContext) -> None:
        """Archive old messages when session exceeds threshold."""
        threshold = int(self.max_active_messages * 1.5)
        if len(session.messages) <= threshold:
            return

        to_keep = self.max_active_messages
        to_archive = session.messages[:-to_keep]
        kept = session.messages[-to_keep:]

        # Append to archive file
        archive_path = self._get_archive_path(ctx.workspace, session.key)
        ensure_dir(archive_path.parent)

        with open(archive_path, "a") as f:
            for msg in to_archive:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        archived_count = len(to_archive)
        prev_archived = session.metadata.get("archived_count", 0)

        # Build summary from archived messages
        summary = self._build_summary(to_archive, session.metadata.get("compaction_summary"))

        # Update session
        session.messages = kept
        session.metadata["compaction_summary"] = summary
        session.metadata["archive_path"] = str(archive_path)
        session.metadata["archived_count"] = prev_archived + archived_count

        logger.info(
            f"Compacted session {session.key}: archived {archived_count} messages, "
            f"kept {len(kept)}, total archived: {prev_archived + archived_count}"
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
            # Keep first 300 chars of previous summary
            parts.append(prev_summary[:300].rstrip())

        # Extract user messages as topic markers (take every few to avoid repetition)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        # Sample up to 5 user messages spread across the archived range
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
        # Cap total summary size
        return summary[:1000]
