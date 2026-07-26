"""
Google Sheets интеграция: журнал платежей + живой дашборд.

Активируется только если заданы обе переменные окружения:
  GOOGLE_SERVICE_ACCOUNT_JSON — содержимое JSON-ключа Service Account (целиком, как строка)
  GOOGLE_SHEET_ID             — ID таблицы (из её URL)

Если хотя бы одна не задана — интеграция просто не включается, бот работает как раньше.
Аккаунт Google и Service Account — клиента (Nazir), не разработчика: никакого хардкода,
всё приходит через .env, поэтому легко переезжает на другой Google-аккаунт.

Листы создаются автоматически при первом запуске:
  «Платежи»  — построчный журнал каждой успешной оплаты
  «Дашборд»  — счётчики подписчиков (пишутся из Python) + выручка/чистыми
               (формулами, которые сами считают по листу «Платежи»)

Важно: gspread — синхронная библиотека (blocking I/O). Все вызовы этого модуля
должны выполняться через run_in_executor из async-кода (см. bot.py), чтобы не
блокировать event loop бота.
"""

import json
import logging
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("sheets")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

PAYMENTS_SHEET = "Платежи"
DASHBOARD_SHEET = "Дашборд"

PAYMENTS_HEADER = [
    "Дата", "user_id", "Тариф", "Сумма", "Комиссия Продамуса", "Чистыми", "Статус", "order_id",
]

# Сколько строк учитывать в формулах дашборда (с запасом на будущее).
FORMULA_ROWS = 100000


class SheetsClient:
    """Тонкая обёртка над gspread. Все методы синхронные — вызывайте их
    из bot.py через loop.run_in_executor(None, ...)."""

    def __init__(self, service_account_json: str, sheet_id: str):
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self.sh = self.gc.open_by_key(sheet_id)
        self._ensure_sheets()

    # ---------- Инициализация листов ----------

    def _ensure_sheets(self):
        titles = [ws.title for ws in self.sh.worksheets()]

        if PAYMENTS_SHEET not in titles:
            ws = self.sh.add_worksheet(
                title=PAYMENTS_SHEET, rows=1000, cols=len(PAYMENTS_HEADER)
            )
            ws.append_row(PAYMENTS_HEADER, value_input_option="USER_ENTERED")
        else:
            ws = self.sh.worksheet(PAYMENTS_SHEET)
            first_row = ws.row_values(1)
            if first_row != PAYMENTS_HEADER:
                ws.update("A1", [PAYMENTS_HEADER])

        if DASHBOARD_SHEET not in titles:
            self.sh.add_worksheet(title=DASHBOARD_SHEET, rows=50, cols=4)

    # ---------- Журнал платежей ----------

    def log_payment(
        self,
        user_id: str,
        tier: str,
        amount: float,
        commission_sum: float,
        status: str,
        order_id: str,
    ):
        """Дописывает одну строку в «Платежи». Вызывается при каждой успешной оплате."""
        ws = self.sh.worksheet(PAYMENTS_SHEET)
        amount = amount or 0
        commission_sum = commission_sum or 0
        net = round(amount - commission_sum, 2)
        row = [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            str(user_id),
            tier,
            amount,
            commission_sum,
            net,
            status,
            order_id or "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

    # ---------- Дашборд ----------

    def write_dashboard_snapshot(self, metrics: dict):
        """Обновляет лист «Дашборд»: счётчики — статичными значениями из SQLite
        (источник истины: storage.dashboard_metrics()), выручка/чистыми — формулами
        по листу «Платежи», чтобы Nazir видел живой пересчёт прямо в таблице."""
        p = f"'{PAYMENTS_SHEET}'"
        today = "TEXT(TODAY(),\"yyyy-mm-dd\")"
        month = "TEXT(TODAY(),\"yyyy-mm\")"

        def sum_formula(col: str, period: str) -> str:
            # period: "today" -> сравниваем первые 10 символов даты (yyyy-mm-dd)
            #         "month" -> первые 7 символов (yyyy-mm)
            length = 10 if period == "today" else 7
            match = today if period == "today" else month
            return (
                f'=SUMPRODUCT((LEFT({p}!$A$2:$A${FORMULA_ROWS},{length})={match})'
                f'*({p}!${col}$2:${col}${FORMULA_ROWS}))'
            )

        rows = [
            ["Обновлено", datetime.now(timezone.utc).isoformat(timespec="seconds")],
            ["", ""],
            ["Всего зашло в бота", metrics.get("total_registered", 0)],
            ["Активные подписчики", metrics.get("active", 0)],
            ["Пробные (не платили)", metrics.get("trial", 0)],
            ["Истёкшие", metrics.get("expired", 0)],
            ["Заблокировали бота", metrics.get("blocked", 0)],
            ["", ""],
            ["Оплатили хотя бы раз", metrics.get("paid_at_least_once", 0)],
            ["Продлили подписку", metrics.get("renewed", 0)],
            ["Оплатили, но не продлили", metrics.get("not_renewed", 0)],
            ["", ""],
            ["Выручка сегодня", sum_formula("D", "today")],
            ["Чистыми сегодня", sum_formula("F", "today")],
            ["Выручка за месяц", sum_formula("D", "month")],
            ["Чистыми за месяц", sum_formula("F", "month")],
            ["Выручка всего", f'=SUM({p}!$D$2:$D${FORMULA_ROWS})'],
            ["Чистыми всего", f'=SUM({p}!$F$2:$F${FORMULA_ROWS})'],
        ]
        ws = self.sh.worksheet(DASHBOARD_SHEET)
        ws.update("A1", rows, value_input_option="USER_ENTERED")
