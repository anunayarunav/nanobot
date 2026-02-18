"""MCP stdio server for Claude Code integration.

Runs as a subprocess of Claude CLI via --mcp-config.
Connects back to nanobot via Unix domain socket to forward
tool calls (send_message, send_progress) to the message bus.

Protocol: JSON-RPC 2.0 over stdin/stdout (MCP spec 2025-06-18).
"""

import json
import os
import socket
import sys

NANOBOT_SOCKET = os.environ.get("NANOBOT_SOCKET", "")

TOOLS = [
    {
        "name": "send_message",
        "description": (
            "Send a message to the user through their chat channel (e.g. Telegram). "
            "Use this to communicate results, share files, or send media. "
            "The message will appear in the user's active chat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message text to send to the user.",
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of absolute file paths to send as media "
                        "attachments (images, videos, audio, documents)."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "send_progress",
        "description": (
            "Send a brief progress update to the user. "
            "Use this for long-running tasks to keep the user informed. "
            "Keep messages short."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Brief progress text, e.g. 'Processing video...' "
                        "or 'Downloading files (3/5)'"
                    ),
                },
            },
            "required": ["text"],
        },
    },
]


def _send_to_nanobot(method: str, params: dict) -> dict:
    """Send a request to nanobot over Unix domain socket."""
    if not NANOBOT_SOCKET:
        return {"ok": False, "error": "NANOBOT_SOCKET not set"}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(NANOBOT_SOCKET)
        sock.settimeout(30.0)

        request = json.dumps({"method": method, "params": params}) + "\n"
        sock.sendall(request.encode())

        # Read response (newline-delimited)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()

        if data:
            return json.loads(data.decode().strip())
        return {"ok": False, "error": "No response from nanobot"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _write(msg: dict) -> None:
    """Write a JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle_initialize(msg_id: int | str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "nanobot", "version": "1.0.0"},
        },
    }


def _handle_tools_list(msg_id: int | str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"tools": TOOLS},
    }


def _handle_tools_call(msg_id: int | str, params: dict) -> dict:
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    if tool_name == "send_message":
        result = _send_to_nanobot("send_message", {
            "content": arguments.get("content", ""),
            "media": arguments.get("media", []),
        })
    elif tool_name == "send_progress":
        result = _send_to_nanobot("send_progress", {
            "text": arguments.get("text", ""),
        })
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            },
        }

    if result.get("ok"):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": "Message sent successfully."}],
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "content": [{"type": "text", "text": f"Error: {result.get('error', 'unknown')}"}],
            "isError": True,
        },
    }


def _handle_request(msg: dict) -> dict | None:
    """Handle a JSON-RPC request. Returns response dict or None for notifications."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _handle_initialize(msg_id)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _handle_tools_list(msg_id)
    if method == "tools/call":
        return _handle_tools_call(msg_id, params)

    # Unknown method
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main() -> None:
    """Run the MCP stdio server loop."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _handle_request(msg)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
