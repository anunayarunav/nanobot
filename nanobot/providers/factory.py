"""Unified provider creation for all entry points."""

import os
import shutil

from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider


def make_provider(config: Config, model: str | None = None, mode: str | None = None) -> LLMProvider:
    """Create the right LLM provider for a given model and mode.

    This is the single source of truth for provider creation.
    Used by CLI startup, /model command, and anywhere else a provider is needed.

    Args:
        config: Full nanobot config.
        model: Model identifier. Defaults to config default model.
        mode: "api" or "oauth". If None, auto-detects from model name and available credentials.

    Returns:
        Configured LLMProvider instance.

    Raises:
        ValueError: If required credentials are missing.
    """
    model = model or config.agents.defaults.model

    # Auto-detect mode if not specified
    if mode is None:
        oauth_token = (
            config.providers.anthropic.oauth_access_token
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        model_is_anthropic = "anthropic" in model.lower() or "claude" in model.lower()
        mode = "oauth" if oauth_token and model_is_anthropic else "api"

    if mode == "oauth":
        oauth_token = (
            config.providers.anthropic.oauth_access_token
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        if not oauth_token:
            raise ValueError("No OAuth token configured for Anthropic")
        from nanobot.providers.anthropic_oauth import AnthropicOAuthProvider
        claude_bin = shutil.which("claude") or "claude"
        return AnthropicOAuthProvider(
            oauth_token=oauth_token,
            default_model=model,
            claude_bin=claude_bin,
        )

    # API key mode — use LiteLLM
    from nanobot.providers.litellm_provider import LiteLLMProvider

    provider_cfg = config.get_provider(model)
    if not provider_cfg or (not provider_cfg.api_key and not model.startswith("bedrock/")):
        raise ValueError(f"No API key configured for model `{model}`")

    return LiteLLMProvider(
        api_key=provider_cfg.api_key if provider_cfg else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=provider_cfg.extra_headers if provider_cfg else None,
    )
