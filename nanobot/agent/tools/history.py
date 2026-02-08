"""Tool for searching archived conversation history."""

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import ContextAwareTool
from nanobot.utils.helpers import safe_filename


class HistorySearchTool(ContextAwareTool):
    """Search through archived conversation history from session compaction."""

    def __init__(self, workspace: str, archive_dir: str = "sessions/archives"):
        self._workspace = workspace
        self._archive_dir = archive_dir
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "history_search"

    @property
    def description(self) -> str:
        return (
            "Search your archived conversation history for past messages. "
            "Use this when you need to recall something discussed earlier that "
            "may have been compacted out of your current context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for in archived messages (case-insensitive)"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching messages to return (default 10)"
                },
            },
            "required": ["query"]
        }

    async def execute(self, query: str, max_results: int = 10, **kwargs: Any) -> str:
        archive_path = self._get_archive_path()
        if not archive_path.exists():
            return "No archived conversation history found for this session."

        query_lower = query.lower()
        results: list[dict[str, Any]] = []

        with open(archive_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and query_lower in content.lower():
                    results.append(msg)
                    if len(results) >= max_results:
                        break

        if not results:
            return f"No archived messages matching '{query}'."

        lines = [f"Found {len(results)} archived message(s) matching '{query}':\n"]
        for msg in results:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:500]
                if len(content) > 500:
                    preview += "..."
            else:
                preview = str(content)[:500]
            lines.append(f"[{role}] {preview}\n")
        return "\n".join(lines)

    def _get_archive_path(self) -> Path:
        session_key = f"{self._channel}:{self._chat_id}"
        safe_key = safe_filename(session_key.replace(":", "_"))
        return Path(self._workspace) / self._archive_dir / f"{safe_key}.jsonl"
