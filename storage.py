"""
SQLite-хранилище МарИИи:
- clients: профиль клиента (цели по КБЖУ, аллергии, нелюбимое, особенности)
- dialog: история разговора (последние N реплик на клиента)
- facts: авто-собранные факты о клиенте (через Haiku)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    profile_json TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )""")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS dialog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    role TEXT,
                    text TEXT,
                    created_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialog_user ON dialog(user_id, id)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    text TEXT,
                    created_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id)"
            )
            await db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---------- Клиенты ----------

    async def get_client(self, user_id: str) -> dict:
        """Возвращает профиль клиента + факты. Если клиента нет — пустой шаблон."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name, profile_json FROM clients WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            facts = []
            async with db.execute(
                "SELECT id, text FROM facts WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user_id,),
            ) as cur:
                facts = [{"id": r[0], "text": r[1]} for r in await cur.fetchall()]

        if not row:
            return {"user_id": user_id, "name": None, "profile": {}, "facts": facts}

        name, profile_json = row
        try:
            profile = json.loads(profile_json) if profile_json else {}
        except json.JSONDecodeError:
            profile = {}
        return {"user_id": user_id, "name": name, "profile": profile, "facts": facts}

    async def upsert_client(self, user_id: str, name: str | None, profile: dict):
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT created_at FROM clients WHERE user_id = ?", (user_id,)
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute(
                    "UPDATE clients SET name = COALESCE(?, name), profile_json = ?, updated_at = ? WHERE user_id = ?",
                    (name, json.dumps(profile, ensure_ascii=False), now, user_id),
                )
            else:
                await db.execute(
                    "INSERT INTO clients (user_id, name, profile_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, json.dumps(profile, ensure_ascii=False), now, now),
                )
            await db.commit()

    # ---------- Диалог ----------

    async def add_dialog(self, user_id: str, role: str, text: str, keep_last: int = 40):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO dialog (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, text, self._now()),
            )
            # Чистим всё что старше keep_last реплик
            await db.execute(f"""
                DELETE FROM dialog WHERE id IN (
                    SELECT id FROM dialog WHERE user_id = ?
                    ORDER BY id DESC LIMIT -1 OFFSET {keep_last}
                )""", (user_id,))
            await db.commit()

    async def get_dialog(self, user_id: str, limit: int = 40) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT role, text FROM dialog WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return list(reversed([{"role": r[0], "content": r[1]} for r in rows]))

    async def clear_dialog(self, user_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM dialog WHERE user_id = ?", (user_id,))
            await db.commit()

    # ---------- Факты ----------

    async def add_facts(self, user_id: str, facts: list[str], max_keep: int = 100):
        if not facts:
            return
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            for f in facts:
                await db.execute(
                    "INSERT INTO facts (user_id, text, created_at) VALUES (?, ?, ?)",
                    (user_id, f, now),
                )
            # Лимит: не больше max_keep на клиента — удаляем самые старые
            await db.execute(f"""
                DELETE FROM facts WHERE id IN (
                    SELECT id FROM facts WHERE user_id = ?
                    ORDER BY id DESC LIMIT -1 OFFSET {max_keep}
                )""", (user_id,))
            await db.commit()

    async def clear_facts(self, user_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
            await db.commit()

    async def full_reset(self, user_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM dialog WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM clients WHERE user_id = ?", (user_id,))
            await db.commit()
