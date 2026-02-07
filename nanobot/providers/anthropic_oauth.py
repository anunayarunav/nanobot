"""Anthropic OAuth provider for Claude Pro/Max subscription tokens.

Supports OAuth tokens (sk-ant-oat01-...) from Claude Code / CLI,
sending them as Bearer tokens with the required beta headers.
Handles automatic token refresh when expired.
"""

import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
API_URL = "https://api.anthropic.com/v1/messages"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Headers that identify the request as a Claude Code client
CLAUDE_CODE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "user-agent": "claude-cli/2.1.2 (external, cli)",
    "x-app": "cli",
    "anthropic-dangerous-direct-browser-access": "true",
    "accept": "application/json",
    "content-type": "application/json",
}


class AnthropicOAuthProvider(LLMProvider):
    """LLM provider using Anthropic OAuth (Claude Pro/Max subscription).

    Uses Bearer token auth with Claude Code headers.
    Automatically refreshes expired tokens using the refresh token.
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str = "",
        expires_at: int = 0,
        default_model: str = "anthropic/claude-opus-4-6",
        credentials_path: str | None = None,
    ):
        super().__init__(api_key=access_token)
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.default_model = default_model
        self.credentials_path = credentials_path
        self._client = httpx.AsyncClient(timeout=120.0)

        # Load cached tokens if available (from previous refresh)
        if credentials_path:
            self._load_cached_tokens()

    def _load_cached_tokens(self) -> None:
        """Load previously refreshed tokens from cache file."""
        try:
            p = Path(self.credentials_path)
            if not p.exists():
                return
            data = json.loads(p.read_text())
            cached_at = data.get("expires_at", 0)
            # Use cached tokens only if they're newer than what we have
            if cached_at > self.expires_at:
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.expires_at = cached_at
                logger.info("Loaded refreshed OAuth tokens from cache")
        except Exception as e:
            logger.debug(f"No cached OAuth tokens: {e}")

    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        if self.refresh_token and self.expires_at and time.time() * 1000 > self.expires_at:
            logger.info("OAuth token expired, refreshing...")
            try:
                await self._refresh()
                logger.info("OAuth token refreshed successfully")
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}, using existing token")
        return self.access_token

    async def _refresh(self) -> None:
        """Refresh the OAuth token using the refresh token."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": self.refresh_token,
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        # expires_in is in seconds, store as ms with 5min buffer
        self.expires_at = int(time.time() * 1000) + data["expires_in"] * 1000 - 300_000

        # Persist refreshed credentials if path is configured
        if self.credentials_path:
            self._save_credentials()

    def _save_credentials(self) -> None:
        """Persist refreshed credentials to disk."""
        path = Path(self.credentials_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
            }
            path.write_text(json.dumps(data, indent=2))
            logger.debug(f"OAuth credentials saved to {path}")
        except Exception as e:
            logger.warning(f"Failed to save OAuth credentials: {e}")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model = model or self.default_model
        # Strip provider prefix (e.g. "anthropic/claude-opus-4-6" -> "claude-opus-4-6")
        if "/" in model:
            model = model.split("/", 1)[1]

        token = await self._ensure_valid_token()

        headers = {
            **CLAUDE_CODE_HEADERS,
            "Authorization": f"Bearer {token}",
        }

        # Convert OpenAI-format messages to Anthropic format
        system_prompt, anthropic_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_prompt:
            payload["system"] = system_prompt

        if tools:
            payload["tools"] = self._convert_tools(tools)
            payload["tool_choice"] = {"type": "auto"}

        try:
            resp = await self._client.post(API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            return self._parse_response(resp.json())
        except httpx.HTTPStatusError as e:
            body = e.response.text
            logger.error(f"Anthropic OAuth API error {e.response.status_code}: {body}")
            return LLMResponse(
                content=f"Error calling Anthropic API: {e.response.status_code} - {body}",
                finish_reason="error",
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Anthropic API: {str(e)}",
                finish_reason="error",
            )

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic format.

        Returns (system_prompt, anthropic_messages).
        Handles: system→top-level, tool→tool_result, assistant tool_calls→tool_use.
        """
        system_parts: list[str] = []
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                system_parts.append(msg.get("content", ""))

            elif role == "user":
                result.append({"role": "user", "content": msg.get("content", "")})

            elif role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                text = msg.get("content")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    args = func.get("arguments", {})
                    # arguments may be a JSON string from OpenAI format
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {"raw": args}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args,
                    })
                result.append({"role": "assistant", "content": content_blocks or text or ""})

            elif role == "tool":
                # Anthropic expects tool_result as a user message
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })

        return "\n\n".join(system_parts), result

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool definitions to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return anthropic_tools

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse Anthropic API response into LLMResponse."""
        content_parts = []
        tool_calls = []

        for block in data.get("content", []):
            if block["type"] == "text":
                content_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input", {}),
                ))

        usage = {}
        if "usage" in data:
            usage = {
                "prompt_tokens": data["usage"].get("input_tokens", 0),
                "completion_tokens": data["usage"].get("output_tokens", 0),
                "total_tokens": data["usage"].get("input_tokens", 0)
                + data["usage"].get("output_tokens", 0),
            }

        return LLMResponse(
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
            usage=usage,
        )

    def get_default_model(self) -> str:
        return self.default_model

    @classmethod
    def from_credentials_file(
        cls,
        path: str,
        default_model: str = "anthropic/claude-opus-4-6",
    ) -> "AnthropicOAuthProvider | None":
        """Load provider from a credentials file (JSON with access_token, refresh_token, expires_at)."""
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            return cls(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                expires_at=data.get("expires_at", 0),
                default_model=default_model,
                credentials_path=path,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load OAuth credentials from {path}: {e}")
            return None

    @classmethod
    def from_openclaw(
        cls,
        default_model: str = "anthropic/claude-opus-4-6",
    ) -> "AnthropicOAuthProvider | None":
        """Auto-detect credentials from OpenClaw's auth profile store."""
        store_path = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
        if not store_path.exists():
            return None
        try:
            store = json.loads(store_path.read_text())
            profiles = store.get("profiles", {})

            # Try lastGood first, then any anthropic profile
            last_good_id = store.get("lastGood", {}).get("anthropic")
            candidates = [last_good_id] if last_good_id else []
            candidates += [pid for pid in profiles if pid.startswith("anthropic:")]

            for pid in candidates:
                cred = profiles.get(pid)
                if not cred or cred.get("provider") != "anthropic":
                    continue
                if cred.get("type") == "oauth":
                    return cls(
                        access_token=cred["access"],
                        refresh_token=cred.get("refresh", ""),
                        expires_at=cred.get("expires", 0),
                        default_model=default_model,
                        credentials_path=str(store_path),
                    )
                if cred.get("type") == "token":
                    return cls(
                        access_token=cred["token"],
                        refresh_token="",
                        expires_at=cred.get("expires", 0),
                        default_model=default_model,
                    )
            return None
        except Exception as e:
            logger.warning(f"Failed to load OpenClaw credentials: {e}")
            return None

    @classmethod
    def from_claude_cli(
        cls,
        default_model: str = "anthropic/claude-opus-4-6",
    ) -> "AnthropicOAuthProvider | None":
        """Auto-detect credentials from Claude CLI (~/.claude/.credentials.json)."""
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if not cred_path.exists():
            return None
        try:
            data = json.loads(cred_path.read_text())
            oauth = data.get("claudeAiOauth", {})
            access = oauth.get("accessToken")
            if not access:
                return None
            return cls(
                access_token=access,
                refresh_token=oauth.get("refreshToken", ""),
                expires_at=oauth.get("expiresAt", 0),
                default_model=default_model,
                credentials_path=str(cred_path),
            )
        except Exception as e:
            logger.warning(f"Failed to load Claude CLI credentials: {e}")
            return None
