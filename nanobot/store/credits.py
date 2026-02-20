"""Credit storage backed by SQLite."""

import asyncio
from pathlib import Path

import aiosqlite
from loguru import logger

from nanobot.utils.helpers import ensure_dir


class CreditStore:
    """Async SQLite store for user credits and transactions."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = ensure_dir(Path.home() / ".nanobot" / "data") / "credits.db"
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open connection and create tables if needed."""
        ensure_dir(self._db_path.parent)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'telegram',
                credits INTEGER NOT NULL DEFAULT 0,
                stripe_customer_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, channel)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'telegram',
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                stripe_session_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_chat
                ON transactions(chat_id, channel);
            CREATE INDEX IF NOT EXISTS idx_transactions_stripe_session
                ON transactions(stripe_session_id);
        """)
        await self._db.commit()
        logger.info(f"Credit store initialized at {self._db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def get_or_create_user(
        self, chat_id: str, channel: str = "telegram", free_credits: int = 0,
    ) -> tuple[int, bool]:
        """Get user credits, creating with free_credits grant if new.

        Returns:
            Tuple of (credits, is_new_user).
        """
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT credits FROM users WHERE chat_id = ? AND channel = ?",
                (chat_id, channel),
            )
            row = await cursor.fetchone()
            if row is not None:
                return row[0], False

            # New user — grant free credits
            await self._db.execute(
                "INSERT INTO users (chat_id, channel, credits) VALUES (?, ?, ?)",
                (chat_id, channel, free_credits),
            )
            if free_credits > 0:
                await self._db.execute(
                    "INSERT INTO transactions (chat_id, channel, type, amount) "
                    "VALUES (?, ?, 'free_grant', ?)",
                    (chat_id, channel, free_credits),
                )
            await self._db.commit()
            return free_credits, True

    async def get_credits(self, chat_id: str, channel: str = "telegram") -> int:
        """Get current credit balance. Returns 0 if user not found."""
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT credits FROM users WHERE chat_id = ? AND channel = ?",
                (chat_id, channel),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def deduct_credit(self, chat_id: str, channel: str = "telegram") -> bool:
        """Deduct 1 credit. Returns False if insufficient balance."""
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT credits FROM users WHERE chat_id = ? AND channel = ?",
                (chat_id, channel),
            )
            row = await cursor.fetchone()
            if not row or row[0] <= 0:
                return False

            await self._db.execute(
                "UPDATE users SET credits = credits - 1, updated_at = datetime('now') "
                "WHERE chat_id = ? AND channel = ?",
                (chat_id, channel),
            )
            await self._db.execute(
                "INSERT INTO transactions (chat_id, channel, type, amount) "
                "VALUES (?, ?, 'deduction', -1)",
                (chat_id, channel),
            )
            await self._db.commit()
            return True

    async def add_credits(
        self,
        chat_id: str,
        amount: int,
        channel: str = "telegram",
        stripe_session_id: str | None = None,
    ) -> int:
        """Add credits (from purchase). Returns new balance."""
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT credits FROM users WHERE chat_id = ? AND channel = ?",
                (chat_id, channel),
            )
            row = await cursor.fetchone()
            if row is None:
                await self._db.execute(
                    "INSERT INTO users (chat_id, channel, credits) VALUES (?, ?, ?)",
                    (chat_id, channel, amount),
                )
                new_balance = amount
            else:
                await self._db.execute(
                    "UPDATE users SET credits = credits + ?, updated_at = datetime('now') "
                    "WHERE chat_id = ? AND channel = ?",
                    (amount, chat_id, channel),
                )
                new_balance = row[0] + amount

            await self._db.execute(
                "INSERT INTO transactions (chat_id, channel, type, amount, stripe_session_id) "
                "VALUES (?, ?, 'purchase', ?, ?)",
                (chat_id, channel, amount, stripe_session_id),
            )
            await self._db.commit()
            return new_balance

    async def has_processed_session(self, stripe_session_id: str) -> bool:
        """Check if a Stripe session has already been processed (idempotency)."""
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT 1 FROM transactions WHERE stripe_session_id = ? LIMIT 1",
                (stripe_session_id,),
            )
            return await cursor.fetchone() is not None
