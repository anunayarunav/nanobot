"""Unix domain socket listener for the MCP bridge.

Runs in nanobot's asyncio event loop.  Receives tool calls from the
MCP stdio server (a subprocess of the Claude CLI) and routes them to
the appropriate callbacks (message bus, progress).
"""

import asyncio
import json
import os
import sys
import tempfile
import uuid
from typing import Any

from loguru import logger

from nanobot.providers.base import MessageCallback, ProgressCallback


async def start_listener(
    message_cb: MessageCallback | None,
    progress_cb: ProgressCallback | None,
) -> tuple[str, asyncio.AbstractServer]:
    """Start a Unix socket listener and return (socket_path, server).

    The caller is responsible for closing the server and cleaning up the socket file.
    """
    socket_path = os.path.join(
        tempfile.gettempdir(), f"nanobot-mcp-{uuid.uuid4().hex[:12]}.sock",
    )

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await reader.readline()
                if not data:
                    break
                line = data.decode().strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    _write_response(writer, {"ok": False, "error": "Invalid JSON"})
                    await writer.drain()
                    continue

                method = request.get("method", "")
                params = request.get("params", {})
                response = await _dispatch(method, params, message_cb, progress_cb)
                _write_response(writer, response)
                await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"MCP socket client error: {e}")
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)
    logger.debug(f"MCP socket listener started: {socket_path}")
    return socket_path, server


async def _dispatch(
    method: str,
    params: dict[str, Any],
    message_cb: MessageCallback | None,
    progress_cb: ProgressCallback | None,
) -> dict[str, Any]:
    """Route a tool call to the appropriate callback."""
    if method == "send_message":
        if not message_cb:
            return {"ok": False, "error": "No message callback configured"}
        content = params.get("content", "")
        media = params.get("media", [])
        try:
            await message_cb(content, media)
            return {"ok": True}
        except Exception as e:
            logger.error(f"MCP send_message error: {e}")
            return {"ok": False, "error": str(e)}

    if method == "send_progress":
        if not progress_cb:
            return {"ok": False, "error": "No progress callback configured"}
        text = params.get("text", "")
        try:
            await progress_cb(text)
            return {"ok": True}
        except Exception as e:
            logger.error(f"MCP send_progress error: {e}")
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Unknown method: {method}"}


def _write_response(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
    """Write a JSON response line to the socket."""
    writer.write((json.dumps(response) + "\n").encode())


def generate_mcp_config(socket_path: str) -> dict[str, Any]:
    """Generate MCP config dict for Claude CLI's --mcp-config flag."""
    return {
        "mcpServers": {
            "nanobot": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "nanobot.mcp.server"],
                "env": {
                    "NANOBOT_SOCKET": socket_path,
                },
            },
        },
    }
