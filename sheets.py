"""Google Sheets: платежи, метрики, база пользователей и сегменты рассылок."""

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import a1_range_to_grid_range

log = logging.getLogger("sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PAYMENTS_SHEET = "Платежи"
DASHBOARD_SHEET = "Дашборд"
USERS_SHEET = "Пользователи"
DAILY_SHEET = "Сводка по датам"
SEGMENTS_SHEET = "Сегменты и рассылки"
SOURCES_SHEET = "По источникам"

PAYMENTS_HEADER = [
    "Дата", "user_id", "Тариф", "Сумма", "Комиссия Продамуса",
    "Чистыми", "Статус", "order_id", "Источник",
]


class SheetsClient:
    """Синхронная обёртка над gspread; вызывается через run_in_executor."""

    def __init__(self, service_account_json: str, sheet_id: str):
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self._lock = threading.RLock()
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

    @staticmethod
    def _cell(value):
        if value is None or value == "":
            return {}
        if isinstance(value, bool):
            return {"userEnteredValue": {"boolValue": value}}
        if isinstance(value, (int, float)):
            return {"userEnteredValue": {"numberValue": value}}
        return {"userEnteredValue": {"stringValue": str(value)}}

    def _retry(self, operation, *args, retry_statuses=(429, 500, 502, 503, 504), **kwargs):
        for attempt in range(4):
            try:
                return operation(*args, **kwargs)
            except gspread.exceptions.APIError as error:
                status = getattr(error.response, "status_code", None)
                if status not in retry_statuses or attempt == 3:
                    raise
                delay = min(15 * (2 ** attempt) + random.random(), 60)
                log.warning("Google Sheets temporary HTTP %s; retry in %.1fs", status, delay)
                time.sleep(delay)

    def _batch_update(self, requests):
        with self._lock:
            return self._retry(self.sh.batch_update, {"requests": requests})

    @staticmethod
    def _format_request(ws, range_name, cell_format):
        return {
            "repeatCell": {
                "range": a1_range_to_grid_range(range_name, sheet_id=ws.id),
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join("userEnteredFormat." + key for key in cell_format),
            }
        }

    def _ensure_sheets(self):
        self._worksheets = {ws.title: ws for ws in self.sh.worksheets()}
        for title, rows, cols in (
            (PAYMENTS_SHEET, 1000, len(PAYMENTS_HEADER)),
            (DASHBOARD_SHEET, 50, 4),
            (USERS_SHEET, 1000, 16),
            (DAILY_SHEET, 1000, 12),
            (SEGMENTS_SHEET, 30, 4),
            (SOURCES_SHEET, 100, 8),
        ):
            if title not in self._worksheets:
                self._worksheets[title] = self.sh.add_worksheet(title=title, rows=rows, cols=cols)
        self._grid_sizes = {
            title: (ws.row_count, ws.col_count) for title, ws in self._worksheets.items()
        }
        ws = self._worksheets[PAYMENTS_SHEET]
        requests = [{
            "updateSheetProperties": {
                "properties": {"sheetId": ws.id, "gridProperties": {
                    "rowCount": max(ws.row_count, 1000),
                    "columnCount": max(ws.col_count, len(PAYMENTS_HEADER)),
                    "frozenRowCount": 1,
                }},
                "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount",
            }
        }]
        if ws.row_values(1) != PAYMENTS_HEADER:
            requests.append({"updateCells": {
                "start": {"sheetId": ws.id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [self._cell(value) for value in PAYMENTS_HEADER]}],
                "fields": "userEnteredValue",
            }})
        requests.extend([
            self._format_request(ws, "A1:I1", self._header_format()),
            self._format_request(ws, "D2:F1000", {
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"},
            }),
            {"setBasicFilter": {"filter": {"range": a1_range_to_grid_range("A1:I1000", sheet_id=ws.id)}}},
        ])
        self._batch_update(requests)

    def _snapshot_requests(self, title, rows, cols, min_rows, frozen_rows=1, frozen_cols=0):
        ws = self._worksheets[title]
        old_rows, old_cols = self._grid_sizes[title]
        row_count, col_count = max(old_rows, min_rows, len(rows)), max(old_cols, cols)
        requests = [{
            "updateSheetProperties": {
                "properties": {"sheetId": ws.id, "gridProperties": {
                    "rowCount": row_count, "columnCount": col_count,
                    "frozenRowCount": frozen_rows, "frozenColumnCount": frozen_cols,
                }},
                "fields": "gridProperties.rowCount,gridProperties.columnCount,"
                          "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }, {
            "updateCells": {
                # Uncovered cells inside this managed range lose stale values.
                "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": row_count,
                          "startColumnIndex": 0, "endColumnIndex": cols},
                "rows": [{"values": [self._cell(value) for value in row]} for row in rows],
                "fields": "userEnteredValue",
            }
        }]
        return requests, (row_count, col_count)

    def log_payment(
        self,
        user_id: str,
        tier: str,
        amount: float,
        commission_sum: float,
        status: str,
        order_id: str,
        source_tag: str,
    ):
        ws = self._worksheets[PAYMENTS_SHEET]
        amount = amount or 0
        commission_sum = commission_sum or 0
        with self._lock:
            # A 429 rejects the append; retry it without retrying uncertain writes.
            self._retry(
                ws.append_row,
                [
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    str(user_id), tier, amount, commission_sum,
                    round(amount - commission_sum, 2), status,
                    order_id or "", source_tag or "direct",
                ],
                value_input_option="USER_ENTERED", retry_statuses=(429,),
            )

    def read_dashboard_period(self) -> tuple[str, str]:
        """Возвращает выбранные пользователем даты из строки 2 дашборда."""
        ws = self._worksheets[DASHBOARD_SHEET]
        with self._lock:
            values = self._retry(ws.get, "B2:D2")
        row = values[0] if values else []
        start_date = row[0] if len(row) > 0 else ""
        end_date = row[2] if len(row) > 2 else ""
        return str(start_date).strip(), str(end_date).strip()

    def _dashboard_width_requests(self, ws):
        """Фиксированные ширины не дают подписям обрезаться на дашборде."""
        widths = (250, 170, 170, 170)
        requests = []
        for index, width in enumerate(widths):
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            })
        return requests

    def write_dashboard_snapshot(
        self,
        metrics: dict,
        snapshot: dict,
        start_date: str,
        end_date: str,
        period_sources: list[dict],
    ):
        """Записывает готовые числа: никаких формул, зависящих от локали Sheets."""
        # Refresh dimensions once, then commit values and formatting atomically.
        # Keep existing extra rows/columns rather than shrinking user sheets.
        with self._lock:
            metadata = self._retry(self.sh.fetch_sheet_metadata, params={"fields": "sheets(properties)"})
            for item in metadata.get("sheets", []):
                props = item["properties"]
                grid = props.get("gridProperties", {})
                self._grid_sizes[props["title"]] = (grid.get("rowCount", 1000), grid.get("columnCount", 16))
        requests = []
        grid_sizes = {}
        def write_values(title, rows, cols, min_rows, frozen_rows=1, frozen_cols=0):
            queued, size = self._snapshot_requests(title, rows, cols, min_rows, frozen_rows, frozen_cols)
            requests.extend(queued)
            grid_sizes[title] = size
            return self._worksheets[title]
        def format_cells(ws, range_name, cell_format):
            requests.append(self._format_request(ws, range_name, cell_format))
        def set_filter(ws, range_name):
            requests.append({"setBasicFilter": {"filter": {
                "range": a1_range_to_grid_range(range_name, sheet_id=ws.id),
            }}})
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        daily = snapshot.get("daily", [])
        all_sources = snapshot.get("sources", [])

        def total(field: str, prefix: str | None = None):
            return sum(
                row.get(field, 0) or 0
                for row in daily
                if prefix is None or row.get("date", "").startswith(prefix)
            )

        def period_total(field: str):
            return sum(
                row.get(field, 0) or 0
                for row in daily
                if start_date <= row.get("date", "") <= end_date
            )

        dashboard_rows = [
            ["МЕТРИКИ TELEGRAM-БОТА", "", "", ""],
            ["Дата с", start_date, "Дата по", end_date],
            [
                "Обновлено (UTC)",
                now.isoformat(timespec="seconds"),
                "Выбранный период",
                f"{start_date} — {end_date}",
            ],
            ["", "", "", ""],
            ["АУДИТОРИЯ ЗА ПЕРИОД", "", "", ""],
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
            ["ФИНАНСЫ", "Выбранный период", "Сегодня", "Всё время"],
            ["Выручка", period_total("revenue"), total("revenue", today), total("revenue")],
            ["Комиссия Prodamus", period_total("commission"), total("commission", today), total("commission")],
            ["Чистыми", period_total("net"), total("net", today), total("net")],
            ["", "", "", ""],
            ["ИСТОЧНИКИ", "Регистрации", "Покупатели", "Оплаты"],
        ]
        for row in period_sources:
            dashboard_rows.append([
                row["source_tag"], row["registrations"],
                row["buyers"], row["payments"],
            ])
        dashboard_rows.extend([
            ["", "", "", ""],
            [
                "Где смотреть детали",
                "Сводка по датам",
                "Пользователи",
                "По источникам",
            ],
        ])
        # Sheets stores calendar dates as days since 1899-12-30.
        for index, date_text in ((1, start_date), (3, end_date)):
            dashboard_rows[1][index] = (
                datetime.strptime(date_text, "%Y-%m-%d") - datetime(1899, 12, 30)
            ).days
        ws = write_values(DASHBOARD_SHEET, dashboard_rows, 4, 50, frozen_rows=3)
        source_header_row = 22
        for row in (1, 5, 12, 17, source_header_row):
            format_cells(ws, f"A{row}:D{row}", self._header_format())
        format_cells(ws, 
            "B18:D20",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        for cell in ("B2", "D2"):
            format_cells(ws, 
                cell,
                {
                    "numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"},
                    "backgroundColor": {"red": 0.92, "green": 0.88, "blue": 1},
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                },
            )
        format_cells(ws, "A1:D40", {"wrapStrategy": "WRAP"})
        requests.extend(self._dashboard_width_requests(ws))

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
        ws = write_values(DAILY_SHEET, [daily_header] + daily_rows, 12, 1000)
        format_cells(ws, "A1:L1", self._header_format())
        format_cells(ws, 
            "I2:K1000",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        format_cells(ws, 
            "L2:L1000",
            {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}},
        )
        set_filter(ws, "A1:L1000")

        users_header = [
            "Дата входа", "Telegram user_id", "Имя", "Username", "Статус",
            "Источник", "Сегмент", "Тариф", "Доступ до", "Количество оплат",
            "Первая оплата", "Последняя оплата", "Выручка", "Комиссия",
            "Чистыми", "Заблокировал бота",
        ]
        users_rows = [
            [
                u["registered_at"], u["user_id"], u["name"],
                f"@{u['username']}" if u["username"] else "",
                u["status"], u["source_tag"], u["segment"], u["tier"], u["paid_until"],
                u["payment_count"], u["first_paid_at"], u["last_paid_at"],
                u["revenue"], u["commission"], u["net"],
                "Да" if u["blocked"] else "Нет",
            ]
            for u in snapshot.get("users", [])
        ]
        ws = write_values(USERS_SHEET, [users_header] + users_rows, 16, 1000, frozen_cols=2)
        format_cells(ws, "A1:P1", self._header_format())
        format_cells(ws, 
            "M2:O1000",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        set_filter(ws, "A1:P1000")

        sources_header = [
            "Источник", "Регистрации", "Уникальные покупатели", "Количество оплат",
            "Выручка", "Комиссия", "Чистыми", "Конверсия в покупателя",
        ]
        sources_rows = [
            [
                row["source_tag"], row["registrations"], row["buyers"],
                row["payments"], row["revenue"], row["commission"],
                row["net"], row["conversion"],
            ]
            for row in all_sources
        ]
        ws = write_values(SOURCES_SHEET, [sources_header] + sources_rows, 8, 100)
        format_cells(ws, "A1:H1", self._header_format())
        format_cells(ws, 
            "E2:G100",
            {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}},
        )
        format_cells(ws, 
            "H2:H100",
            {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}},
        )
        set_filter(ws, "A1:H100")

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
        ws = write_values(SEGMENTS_SHEET, segments_rows, 4, 30)
        format_cells(ws, "A1:D1", self._header_format())

        with self._lock:
            self._batch_update(requests)
            self._grid_sizes.update(grid_sizes)
