from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from app.models import (
    BroadcastAction,
    BroadcastClaimResult,
    ConsumeResult,
    MilestoneReward,
    PaymentRecordResult,
    PromoActivationResult,
    QwenKeyRecord,
    ReferralPurchaseResult,
    SearchMode,
    UserRecord,
    WarningResult,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_expired(value: str | None, *, now: datetime | None = None) -> bool:
    parsed = _parse_iso(value)
    return parsed is not None and parsed <= (now or utc_now())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        try:
            yield db
        finally:
            await db.close()

    @staticmethod
    async def _fetchone(
        db: aiosqlite.Connection,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    async def _fetchall(
        db: aiosqlite.Connection,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[aiosqlite.Row]:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    @staticmethod
    async def _ensure_column(
        db: aiosqlite.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        rows = await Database._fetchall(db, f"PRAGMA table_info({table})")
        if column not in {str(row["name"]) for row in rows}:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def init(self) -> None:
        async with self.connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    basic_credits INTEGER NOT NULL DEFAULT 0 CHECK (basic_credits >= 0),
                    extended_credits INTEGER NOT NULL DEFAULT 0 CHECK (extended_credits >= 0),
                    trial_granted INTEGER NOT NULL DEFAULT 0,
                    penalty_prices INTEGER NOT NULL DEFAULT 0,
                    unsubscribe_warned INTEGER NOT NULL DEFAULT 0,
                    warnings INTEGER NOT NULL DEFAULT 0 CHECK (warnings >= 0),
                    blocked INTEGER NOT NULL DEFAULT 0,
                    restricted INTEGER NOT NULL DEFAULT 0,
                    preferred_mode TEXT NOT NULL DEFAULT 'basic' CHECK (preferred_mode IN ('basic', 'extended')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(lower(username));
                CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);

                CREATE TABLE IF NOT EXISTS warning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_class TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_searches_user_created ON searches(user_id, created_at);

                CREATE TABLE IF NOT EXISTS payments (
                    telegram_charge_id TEXT PRIMARY KEY,
                    provider_charge_id TEXT,
                    user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at);

                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    mode TEXT NOT NULL CHECK (mode IN ('basic', 'extended')),
                    credits INTEGER NOT NULL CHECK (credits > 0),
                    max_activations INTEGER NOT NULL CHECK (max_activations > 0),
                    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_activations (
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY (code, user_id),
                    FOREIGN KEY (code) REFERENCES promo_codes(code) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_promo_activations_user_created
                    ON promo_activations(user_id, activated_at);

                CREATE TABLE IF NOT EXISTS discount_promo_codes (
                    code TEXT PRIMARY KEY,
                    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 90),
                    max_activations INTEGER NOT NULL CHECK (max_activations > 0),
                    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS discount_promo_activations (
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY (code, user_id),
                    FOREIGN KEY (code) REFERENCES discount_promo_codes(code) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_discount_activations_user_created
                    ON discount_promo_activations(user_id, activated_at);

                CREATE TABLE IF NOT EXISTS user_discounts (
                    user_id INTEGER PRIMARY KEY,
                    percent INTEGER NOT NULL CHECK (percent BETWEEN 1 AND 90),
                    expires_at TEXT,
                    source_code TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS moderation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    sexual_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT,
                    resolved_by INTEGER,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL,
                    text_html TEXT NOT NULL,
                    action_type TEXT NOT NULL CHECK (
                        action_type IN ('channel', 'search', 'payment', 'promo', 'click')
                    ),
                    reward_mode TEXT NOT NULL CHECK (reward_mode IN ('basic', 'extended')),
                    reward_credits INTEGER NOT NULL CHECK (reward_credits > 0),
                    status TEXT NOT NULL DEFAULT 'sending',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                    broadcast_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    delivered INTEGER NOT NULL,
                    error TEXT,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (broadcast_id, user_id),
                    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS broadcast_rewards (
                    broadcast_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    rewarded_at TEXT NOT NULL,
                    PRIMARY KEY (broadcast_id, user_id),
                    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL CHECK (source IN ('telegram', 'referral_balance')),
                    source_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    package_key TEXT NOT NULL,
                    stars_value INTEGER NOT NULL CHECK (stars_value >= 0),
                    mode TEXT NOT NULL CHECK (mode IN ('basic', 'extended')),
                    credits INTEGER NOT NULL CHECK (credits > 0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);

                CREATE TABLE IF NOT EXISTS referrals (
                    invitee_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (invitee_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    CHECK (invitee_id != referrer_id)
                );
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

                CREATE TABLE IF NOT EXISTS referral_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_charge_id TEXT NOT NULL UNIQUE,
                    referrer_id INTEGER NOT NULL,
                    invitee_id INTEGER NOT NULL,
                    payment_stars INTEGER NOT NULL CHECK (payment_stars > 0),
                    reward_millistars INTEGER NOT NULL CHECK (reward_millistars > 0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (invitee_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS milestone_events (
                    threshold INTEGER PRIMARY KEY,
                    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 25),
                    users_count INTEGER NOT NULL,
                    triggered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS milestone_rewards (
                    threshold INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 25),
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    PRIMARY KEY (threshold, user_id),
                    FOREIGN KEY (threshold) REFERENCES milestone_events(threshold) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_milestone_rewards_user
                    ON milestone_rewards(user_id, used_at, discount_percent);

                CREATE TABLE IF NOT EXISTS channel_post_broadcasts (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY (chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS qwen_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (
                        status IN ('active', 'exhausted', 'invalid', 'disabled')
                    ),
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    exhausted_at TEXT,
                    last_used_at TEXT,
                    cooldown_until TEXT,
                    usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
                    added_by INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_qwen_keys_status
                    ON qwen_api_keys(status, cooldown_until, last_used_at);
                """
            )
            await self._ensure_column(
                db,
                "users",
                "referral_balance_millistars",
                "INTEGER NOT NULL DEFAULT 0 CHECK (referral_balance_millistars >= 0)",
            )
            # Старые скидки v2 могли быть выше нового общего лимита 25%.
            await db.execute(
                "UPDATE discount_promo_codes SET discount_percent = 25 WHERE discount_percent > 25"
            )
            await db.execute(
                "UPDATE user_discounts SET percent = 25 WHERE percent > 25"
            )
            # v3.1: объединяем старые расширенные запросы с единым балансом.
            # Обновление идемпотентно: после первого запуска extended_credits становится 0.
            await db.execute(
                """
                UPDATE users
                SET basic_credits = basic_credits + extended_credits,
                    extended_credits = 0,
                    preferred_mode = 'basic'
                WHERE extended_credits > 0 OR preferred_mode != 'basic'
                """
            )
            await db.execute("UPDATE promo_codes SET mode = 'basic' WHERE mode != 'basic'")
            await db.execute("UPDATE broadcasts SET reward_mode = 'basic' WHERE reward_mode != 'basic'")

            # Существующие Telegram-платежи становятся заказами для корректной миграции v2 -> v3.
            await db.execute(
                """
                INSERT OR IGNORE INTO orders(
                    source, source_id, user_id, package_key, stars_value, mode, credits, created_at
                )
                SELECT 'telegram', telegram_charge_id, user_id,
                       CASE
                           WHEN mode = 'basic' AND credits = 1 THEN 'b1'
                           WHEN mode = 'basic' AND credits = 10 THEN 'b10'
                           WHEN mode = 'basic' AND credits = 100 THEN 'b100'
                           WHEN mode = 'extended' AND credits = 1 THEN 'e1'
                           WHEN mode = 'extended' AND credits = 10 THEN 'e10'
                           WHEN mode = 'extended' AND credits = 100 THEN 'e100'
                           ELSE mode || ':' || credits
                       END,
                       stars, mode, credits, created_at
                FROM payments
                """
            )
            await db.commit()

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
    ) -> bool:
        """Создаёт/обновляет пользователя. Возвращает True только при первом создании."""
        now = utc_now_iso()
        username_normalized = username.lower() if username else None
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await self._fetchone(db, "SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            await db.execute(
                """
                INSERT INTO users(user_id, username, first_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username_normalized, first_name, now, now),
            )
            await db.commit()
            return exists is None

    async def get_user(self, user_id: int) -> UserRecord | None:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT * FROM users WHERE user_id = ?", (user_id,))
            if row is None:
                return None
            discount = await self._fetchone(
                db,
                "SELECT percent, expires_at FROM user_discounts WHERE user_id = ?",
                (user_id,),
            )
            discount_percent = 0
            discount_until: str | None = None
            if discount:
                if _is_expired(discount["expires_at"]):
                    await db.execute("DELETE FROM user_discounts WHERE user_id = ?", (user_id,))
                    await db.commit()
                else:
                    discount_percent = int(discount["percent"])
                    discount_until = discount["expires_at"]
        return self._row_to_user(row, discount_percent, discount_until)

    @staticmethod
    def _row_to_user(
        row: aiosqlite.Row,
        discount_percent: int = 0,
        discount_until: str | None = None,
    ) -> UserRecord:
        return UserRecord(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            basic_credits=row["basic_credits"],
            extended_credits=row["extended_credits"],
            trial_granted=bool(row["trial_granted"]),
            penalty_prices=bool(row["penalty_prices"]),
            unsubscribe_warned=bool(row["unsubscribe_warned"]),
            warnings=row["warnings"],
            blocked=bool(row["blocked"]),
            restricted=bool(row["restricted"]),
            preferred_mode=row["preferred_mode"],
            active_discount_percent=min(25, discount_percent),
            active_discount_until=discount_until,
            referral_balance_millistars=int(row["referral_balance_millistars"] or 0),
        )

    async def set_mode(self, user_id: int, mode: SearchMode) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET preferred_mode = ?, updated_at = ? WHERE user_id = ?",
                ("basic", utc_now_iso(), user_id),
            )
            await db.commit()

    async def grant_trial_if_eligible(self, user_id: int) -> bool:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetchone(db, "SELECT trial_granted FROM users WHERE user_id = ?", (user_id,))
            if not row or row["trial_granted"]:
                await db.rollback()
                return False
            await db.execute(
                """
                UPDATE users
                SET trial_granted = 1,
                    basic_credits = basic_credits + 1,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (utc_now_iso(), user_id),
            )
            await db.commit()
            return True

    async def consume_credit(self, user_id: int, mode: SearchMode) -> ConsumeResult:
        column = "basic_credits"
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetchone(
                db,
                f"SELECT {column} AS credits, blocked, restricted FROM users WHERE user_id = ?",
                (user_id,),
            )
            if not row:
                await db.rollback()
                return ConsumeResult(False, "user_missing", 0)
            if row["blocked"]:
                await db.rollback()
                return ConsumeResult(False, "blocked", row["credits"])
            if row["restricted"]:
                await db.rollback()
                return ConsumeResult(False, "restricted", row["credits"])
            if row["credits"] <= 0:
                await db.rollback()
                return ConsumeResult(False, "no_credits", 0)

            await db.execute(
                f"UPDATE users SET {column} = {column} - 1, updated_at = ? WHERE user_id = ?",
                (utc_now_iso(), user_id),
            )
            await db.commit()
            return ConsumeResult(True, "ok", row["credits"] - 1)

    async def refund_credit(self, user_id: int, mode: SearchMode, amount: int = 1) -> None:
        if amount <= 0:
            return
        column = "basic_credits"
        async with self.connect() as db:
            await db.execute(
                f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                (amount, utc_now_iso(), user_id),
            )
            await db.commit()

    async def add_credits(self, user_id: int, mode: SearchMode, amount: int) -> bool:
        if amount <= 0:
            return False
        column = "basic_credits"
        async with self.connect() as db:
            cursor = await db.execute(
                f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                (amount, utc_now_iso(), user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def add_warning(self, user_id: int, reason: str) -> WarningResult:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetchone(db, "SELECT warnings FROM users WHERE user_id = ?", (user_id,))
            if not row:
                await db.rollback()
                return WarningResult(0, False, False)
            warnings = row["warnings"] + 1
            blocked = warnings >= 3
            await db.execute(
                "UPDATE users SET warnings = ?, blocked = ?, restricted = 0, updated_at = ? WHERE user_id = ?",
                (warnings, int(blocked), now, user_id),
            )
            await db.execute(
                "INSERT INTO warning_events(user_id, reason, created_at) VALUES (?, ?, ?)",
                (user_id, reason, now),
            )
            await db.commit()
            return WarningResult(warnings, blocked, True)

    async def apply_unsubscribe_penalty(self, user_id: int) -> WarningResult:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetchone(
                db,
                "SELECT trial_granted, unsubscribe_warned, warnings FROM users WHERE user_id = ?",
                (user_id,),
            )
            if not row or not row["trial_granted"] or row["unsubscribe_warned"]:
                await db.rollback()
                return WarningResult(row["warnings"] if row else 0, False, False)

            warnings = row["warnings"] + 1
            blocked = warnings >= 3
            await db.execute(
                """
                UPDATE users
                SET penalty_prices = 1,
                    unsubscribe_warned = 1,
                    warnings = ?,
                    blocked = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (warnings, int(blocked), now, user_id),
            )
            await db.execute(
                "INSERT INTO warning_events(user_id, reason, created_at) VALUES (?, ?, ?)",
                (user_id, "Отписка от обязательного канала после получения пробного запроса", now),
            )
            await db.commit()
            return WarningResult(warnings, blocked, True)

    async def set_restricted(self, user_id: int, restricted: bool) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET restricted = ?, updated_at = ? WHERE user_id = ?",
                (int(restricted), utc_now_iso(), user_id),
            )
            await db.commit()

    async def block_user(self, user_id: int, reason: str) -> None:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE users SET blocked = 1, restricted = 0, updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            await db.execute(
                "INSERT INTO warning_events(user_id, reason, created_at) VALUES (?, ?, ?)",
                (user_id, reason, now),
            )
            await db.commit()

    async def unblock_user(self, user_id: int, reason: str) -> bool:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE users
                SET blocked = 0, restricted = 0, warnings = 0, updated_at = ?
                WHERE user_id = ?
                """,
                (now, user_id),
            )
            if cursor.rowcount <= 0:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO warning_events(user_id, reason, created_at) VALUES (?, ?, ?)",
                (user_id, reason, now),
            )
            await db.commit()
            return True

    async def find_user(self, value: str) -> UserRecord | None:
        value = value.strip()
        async with self.connect() as db:
            if value.lstrip("-").isdigit():
                row = await self._fetchone(db, "SELECT * FROM users WHERE user_id = ?", (int(value),))
            else:
                username = value.lstrip("@").lower()
                row = await self._fetchone(db, "SELECT * FROM users WHERE lower(username) = ?", (username,))
        if row is None:
            return None
        return await self.get_user(int(row["user_id"]))

    async def log_search(
        self,
        user_id: int,
        mode: SearchMode,
        status: str,
        content_class: str | None = None,
        title: str | None = None,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO searches(user_id, mode, status, content_class, title, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, "basic", status, content_class, title, utc_now_iso()),
            )
            await db.commit()

    async def record_payment(
        self,
        telegram_charge_id: str,
        provider_charge_id: str | None,
        user_id: int,
        payload: str,
        stars: int,
        mode: SearchMode,
        credits: int,
        package_key: str,
        milestone_threshold: int = 0,
    ) -> PaymentRecordResult:
        """Записывает оплату, заказ, запросы и реферальные 15% одной транзакцией."""
        column = "basic_credits"
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await self._fetchone(
                db,
                "SELECT 1 FROM payments WHERE telegram_charge_id = ?",
                (telegram_charge_id,),
            )
            if exists:
                await db.rollback()
                return PaymentRecordResult(False)
            await db.execute(
                """
                INSERT INTO payments(
                    telegram_charge_id, provider_charge_id, user_id, payload,
                    stars, mode, credits, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_charge_id, provider_charge_id, user_id, payload, stars, "basic", credits, now),
            )
            await db.execute(
                """
                INSERT INTO orders(source, source_id, user_id, package_key, stars_value, mode, credits, created_at)
                VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_charge_id, user_id, package_key, stars, "basic", credits, now),
            )
            await db.execute(
                f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                (credits, now, user_id),
            )
            if milestone_threshold > 0:
                await db.execute(
                    """
                    UPDATE milestone_rewards SET used_at = ?
                    WHERE threshold = ? AND user_id = ? AND used_at IS NULL
                    """,
                    (now, milestone_threshold, user_id),
                )

            referral = await self._fetchone(
                db,
                "SELECT referrer_id FROM referrals WHERE invitee_id = ?",
                (user_id,),
            )
            referrer_id: int | None = None
            reward_millistars = 0
            if referral is not None:
                referrer_id = int(referral["referrer_id"])
                # 15% точно: одна оплаченная звезда = 150 миллизвёзд комиссии.
                reward_millistars = stars * 150
                await db.execute(
                    """
                    INSERT INTO referral_rewards(
                        payment_charge_id, referrer_id, invitee_id, payment_stars,
                        reward_millistars, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (telegram_charge_id, referrer_id, user_id, stars, reward_millistars, now),
                )
                await db.execute(
                    """
                    UPDATE users
                    SET referral_balance_millistars = referral_balance_millistars + ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (reward_millistars, now, referrer_id),
                )
            await db.commit()
            return PaymentRecordResult(True, referrer_id, reward_millistars)

    async def promo_code_exists(self, code: str) -> bool:
        normalized = code.strip().upper()
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT 1 FROM promo_codes WHERE code = ?
                UNION ALL
                SELECT 1 FROM discount_promo_codes WHERE code = ?
                LIMIT 1
                """,
                (normalized, normalized),
            )
        return row is not None

    async def create_promo(
        self,
        code: str,
        mode: SearchMode,
        credits: int,
        max_activations: int,
        expires_at: str | None,
        created_by: int,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO promo_codes(
                    code, mode, credits, max_activations, expires_at, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (code, "basic", credits, max_activations, expires_at, created_by, utc_now_iso()),
            )
            await db.commit()

    async def create_discount_promo(
        self,
        code: str,
        discount_percent: int,
        max_activations: int,
        expires_at: str | None,
        created_by: int,
    ) -> None:
        if not 1 <= discount_percent <= 25:
            raise ValueError("discount_percent должен быть от 1 до 25")
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO discount_promo_codes(
                    code, discount_percent, max_activations, expires_at, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code, discount_percent, max_activations, expires_at, created_by, utc_now_iso()),
            )
            await db.commit()

    async def activate_promo(self, user_id: int, code: str) -> PromoActivationResult:
        normalized = code.strip().upper()
        now = utc_now()
        now_iso = now.isoformat()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")

            promo = await self._fetchone(db, "SELECT * FROM promo_codes WHERE code = ?", (normalized,))
            if promo is not None:
                if not promo["active"]:
                    await db.rollback()
                    return PromoActivationResult(False, "Промокод отключён.")
                if _is_expired(promo["expires_at"], now=now):
                    await db.rollback()
                    return PromoActivationResult(False, "Срок действия промокода истёк.")
                if promo["used_count"] >= promo["max_activations"]:
                    await db.rollback()
                    return PromoActivationResult(False, "Лимит активаций промокода исчерпан.")
                used = await self._fetchone(
                    db,
                    "SELECT 1 FROM promo_activations WHERE code = ? AND user_id = ?",
                    (normalized, user_id),
                )
                if used:
                    await db.rollback()
                    return PromoActivationResult(False, "Вы уже активировали этот промокод.")

                mode: SearchMode = "basic"
                column = "basic_credits"
                await db.execute(
                    "INSERT INTO promo_activations(code, user_id, activated_at) VALUES (?, ?, ?)",
                    (normalized, user_id, now_iso),
                )
                await db.execute(
                    "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?",
                    (normalized,),
                )
                await db.execute(
                    f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                    (promo["credits"], now_iso, user_id),
                )
                await db.commit()
                return PromoActivationResult(
                    True,
                    "Промокод успешно активирован.",
                    kind="credits",
                    mode=mode,
                    credits=promo["credits"],
                )

            promo = await self._fetchone(
                db,
                "SELECT * FROM discount_promo_codes WHERE code = ?",
                (normalized,),
            )
            if promo is None or not promo["active"]:
                await db.rollback()
                return PromoActivationResult(False, "Промокод не найден или отключён.")
            if _is_expired(promo["expires_at"], now=now):
                await db.rollback()
                return PromoActivationResult(False, "Срок действия промокода истёк.")
            if promo["used_count"] >= promo["max_activations"]:
                await db.rollback()
                return PromoActivationResult(False, "Лимит активаций промокода исчерпан.")
            used = await self._fetchone(
                db,
                "SELECT 1 FROM discount_promo_activations WHERE code = ? AND user_id = ?",
                (normalized, user_id),
            )
            if used:
                await db.rollback()
                return PromoActivationResult(False, "Вы уже активировали этот промокод.")

            new_percent = min(25, int(promo["discount_percent"]))
            new_until = promo["expires_at"]
            current = await self._fetchone(
                db,
                "SELECT percent, expires_at FROM user_discounts WHERE user_id = ?",
                (user_id,),
            )
            if current and not _is_expired(current["expires_at"], now=now):
                current_percent = min(25, int(current["percent"]))
                current_until = _parse_iso(current["expires_at"])
                new_until_dt = _parse_iso(new_until)
                current_is_better = current_percent > new_percent
                same_not_extended = (
                    current_percent == new_percent
                    and (
                        current_until is None
                        or (new_until_dt is not None and current_until >= new_until_dt)
                    )
                )
                if current_is_better or same_not_extended:
                    await db.rollback()
                    return PromoActivationResult(
                        False,
                        f"У вас уже действует скидка {current_percent}%, которая не хуже этого промокода.",
                    )

            await db.execute(
                "INSERT INTO discount_promo_activations(code, user_id, activated_at) VALUES (?, ?, ?)",
                (normalized, user_id, now_iso),
            )
            await db.execute(
                "UPDATE discount_promo_codes SET used_count = used_count + 1 WHERE code = ?",
                (normalized,),
            )
            await db.execute(
                """
                INSERT INTO user_discounts(user_id, percent, expires_at, source_code, activated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    percent = excluded.percent,
                    expires_at = excluded.expires_at,
                    source_code = excluded.source_code,
                    activated_at = excluded.activated_at
                """,
                (user_id, new_percent, new_until, normalized, now_iso),
            )
            await db.commit()
            return PromoActivationResult(
                True,
                "Промокод успешно активирован.",
                kind="discount",
                discount_percent=new_percent,
                discount_until=new_until,
            )

    async def create_moderation_case(self, user_id: int, photo_file_id: str, sexual_score: float) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO moderation_cases(user_id, photo_file_id, sexual_score, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, photo_file_id, sexual_score, utc_now_iso()),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def resolve_moderation_case(self, case_id: int, decision: str, admin_id: int) -> int | None:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._fetchone(
                db,
                "SELECT user_id, status FROM moderation_cases WHERE id = ?",
                (case_id,),
            )
            if not row or row["status"] != "pending":
                await db.rollback()
                return None
            await db.execute(
                """
                UPDATE moderation_cases
                SET status = 'resolved', decision = ?, resolved_by = ?, resolved_at = ?
                WHERE id = ?
                """,
                (decision, admin_id, now, case_id),
            )
            await db.commit()
            return int(row["user_id"])

    async def create_broadcast(
        self,
        created_by: int,
        text_html: str,
        action_type: BroadcastAction,
        reward_mode: SearchMode,
        reward_credits: int,
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO broadcasts(
                    created_by, text_html, action_type, reward_mode, reward_credits, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_by, text_html, action_type, "basic", reward_credits, utc_now_iso()),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_broadcast(self, broadcast_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,))
        return dict(row) if row else None

    async def get_broadcast_recipients(self) -> list[int]:
        async with self.connect() as db:
            rows = await self._fetchall(
                db,
                "SELECT user_id FROM users WHERE blocked = 0 ORDER BY user_id",
            )
        return [int(row["user_id"]) for row in rows]

    async def record_broadcast_deliveries(
        self,
        broadcast_id: int,
        deliveries: Iterable[tuple[int, bool, str | None]],
    ) -> None:
        rows = [
            (broadcast_id, user_id, int(delivered), error, utc_now_iso())
            for user_id, delivered, error in deliveries
        ]
        if not rows:
            return
        async with self.connect() as db:
            await db.executemany(
                """
                INSERT OR REPLACE INTO broadcast_deliveries(
                    broadcast_id, user_id, delivered, error, sent_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()

    async def finish_broadcast(self, broadcast_id: int, sent_count: int, failed_count: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE broadcasts
                SET status = 'sent', sent_count = ?, failed_count = ?, finished_at = ?
                WHERE id = ?
                """,
                (sent_count, failed_count, utc_now_iso(), broadcast_id),
            )
            await db.commit()

    async def claim_broadcast_reward(
        self,
        broadcast_id: int,
        user_id: int,
        *,
        external_action_verified: bool | None = None,
    ) -> BroadcastClaimResult:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            broadcast = await self._fetchone(
                db,
                "SELECT * FROM broadcasts WHERE id = ?",
                (broadcast_id,),
            )
            if broadcast is None or broadcast["status"] not in {"sending", "sent"}:
                await db.rollback()
                return BroadcastClaimResult(False, "not_found")

            user = await self._fetchone(
                db,
                "SELECT blocked FROM users WHERE user_id = ?",
                (user_id,),
            )
            if user is None:
                await db.rollback()
                return BroadcastClaimResult(False, "user_missing")
            if user["blocked"]:
                await db.rollback()
                return BroadcastClaimResult(False, "blocked")

            claimed = await self._fetchone(
                db,
                "SELECT 1 FROM broadcast_rewards WHERE broadcast_id = ? AND user_id = ?",
                (broadcast_id, user_id),
            )
            if claimed:
                await db.rollback()
                return BroadcastClaimResult(False, "already_claimed")

            action: BroadcastAction = broadcast["action_type"]
            completed = False
            if action == "click":
                completed = True
            elif action == "channel":
                completed = external_action_verified is True
            elif action == "search":
                completed = (
                    await self._fetchone(
                        db,
                        """
                        SELECT 1 FROM searches
                        WHERE user_id = ? AND status = 'success' AND created_at >= ?
                        LIMIT 1
                        """,
                        (user_id, broadcast["created_at"]),
                    )
                    is not None
                )
            elif action == "payment":
                completed = (
                    await self._fetchone(
                        db,
                        "SELECT 1 FROM payments WHERE user_id = ? AND created_at >= ? LIMIT 1",
                        (user_id, broadcast["created_at"]),
                    )
                    is not None
                )
            elif action == "promo":
                completed = (
                    await self._fetchone(
                        db,
                        """
                        SELECT 1 FROM promo_activations
                        WHERE user_id = ? AND activated_at >= ?
                        UNION ALL
                        SELECT 1 FROM discount_promo_activations
                        WHERE user_id = ? AND activated_at >= ?
                        LIMIT 1
                        """,
                        (user_id, broadcast["created_at"], user_id, broadcast["created_at"]),
                    )
                    is not None
                )

            if not completed:
                await db.rollback()
                return BroadcastClaimResult(False, "not_completed")

            mode: SearchMode = "basic"
            credits = int(broadcast["reward_credits"])
            column = "basic_credits"
            await db.execute(
                "INSERT INTO broadcast_rewards(broadcast_id, user_id, rewarded_at) VALUES (?, ?, ?)",
                (broadcast_id, user_id, now),
            )
            await db.execute(
                f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                (credits, now, user_id),
            )
            await db.commit()
            return BroadcastClaimResult(True, "ok", mode=mode, credits=credits)

    async def has_purchase(self, user_id: int) -> bool:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT 1 FROM orders WHERE user_id = ? LIMIT 1", (user_id,))
        return row is not None

    async def set_referrer(self, invitee_id: int, referrer_id: int) -> bool:
        if invitee_id == referrer_id:
            return False
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            users = await self._fetchone(
                db,
                "SELECT COUNT(*) AS count FROM users WHERE user_id IN (?, ?)",
                (invitee_id, referrer_id),
            )
            if not users or int(users["count"]) != 2:
                await db.rollback()
                return False
            purchased = await self._fetchone(db, "SELECT 1 FROM orders WHERE user_id = ? LIMIT 1", (invitee_id,))
            if purchased:
                await db.rollback()
                return False
            cursor = await db.execute(
                "INSERT OR IGNORE INTO referrals(invitee_id, referrer_id, created_at) VALUES (?, ?, ?)",
                (invitee_id, referrer_id, utc_now_iso()),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def referral_stats(self, referrer_id: int, limit: int = 20) -> dict[str, Any]:
        async with self.connect() as db:
            summary = await self._fetchone(
                db,
                """
                SELECT
                    (SELECT COUNT(*) FROM referrals WHERE referrer_id = ?) AS referrals_count,
                    (SELECT COALESCE(SUM(reward_millistars), 0)
                     FROM referral_rewards WHERE referrer_id = ?) AS earned_millistars
                """,
                (referrer_id, referrer_id),
            )
            rows = await self._fetchall(
                db,
                """
                SELECT u.user_id, u.username, u.first_name, r.created_at,
                       COALESCE(p.payments_count, 0) AS payments_count,
                       COALESCE(p.paid_stars, 0) AS paid_stars,
                       COALESCE(rr.reward_millistars, 0) AS reward_millistars
                FROM referrals r
                JOIN users u ON u.user_id = r.invitee_id
                LEFT JOIN (
                    SELECT user_id, COUNT(*) AS payments_count, SUM(stars) AS paid_stars
                    FROM payments GROUP BY user_id
                ) p ON p.user_id = r.invitee_id
                LEFT JOIN (
                    SELECT referrer_id, invitee_id, SUM(reward_millistars) AS reward_millistars
                    FROM referral_rewards GROUP BY referrer_id, invitee_id
                ) rr ON rr.invitee_id = r.invitee_id AND rr.referrer_id = r.referrer_id
                WHERE r.referrer_id = ?
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (referrer_id, limit),
            )
            user = await self._fetchone(
                db,
                "SELECT referral_balance_millistars FROM users WHERE user_id = ?",
                (referrer_id,),
            )
        return {
            "count": int(summary["referrals_count"] or 0) if summary else 0,
            "earned_millistars": int(summary["earned_millistars"] or 0) if summary else 0,
            "balance_millistars": int(user["referral_balance_millistars"] or 0) if user else 0,
            "items": [dict(row) for row in rows],
        }

    async def get_available_milestone_reward(self, user_id: int) -> MilestoneReward | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT threshold, discount_percent, created_at
                FROM milestone_rewards
                WHERE user_id = ? AND used_at IS NULL
                ORDER BY discount_percent DESC, threshold DESC
                LIMIT 1
                """,
                (user_id,),
            )
        if row is None:
            return None
        return MilestoneReward(
            threshold=int(row["threshold"]),
            discount_percent=int(row["discount_percent"]),
            created_at=str(row["created_at"]),
        )

    async def trigger_due_milestones(
        self,
        milestones: Iterable[tuple[int, int]],
    ) -> list[dict[str, Any]]:
        """Атомарно запускает ещё не запущенные акции и выдаёт их прошлым покупателям."""
        triggered: list[dict[str, Any]] = []
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            count_row = await self._fetchone(db, "SELECT COUNT(*) AS count FROM users")
            users_count = int(count_row["count"] or 0) if count_row else 0
            for threshold, percent in milestones:
                if users_count < threshold:
                    continue
                exists = await self._fetchone(
                    db,
                    "SELECT 1 FROM milestone_events WHERE threshold = ?",
                    (threshold,),
                )
                if exists:
                    continue
                now = utc_now_iso()
                await db.execute(
                    """
                    INSERT INTO milestone_events(threshold, discount_percent, users_count, triggered_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (threshold, percent, users_count, now),
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO milestone_rewards(
                        threshold, user_id, discount_percent, created_at
                    )
                    SELECT ?, u.user_id, ?, ?
                    FROM users u
                    WHERE u.blocked = 0
                      AND EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.user_id)
                    """,
                    (threshold, percent, now),
                )
                recipients = await self._fetchall(
                    db,
                    "SELECT user_id FROM milestone_rewards WHERE threshold = ?",
                    (threshold,),
                )
                triggered.append({
                    "threshold": threshold,
                    "percent": percent,
                    "users_count": users_count,
                    "recipients": [int(row["user_id"]) for row in recipients],
                })
            await db.commit()
        return triggered

    async def redeem_referral_package(
        self,
        user_id: int,
        package_key: str,
        mode: SearchMode,
        credits: int,
        stars: int,
        milestone_threshold: int = 0,
    ) -> ReferralPurchaseResult:
        if stars <= 0:
            return ReferralPurchaseResult(False, "invalid_price")
        required = stars * 1000
        now = utc_now_iso()
        source_id = f"ref-{user_id}-{int(utc_now().timestamp() * 1_000_000)}"
        column = "basic_credits"
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            user = await self._fetchone(
                db,
                "SELECT referral_balance_millistars, blocked, restricted FROM users WHERE user_id = ?",
                (user_id,),
            )
            if not user:
                await db.rollback()
                return ReferralPurchaseResult(False, "user_missing")
            if user["blocked"] or user["restricted"]:
                await db.rollback()
                return ReferralPurchaseResult(False, "blocked")
            balance = int(user["referral_balance_millistars"] or 0)
            if balance < required:
                await db.rollback()
                return ReferralPurchaseResult(False, "insufficient", balance)
            await db.execute(
                """
                UPDATE users
                SET referral_balance_millistars = referral_balance_millistars - ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (required, now, user_id),
            )
            await db.execute(
                f"UPDATE users SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
                (credits, now, user_id),
            )
            await db.execute(
                """
                INSERT INTO orders(source, source_id, user_id, package_key, stars_value, mode, credits, created_at)
                VALUES ('referral_balance', ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, user_id, package_key, stars, "basic", credits, now),
            )
            if milestone_threshold > 0:
                await db.execute(
                    """
                    UPDATE milestone_rewards SET used_at = ?
                    WHERE threshold = ? AND user_id = ? AND used_at IS NULL
                    """,
                    (now, milestone_threshold, user_id),
                )
            await db.commit()
            return ReferralPurchaseResult(True, "ok", balance - required)

    async def claim_channel_post(self, chat_id: int, message_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO channel_post_broadcasts(chat_id, message_id, created_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, message_id, utc_now_iso()),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def finish_channel_post(
        self,
        chat_id: int,
        message_id: int,
        sent_count: int,
        failed_count: int,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE channel_post_broadcasts
                SET sent_count = ?, failed_count = ?, finished_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (sent_count, failed_count, utc_now_iso(), chat_id, message_id),
            )
            await db.commit()

    async def stats(self) -> dict[str, int]:
        now = utc_now()
        day_ago = (now - timedelta(days=1)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()
        month_ago = (now - timedelta(days=30)).isoformat()
        async with self.connect() as db:
            users = await self._fetchone(
                db,
                """
                SELECT
                    COUNT(*) AS total_users,
                    SUM(CASE WHEN blocked = 0 AND restricted = 0 THEN 1 ELSE 0 END) AS active_users,
                    SUM(CASE WHEN blocked = 1 THEN 1 ELSE 0 END) AS blocked_users,
                    SUM(CASE WHEN restricted = 1 THEN 1 ELSE 0 END) AS restricted_users,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS new_day,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS new_week,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS new_month,
                    COALESCE(SUM(basic_credits), 0) AS basic_credits,
                    COALESCE(SUM(extended_credits), 0) AS extended_credits
                FROM users
                """,
                (day_ago, week_ago, month_ago),
            )
            payments = await self._fetchone(
                db,
                "SELECT COALESCE(SUM(stars), 0) AS stars, COUNT(*) AS count FROM payments",
            )
            searches = await self._fetchone(
                db,
                "SELECT COUNT(*) AS count FROM searches WHERE status IN ('success', 'not_anime', 'sexual')",
            )
        return {
            "total_users": int(users["total_users"] or 0),
            "active_users": int(users["active_users"] or 0),
            "blocked_users": int(users["blocked_users"] or 0),
            "restricted_users": int(users["restricted_users"] or 0),
            "new_day": int(users["new_day"] or 0),
            "new_week": int(users["new_week"] or 0),
            "new_month": int(users["new_month"] or 0),
            "basic_credits": int(users["basic_credits"] or 0),
            "extended_credits": int(users["extended_credits"] or 0),
            "stars_received_db": int(payments["stars"] or 0),
            "payments_count": int(payments["count"] or 0),
            "searches_count": int(searches["count"] or 0),
        }

    @staticmethod
    def _qwen_key_from_row(row: aiosqlite.Row) -> QwenKeyRecord:
        return QwenKeyRecord(
            id=int(row["id"]),
            api_key=str(row["api_key"]),
            label=str(row["label"]),
            base_url=str(row["base_url"]),
            status=str(row["status"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            exhausted_at=str(row["exhausted_at"]) if row["exhausted_at"] is not None else None,
            last_used_at=str(row["last_used_at"]) if row["last_used_at"] is not None else None,
            cooldown_until=str(row["cooldown_until"]) if row["cooldown_until"] is not None else None,
            usage_count=int(row["usage_count"] or 0),
        )

    async def seed_qwen_key(
        self,
        api_key: str,
        base_url: str,
        *,
        label: str = "Основной ключ из .env",
    ) -> int:
        """Добавляет стартовый ключ только при первом появлении.

        Уже исчерпанный ключ не активируется автоматически после перезапуска.
        """
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO qwen_api_keys(
                    api_key, label, base_url, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (api_key.strip(), label.strip() or "Ключ из .env", base_url.rstrip("/"), now, now),
            )
            row = await self._fetchone(
                db,
                "SELECT id FROM qwen_api_keys WHERE api_key = ?",
                (api_key.strip(),),
            )
            await db.commit()
            if row is None:
                raise RuntimeError("Не удалось сохранить стартовый ключ Qwen")
            return int(row["id"])

    async def add_qwen_key(
        self,
        api_key: str,
        base_url: str,
        label: str,
        added_by: int,
    ) -> tuple[QwenKeyRecord, bool]:
        key = api_key.strip()
        url = base_url.rstrip("/")
        clean_label = label.strip() or "Резервный ключ"
        now = utc_now_iso()
        async with self.connect() as db:
            existing = await self._fetchone(
                db,
                "SELECT * FROM qwen_api_keys WHERE api_key = ?",
                (key,),
            )
            created = existing is None
            if created:
                cursor = await db.execute(
                    """
                    INSERT INTO qwen_api_keys(
                        api_key, label, base_url, status, created_at, updated_at, added_by
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (key, clean_label, url, now, now, added_by),
                )
                key_id = int(cursor.lastrowid)
            else:
                key_id = int(existing["id"])
                await db.execute(
                    """
                    UPDATE qwen_api_keys
                    SET label = ?, base_url = ?, status = 'active', last_error = NULL,
                        exhausted_at = NULL, cooldown_until = NULL, updated_at = ?, added_by = ?
                    WHERE id = ?
                    """,
                    (clean_label, url, now, added_by, key_id),
                )
            row = await self._fetchone(db, "SELECT * FROM qwen_api_keys WHERE id = ?", (key_id,))
            await db.commit()
            if row is None:
                raise RuntimeError("Не удалось прочитать добавленный ключ Qwen")
            return self._qwen_key_from_row(row), created

    async def list_qwen_keys(self) -> list[QwenKeyRecord]:
        async with self.connect() as db:
            rows = await self._fetchall(
                db,
                """
                SELECT * FROM qwen_api_keys
                ORDER BY
                    CASE status
                        WHEN 'active' THEN 0
                        WHEN 'exhausted' THEN 1
                        WHEN 'invalid' THEN 2
                        ELSE 3
                    END,
                    id
                """,
            )
        return [self._qwen_key_from_row(row) for row in rows]

    async def get_qwen_key(self, key_id: int) -> QwenKeyRecord | None:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT * FROM qwen_api_keys WHERE id = ?", (key_id,))
        return self._qwen_key_from_row(row) if row is not None else None

    async def available_qwen_keys(self) -> list[QwenKeyRecord]:
        now = utc_now_iso()
        async with self.connect() as db:
            rows = await self._fetchall(
                db,
                """
                SELECT * FROM qwen_api_keys
                WHERE status = 'active'
                  AND (cooldown_until IS NULL OR cooldown_until <= ?)
                ORDER BY
                    CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END,
                    last_used_at, id
                """,
                (now,),
            )
        return [self._qwen_key_from_row(row) for row in rows]

    async def qwen_key_counts(self) -> dict[str, int]:
        async with self.connect() as db:
            rows = await self._fetchall(
                db,
                "SELECT status, COUNT(*) AS count FROM qwen_api_keys GROUP BY status",
            )
            cooling = await self._fetchone(
                db,
                """
                SELECT COUNT(*) AS count FROM qwen_api_keys
                WHERE status = 'active' AND cooldown_until > ?
                """,
                (utc_now_iso(),),
            )
        result = {"active": 0, "exhausted": 0, "invalid": 0, "disabled": 0, "cooling": 0}
        for row in rows:
            result[str(row["status"])] = int(row["count"] or 0)
        result["cooling"] = int(cooling["count"] or 0) if cooling is not None else 0
        return result

    async def mark_qwen_key_used(self, key_id: int) -> None:
        now = utc_now_iso()
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE qwen_api_keys
                SET last_used_at = ?, updated_at = ?, usage_count = usage_count + 1,
                    cooldown_until = NULL, last_error = NULL
                WHERE id = ? AND status = 'active'
                """,
                (now, now, key_id),
            )
            await db.commit()

    async def mark_qwen_key_exhausted(self, key_id: int, error: str) -> bool:
        now = utc_now_iso()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE qwen_api_keys
                SET status = 'exhausted', last_error = ?, exhausted_at = ?,
                    cooldown_until = NULL, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (error[:1000], now, now, key_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_qwen_key_invalid(self, key_id: int, error: str) -> bool:
        now = utc_now_iso()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE qwen_api_keys
                SET status = 'invalid', last_error = ?, cooldown_until = NULL, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (error[:1000], now, key_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def cooldown_qwen_key(self, key_id: int, seconds: int, error: str) -> None:
        until = (utc_now() + timedelta(seconds=max(10, seconds))).isoformat()
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE qwen_api_keys
                SET cooldown_until = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (until, error[:1000], utc_now_iso(), key_id),
            )
            await db.commit()

    async def set_qwen_key_enabled(self, key_id: int, enabled: bool) -> bool:
        now = utc_now_iso()
        status = "active" if enabled else "disabled"
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE qwen_api_keys
                SET status = ?, last_error = NULL, exhausted_at = NULL,
                    cooldown_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status, now, key_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_qwen_key(self, key_id: int) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "DELETE FROM qwen_api_keys WHERE id = ?",
                (key_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

