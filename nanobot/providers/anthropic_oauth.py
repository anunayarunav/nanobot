"""Anthropic OAuth provider for Claude Pro/Max subscription tokens.

Uses the Claude CLI (`claude -p`) as a proxy since the API requires
cryptographic verification that only the official CLI provides.
The OAuth token is passed via CLAUDE_CODE_OAUTH_TOKEN env var.

Streams stdout line-by-line using --output-format stream-json --verbose
to provide real-time progress feedback while the CLI works.

When message_callback is set, starts an MCP bridge so the CLI can send
messages (with media) back to the user through nanobot's channel layer.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ProgressCallback

_MIN_PROGRESS_INTERVAL = 3.0  # seconds between progress messages
_READ_TIMEOUT = 30.0  # seconds before sending a heartbeat


class AnthropicOAuthProvider(LLMProvider):
    """LLM provider using Claude CLI with OAuth token.

    Requires `claude` CLI installed (npm install -g @anthropic-ai/claude-code).
    Passes the OAuth token via CLAUDE_CODE_OAUTH_TOKEN environment variable.
    Streams output for real-time progress via progress_callback.
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

        prompt = self._build_prompt(messages)

        # Claude CLI args — prompt is piped via stdin to avoid ARG_MAX limits
        args = [
            self.claude_bin,
            "-p",
            "--model", model,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

        env = {
            **os.environ,
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
            "CLAUDE_CODE_ENTRYPOINT": "cli",
        }

        # MCP bridge setup — gives CLI access to nanobot's message bus
        socket_path: str | None = None
        socket_server: asyncio.AbstractServer | None = None
        mcp_config_path: str | None = None

        if self.message_callback:
            try:
                from nanobot.mcp.listener import start_listener, generate_mcp_config

                socket_path, socket_server = await start_listener(
                    message_cb=self.message_callback,
                    progress_cb=self.progress_callback,
                )
                mcp_config = generate_mcp_config(socket_path)
                mcp_config_path = socket_path.replace(".sock", ".json")
                with open(mcp_config_path, "w") as f:
                    json.dump(mcp_config, f)

                args.extend(["--mcp-config", mcp_config_path])
                logger.debug(f"MCP bridge enabled: {socket_path}")
            except Exception as e:
                logger.warning(f"Failed to start MCP bridge: {e}")
                # Continue without MCP — CLI still works, just can't send messages

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=2 * 1024 * 1024,  # 2MB — CLI can emit large JSON lines
            )
            return await self._stream_response(proc, prompt)

        except FileNotFoundError:
            logger.error(f"Claude CLI not found at: {self.claude_bin}")
            return LLMResponse(
                content="Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
                finish_reason="error",
            )
        except Exception as e:
            logger.error(f"Claude CLI error: {e}")
            return LLMResponse(content=f"Error: {str(e)}", finish_reason="error")
        finally:
            # Clean up MCP bridge
            if socket_server:
                socket_server.close()
                await socket_server.wait_closed()
            if socket_path and os.path.exists(socket_path):
                os.unlink(socket_path)
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)

    async def _stream_response(
        self,
        proc: asyncio.subprocess.Process,
        prompt: str,
    ) -> LLMResponse:
        """Stream stdout from the Claude CLI, forwarding progress and collecting the result."""
        # Capture callback at call time for concurrency safety
        progress_cb = self.progress_callback

        # Drain stderr in background to prevent pipe blocking
        stderr_task = asyncio.create_task(self._drain_stderr(proc))

        # Write prompt to stdin and close
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as e:
            logger.warning(f"Failed to write to Claude CLI stdin: {e}")

        # Streaming state
        result_content: str | None = None
        result_usage: dict[str, Any] = {}
        is_error = False
        last_progress_time = 0.0
        start_time = time.monotonic()
        accumulated_text: list[str] = []

        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=_READ_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # No output for 30s — send heartbeat
                    if progress_cb:
                        elapsed = int(time.monotonic() - start_time)
                        mins, secs = divmod(elapsed, 60)
                        label = f"{mins}m{secs}s" if mins else f"{secs}s"
                        try:
                            await progress_cb(f"⏳ Still working... ({label})")
                        except Exception:
                            pass
                    continue

                if not line_bytes:
                    break  # EOF — process closed stdout

                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
                if not line:
                    continue

                # Parse JSON event
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    accumulated_text.append(line)
                    continue

                if not isinstance(event, dict):
                    accumulated_text.append(line)
                    continue

                event_type = event.get("type", "")

                if event_type == "result":
                    result_content = event.get("result", "")
                    is_error = event.get("is_error", False)
                    if "cost_usd" in event:
                        result_usage["cost_usd"] = event["cost_usd"]
                    if "total_cost_usd" in event:
                        result_usage["total_cost_usd"] = event["total_cost_usd"]

                elif event_type == "assistant":
                    last_progress_time = await self._handle_assistant_event(
                        event, progress_cb, last_progress_time,
                    )

                elif event_type == "system":
                    logger.debug(f"Claude CLI init: {json.dumps(event)[:200]}")

                else:
                    logger.debug(f"Claude CLI event '{event_type}': {line[:200]}")

        except Exception as e:
            logger.error(f"Error reading Claude CLI stdout: {e}")

        # Wait for stderr and process exit
        stderr_output = await stderr_task
        await proc.wait()

        if proc.returncode != 0 and result_content is None:
            err = stderr_output or "unknown error"
            logger.error(f"Claude CLI error (exit {proc.returncode}): {err}")
            return LLMResponse(content=f"Claude CLI error: {err}", finish_reason="error")

        # Prefer explicit result event
        if result_content is not None:
            return LLMResponse(
                content=result_content,
                finish_reason="error" if is_error else "stop",
                usage=result_usage,
            )

        # Fallback: no result event (GitHub issue #1920)
        if accumulated_text:
            logger.warning("No result event from Claude CLI; using accumulated text")
            return LLMResponse(content="\n".join(accumulated_text), finish_reason="stop")

        return LLMResponse(content="Claude CLI produced no output", finish_reason="error")

    @staticmethod
    async def _handle_assistant_event(
        event: dict[str, Any],
        progress_cb: ProgressCallback | None,
        last_progress_time: float,
    ) -> float:
        """Extract tool usage from an assistant event and send progress. Returns updated timestamp."""
        if not progress_cb:
            return last_progress_time

        now = time.monotonic()
        if now - last_progress_time < _MIN_PROGRESS_INTERVAL:
            return last_progress_time

        message = event.get("message", {})
        content_blocks = message.get("content", [])

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "?")
                # Skip MCP bridge tools — message is delivered directly
                if name.startswith("mcp__nanobot__"):
                    return last_progress_time
                detail = _tool_detail(name, block.get("input", {}))
                try:
                    await progress_cb(f"🔧 `{name}`: {detail}")
                except Exception:
                    pass
                return now

        return last_progress_time

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> str:
        """Read all stderr to prevent pipe blocking."""
        try:
            data = await proc.stderr.read()
            return data.decode("utf-8", errors="replace").strip()
        except Exception as e:
            logger.warning(f"Error draining Claude CLI stderr: {e}")
            return ""

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

    def get_default_model(self) -> str:
        return self.default_model


def _tool_detail(name: str, tool_input: dict[str, Any]) -> str:
    """Extract a brief description from a tool's input."""
    if name == "Bash":
        cmd = str(tool_input.get("command", ""))
        return cmd[:80] + "..." if len(cmd) > 80 else cmd
    elif name in ("Read", "Write", "Edit"):
        return str(tool_input.get("file_path", ""))[:80]
    elif name == "Glob":
        return str(tool_input.get("pattern", ""))[:80]
    elif name == "Grep":
        return str(tool_input.get("pattern", ""))[:80]
    elif name in ("WebSearch", "WebFetch"):
        return str(tool_input.get("query", tool_input.get("url", "")))[:80]
    elif name == "Task":
        return str(tool_input.get("prompt", ""))[:80]
    # Generic: first key-value
    for k, v in tool_input.items():
        v_str = str(v)
        return f"{k}={v_str[:60]}" if len(v_str) > 60 else f"{k}={v_str}"
    return ""
