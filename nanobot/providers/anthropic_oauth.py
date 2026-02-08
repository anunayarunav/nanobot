"""Anthropic OAuth provider for Claude Pro/Max subscription tokens.

Uses the Claude CLI (`claude -p`) as a proxy since the API requires
cryptographic verification that only the official CLI provides.
The OAuth token is passed via CLAUDE_CODE_OAUTH_TOKEN env var.
"""

import asyncio
import json
import os
import shutil
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse


class AnthropicOAuthProvider(LLMProvider):
    """LLM provider using Claude CLI with OAuth token.

    Requires `claude` CLI installed (npm install -g @anthropic-ai/claude-code).
    Passes the OAuth token via CLAUDE_CODE_OAUTH_TOKEN environment variable.
    """

    def __init__(
        self,
        oauth_token: str,
        default_model: str = "anthropic/claude-opus-4-6",
        claude_bin: str | None = None,
    ):
        super().__init__()
        self.oauth_token = oauth_token
        self.default_model = default_model
        self.claude_bin = claude_bin or shutil.which("claude") or "claude"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model = model or self.default_model
        if "/" in model:
            model = model.split("/", 1)[1]

        # Build the prompt from messages
        prompt = self._build_prompt(messages)

        # Claude CLI args
        args = [
            self.claude_bin,
            "-p", prompt,
            "--model", model,
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]

        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token, "CLAUDE_CODE_ENTRYPOINT": "cli"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300.0
            )

            if proc.returncode != 0:
                err = stderr.decode().strip() or stdout.decode().strip()
                logger.error(f"Claude CLI error (exit {proc.returncode}): {err}")
                return LLMResponse(
                    content=f"Claude CLI error: {err}",
                    finish_reason="error",
                )

            return self._parse_cli_output(stdout.decode())

        except asyncio.TimeoutError:
            logger.error("Claude CLI timed out after 300s")
            return LLMResponse(content="Claude CLI timed out", finish_reason="error")
        except FileNotFoundError:
            logger.error(f"Claude CLI not found at: {self.claude_bin}")
            return LLMResponse(
                content="Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
                finish_reason="error",
            )
        except Exception as e:
            logger.error(f"Claude CLI error: {e}")
            return LLMResponse(content=f"Error: {str(e)}", finish_reason="error")

    @staticmethod
    def _build_prompt(messages: list[dict[str, Any]]) -> str:
        """Build a single prompt string from OpenAI-format messages.

        Claude CLI's -p mode takes a single string prompt.
        We combine system + conversation into one prompt, preserving
        tool call structure for multi-turn context.
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                parts.append(content)
            elif role == "user":
                parts.append(content)
            elif role == "assistant":
                # Preserve tool call info if present
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    calls = ", ".join(
                        tc.get("function", {}).get("name", "?") for tc in tool_calls
                    )
                    text = f"[Assistant called tools: {calls}]"
                    if content:
                        text = f"{content}\n{text}"
                    parts.append(text)
                elif content:
                    parts.append(f"[Previous assistant response: {content}]")
            elif role == "tool":
                name = msg.get("name", "tool")
                # Truncate very long tool results to keep prompt manageable
                result = content[:2000] if len(content) > 2000 else content
                parts.append(f"[Tool result from {name}:\n{result}]")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_cli_output(output: str) -> LLMResponse:
        """Parse Claude CLI JSON output into LLMResponse."""
        try:
            data = json.loads(output)
            # JSON output format: {"type":"result","subtype":"success","cost_usd":...,"duration_ms":...,"duration_api_ms":...,"is_error":false,"num_turns":1,"result":"...","session_id":"...","total_cost_usd":...}
            content = data.get("result", "")
            is_error = data.get("is_error", False)
            usage = {}
            if "cost_usd" in data:
                usage["cost_usd"] = data["cost_usd"]
            return LLMResponse(
                content=content,
                finish_reason="error" if is_error else "stop",
                usage=usage,
            )
        except json.JSONDecodeError:
            # Plain text output (non-JSON mode fallback)
            return LLMResponse(content=output.strip(), finish_reason="stop")

    def get_default_model(self) -> str:
        return self.default_model
