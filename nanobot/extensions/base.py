"""Base class and context for nanobot extensions."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ExtensionContext:
    """Context passed to all extension hooks."""
    channel: str
    chat_id: str
    session_key: str
    workspace: str


class Extension:
    """Base class for extensions. Override only the hooks you need."""

    name: str = "unnamed"

    async def on_load(self, config: dict[str, Any]) -> None:
        """Called once when the extension is loaded. config = extension options dict."""
        pass

    async def pre_process(
        self, msg: Any, session: Any, ctx: ExtensionContext,
    ) -> str | None:
        """Called before LLM processing. Return a string to short-circuit
        (skip LLM, use that string as response). Return None to proceed normally.

        Use case: credit gating, rate limiting, content filtering, etc.
        """
        return None

    async def transform_history(
        self, history: list[dict[str, Any]], session: Any, ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        """Modify conversation history before build_messages. Return modified history."""
        return history

    async def transform_messages(
        self, messages: list[dict[str, Any]], ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        """Modify full message array (system + history + user) before LLM call."""
        return messages

    async def transform_response(
        self, content: str, ctx: ExtensionContext
    ) -> str:
        """Modify the agent's final response before send and save."""
        return content

    async def pre_session_save(
        self, session: Any, ctx: ExtensionContext
    ) -> None:
        """Called before session save. Mutate session in place."""
        pass
