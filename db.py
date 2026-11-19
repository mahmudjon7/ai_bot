"""SQLite saqlash qatlami (aiosqlite)."""
from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id        INTEGER PRIMARY KEY,
    title          TEXT,
    required       INTEGER NOT NULL DEFAULT 0,   -- 0 = o'chiq
    delete_service INTEGER NOT NULL DEFAULT 1,   -- "kim kimni qo'shdi" xabarlarini o'chirish
    notify         INTEGER NOT NULL DEFAULT 1    -- har bir qo'shishda qisqa progress xabari
);

-- invitee har bir guruhda faqat bitta marta hisoblanadi (birinchi qo'shgan odamga)
CREATE TABLE IF NOT EXISTS invites (
    chat_id    INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    invitee_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, invitee_id)
);
CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites (chat_id, inviter_id);

CREATE TABLE IF NOT EXISTS exempt (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);

-- taqiqlangan so'zlar (kichik harfda)
CREATE TABLE IF NOT EXISTS badwords (
    chat_id INTEGER NOT NULL,
    word    TEXT NOT NULL,
    PRIMARY KEY (chat_id, word)
);

-- cheklangan a'zolarning adminga murojaatlari
CREATE TABLE IF NOT EXISTS appeals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    user_name    TEXT NOT NULL,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / exempted / rejected
    created_at   INTEGER NOT NULL,
    group_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_appeals_user ON appeals (chat_id, user_id);

