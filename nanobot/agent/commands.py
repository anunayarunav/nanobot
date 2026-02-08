"""Slash command handling for the agent (e.g. /model)."""

import os

from loguru import logger

from nanobot.config.schema import Config, ModelAlias
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import make_provider


# Built-in aliases used when config has none
_DEFAULT_ALIASES: dict[str, ModelAlias] = {
    "cc": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "claude-code": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "opus": ModelAlias(model="anthropic/claude-opus-4-6", mode="oauth"),
    "sonnet": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="oauth"),
    "gemini": ModelAlias(model="gemini/gemini-2.5-pro-preview-06-05", mode="api"),
    "flash": ModelAlias(model="gemini/gemini-2.5-flash-preview-05-20", mode="api"),
    "claude": ModelAlias(model="anthropic/claude-sonnet-4-5-20250929", mode="api"),
}


class CommandHandler:
    """Handles slash commands like /model."""

    def __init__(self, config: Config):
        self.config = config

    def get_aliases(self) -> dict[str, ModelAlias]:
        """Get model aliases — config overrides, then built-in defaults."""
        aliases = dict(_DEFAULT_ALIASES)
        aliases.update(self.config.agents.model_aliases)
        return aliases

    def handle_model(self, text: str, current_model: str) -> tuple[str, str | None, LLMProvider | None]:
        """Handle /model command.

        Args:
            text: Full command text (e.g. "/model opus").
            current_model: The currently active model ID.

        Returns:
            (status_message, new_model_id_or_None, new_provider_or_None).
            If new_provider is None, model was not switched (just status display or error).
        """
        parts = text.split(None, 1)
        if len(parts) == 1:
            return self._model_status(current_model), None, None

        target = parts[1].strip()
        aliases = self.get_aliases()

        if target.lower() in aliases:
            alias = aliases[target.lower()]
            model, mode = alias.model, alias.mode
        else:
            model = target
            has_oauth = (
                self.config.providers.anthropic.oauth_access_token
                or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
            )
            mode = "oauth" if has_oauth and "anthropic" in model.lower() else "api"

        try:
            provider = make_provider(self.config, model, mode)
            label = "Claude Code (OAuth)" if mode == "oauth" else "API key"
            logger.info(f"Switched to model: {model} (mode: {mode})")
            return f"Switched to `{model}` via {label}", model, provider
        except Exception as e:
            logger.error(f"Failed to switch model: {e}")
            return f"Failed to switch model: {e}", None, None

    def _model_status(self, current_model: str) -> str:
        """Show current model and available shortcuts."""
        lines = [f"Current model: `{current_model}`", ""]
        p = self.config.providers
        lines.append("Available shortcuts:")
        has_oauth = (
            p.anthropic.oauth_access_token
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        if has_oauth:
            lines.append("  `/model opus` — Claude Opus 4.6 (OAuth/CLI)")
            lines.append("  `/model sonnet` — Claude Sonnet 4.5 (OAuth/CLI)")
            lines.append("  `/model cc` — alias for sonnet via Claude Code")
        if p.anthropic.api_key:
            lines.append("  `/model claude` — Claude Sonnet 4.5 (API key)")
        if p.gemini.api_key:
            lines.append("  `/model gemini` — Gemini 2.5 Pro")
            lines.append("  `/model flash` — Gemini 2.5 Flash")
        lines.append("")
        lines.append("Or use an explicit model: `/model anthropic/claude-opus-4-6`")
        return "\n".join(lines)


