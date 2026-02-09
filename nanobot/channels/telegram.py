"""Telegram channel implementation using python-telegram-bot."""

import asyncio
import re
from pathlib import Path

from loguru import logger
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import TelegramConfig

# File extensions grouped by Telegram send method
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_AUDIO_EXTS = {".mp3", ".ogg", ".m4a", ".wav", ".flac"}
_GROUPABLE_EXTS = _PHOTO_EXTS | _VIDEO_EXTS  # can go into a media group

_TG_MAX_LENGTH = 4096
_SEND_RETRIES = 3


def _chunk_text(text: str, max_len: int = _TG_MAX_LENGTH) -> list[str]:
    """Split text into chunks that fit Telegram's message limit.

    Splits at paragraph boundaries, then line boundaries, then hard cuts.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Try paragraph boundary
        cut = text.rfind("\n\n", 0, max_len)
        if cut > max_len // 4:
            chunks.append(text[:cut])
            text = text[cut + 2:]
            continue

        # Try line boundary
        cut = text.rfind("\n", 0, max_len)
        if cut > max_len // 4:
            chunks.append(text[:cut])
            text = text[cut + 1:]
            continue

        # Hard cut
        chunks.append(text[:max_len])
        text = text[max_len:]

    return chunks


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""
    
    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.
    
    Simple and reliable - no webhook/public IP needed.
    """
    
    name = "telegram"
    
    def __init__(self, config: TelegramConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
    
    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        
        self._running = True
        
        # Build the application
        self._app = (
            Application.builder()
            .token(self.config.token)
            .build()
        )
        
        # Add /start command handler (must be before the general message handler)
        from telegram.ext import CommandHandler
        self._app.add_handler(CommandHandler("start", self._on_start))

        # Add message handler for text (including /commands), photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL,
                self._on_message
            )
        )
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        
        # Get bot info
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")
        
        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True  # Ignore old messages on startup
        )
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram, including media if provided.

        Multiple photos/videos are batched into a media group (album).
        Audio and documents are sent individually.
        """
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
            return

        # --- media ---
        if msg.media:
            groupable: list[Path] = []   # photos + videos → album
            others: list[Path] = []      # audio, docs → sent individually

            for p_str in msg.media:
                p = Path(p_str)
                if not p.exists():
                    logger.warning(f"Media file not found: {p}")
                    continue
                if p.suffix.lower() in _GROUPABLE_EXTS:
                    groupable.append(p)
                else:
                    others.append(p)

            # Send groupable items as album(s) (max 10 per group)
            for i in range(0, len(groupable), 10):
                batch = groupable[i : i + 10]
                if len(batch) == 1:
                    await self._send_single_media(chat_id, batch[0])
                else:
                    await self._send_media_group(chat_id, batch)

            # Send non-groupable items individually
            for p in others:
                await self._send_single_media(chat_id, p)

        # --- text ---
        if msg.content:
            await self._send_text(chat_id, msg.content)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_media_group(self, chat_id: int, files: list[Path]) -> None:
        """Send multiple photos/videos as a Telegram album."""
        media_items = []
        for p in files:
            suffix = p.suffix.lower()
            if suffix in _VIDEO_EXTS:
                media_items.append(InputMediaVideo(media=open(p, "rb"), supports_streaming=True))
            else:
                media_items.append(InputMediaPhoto(media=open(p, "rb")))
        try:
            await self._app.bot.send_media_group(chat_id=chat_id, media=media_items)
            logger.debug(f"Sent media group ({len(files)} items) to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send media group: {e}")
            # Fallback: send individually
            for p in files:
                await self._send_single_media(chat_id, p)
        finally:
            for item in media_items:
                if hasattr(item.media, "close"):
                    item.media.close()

    async def _send_single_media(self, chat_id: int, file_path: Path) -> None:
        """Send a single media file using the appropriate Telegram method."""
        suffix = file_path.suffix.lower()
        try:
            with open(file_path, "rb") as f:
                if suffix in _PHOTO_EXTS:
                    await self._app.bot.send_photo(chat_id=chat_id, photo=f)
                elif suffix in _VIDEO_EXTS:
                    await self._app.bot.send_video(
                        chat_id=chat_id, video=f, supports_streaming=True,
                    )
                elif suffix in _AUDIO_EXTS:
                    await self._app.bot.send_audio(chat_id=chat_id, audio=f)
                else:
                    await self._app.bot.send_document(chat_id=chat_id, document=f)
            logger.debug(f"Sent media: {file_path}")
        except Exception as e:
            logger.error(f"Failed to send media {file_path}: {e}")

    async def _send_text(self, chat_id: int, content: str) -> None:
        """Send text with chunking, HTML formatting, and retry."""
        chunks = _chunk_text(content)
        for chunk in chunks:
            if not await self._send_text_chunk(chat_id, chunk):
                try:
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text="[Message delivery failed. Please retry your request.]",
                    )
                except Exception:
                    pass
                return

    async def _send_text_chunk(self, chat_id: int, text: str) -> bool:
        """Send a single text chunk with HTML fallback. Returns True on success."""
        html = _markdown_to_telegram_html(text)
        if len(html) <= _TG_MAX_LENGTH:
            if await self._try_send(chat_id, html, parse_mode="HTML"):
                return True
        # HTML too long or parse error — fall back to plain text
        return await self._try_send(chat_id, text)

    async def _try_send(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> bool:
        """Send a message with retries for transient errors. Returns True on success."""
        for attempt in range(_SEND_RETRIES):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=parse_mode,
                )
                return True
            except RetryAfter as e:
                logger.warning(f"Rate limited, waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
            except (NetworkError, TimedOut) as e:
                if attempt < _SEND_RETRIES - 1:
                    wait = (attempt + 1) * 1.5
                    logger.warning(f"Network error (attempt {attempt + 1}/{_SEND_RETRIES}): {e}, retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Send failed after {_SEND_RETRIES} attempts: {e}")
                    return False
            except Exception as e:
                logger.error(f"Send failed: {e}")
                return False
        return False
    
    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!"
        )
    
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return
        
        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        
        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        
        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id
        
        # Build content from text and/or media
        content_parts = []
        media_paths = []
        
        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)
        
        # Handle media files
        media_file = None
        media_type = None
        
        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"
        
        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))
                
                # Save to workspace/media/
                from pathlib import Path
                media_dir = Path.home() / ".nanobot" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                
                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))
                
                media_paths.append(str(file_path))
                
                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    from nanobot.providers.transcription import GroqTranscriptionProvider
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")
                    
                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")
        
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")
        
        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"
            }
        )
    
    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
