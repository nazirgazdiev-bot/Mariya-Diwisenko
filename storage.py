"""
SQLite-хранилище МарИИи:
- clients: профиль клиента (цели по КБЖУ, аллергии, нелюбимое, особенности)
- dialog: история разговора (последние N реплик на клиента)
- facts: авто-собранные факты о клиенте (через Haiku)
- subscriptions: статус подписки и состояние воронки продаж
"""

import json
from datetime import datetime, timedelta, timezone
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
                    renewal_stage INTEGER NOT NULL DEFAULT 0,
                    renewal_next_at TEXT,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_funnel ON subscriptions(funnel_next_at)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    created_at TEXT,
                    PRIMARY KEY (user_id, recipe_id)
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id, created_at)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ui_state (
                    user_id TEXT PRIMARY KEY,
                    recipe_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    mariya_mode INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_renewal ON subscriptions(renewal_next_at)"
            )
            # Миграция для баз, созданных до появления многошаговой воронки
            # продления (renewal_stage/renewal_next_at) — ADD COLUMN IF NOT EXISTS
            # для sqlite делаем вручную через PRAGMA table_info.
            async with db.execute("PRAGMA table_info(subscriptions)") as cur:
                existing_cols = {row[1] for row in await cur.fetchall()}
            if "renewal_stage" not in existing_cols:
                await db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN renewal_stage INTEGER NOT NULL DEFAULT 0"
                )
            if "renewal_next_at" not in existing_cols:
                await db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN renewal_next_at TEXT"
                )
            if "blocked" not in existing_cols:
                await db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
                )

            # Профиль Telegram нужен для выгрузки базы пользователей и сегментов.
            async with db.execute("PRAGMA table_info(clients)") as cur:
                client_cols = {row[1] for row in await cur.fetchall()}
            if "username" not in client_cols:
                await db.execute("ALTER TABLE clients ADD COLUMN username TEXT")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    tier TEXT,
                    amount REAL,
                    commission_sum REAL,
                    order_id TEXT,
                    status TEXT,
                    created_at TEXT
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, id)"
            )
            # Prodamus может повторять вебхуки. Один order_id должен учитываться
            # ровно один раз, иначе дублируются выручка и продления.
            await db.execute("""
                DELETE FROM payments
                WHERE order_id IS NOT NULL AND order_id != ''
                  AND id NOT IN (
                    SELECT MIN(id) FROM payments
                    WHERE order_id IS NOT NULL AND order_id != ''
                    GROUP BY order_id
                  )""")
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_order_unique
                ON payments(order_id)
                WHERE order_id IS NOT NULL AND order_id != ''""")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )""")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type_date ON events(event_type, created_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, id)"
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
                "SELECT name, username, profile_json FROM clients WHERE user_id = ?",
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
            return {
                "user_id": user_id, "name": None, "username": None,
                "profile": {}, "facts": facts,
            }

        name, username, profile_json = row
        try:
            profile = json.loads(profile_json) if profile_json else {}
        except json.JSONDecodeError:
            profile = {}
        return {
            "user_id": user_id, "name": name, "username": username,
            "profile": profile, "facts": facts,
        }

    async def upsert_client(
        self,
        user_id: str,
        name: str | None,
        profile: dict,
        username: str | None = None,
    ):
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT created_at FROM clients WHERE user_id = ?", (user_id,)
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute(
                    """UPDATE clients
                       SET name = COALESCE(?, name),
                           username = COALESCE(?, username),
                           profile_json = ?,
                           updated_at = ?
                       WHERE user_id = ?""",
                    (
                        name, username, json.dumps(profile, ensure_ascii=False),
                        now, user_id,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO clients
                       (user_id, name, username, profile_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, name, username,
                        json.dumps(profile, ensure_ascii=False), now, now,
                    ),
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
        """Сбрасывает персонализацию, но никогда не удаляет оплату и доступ."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM dialog WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM clients WHERE user_id = ?", (user_id,))
            await db.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
            await db.commit()

    # ---------- События воронки ----------

    async def add_event(
        self,
        user_id: str,
        event_type: str,
        metadata: dict | None = None,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO events (user_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    user_id, event_type,
                    json.dumps(metadata or {}, ensure_ascii=False), self._now(),
                ),
            )
            await db.commit()

    # ---------- Подписки / воронка ----------

    _SUB_FIELDS = (
        "user_id", "status", "tier", "paid_until",
        "funnel_step", "funnel_next_at", "renewal_reminder_sent",
        "renewal_stage", "renewal_next_at", "blocked",
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
        renewal_stage=_UNSET,
        renewal_next_at=_UNSET,
        blocked=_UNSET,
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
            "renewal_stage": renewal_stage,
            "renewal_next_at": renewal_next_at,
            "blocked": blocked,
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
                    "renewal_stage": 0,
                    "renewal_next_at": None,
                    "blocked": 0,
                }
                values.update(fields)
                await db.execute(
                    """INSERT INTO subscriptions
                       (user_id, status, tier, paid_until, funnel_step,
                        funnel_next_at, renewal_reminder_sent,
                        renewal_stage, renewal_next_at, blocked, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, values["status"], values["tier"], values["paid_until"],
                     values["funnel_step"], values["funnel_next_at"],
                     values["renewal_reminder_sent"], values["renewal_stage"],
                     values["renewal_next_at"], values["blocked"], now, now),
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

    async def due_renewal_users(self, now_iso: str) -> list[dict]:
        """Кому пора слать следующий шаг многоступенчатой воронки продления
        (за 3 дня / за 1 день / в день списания / за 1 час / win-back —
        см. RENEWAL_STAGES в bot.py). Работает по точному времени
        renewal_next_at, а не по флагу "уже отправлено", так что не зависит
        от статуса (active/expired) — win-back шлётся уже после истечения."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"""{self._SUB_SELECT}
                    WHERE renewal_next_at IS NOT NULL
                      AND renewal_next_at <= ?""",
                (now_iso,),
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

    # ---------- Избранное ----------

    async def add_favorite(self, user_id: str, recipe_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO favorites (user_id, recipe_id, created_at) VALUES (?, ?, ?)",
                (user_id, recipe_id, self._now()),
            )
            await db.commit()

    async def remove_favorite(self, user_id: str, recipe_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND recipe_id = ?",
                (user_id, recipe_id),
            )
            await db.commit()

    async def is_favorite(self, user_id: str, recipe_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND recipe_id = ?",
                (user_id, recipe_id),
            ) as cur:
                return await cur.fetchone() is not None

    async def get_favorites(self, user_id: str) -> list[str]:
        """Список recipe_id в избранном, новые сверху."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT recipe_id FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    # ---------- Состояние интерфейса ----------

    async def get_ui_state(self, user_id: str) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT recipe_message_ids_json, mariya_mode
                   FROM ui_state WHERE user_id = ?""",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return {"recipe_message_ids": [], "mariya_mode": False}
        try:
            message_ids = [
                int(message_id)
                for message_id in json.loads(row[0] or "[]")
                if str(message_id).isdigit()
            ]
        except (json.JSONDecodeError, TypeError, ValueError):
            message_ids = []
        return {
            "recipe_message_ids": message_ids,
            "mariya_mode": bool(row[1]),
        }

    async def set_recipe_message_ids(self, user_id: str, message_ids: list[int]):
        await self._upsert_ui_state(
            user_id,
            recipe_message_ids_json=json.dumps(message_ids),
        )

    async def set_mariya_mode(self, user_id: str, enabled: bool):
        await self._upsert_ui_state(user_id, mariya_mode=1 if enabled else 0)

    async def _upsert_ui_state(
        self,
        user_id: str,
        *,
        recipe_message_ids_json=_UNSET,
        mariya_mode=_UNSET,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM ui_state WHERE user_id = ?", (user_id,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                fields = {}
                if recipe_message_ids_json is not _UNSET:
                    fields["recipe_message_ids_json"] = recipe_message_ids_json
                if mariya_mode is not _UNSET:
                    fields["mariya_mode"] = mariya_mode
                sets = ", ".join(f"{key} = ?" for key in fields)
                if sets:
                    await db.execute(
                        f"UPDATE ui_state SET {sets}, updated_at = ? WHERE user_id = ?",
                        (*fields.values(), self._now(), user_id),
                    )
            else:
                await db.execute(
                    """INSERT INTO ui_state
                       (user_id, recipe_message_ids_json, mariya_mode, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        user_id,
                        "[]" if recipe_message_ids_json is _UNSET else recipe_message_ids_json,
                        0 if mariya_mode is _UNSET else mariya_mode,
                        self._now(),
                    ),
                )
            await db.commit()

    # ---------- Блокировка бота юзером ----------

    async def set_blocked(self, user_id: str, blocked: bool):
        """Отдельный флаг (не путать со status): юзер заблокировал бота в Telegram.
        Не трогает status/tier/paid_until — платящий юзер, временно заблокировавший
        бота и потом разблокировавший его, не теряет доступ."""
        await self.upsert_subscription(user_id, blocked=1 if blocked else 0)

    # ---------- Платежи (история — источник метрик "продлил/не продлил") ----------

    async def add_payment(
        self,
        user_id: str,
        tier: str,
        amount: float | None,
        commission_sum: float | None,
        order_id: str | None,
        status: str = "success",
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO payments
                   (user_id, tier, amount, commission_sum, order_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, tier, amount, commission_sum, order_id, status, self._now()),
            )
            await db.commit()

    async def payment_exists(self, order_id: str | None) -> bool:
        if not order_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM payments WHERE order_id = ? LIMIT 1", (order_id,)
            ) as cur:
                return await cur.fetchone() is not None

    async def activate_payment(
        self,
        *,
        user_id: str,
        tier: str,
        paid_until: str,
        renewal_next_at: str | None,
        amount: float | None,
        commission_sum: float | None,
        order_id: str | None,
    ) -> bool:
        """Атомарно записывает оплату и открывает доступ.

        Возвращает False, если order_id уже был обработан. Это защищает
        подписку, выручку и метрики от повторных вебхуков Prodamus.
        """
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if order_id:
                async with db.execute(
                    "SELECT 1 FROM payments WHERE order_id = ? LIMIT 1",
                    (order_id,),
                ) as cur:
                    if await cur.fetchone():
                        await db.rollback()
                        return False

            async with db.execute(
                "SELECT 1 FROM subscriptions WHERE user_id = ?", (user_id,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                await db.execute(
                    """UPDATE subscriptions
                       SET status = 'active', tier = ?, paid_until = ?,
                           funnel_next_at = NULL, renewal_reminder_sent = 0,
                           renewal_stage = 0, renewal_next_at = ?,
                           updated_at = ?
                       WHERE user_id = ?""",
                    (tier, paid_until, renewal_next_at, now, user_id),
                )
            else:
                await db.execute(
                    """INSERT INTO subscriptions
                       (user_id, status, tier, paid_until, funnel_step,
                        funnel_next_at, renewal_reminder_sent, renewal_stage,
                        renewal_next_at, blocked, created_at, updated_at)
                       VALUES (?, 'active', ?, ?, 0, NULL, 0, 0, ?, 0, ?, ?)""",
                    (user_id, tier, paid_until, renewal_next_at, now, now),
                )

            await db.execute(
                """INSERT INTO payments
                   (user_id, tier, amount, commission_sum, order_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'success', ?)""",
                (
                    user_id, tier, amount, commission_sum,
                    order_id or "", now,
                ),
            )
            await db.execute(
                """INSERT INTO events (user_id, event_type, metadata_json, created_at)
                   VALUES (?, 'payment_success', ?, ?)""",
                (
                    user_id,
                    json.dumps({"tier": tier, "order_id": order_id or ""}),
                    now,
                ),
            )
            await db.commit()
            return True

    # ---------- Сегменты для рассылок ----------

    async def users_in_segment(self, segment: str | None) -> list[str]:
        """Получатели рассылки по понятному бизнес-сегменту."""
        async with aiosqlite.connect(self.db_path) as db:
            if segment in (None, "all"):
                query = "SELECT user_id FROM subscriptions WHERE blocked = 0"
                params: tuple = ()
            elif segment == "firstpaid":
                query = """
                    SELECT s.user_id
                    FROM subscriptions s
                    JOIN payments p ON p.user_id = s.user_id AND p.status = 'success'
                    WHERE s.blocked = 0 AND s.status = 'active'
                    GROUP BY s.user_id
                    HAVING COUNT(p.id) = 1"""
                params = ()
            elif segment == "renewed":
                query = """
                    SELECT s.user_id
                    FROM subscriptions s
                    JOIN payments p ON p.user_id = s.user_id AND p.status = 'success'
                    WHERE s.blocked = 0
                    GROUP BY s.user_id
                    HAVING COUNT(p.id) >= 2"""
                params = ()
            elif segment == "notrenewed":
                query = """
                    SELECT s.user_id
                    FROM subscriptions s
                    JOIN payments p ON p.user_id = s.user_id AND p.status = 'success'
                    WHERE s.blocked = 0 AND s.status = 'expired'
                    GROUP BY s.user_id
                    HAVING COUNT(p.id) = 1"""
                params = ()
            else:
                query = "SELECT user_id FROM subscriptions WHERE blocked = 0 AND status = ?"
                params = (segment,)
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def report_snapshot(self) -> dict:
        """Данные для Google Sheets: пользователи, дневная сводка и сегменты."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT
                    s.user_id,
                    COALESCE(c.name, ''),
                    COALESCE(c.username, ''),
                    s.created_at,
                    s.status,
                    COALESCE(s.tier, ''),
                    s.paid_until,
                    s.blocked,
                    COUNT(CASE WHEN p.status = 'success' THEN 1 END),
                    MIN(CASE WHEN p.status = 'success' THEN p.created_at END),
                    MAX(CASE WHEN p.status = 'success' THEN p.created_at END),
                    COALESCE(SUM(CASE WHEN p.status = 'success' THEN p.amount ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN p.status = 'success' THEN p.commission_sum ELSE 0 END), 0)
                FROM subscriptions s
                LEFT JOIN clients c ON c.user_id = s.user_id
                LEFT JOIN payments p ON p.user_id = s.user_id
                GROUP BY s.user_id
                ORDER BY s.created_at DESC
            """) as cur:
                rows = await cur.fetchall()

            users = []
            for row in rows:
                (
                    user_id, name, username, registered_at, status, tier,
                    paid_until, blocked, payment_count, first_paid_at,
                    last_paid_at, revenue, commission,
                ) = row
                if payment_count >= 2:
                    segment = "Оплатил и продлил"
                elif payment_count == 1 and status == "expired":
                    segment = "Оплатил и не продлил"
                elif payment_count == 1:
                    segment = "Оплатил впервые"
                else:
                    segment = "Зашёл и не оплатил"
                users.append({
                    "registered_at": registered_at,
                    "user_id": user_id,
                    "name": name,
                    "username": username,
                    "status": status,
                    "segment": segment,
                    "tier": tier,
                    "paid_until": paid_until,
                    "payment_count": payment_count,
                    "first_paid_at": first_paid_at,
                    "last_paid_at": last_paid_at,
                    "revenue": revenue or 0,
                    "commission": commission or 0,
                    "net": (revenue or 0) - (commission or 0),
                    "blocked": bool(blocked),
                })

            async with db.execute("""
                SELECT substr(created_at, 1, 10), COUNT(DISTINCT user_id)
                FROM subscriptions
                WHERE created_at IS NOT NULL
                GROUP BY substr(created_at, 1, 10)
            """) as cur:
                registrations = dict(await cur.fetchall())

            async with db.execute("""
                SELECT
                    substr(created_at, 1, 10),
                    COUNT(*),
                    COUNT(DISTINCT user_id),
                    COALESCE(SUM(amount), 0),
                    COALESCE(SUM(commission_sum), 0)
                FROM payments
                WHERE status = 'success'
                GROUP BY substr(created_at, 1, 10)
            """) as cur:
                payments_by_day = {
                    row[0]: {
                        "payments": row[1],
                        "buyers": row[2],
                        "revenue": row[3] or 0,
                        "commission": row[4] or 0,
                    }
                    for row in await cur.fetchall()
                }

            async with db.execute("""
                SELECT
                    day,
                    SUM(CASE WHEN payment_number = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN payment_number > 1 THEN 1 ELSE 0 END)
                FROM (
                    SELECT
                        substr(created_at, 1, 10) AS day,
                        ROW_NUMBER() OVER (
                            PARTITION BY user_id ORDER BY id
                        ) AS payment_number
                    FROM payments
                    WHERE status = 'success'
                )
                GROUP BY day
            """) as cur:
                payment_types = {
                    row[0]: {
                        "first_payments": row[1] or 0,
                        "renewals": row[2] or 0,
                    }
                    for row in await cur.fetchall()
                }

            async with db.execute("""
                SELECT substr(created_at, 1, 10), COUNT(DISTINCT user_id)
                FROM events
                WHERE event_type = 'tariff_opened'
                GROUP BY substr(created_at, 1, 10)
            """) as cur:
                tariff_opens = dict(await cur.fetchall())

            async with db.execute("""
                SELECT substr(created_at, 1, 10), COUNT(DISTINCT user_id)
                FROM events
                WHERE event_type = 'payment_link_created'
                GROUP BY substr(created_at, 1, 10)
            """) as cur:
                payment_links = dict(await cur.fetchall())

        dates = sorted(
            set(registrations) | set(payments_by_day)
            | set(tariff_opens) | set(payment_links),
            reverse=True,
        )
        daily = []
        for day in dates:
            p = payments_by_day.get(day, {})
            payment_type = payment_types.get(day, {})
            registrations_count = registrations.get(day, 0)
            buyers = p.get("buyers", 0)
            daily.append({
                "date": day,
                "registrations": registrations_count,
                "tariff_opens": tariff_opens.get(day, 0),
                "payment_links": payment_links.get(day, 0),
                "payments": p.get("payments", 0),
                "first_payments": payment_type.get("first_payments", 0),
                "renewals": payment_type.get("renewals", 0),
                "buyers": buyers,
                "revenue": p.get("revenue", 0),
                "commission": p.get("commission", 0),
                "net": p.get("revenue", 0) - p.get("commission", 0),
                "conversion": buyers / registrations_count if registrations_count else 0,
            })
        return {"users": users, "daily": daily}

    # ---------- Метрики для дашборда Google Sheets ----------

    async def dashboard_metrics(self) -> dict:
        """Все счётчики для листа «Дашборд»:
        всего зашло / активных / пробных / истёкших / заблокировавших /
        оплативших хотя бы раз / продливших / оплативших но не продливших.

        "Продлил" — у юзера >= 2 успешных платежа.
        "Не продлил" — у юзера ровно 1 успешный платёж И его подписка сейчас
        expired (то есть первый оплаченный период уже закончился, а нового
        платежа не было). Если платёж один и подписка ещё active — юзер
        просто не успел продлить, это не считаем "не продлил"."""
        async with aiosqlite.connect(self.db_path) as db:
            counts = {"active": 0, "trial": 0, "expired": 0}
            async with db.execute(
                "SELECT status, COUNT(*) FROM subscriptions GROUP BY status"
            ) as cur:
                for status, cnt in await cur.fetchall():
                    if status in counts:
                        counts[status] = cnt

            async with db.execute("SELECT COUNT(*) FROM subscriptions") as cur:
                total_registered = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE blocked = 1"
            ) as cur:
                blocked = (await cur.fetchone())[0]

            paid_at_least_once = 0
            renewed = 0
            not_renewed = 0
            async with db.execute(
                """SELECT p.user_id, COUNT(*), MAX(s.status)
                   FROM payments p
                   LEFT JOIN subscriptions s ON s.user_id = p.user_id
                   WHERE p.status = 'success'
                   GROUP BY p.user_id"""
            ) as cur:
                rows = await cur.fetchall()
            for _user_id, pay_count, sub_status in rows:
                paid_at_least_once += 1
                if pay_count >= 2:
                    renewed += 1
                elif sub_status == "expired":
                    not_renewed += 1

        return {
            "total_registered": total_registered,
            "active": counts["active"],
            "trial": counts["trial"],
            "expired": counts["expired"],
            "blocked": blocked,
            "paid_at_least_once": paid_at_least_once,
            "renewed": renewed,
            "not_renewed": not_renewed,
        }
