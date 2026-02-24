"""Terminal mode: direct shell execution bypassing the LLM pipeline.

Supports two protocols:

- **plain** (default): wait for process exit, capture stdout/stderr,
  regex-scan for media file paths.  Backward-compatible.
- **rich**: read stdout line-by-line as JSONL frames.  Each frame has a
  ``type`` field (``message``, ``progress``, ``error``, ``log``).
  Enables real-time progress, multiple messages, and structured media.

Both protocols receive a JSON input envelope on stdin containing the
user's message, media paths, and session metadata.
"""

import asyncio
import json
import os
import re
import shlex
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.config.schema import TerminalConfig

# ---------------------------------------------------------------------------
# Media detection (shared by plain mode and rich-mode fallback)
# ---------------------------------------------------------------------------

# File extensions recognized as media
_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".ogg", ".m4a", ".wav", ".flac",
    ".pdf",
}

# Regex to find absolute file paths in output
_PATH_PATTERN = re.compile(r"(?:^|\s)(/[\w./-]+)", re.MULTILINE)


def extract_media_paths(text: str) -> list[str]:
    """Scan text for file paths that look like media files and exist on disk."""
    media: list[str] = []
    seen: set[str] = set()

    for match in _PATH_PATTERN.finditer(text):
        raw = match.group(1).strip()
        try:
            resolved = Path(raw).resolve()
        except (OSError, ValueError):
            continue

        str_path = str(resolved)
        if str_path in seen:
            continue
        seen.add(str_path)

        if resolved.suffix.lower() in _MEDIA_EXTENSIONS and resolved.is_file():
            media.append(str_path)

    return media


# ---------------------------------------------------------------------------
# Input envelope (stdin JSON — shared by both protocols)
# ---------------------------------------------------------------------------

def _ensure_user_data_dir(workspace: str, chat_id: str) -> str:
    """Create and return the per-user data directory.

    Layout: ``{workspace}/users/{chat_id}/``
    The directory is created if it does not exist.
    """
    user_dir = Path(workspace) / "users" / chat_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return str(user_dir)


def _build_input_envelope(
    msg: InboundMessage,
    workspace: str,
    config: TerminalConfig,
) -> str:
    """Build the JSON input envelope written to the subprocess's stdin."""
    user_data_dir = _ensure_user_data_dir(workspace, msg.chat_id)
    envelope: dict[str, Any] = {
        "version": 1,
        "text": msg.content,
        "channel": msg.channel,
        "chat_id": msg.chat_id,
        "session_key": msg.session_key,
        "workspace": workspace,
        "user_data_dir": user_data_dir,
    }
    if config.pass_media and msg.media:
        envelope["media"] = list(msg.media)
    if config.providers:
        envelope["providers"] = {
            name: {
                "api_keys": p.api_keys,
                **({"models": p.models} if p.models else {}),
                **({"base_url": p.base_url} if p.base_url else {}),
            }
            for name, p in config.providers.items()
            if p.api_keys  # only include providers that have keys
        }
    return json.dumps(envelope, ensure_ascii=False)


# ---------------------------------------------------------------------------
# JSONL frame parser (rich protocol)
# ---------------------------------------------------------------------------

