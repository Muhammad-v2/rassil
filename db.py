import asyncpg
import os
from typing import Optional, List

_pool: asyncpg.Pool | None = None


async def init_db():
    """Создаёт пул соединений и таблицы."""
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=10)

    async with _pool.acquire() as conn:
        # Админы
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # Аккаунты
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                session_blob BYTEA NOT NULL,
                phone TEXT,
                username TEXT,
                added_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # Кешированные диалоги
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats_cache (
                id SERIAL PRIMARY KEY,
                account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                chat_id BIGINT NOT NULL,
                chat_title TEXT,
                is_group BOOLEAN,
                is_bot BOOLEAN DEFAULT false,
                cached_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(account_id, chat_id)
            );
            """
        )
        # Отложенные/повторяющиеся сообщения
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id SERIAL PRIMARY KEY,
                account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                admin_id BIGINT NOT NULL,
                chat_ids BIGINT[] NOT NULL,
                message_text TEXT NOT NULL,
                message_photo BYTEA,
                repeat_interval_minutes INT,
                next_send_at TIMESTAMPTZ NOT NULL,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                last_sent_at TIMESTAMPTZ
            );
            """
        )
        # Логирование сессий (для уведомлений о новых устройствах)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_logs (
                id SERIAL PRIMARY KEY,
                account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                auth_hash BIGINT,
                device_model TEXT,
                platform TEXT,
                country TEXT,
                region TEXT,
                ip TEXT,
                first_seen_at TIMESTAMPTZ DEFAULT now(),
                last_seen_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # Уведомления о новых сессиях
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_alerts (
                id SERIAL PRIMARY KEY,
                account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                admin_id BIGINT NOT NULL,
                auth_hash BIGINT,
                device_model TEXT,
                country TEXT,
                alert_sent_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(account_id, auth_hash)
            );
            """
        )
    return _pool


def pool() -> asyncpg.Pool:
    assert _pool is not None, "DB не инициализирована"
    return _pool


# ========== АДМИНЫ ==========

async def is_admin(user_id: int, env_admin_ids: set[int]) -> bool:
    if user_id in env_admin_ids:
        return True
    row = await pool().fetchrow("SELECT 1 FROM admins WHERE id = $1", user_id)
    return row is not None


async def add_admin(user_id: int, added_by: int):
    await pool().execute(
        "INSERT INTO admins (id, added_by) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        user_id, added_by,
    )


async def remove_admin(user_id: int):
    await pool().execute("DELETE FROM admins WHERE id = $1", user_id)


async def list_admins() -> List[asyncpg.Record]:
    return await pool().fetch("SELECT id, added_at FROM admins ORDER BY added_at")


# ========== АККАУНТЫ ==========

async def add_account(owner_id: int, label: str, session_blob: bytes, phone: str | None = None, username: str | None = None) -> int:
    row = await pool().fetchrow(
        """
        INSERT INTO accounts (owner_id, label, session_blob, phone, username)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        owner_id, label, session_blob, phone, username,
    )
    return row["id"]


async def list_accounts(owner_id: int) -> List[asyncpg.Record]:
    return await pool().fetch(
        "SELECT id, label, phone, username, added_at FROM accounts WHERE owner_id = $1 ORDER BY added_at",
        owner_id,
    )


