"""Google Sheets: платежи, метрики, база пользователей и сегменты рассылок."""

import json
import logging
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PAYMENTS_SHEET = "Платежи"
DASHBOARD_SHEET = "Дашборд"
USERS_SHEET = "Пользователи"
DAILY_SHEET = "Сводка по датам"
SEGMENTS_SHEET = "Сегменты и рассылки"

PAYMENTS_HEADER = [
    "Дата", "user_id", "Тариф", "Сумма", "Комиссия Продамуса",
    "Чистыми", "Статус", "order_id",
]


class SheetsClient:
    """Синхронная обёртка над gspread; вызывается через run_in_executor."""

    def __init__(self, service_account_json: str, sheet_id: str):
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self.sh = self.gc.open_by_key(sheet_id)
        self._ensure_sheets()

    @staticmethod
    def _header_format():
        return {
            "backgroundColor": {"red": 0.19, "green": 0.11, "blue": 0.36},
            "textFormat": {
                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                "bold": True,
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }

    def _ensure_sheets(self):
        titles = [ws.title for ws in self.sh.worksheets()]
        if PAYMENTS_SHEET not in titles:
            ws = self.sh.add_worksheet(
                title=PAYMENTS_SHEET, rows=1000, cols=len(PAYMENTS_HEADER)
            )
            ws.append_row(PAYMENTS_HEADER, value_input_option="USER_ENTERED")
        else:
            ws = self.sh.worksheet(PAYMENTS_SHEET)
            if ws.row_values(1) != PAYMENTS_HEADER:
                ws.update([PAYMENTS_HEADER], "A1")

        for title, rows, cols in (
            (DASHBOARD_SHEET, 50, 4),
            (USERS_SHEET, 1000, 15),
            (DAILY_SHEET, 1000, 12),
            (SEGMENTS_SHEET, 30, 4),
        ):
            if title not in titles:
                self.sh.add_worksheet(title=title, rows=rows, cols=cols)

        ws = self.sh.worksheet(PAYMENTS_SHEET)
        ws.freeze(rows=1)
        ws.format("A1:H1", self._header_format())
        ws.format(
            "D2:F1000",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        ws.set_basic_filter("A1:H1000")

    def log_payment(
        self,
        user_id: str,
        tier: str,
        amount: float,
        commission_sum: float,
        status: str,
        order_id: str,
    ):
        ws = self.sh.worksheet(PAYMENTS_SHEET)
        amount = amount or 0
        commission_sum = commission_sum or 0
        ws.append_row(
            [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                str(user_id),
                tier,
                amount,
                commission_sum,
                round(amount - commission_sum, 2),
                status,
                order_id or "",
            ],
            value_input_option="USER_ENTERED",
        )

    def write_dashboard_snapshot(self, metrics: dict, snapshot: dict):
        """Записывает готовые числа: никаких формул, зависящих от локали Sheets."""
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        month = today[:7]
        daily = snapshot.get("daily", [])

        def total(field: str, prefix: str | None = None):
            return sum(
                row.get(field, 0) or 0
                for row in daily
                if prefix is None or row.get("date", "").startswith(prefix)
            )

        dashboard_rows = [
            ["МЕТРИКИ TELEGRAM-БОТА", "", "", ""],
            ["Обновлено (UTC)", now.isoformat(timespec="seconds"), "", ""],
            ["", "", "", ""],
            ["АУДИТОРИЯ СЕЙЧАС", "", "", ""],
            ["Всего зашло в бота", metrics.get("total_registered", 0), "", ""],
            ["Активные подписчики", metrics.get("active", 0), "", ""],
            ["Зашли и не оплатили", metrics.get("trial", 0), "", ""],
            ["Подписка истекла", metrics.get("expired", 0), "", ""],
            ["Заблокировали бота", metrics.get("blocked", 0), "", ""],
            ["", "", "", ""],
            ["ОПЛАТЫ И ПРОДЛЕНИЯ", "", "", ""],
            ["Оплатили хотя бы раз", metrics.get("paid_at_least_once", 0), "", ""],
            ["Оплатили и продлили", metrics.get("renewed", 0), "", ""],
            ["Оплатили и не продлили", metrics.get("not_renewed", 0), "", ""],
            ["", "", "", ""],
            ["ФИНАНСЫ", "Сегодня", "Текущий месяц", "Всё время"],
            ["Выручка", total("revenue", today), total("revenue", month), total("revenue")],
            ["Комиссия Prodamus", total("commission", today), total("commission", month), total("commission")],
            ["Чистыми", total("net", today), total("net", month), total("net")],
            ["", "", "", ""],
            [
                "Где смотреть детали",
                "Сводка по датам",
                "Пользователи",
                "Сегменты и рассылки",
            ],
        ]
        ws = self.sh.worksheet(DASHBOARD_SHEET)
        ws.clear()
        ws.update(dashboard_rows, "A1", value_input_option="USER_ENTERED")
        ws.freeze(rows=2)
        for row in (1, 4, 11, 16):
            ws.format(f"A{row}:D{row}", self._header_format())
        ws.format(
            "B17:D19",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )

        daily_header = [
            "Дата", "Зашли в бота", "Открыли тарифы",
            "Создали ссылку оплаты", "Всего оплат", "Первые оплаты",
            "Продления", "Уникальных покупателей", "Выручка", "Комиссия",
            "Чистыми", "Конверсия в оплату",
        ]
        daily_rows = [
            [
                row["date"], row["registrations"], row["tariff_opens"],
                row["payment_links"], row["payments"], row["first_payments"],
                row["renewals"], row["buyers"], row["revenue"],
                row["commission"], row["net"], row["conversion"],
            ]
            for row in daily
        ]
        ws = self.sh.worksheet(DAILY_SHEET)
        ws.clear()
        ws.resize(rows=max(1000, len(daily_rows) + 1), cols=12)
        ws.update(
            [daily_header] + daily_rows,
            "A1",
            value_input_option="USER_ENTERED",
        )
        ws.freeze(rows=1)
        ws.format("A1:L1", self._header_format())
        ws.format(
            "I2:K1000",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        ws.format(
            "L2:L1000",
            {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}},
        )
        ws.set_basic_filter("A1:L1000")

        users_header = [
            "Дата входа", "Telegram user_id", "Имя", "Username", "Статус",
            "Сегмент", "Тариф", "Доступ до", "Количество оплат",
            "Первая оплата", "Последняя оплата", "Выручка", "Комиссия",
            "Чистыми", "Заблокировал бота",
        ]
        users_rows = [
            [
                u["registered_at"], u["user_id"], u["name"],
                f"@{u['username']}" if u["username"] else "",
                u["status"], u["segment"], u["tier"], u["paid_until"],
                u["payment_count"], u["first_paid_at"], u["last_paid_at"],
                u["revenue"], u["commission"], u["net"],
                "Да" if u["blocked"] else "Нет",
            ]
            for u in snapshot.get("users", [])
        ]
        ws = self.sh.worksheet(USERS_SHEET)
        ws.clear()
        ws.resize(rows=max(1000, len(users_rows) + 1), cols=15)
        ws.update(
            [users_header] + users_rows,
            "A1",
            value_input_option="USER_ENTERED",
        )
        ws.freeze(rows=1, cols=2)
        ws.format("A1:O1", self._header_format())
        ws.format(
            "L2:N1000",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        ws.set_basic_filter("A1:O1000")

        segments_rows = [
            ["Сегмент", "Кто входит", "Код", "Пример команды"],
            ["Все доступные", "Все, кто не заблокировал бота", "all", "/broadcast all Текст сообщения"],
            ["Активная подписка", "Все с действующим доступом", "paid", "/broadcast paid Текст сообщения"],
            ["Зашли и не оплатили", "Статус trial, оплат не было", "unpaid", "/broadcast unpaid Текст сообщения"],
            ["Оплатили впервые", "Активный доступ и ровно одна оплата", "firstpaid", "/broadcast firstpaid Текст сообщения"],
            ["Оплатили и продлили", "Две успешные оплаты или больше", "renewed", "/broadcast renewed Текст сообщения"],
            ["Оплатили и не продлили", "Одна оплата, доступ уже истёк", "notrenewed", "/broadcast notrenewed Текст сообщения"],
            ["Истёкшая подписка", "Все пользователи со статусом expired", "expired", "/broadcast expired Текст сообщения"],
            ["", "", "", ""],
            ["Как отправить", "Отправить команду боту с Telegram-аккаунта администратора", "", ""],
            ["Важно", "Рассылка пропускает пользователей, заблокировавших бота", "", ""],
        ]
        ws = self.sh.worksheet(SEGMENTS_SHEET)
        ws.clear()
        ws.update(segments_rows, "A1", value_input_option="USER_ENTERED")
        ws.freeze(rows=1)
        ws.format("A1:D1", self._header_format())