def _parse_frame(line: str) -> dict[str, Any] | None:
    """Parse a single JSONL frame.

    Returns the parsed dict if *line* is valid JSON containing a ``type``
    field, otherwise ``None``.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and "type" in obj:
        return obj
    return None


# ---------------------------------------------------------------------------
# Command template helpers
# ---------------------------------------------------------------------------

def _build_command(template: str, msg: InboundMessage) -> str:
    """Substitute placeholders in the command template."""
    command = template
    if "{message}" in command:
        command = command.replace("{message}", shlex.quote(msg.content.strip()))
    return command


_PROVIDER_ENV_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
}


def _build_env(config: TerminalConfig) -> dict[str, str] | None:
    """Build the subprocess environment.

    Merges static ``config.env`` vars and injects provider API keys as
    standard environment variables (e.g. ``ANTHROPIC_API_KEY``) so that
    SDKs like litellm, openai, google-genai work out of the box.

    When a provider has multiple keys, the first is set as the standard
    env var and all are available as ``{NAME}_API_KEYS`` (comma-separated).
    """
    has_extras = bool(config.env) or bool(config.providers)
    if not has_extras:
        return None  # inherit parent environment
    env = dict(os.environ)
    # Inject provider API keys as env vars
    for name, provider in config.providers.items():
        if not provider.api_keys:
            continue
        env_var = _PROVIDER_ENV_MAP.get(name)
        if env_var:
            env[env_var] = provider.api_keys[0]
        # Always set {NAME}_API_KEYS with all keys (comma-separated)
        env[f"{name.upper()}_API_KEYS"] = ",".join(provider.api_keys)
    # Static env vars from config (override provider defaults if set)
    env.update(config.env)
    return env


# ---------------------------------------------------------------------------
# Cancellation watcher (shared by both protocols)
# ---------------------------------------------------------------------------

def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the subprocess and its entire process group.

    ``create_subprocess_shell`` spawns ``sh -c <cmd>``.  Killing just the
    shell leaves the child command running.  We kill the whole process
    group instead so nothing survives.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already dead or we lack rights — fall back to direct kill
        proc.kill()


async def _cancel_watcher(
    event: asyncio.Event, proc: asyncio.subprocess.Process,
) -> None:
    """Await *event* and kill the subprocess when it fires."""
    await event.wait()
    _kill_process_tree(proc)


# ---------------------------------------------------------------------------
# Plain protocol (original behaviour)
# ---------------------------------------------------------------------------

async def execute_terminal_command(
    msg: InboundMessage,
    template: str,
    workspace: str,
    timeout: int = 120,
    *,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> OutboundMessage:
    """Execute a user message as a shell command via a template.

    The ``{message}`` placeholder in *template* is replaced with the
    shell-escaped user text.
    """
    command = _build_command(template, msg)

    preview = command[:120] + "..." if len(command) > 120 else command
    logger.info(f"Terminal exec [{msg.session_key}]: {preview}")

    watcher: asyncio.Task | None = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_project_root(workspace),
            env=env,
            start_new_session=True,
        )

        if cancel_event is not None:
            watcher = asyncio.create_task(_cancel_watcher(cancel_event, process))

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(
                    input=stdin_data.encode("utf-8") if stdin_data else None,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _kill_process_tree(process)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Something went wrong. Please try again later.",
                error=True,
            )
    except Exception as e:
        logger.error(f"Terminal exec error: {e}")
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Something went wrong. Please try again later.",
            error=True,
        )
    finally:
        if watcher is not None:
            watcher.cancel()

    # Cancelled by /stop
    if cancel_event is not None and cancel_event.is_set():
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Operation cancelled.",
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    # Build text content
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.strip())
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    if process.returncode != 0:
        parts.append(f"Exit code: {process.returncode}")

    content = "\n".join(parts) if parts else "(no output)"

    # Detect media file paths in stdout
    media = extract_media_paths(stdout)

    return OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=content,
        media=media,
    )


# ---------------------------------------------------------------------------
# Rich protocol (JSONL streaming)
# ---------------------------------------------------------------------------

async def _execute_terminal_rich(
    msg: InboundMessage,
    config: TerminalConfig,
    workspace: str,
    publish: Callable[[OutboundMessage], Awaitable[None]],
    cancel_event: asyncio.Event | None = None,
) -> OutboundMessage | None:
    """Execute with the rich JSONL protocol.

    Reads stdout line-by-line, dispatching frames in real time.  Progress
    and intermediate message frames are published immediately via
    *publish*.  Returns the final ``OutboundMessage`` (or ``None`` if all
    messages were already published).
    """
    command = _build_command(config.command, msg)
    stdin_data = _build_input_envelope(msg, workspace, config)
    env = _build_env(config)

    preview = command[:120] + "..." if len(command) > 120 else command
    logger.info(f"Terminal rich [{msg.session_key}]: {preview}")

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_project_root(workspace),
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        logger.error(f"Failed to start terminal process: {e}")
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Something went wrong. Please try again later.",
            error=True,
        )

    watcher: asyncio.Task | None = None
    if cancel_event is not None:
        watcher = asyncio.create_task(_cancel_watcher(cancel_event, process))

    # Write input envelope and close stdin
    assert process.stdin is not None
    try:
        process.stdin.write(stdin_data.encode("utf-8"))
        process.stdin.write(b"\n")
        await process.stdin.drain()
        process.stdin.close()
    except Exception as e:
        logger.warning(f"Failed to write stdin: {e}")

    # Stream stdout line-by-line
    accumulated_text: list[str] = []
    final_message: OutboundMessage | None = None
    timed_out = False

    assert process.stdout is not None
    try:
        deadline = asyncio.get_event_loop().time() + config.timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                timed_out = True
                break

            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                timed_out = True
                break

            if not line_bytes:
                break  # EOF

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
            if not line:
                continue

            frame = _parse_frame(line)

            if frame is None:
                # Non-JSON line — accumulate as plain text
                accumulated_text.append(line)
                continue

            frame_type = frame.get("type", "message")

            if frame_type == "progress":
                text = frame.get("text", "...")
                await publish(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=f"⏳ {text}",
                ))

            elif frame_type == "message":
                # Publish the previous message, keep the latest as final
                if final_message is not None:
                    await publish(final_message)
                final_message = OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=frame.get("text", ""),
                    media=frame.get("media", []),
                )

            elif frame_type == "error":
                code = frame.get("code", "")
                error_text = frame.get("text", "Unknown error")
                logger.error(f"Terminal error [{msg.session_key}]: {code} — {error_text}")
                final_message = OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Something went wrong. Please try again later.",
                    error=True,
                )

            elif frame_type == "log":
                level = frame.get("level", "debug").upper()
                log_text = frame.get("text", "")
                logger.log(level, f"[terminal] {log_text}")

            else:
                # Unknown frame type — log and skip
                logger.debug(f"Unknown terminal frame type: {frame_type}")

    except Exception as e:
        logger.error(f"Error reading terminal stdout: {e}")

    # Clean up the cancel watcher
    if watcher is not None:
        watcher.cancel()

    # Handle /stop cancellation
    if cancel_event is not None and cancel_event.is_set():
        if process.returncode is None:
            _kill_process_tree(process)
        await process.wait()
        logger.info(f"Terminal process [{msg.session_key}] cancelled by /stop")
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Operation cancelled.",
        )

    # Handle timeout
    if timed_out:
        _kill_process_tree(process)
        await process.wait()
        logger.warning(
            f"Terminal process [{msg.session_key}] killed after {config.timeout}s timeout"
            f" (exit code {process.returncode})"
        )
        timeout_msg = OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Something went wrong. Please try again later.",
            error=True,
        )
        if final_message is not None:
            await publish(final_message)
        return timeout_msg

    # Wait for process to finish and capture stderr
    assert process.stderr is not None
    stderr_bytes = await process.stderr.read()
    await process.wait()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""

    # Always log exit code for diagnostics (especially OOM/SIGKILL = -9)
    rc = process.returncode
    if rc and rc != 0:
        logger.warning(
            f"Terminal process [{msg.session_key}] exited with code {rc}"
            + (f" (signal {-rc})" if rc < 0 else "")
        )
    else:
        logger.info(f"Terminal process [{msg.session_key}] exited with code {rc}")

    is_error = bool(rc and rc != 0)

    # If we got accumulated plain text but no structured message, fall back
    if accumulated_text and final_message is None:
        if is_error:
            # Non-zero exit with no message frame — hide internals
            logger.error(f"Terminal failed [{msg.session_key}]: {chr(10).join(accumulated_text[:5])}")
            final_message = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Something went wrong. Please try again later.",
                error=True,
            )
        else:
            combined = "\n".join(accumulated_text)
            media = extract_media_paths(combined)
            final_message = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=combined,
                media=media,
            )
    elif final_message is None:
        # No output at all
        if is_error:
            if stderr:
                logger.error(f"Terminal stderr [{msg.session_key}]: {stderr[:500]}")
            final_message = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Something went wrong. Please try again later.",
                error=True,
            )
        else:
            final_message = OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="(no output)",
            )
    else:
        # Log stderr for diagnostics but don't show to user
        if stderr:
            logger.warning(f"Terminal stderr [{msg.session_key}]: {stderr[:500]}")
        if is_error and not final_message.error:
            logger.warning(f"Terminal exited {rc} but had message frame — keeping response")

    return final_message


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def _project_root(workspace: str) -> str:
    """Derive the project root from the workspace path.

    Workspace is ``{project}/.nanobot/workspace``.  The project root is
    two levels up.  Terminal scripts (``bot.py``) live at the project
    root, so we use this as ``cwd`` for subprocesses.
    """
    root = Path(workspace).parent.parent
    return str(root) if root.is_dir() else workspace


async def run_terminal_command(
    msg: InboundMessage,
    config: TerminalConfig,
    workspace: str,
    publish: Callable[[OutboundMessage], Awaitable[None]],
    cancel_event: asyncio.Event | None = None,
) -> OutboundMessage | None:
    """Execute a terminal command using the configured protocol.

    Routes to plain or rich mode based on ``config.protocol``.  In both
    modes, a JSON input envelope is written to the subprocess's stdin.
    The subprocess ``cwd`` is the project root (parent of ``.nanobot``),
    not the workspace directory.
    """
    if config.protocol == "rich":
        return await _execute_terminal_rich(
            msg, config, workspace, publish, cancel_event,
        )

    # Plain mode — delegate to the original implementation
    stdin_data = _build_input_envelope(msg, workspace, config)
    env = _build_env(config)
    return await execute_terminal_command(
        msg=msg,
        template=config.command,
        workspace=workspace,
        timeout=config.timeout,
        stdin_data=stdin_data,
        env=env,
        cancel_event=cancel_event,
    )