-- qoidabuzarlik ogohlantirishlari
CREATE TABLE IF NOT EXISTS strikes (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""

# Eski bazalarni buzmaslik uchun: ustunlar keyin qo'shiladi
MIGRATIONS = [
    "ALTER TABLE chats ADD COLUMN block_links   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chats ADD COLUMN block_forward INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chats ADD COLUMN flood_limit   INTEGER NOT NULL DEFAULT 0",  # 10 soniyada nechta xabar (0 = o'chiq)
    "ALTER TABLE chats ADD COLUMN strike_limit  INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE chats ADD COLUMN mute_minutes  INTEGER NOT NULL DEFAULT 60",
    "ALTER TABLE chats ADD COLUMN enabled       INTEGER NOT NULL DEFAULT 1",  # 0 = super admin xizmatni to'xtatgan
    "ALTER TABLE chats ADD COLUMN block_media   INTEGER NOT NULL DEFAULT 0",  # taqiqlangan media turlari (bitmask)
    "ALTER TABLE chats ADD COLUMN active        INTEGER NOT NULL DEFAULT 1",  # 0 = bot guruhda yo'q
    "ALTER TABLE chats ADD COLUMN joined_at     INTEGER NOT NULL DEFAULT 0",
]

SETTING_COLS = {
    "required", "delete_service", "notify",
    "block_links", "block_forward", "flood_limit", "strike_limit", "mute_minutes",
    "enabled", "block_media", "active",
}


@dataclass
class ChatSettings:
    chat_id: int
    required: int
    delete_service: bool
    notify: bool
    block_links: bool
    block_forward: bool
    flood_limit: int
    strike_limit: int
    mute_minutes: int
    enabled: bool = True
    block_media: int = 0


class DB:
    def __init__(self, path: str, default_required: int = 0):
        self.path = path
        self.default_required = default_required
        self.conn: aiosqlite.Connection | None = None
        self._words: dict[int, list[str]] = {}
        self._disabled: set[int] = set()  # xizmati o'chirilgan guruhlar (xotirada, tez tekshirish uchun)

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                await self.conn.execute(stmt)
            except aiosqlite.OperationalError:
                pass  # ustun allaqachon bor
        # hal qilingan eski murojaatlar 30 kundan keyin o'chadi (baza to'lmasligi uchun)
        await self.conn.execute(
            "DELETE FROM appeals WHERE status != 'pending' AND created_at < ?",
            (int(time.time()) - 30 * 86400,),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT chat_id FROM chats WHERE enabled=0") as cur:
            self._disabled = {r[0] async for r in cur}

    def is_disabled(self, chat_id: int) -> bool:
        return chat_id in self._disabled

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---- guruh sozlamalari -------------------------------------------------
    async def get_chat(self, chat_id: int, title: str | None = None) -> ChatSettings:
        assert self.conn
        await self.conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title, required, joined_at) VALUES (?, ?, ?, ?)",
            (chat_id, title, self.default_required, int(time.time())),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT required, delete_service, notify, block_links, block_forward, "
            "flood_limit, strike_limit, mute_minutes, enabled, block_media "
            "FROM chats WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            req, ds, nt, bl, bf, fl, sl, mm, en, bm = await cur.fetchone()
        return ChatSettings(chat_id, req, bool(ds), bool(nt), bool(bl), bool(bf), fl, sl, mm, bool(en), bm)

    async def set_required(self, chat_id: int, n: int) -> None:
        await self._set(chat_id, "required", n)

    async def set_delete_service(self, chat_id: int, on: bool) -> None:
        await self._set(chat_id, "delete_service", int(on))

    async def set_notify(self, chat_id: int, on: bool) -> None:
        await self._set(chat_id, "notify", int(on))

    async def set_value(self, chat_id: int, col: str, value: int) -> None:
        await self._set(chat_id, col, value)

    async def _set(self, chat_id: int, col: str, value: int) -> None:
        assert self.conn and col in SETTING_COLS
        await self.get_chat(chat_id)
        await self.conn.execute(f"UPDATE chats SET {col}=? WHERE chat_id=?", (value, chat_id))
        await self.conn.commit()
        if col == "enabled":
            (self._disabled.discard if value else self._disabled.add)(chat_id)

    # ---- qo'shishlar hisobi --------------------------------------------------
    async def add_invite(self, chat_id: int, inviter_id: int, invitee_id: int) -> bool:
        """True qaytaradi, agar yangi hisob qo'shilgan bo'lsa."""
        assert self.conn
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO invites (chat_id, inviter_id, invitee_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, inviter_id, invitee_id, int(time.time())),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def remove_invitee(self, chat_id: int, invitee_id: int) -> int | None:
        """Odam guruhdan chiqsa, uni qo'shgan kishining hisobidan ayiriladi.
        Qo'shgan odamning ID sini qaytaradi (bo'lmasa None)."""
        assert self.conn
        async with self.conn.execute(
            "SELECT inviter_id FROM invites WHERE chat_id=? AND invitee_id=?",
            (chat_id, invitee_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self.conn.execute(
            "DELETE FROM invites WHERE chat_id=? AND invitee_id=?", (chat_id, invitee_id)
        )
        await self.conn.commit()
        return row[0]

    async def count(self, chat_id: int, inviter_id: int) -> int:
        assert self.conn
        async with self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE chat_id=? AND inviter_id=?",
            (chat_id, inviter_id),
        ) as cur:
            return (await cur.fetchone())[0]

    async def top(self, chat_id: int, limit: int = 10) -> list[tuple[int, str, int]]:
        assert self.conn
        async with self.conn.execute(
            "SELECT i.inviter_id, COALESCE(u.name, CAST(i.inviter_id AS TEXT)), COUNT(*) c "
            "FROM invites i LEFT JOIN users u ON u.user_id = i.inviter_id "
            "WHERE i.chat_id=? GROUP BY i.inviter_id ORDER BY c DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            return [(r[0], r[1], r[2]) async for r in cur]

    # ---- ozod qilinganlar -------------------------------------------------------
    async def is_exempt(self, chat_id: int, user_id: int) -> bool:
        assert self.conn
        async with self.conn.execute(
            "SELECT 1 FROM exempt WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    async def set_exempt(self, chat_id: int, user_id: int, on: bool) -> None:
        assert self.conn
        if on:
            await self.conn.execute(
                "INSERT OR IGNORE INTO exempt (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id)
            )
        else:
            await self.conn.execute(
                "DELETE FROM exempt WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            )
        await self.conn.commit()

    # ---- ismlar -------------------------------------------------------------------
    async def upsert_user(self, user_id: int, name: str) -> None:
        assert self.conn
        await self.conn.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
            (user_id, name),
        )
        await self.conn.commit()

    # ---- taqiqlangan so'zlar -----------------------------------------------------
    async def words(self, chat_id: int) -> list[str]:
        assert self.conn
        if chat_id not in self._words:
            async with self.conn.execute(
                "SELECT word FROM badwords WHERE chat_id=?", (chat_id,)
            ) as cur:
                self._words[chat_id] = [r[0] async for r in cur]
        return self._words[chat_id]

    async def add_word(self, chat_id: int, word: str) -> bool:
        assert self.conn
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (chat_id, word)
        )
        await self.conn.commit()
        self._words.pop(chat_id, None)
        return cur.rowcount == 1

    async def remove_word(self, chat_id: int, word: str) -> bool:
        assert self.conn
        cur = await self.conn.execute(
            "DELETE FROM badwords WHERE chat_id=? AND word=?", (chat_id, word)
        )
        await self.conn.commit()
        self._words.pop(chat_id, None)
        return cur.rowcount == 1

    # ---- ogohlantirishlar (strike) --------------------------------------------------
    async def add_strike(self, chat_id: int, user_id: int, window: int = 86400) -> int:
        """Ogohlantirish qo'shadi. Oxirgisidan `window` soniya o'tgan bo'lsa, sanoq yangidan boshlanadi."""
        assert self.conn
        now = int(time.time())
        async with self.conn.execute(
            "SELECT count, updated_at FROM strikes WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        n = 1 if not row or now - row[1] > window else row[0] + 1
        await self.conn.execute(
            "INSERT INTO strikes (chat_id, user_id, count, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count, updated_at=excluded.updated_at",
            (chat_id, user_id, n, now),
        )
        await self.conn.commit()
        return n

    async def reset_strikes(self, chat_id: int, user_id: int) -> None:
        assert self.conn
        await self.conn.execute(
            "DELETE FROM strikes WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        await self.conn.commit()

    # ---- murojaatlar ------------------------------------------------------------------
    async def last_appeal(self, chat_id: int, user_id: int) -> tuple[int, str, int] | None:
        """Oxirgi murojaat: (id, status, created_at)."""
        assert self.conn
        async with self.conn.execute(
            "SELECT id, status, created_at FROM appeals WHERE chat_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, user_id),
        ) as cur:
            return await cur.fetchone()

    async def create_appeal(self, chat_id: int, user_id: int, name: str, reason: str | None) -> int:
        assert self.conn
        cur = await self.conn.execute(
            "INSERT INTO appeals (chat_id, user_id, user_name, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, name, reason, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_appeal(self, appeal_id: int) -> dict | None:
        assert self.conn
        async with self.conn.execute(
            "SELECT id, chat_id, user_id, user_name, reason, status, group_msg_id "
            "FROM appeals WHERE id=?",
            (appeal_id,),
        ) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        keys = ("id", "chat_id", "user_id", "user_name", "reason", "status", "group_msg_id")
        return dict(zip(keys, r))

    async def set_appeal_status(self, appeal_id: int, status: str) -> None:
        assert self.conn
        await self.conn.execute("UPDATE appeals SET status=? WHERE id=?", (status, appeal_id))
        await self.conn.commit()

    async def set_appeal_msg(self, appeal_id: int, msg_id: int) -> None:
        assert self.conn
        await self.conn.execute("UPDATE appeals SET group_msg_id=? WHERE id=?", (msg_id, appeal_id))
        await self.conn.commit()

    # ---- super admin: guruhlar, statistika --------------------------------------------
    async def set_title(self, chat_id: int, title: str | None) -> None:
        assert self.conn
        await self.conn.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
        await self.conn.commit()

    async def set_active(self, chat_id: int, active: bool) -> None:
        await self._set(chat_id, "active", int(active))

    async def count_chats(self) -> int:
        assert self.conn
        async with self.conn.execute("SELECT COUNT(*) FROM chats WHERE active=1") as cur:
            return (await cur.fetchone())[0]

    async def list_chats(self, offset: int = 0, limit: int = 8) -> list[dict]:
        """Bot hozir bor guruhlar (yangi qo'shilganlari birinchi)."""
        assert self.conn
        async with self.conn.execute(
            "SELECT chat_id, title, required, enabled, joined_at FROM chats WHERE active=1 "
            "ORDER BY joined_at DESC, chat_id LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return [
                {"chat_id": r[0], "title": r[1] or str(r[0]), "required": r[2],
                 "enabled": bool(r[3]), "joined_at": r[4]}
                async for r in cur
            ]

    async def chat_info(self, chat_id: int) -> dict | None:
        assert self.conn
        async with self.conn.execute(
            "SELECT title, required, enabled, active, joined_at, block_links, block_forward, "
            "block_media, flood_limit FROM chats WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        async with self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE chat_id=?", (chat_id,)
        ) as cur:
            invites = (await cur.fetchone())[0]
        keys = ("title", "required", "enabled", "active", "joined_at", "block_links",
                "block_forward", "block_media", "flood_limit")
        d = dict(zip(keys, r))
        d.update(chat_id=chat_id, invites=invites, title=d["title"] or str(chat_id))
        return d

    async def active_chat_ids(self, only_enabled: bool = True) -> list[int]:
        assert self.conn
        q = "SELECT chat_id FROM chats WHERE active=1" + (" AND enabled=1" if only_enabled else "")
        async with self.conn.execute(q) as cur:
            return [r[0] async for r in cur]

    async def stats(self) -> dict:
        assert self.conn

        async def one(q: str) -> int:
            async with self.conn.execute(q) as cur:
                return (await cur.fetchone())[0]

        return {
            "groups": await one("SELECT COUNT(*) FROM chats WHERE active=1"),
            "disabled": await one("SELECT COUNT(*) FROM chats WHERE active=1 AND enabled=0"),
            "left": await one("SELECT COUNT(*) FROM chats WHERE active=0"),
            "invites": await one("SELECT COUNT(*) FROM invites"),
            "pending": await one("SELECT COUNT(*) FROM appeals WHERE status='pending'"),
            "users": await one("SELECT COUNT(*) FROM users"),
        }
