"""
Работа с SQLite-базой бота (aiosqlite, async).

Таблицы:
  users (telegram_id PK, twitch_nick, created_at)
  clips (id PK, user_id, twitch_nick, clip_url, created_at)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id        INTEGER PRIMARY KEY,
    twitch_nick        TEXT    NOT NULL,
    twitch_login       TEXT,
    twitch_access_token  TEXT,
    twitch_refresh_token TEXT,
    twitch_user_id     TEXT,
    twitch_token_expires_at TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    twitch_nick  TEXT    NOT NULL,
    clip_url     TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state        TEXT PRIMARY KEY,
    telegram_id  INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clips_user ON clips(user_id);
CREATE INDEX IF NOT EXISTS idx_clips_nick ON clips(twitch_nick);

CREATE TABLE IF NOT EXISTS user_settings (
    telegram_id    INTEGER PRIMARY KEY,
    clip_duration  INTEGER NOT NULL DEFAULT 60,
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);
"""


class Database:
    """
    Обёртка над aiosqlite с ленивой инициализацией соединения.

    Использование:
        db = Database(path)
        await db.connect()
        ...
        await db.close()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        """Открывает соединение и создаёт схему, если её ещё нет."""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate_users_table()
        await self._conn.commit()
        log.info("SQLite готова: %s", self._path)

    async def _migrate_users_table(self) -> None:
        """Добавляет новые колонки в таблицу users, если база была создана раньше."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(users)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        for column, col_type in (
            ("twitch_login", "TEXT"),
            ("twitch_access_token", "TEXT"),
            ("twitch_refresh_token", "TEXT"),
            ("twitch_user_id", "TEXT"),
            ("twitch_token_expires_at", "TEXT"),
        ):
            if column not in columns:
                await self._conn.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {col_type}"
                )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не инициализирована: вызовите connect()")
        return self._conn

    # ---------- users ----------

    async def upsert_user(self, telegram_id: int, twitch_nick: str) -> None:
        """Создаёт или обновляет связку telegram_id -> twitch_nick."""
        conn = self._require_conn()
        nick = twitch_nick.strip().lower()
        await conn.execute(
            """
            INSERT INTO users (telegram_id, twitch_nick)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET twitch_nick = excluded.twitch_nick
            """,
            (telegram_id, nick),
        )
        await conn.commit()

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT telegram_id, twitch_nick, twitch_login,
                   twitch_access_token, twitch_refresh_token, twitch_user_id,
                   twitch_token_expires_at, created_at
            FROM users WHERE telegram_id = ?
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_user(self, telegram_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await conn.commit()

    async def all_users(self) -> list[dict[str, Any]]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT telegram_id, twitch_nick, created_at FROM users"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def count_users(self) -> int:
        conn = self._require_conn()
        async with conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def users_with_clip_counts(self) -> list[dict[str, Any]]:
        """Все пользователи + число их клипов и дата последнего (для админки)."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT u.telegram_id, u.twitch_nick, u.created_at,
                   COUNT(c.id) AS clip_count,
                   MAX(c.created_at) AS last_clip_at
            FROM users u
            LEFT JOIN clips c ON c.user_id = u.telegram_id
            GROUP BY u.telegram_id
            ORDER BY clip_count DESC, u.created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def find_user_by_nick(self, twitch_nick: str) -> dict[str, Any] | None:
        """Ищет владельца Twitch-ника (нужно для маршрутизации клипов)."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT telegram_id, twitch_nick, twitch_login,
                   twitch_access_token, twitch_refresh_token, twitch_user_id,
                   twitch_token_expires_at
            FROM users WHERE twitch_nick = ?
            """,
            (twitch_nick.strip().lower(),),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ---------- user_settings ----------

    async def get_clip_duration(self, telegram_id: int) -> int:
        """Длительность клипа пользователя в секундах (по умолчанию 60)."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT clip_duration FROM user_settings WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return 60
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 60

    async def set_clip_duration(self, telegram_id: int, duration: int) -> int:
        """Сохраняет желаемую длительность клипа (10–60 сек). Возвращает записанное значение."""
        conn = self._require_conn()
        duration = max(10, min(60, int(duration)))
        await conn.execute(
            """
            INSERT INTO user_settings (telegram_id, clip_duration)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                clip_duration = excluded.clip_duration,
                updated_at = datetime('now')
            """,
            (telegram_id, duration),
        )
        await conn.commit()
        return duration

    # ---------- oauth_states ----------

    async def create_oauth_state(self, state: str, telegram_id: int) -> None:
        """Сохраняет state для OAuth-флоу (защита от CSRF)."""
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO oauth_states (state, telegram_id) VALUES (?, ?)",
            (state, telegram_id),
        )
        await conn.commit()

    async def consume_oauth_state(self, state: str) -> int | None:
        """Возвращает telegram_id по state и удаляет его (state одноразовый)."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT telegram_id FROM oauth_states WHERE state = ?", (state,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        await conn.commit()
        return int(row[0])

    async def update_user_twitch_auth(
        self,
        telegram_id: int,
        *,
        twitch_login: str,
        access_token: str,
        refresh_token: str = "",
        twitch_user_id: str = "",
        token_expires_at: str = "",
    ) -> None:
        """Записывает OAuth-данные пользователя после авторизации Twitch (UPSERT)."""
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO users (
                telegram_id, twitch_nick, twitch_login,
                twitch_access_token, twitch_refresh_token, twitch_user_id,
                twitch_token_expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                twitch_login = excluded.twitch_login,
                twitch_access_token = excluded.twitch_access_token,
                twitch_refresh_token = excluded.twitch_refresh_token,
                twitch_user_id = excluded.twitch_user_id,
                twitch_token_expires_at = excluded.twitch_token_expires_at,
                twitch_nick = excluded.twitch_nick
            """,
            (
                telegram_id,
                twitch_login.strip().lower(),
                twitch_login.strip().lower(),
                access_token.strip(),
                refresh_token.strip(),
                twitch_user_id.strip(),
                token_expires_at.strip(),
            ),
        )
        await conn.commit()

    async def update_user_tokens(
        self,
        telegram_id: int,
        *,
        access_token: str,
        refresh_token: str,
        token_expires_at: str = "",
    ) -> None:
        """Обновляет только OAuth-токены (после refresh)."""
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE users
            SET twitch_access_token = ?,
                twitch_refresh_token = ?,
                twitch_token_expires_at = ?
            WHERE telegram_id = ?
            """,
            (
                access_token.strip(),
                refresh_token.strip(),
                token_expires_at.strip(),
                telegram_id,
            ),
        )
        await conn.commit()

    # ---------- clips ----------

    async def add_clip(
        self,
        user_id: int,
        twitch_nick: str,
        clip_url: str,
    ) -> int:
        """Сохраняет информацию о созданном клипе и возвращает его id."""
        conn = self._require_conn()
        cur = await conn.execute(
            """
            INSERT INTO clips (user_id, twitch_nick, clip_url)
            VALUES (?, ?, ?)
            """,
            (user_id, twitch_nick.strip().lower(), clip_url),
        )
        await conn.commit()
        return int(cur.lastrowid)

    async def get_clip(self, clip_id: int) -> dict[str, Any] | None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT id, user_id, twitch_nick, clip_url, created_at FROM clips WHERE id = ?",
            (clip_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def user_clips(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, user_id, twitch_nick, clip_url, created_at
            FROM clips WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def count_clips(self, user_id: int) -> int:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM clips WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_clips_total(self) -> int:
        conn = self._require_conn()
        async with conn.execute("SELECT COUNT(*) FROM clips") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_clips_since(self, since: str) -> int:
        """Клипы, созданные после указанной даты (формат SQLite datetime)."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM clips WHERE created_at >= ?", (since,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def delete_old_clips(self, hours: int = 24) -> int:
        """Удаляет клипы старше указанного числа часов; возвращает количество удалённых."""
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM clips WHERE created_at < datetime('now', ?)",
            (f"-{hours} hours",),
        )
        await conn.commit()
        return int(cur.rowcount or 0)

    async def clips_per_channel(self, limit: int = 10) -> list[dict[str, Any]]:
        """Топ каналов по числу созданных клипов."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT twitch_nick, COUNT(*) AS clip_count,
                   MAX(created_at) AS last_clip_at
            FROM clips
            GROUP BY twitch_nick
            ORDER BY clip_count DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
