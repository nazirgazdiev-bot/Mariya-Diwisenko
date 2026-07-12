"""
SQLite-хранилище МарИИи:
- clients: профиль клиента (цели по КБЖУ, аллергии, нелюбимое, особенности)
- dialog: история разговора (последние N реплик на клиента)
- facts: авто-собранные факты о клиенте (через Haiku)
- subscriptions: статус подписки и состояние воронки продаж
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_UNSET = object()  # отличаем "не передали" от явного None в upsert_subscription


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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'trial',
                    tier TEXT,
                    paid_until TEXT,
                    funnel_step INTEGER NOT NULL DEFAULT 0,
                    funnel_next_at TEXT,
                    renewal_reminder_sent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_funnel ON subscriptions(funnel_next_at)"
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

    # ---------- Подписки / воронка ----------

    _SUB_FIELDS = (
        "user_id", "status", "tier", "paid_until",
        "funnel_step", "funnel_next_at", "renewal_reminder_sent",
        "created_at", "updated_at",
    )
    _SUB_SELECT = "SELECT " + ", ".join(_SUB_FIELDS) + " FROM subscriptions"

    @classmethod
    def _sub_row(cls, row) -> dict:
        return dict(zip(cls._SUB_FIELDS, row))

    async def get_subscription(self, user_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"{self._SUB_SELECT} WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        return self._sub_row(row) if row else None

    async def upsert_subscription(
        self,
        user_id: str,
        *,
        status=_UNSET,
        tier=_UNSET,
        paid_until=_UNSET,
        funnel_step=_UNSET,
        funnel_next_at=_UNSET,
        renewal_reminder_sent=_UNSET,
    ):
        """Частичное обновление: меняются только переданные поля.
        Явный None допустим (например funnel_next_at=None очищает дату)."""
        now = self._now()
        passed = {
            "status": status,
            "tier": tier,
            "paid_until": paid_until,
            "funnel_step": funnel_step,
            "funnel_next_at": funnel_next_at,
            "renewal_reminder_sent": renewal_reminder_sent,
        }
        fields = {k: v for k, v in passed.items() if v is not _UNSET}
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM subscriptions WHERE user_id = ?", (user_id,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields) or "user_id = user_id"
                await db.execute(
                    f"UPDATE subscriptions SET {sets}, updated_at = ? WHERE user_id = ?",
                    (*fields.values(), now, user_id),
                )
            else:
                values = {
                    "status": "trial",
                    "tier": None,
                    "paid_until": None,
                    "funnel_step": 0,
                    "funnel_next_at": None,
                    "renewal_reminder_sent": 0,
                }
                values.update(fields)
                await db.execute(
                    """INSERT INTO subscriptions
                       (user_id, status, tier, paid_until, funnel_step,
                        funnel_next_at, renewal_reminder_sent, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, values["status"], values["tier"], values["paid_until"],
                     values["funnel_step"], values["funnel_next_at"],
                     values["renewal_reminder_sent"], now, now),
                )
            await db.commit()

    async def set_funnel_step(self, user_id: str, step: int, next_at: str | None):
        await self.upsert_subscription(user_id, funnel_step=step, funnel_next_at=next_at)

    async def due_funnel_users(self, now_iso: str) -> list[dict]:
        """Кому пора слать следующий шаг воронки (не оплатившие)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"""{self._SUB_SELECT}
                    WHERE funnel_next_at IS NOT NULL
                      AND funnel_next_at <= ?
                      AND status != 'active'""",
                (now_iso,),
            ) as cur:
                rows = await cur.fetchall()
        return [self._sub_row(r) for r in rows]

    async def expiring_subscriptions(self, now_iso: str, deadline_iso: str) -> list[dict]:
        """Активные подписки, которые истекают до deadline и ещё не получали напоминание."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"""{self._SUB_SELECT}
                    WHERE status = 'active'
                      AND paid_until IS NOT NULL
                      AND paid_until > ?
                      AND paid_until <= ?
                      AND renewal_reminder_sent = 0""",
                (now_iso, deadline_iso),
            ) as cur:
                rows = await cur.fetchall()
        return [self._sub_row(r) for r in rows]

    async def expired_active_subscriptions(self, now_iso: str) -> list[dict]:
        """Активные подписки с истёкшим paid_until — надо перевести в expired."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"""{self._SUB_SELECT}
                    WHERE status = 'active'
                      AND paid_until IS NOT NULL
                      AND paid_until <= ?""",
                (now_iso,),
            ) as cur:
                rows = await cur.fetchall()
        return [self._sub_row(r) for r in rows]
