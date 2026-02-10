"""Terminal mode: direct shell execution bypassing the LLM pipeline."""

import asyncio
import re
import shlex
from pathlib import Path

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage

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


async def execute_terminal_command(
    msg: InboundMessage,
    template: str,
    workspace: str,
    timeout: int = 120,
) -> OutboundMessage:
    """Execute a user message as a shell command via a template.

    The ``{message}`` placeholder in *template* is replaced with the
    shell-escaped user text.
    """
    user_text = msg.content.strip()
    command = template.replace("{message}", shlex.quote(user_text))

    preview = command[:120] + "..." if len(command) > 120 else command
    logger.info(f"Terminal exec [{msg.session_key}]: {preview}")

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Command timed out after {timeout}s",
            )
    except Exception as e:
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"Error: {e}",
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
