"""Extension manager: loads and runs extensions through lifecycle hooks."""

import importlib
from typing import Any

from loguru import logger

from nanobot.extensions.base import Extension, ExtensionContext


class ExtensionManager:
    """Loads extensions from config and runs them through pipeline hooks."""

    def __init__(self) -> None:
        self._extensions: list[Extension] = []

    async def load_from_config(self, extensions_config: list) -> None:
        """Import and initialize extensions from config entries.

        Args:
            extensions_config: List of ExtensionConfig objects with class_path, enabled, options.
        """
        for ext_cfg in extensions_config:
            if not ext_cfg.enabled:
                continue
            try:
                module_path, class_name = ext_cfg.class_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name)
                ext = cls()
                await ext.on_load(ext_cfg.options)
                self._extensions.append(ext)
                logger.info(f"Loaded extension: {ext.name} ({ext_cfg.class_path})")
            except Exception as e:
                logger.error(f"Failed to load extension {ext_cfg.class_path}: {e}")

    async def transform_history(
        self, history: list[dict[str, Any]], session: Any, ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        for ext in self._extensions:
            history = await ext.transform_history(history, session, ctx)
        return history

    async def transform_messages(
        self, messages: list[dict[str, Any]], ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        for ext in self._extensions:
            messages = await ext.transform_messages(messages, ctx)
        return messages

    async def transform_response(self, content: str, ctx: ExtensionContext) -> str:
        for ext in self._extensions:
            content = await ext.transform_response(content, ctx)
        return content

    async def pre_session_save(self, session: Any, ctx: ExtensionContext) -> None:
        for ext in self._extensions:
            await ext.pre_session_save(session, ctx)
