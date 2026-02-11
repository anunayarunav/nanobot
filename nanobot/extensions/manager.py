"""Extension manager: loads and runs extensions through lifecycle hooks."""

import importlib
from typing import Any

from loguru import logger

from nanobot.extensions.base import Extension, ExtensionContext

# Built-in extensions that are always loaded unless explicitly disabled.
# Each entry: (class_path, default_options)
_BUILTIN_EXTENSIONS = [
    ("nanobot.extensions.compaction.CompactionExtension", {}),
]


class ExtensionManager:
    """Loads extensions from config and runs them through pipeline hooks."""

    def __init__(self) -> None:
        self._extensions: list[Extension] = []

    async def load_from_config(self, extensions_config: list) -> None:
        """Import and initialize extensions from config entries.

        Built-in extensions (compaction) are always loaded unless the config
        explicitly lists them with enabled=false.

        Args:
            extensions_config: List of ExtensionConfig objects with class_path, enabled, options.
        """
        # Index user-provided config by class_path for override lookups
        user_overrides: dict[str, Any] = {}
        for ext_cfg in extensions_config:
            user_overrides[ext_cfg.class_path] = ext_cfg

        # Load built-in extensions first (unless user disabled them)
        for class_path, default_opts in _BUILTIN_EXTENSIONS:
            override = user_overrides.pop(class_path, None)
            if override and not override.enabled:
                logger.info(f"Built-in extension disabled by config: {class_path}")
                continue
            options = override.options if override else default_opts
            await self._load_extension(class_path, options)

        # Load remaining user-configured extensions
        for class_path, ext_cfg in user_overrides.items():
            if not ext_cfg.enabled:
                continue
            await self._load_extension(ext_cfg.class_path, ext_cfg.options)

    async def _load_extension(self, class_path: str, options: dict[str, Any]) -> None:
        """Import, instantiate, and initialize a single extension."""
        try:
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            ext = cls()
            await ext.on_load(options)
            self._extensions.append(ext)
            logger.info(f"Loaded extension: {ext.name} ({class_path})")
        except Exception as e:
            logger.error(f"Failed to load extension {class_path}: {e}")

    async def transform_history(
        self, history: list[dict[str, Any]], session: Any, ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        for ext in self._extensions:
            try:
                history = await ext.transform_history(history, session, ctx)
            except Exception as e:
                logger.error(f"Extension {ext.name} failed in transform_history: {e}")
        return history

    async def transform_messages(
        self, messages: list[dict[str, Any]], ctx: ExtensionContext
    ) -> list[dict[str, Any]]:
        for ext in self._extensions:
            try:
                messages = await ext.transform_messages(messages, ctx)
            except Exception as e:
                logger.error(f"Extension {ext.name} failed in transform_messages: {e}")
        return messages

    async def transform_response(self, content: str, ctx: ExtensionContext) -> str:
        for ext in self._extensions:
            try:
                content = await ext.transform_response(content, ctx)
            except Exception as e:
                logger.error(f"Extension {ext.name} failed in transform_response: {e}")
        return content

    async def pre_session_save(self, session: Any, ctx: ExtensionContext) -> None:
        for ext in self._extensions:
            try:
                await ext.pre_session_save(session, ctx)
            except Exception as e:
                logger.error(f"Extension {ext.name} failed in pre_session_save: {e}")