async def get_account(account_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM accounts WHERE id = $1", account_id)


async def delete_account(account_id: int):
    await pool().execute("DELETE FROM accounts WHERE id = $1", account_id)


async def update_account_info(account_id: int, username: str | None = None, phone: str | None = None):
    if username:
        await pool().execute("UPDATE accounts SET username = $1 WHERE id = $2", username, account_id)
    if phone:
        await pool().execute("UPDATE accounts SET phone = $1 WHERE id = $2", phone, account_id)


# ========== ДИАЛОГИ (КЕШИРОВАНИЕ) ==========

async def cache_chats(account_id: int, chats: list):
    """Сохраняет список диалогов аккаунта."""
    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM chats_cache WHERE account_id = $1", account_id)
        for chat in chats:
            await conn.execute(
                """
                INSERT INTO chats_cache (account_id, chat_id, chat_title, is_group, is_bot)
                VALUES ($1, $2, $3, $4, $5)
                """,
                account_id, chat["id"], chat["title"], chat["is_group"], chat.get("is_bot", False),
            )


async def get_chats(account_id: int) -> List[asyncpg.Record]:
    return await pool().fetch(
        "SELECT chat_id, chat_title, is_group FROM chats_cache WHERE account_id = $1 ORDER BY is_group, chat_title",
        account_id,
    )


# ========== ОТЛОЖЕННЫЕ/ПОВТОРЯЮЩИЕСЯ СООБЩЕНИЯ ==========

async def add_scheduled_message(
    account_id: int,
    admin_id: int,
    chat_ids: list,
    message_text: str,
    repeat_interval_minutes: int | None = None,
    message_photo: bytes | None = None,
    next_send_at = None,
) -> int:
    """Добавляет отложенное или повторяющееся сообщение."""
    row = await pool().fetchrow(
        """
        INSERT INTO scheduled_messages 
        (account_id, admin_id, chat_ids, message_text, message_photo, repeat_interval_minutes, next_send_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        account_id, admin_id, chat_ids, message_text, message_photo, repeat_interval_minutes, next_send_at,
    )
    return row["id"]


async def get_scheduled_messages(account_id: int) -> List[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT id, chat_ids, message_text, repeat_interval_minutes, next_send_at, is_active, last_sent_at
        FROM scheduled_messages 
        WHERE account_id = $1 AND is_active = true
        ORDER BY next_send_at
        """,
        account_id,
    )


async def get_pending_messages() -> List[asyncpg.Record]:
    """Получить все сообщения, которые нужно отправить сейчас."""
    return await pool().fetch(
        """
        SELECT id, account_id, chat_ids, message_text, message_photo, repeat_interval_minutes
        FROM scheduled_messages 
        WHERE is_active = true AND next_send_at <= now()
        """
    )


async def update_message_sent(message_id: int, repeat_interval_minutes: int | None):
    """Обновляет время последней отправки и время следующей (если есть повтор)."""
    if repeat_interval_minutes:
        await pool().execute(
            """
            UPDATE scheduled_messages 
            SET last_sent_at = now(), 
                next_send_at = now() + make_interval(mins := $1)
            WHERE id = $2
            """,
            repeat_interval_minutes, message_id,
        )
    else:
        await pool().execute(
            """
            UPDATE scheduled_messages 
            SET last_sent_at = now(), is_active = false
            WHERE id = $1
            """,
            message_id,
        )


async def cancel_scheduled_message(message_id: int):
    await pool().execute("UPDATE scheduled_messages SET is_active = false WHERE id = $1", message_id)


async def delete_scheduled_message(message_id: int):
    await pool().execute("DELETE FROM scheduled_messages WHERE id = $1", message_id)


# ========== ЛОГИРОВАНИЕ СЕССИЙ ==========

async def log_session(account_id: int, auth_hash: int, session_info: dict):
    """Логирует активную сессию."""
    existing = await pool().fetchrow(
        "SELECT id FROM session_logs WHERE account_id = $1 AND auth_hash = $2",
        account_id, auth_hash,
    )
    if existing:
        await pool().execute(
            "UPDATE session_logs SET last_seen_at = now() WHERE id = $1",
            existing["id"],
        )
    else:
        await pool().execute(
            """
            INSERT INTO session_logs (account_id, auth_hash, device_model, platform, country, region, ip)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            account_id, auth_hash, 
            session_info.get("device_model"), 
            session_info.get("platform"),
            session_info.get("country"), 
            session_info.get("region"), 
            session_info.get("ip"),
        )


async def get_new_sessions(account_id: int) -> List[asyncpg.Record]:
    """Получить новые сессии (которых ещё не логировали как алерт)."""
    return await pool().fetch(
        """
        SELECT sl.auth_hash, sl.device_model, sl.country, sl.region, sl.ip, sl.first_seen_at
        FROM session_logs sl
        LEFT JOIN session_alerts sa ON sl.auth_hash = sa.auth_hash AND sl.account_id = sa.account_id
        WHERE sl.account_id = $1 AND sa.id IS NULL
        """,
        account_id,
    )


async def mark_alert_sent(account_id: int, admin_id: int, auth_hash: int):
    """Отмечает, что уведомление об этой сессии отправлено."""
    await pool().execute(
        """
        INSERT INTO session_alerts (account_id, admin_id, auth_hash)
        VALUES ($1, $2, $3)
        ON CONFLICT (account_id, auth_hash) DO NOTHING
        """,
        account_id, admin_id, auth_hash,
    )
