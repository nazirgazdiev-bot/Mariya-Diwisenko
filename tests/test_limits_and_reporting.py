"""Run with the deployed dependencies, using temporary DBs and fake network calls."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('BOT_TOKEN', '123456789:TEST_TOKEN_FOR_LOCAL_CHECK_ONLY')
os.environ.setdefault('ANTHROPIC_API_KEY', 'test-key-no-network-calls')

import bot as app
from manual_access import reserve_grants, claim_grants
from storage import Storage
import sheets


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='quota-test-')
        self.path = str(Path(self.folder.name) / 'test.db')
        self.store = Storage(self.path)
        await self.store.init()
        self.uid = '987650111'
        app.storage = self.store
        self.chat = AsyncMock(return_value='Ответ')
        self.learn = AsyncMock(return_value=[])
        app.mariya = SimpleNamespace(chat=self.chat, extract_facts=self.learn)
        app.bot = SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock(), delete_message=AsyncMock())

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def grant(self, uid=None):
        uid = uid or self.uid
        name = 'quota_user_' + uid
        await self.store.upsert_client(uid, 'Test', {}, username=name)
        reserve_grants(self.path, 'test-' + uid, [{'username': name, 'tier': '1m'}])
        claim_grants(self.path, uid, name, app.next_renewal_schedule)
        await self.store.set_mariya_mode(uid, True)

    def message(self, uid=None):
        uid = uid or self.uid
        return SimpleNamespace(from_user=SimpleNamespace(id=int(uid)), chat=SimpleNamespace(id=int(uid)),
                               message_id=1, text='Спасибо', answer=AsyncMock())

    def count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT coalesce(sum(request_count),0) FROM mariya_daily_usage').fetchone()[0]

    async def test_eleventh_message_never_calls_either_ai_method(self):
        await self.grant()
        for _ in range(10):
            await app.handle_text(self.message())
        last = self.message()
        await app.handle_text(last)
        self.assertEqual(self.chat.await_count, 10)
        self.assertEqual(self.learn.await_count, 10)
        self.assertEqual(self.count(), 10)
        last.answer.assert_awaited_once_with('Ты исчерпала дневной лимит — 10 обращений. Возвращайся завтра 💛', parse_mode=None)

    async def test_samara_midnight_starts_a_new_bucket(self):
        await self.grant()
        before = datetime(2026, 10, 27, 19, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        self.assertEqual(sum([await self.store.consume_mariya_request(self.uid, now=before) for _ in range(11)]), 10)
        self.assertEqual(sum([await self.store.consume_mariya_request(self.uid, now=after) for _ in range(11)]), 10)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT usage_date,request_count FROM mariya_daily_usage ORDER BY usage_date').fetchall(), [('2026-10-27', 10), ('2026-10-28', 10)])

    async def test_restart_does_not_reset_usage(self):
        await self.grant()
        for _ in range(10):
            self.assertTrue(await self.store.consume_mariya_request(self.uid))
        restarted = Storage(self.path)
        await restarted.init()
        self.assertFalse(await restarted.consume_mariya_request(self.uid))

    async def test_first_activation_counts_existing_ai_messages_today(self):
        await self.grant()
        for _ in range(12):
            await self.store.add_dialog(self.uid, 'user', 'Old AI request')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE mariya_daily_usage')
            db.commit()
        await self.store.init()
        self.assertFalse(await self.store.consume_mariya_request(self.uid))
        self.assertEqual(self.count(), 10)

    async def test_concurrent_requests_cannot_exceed_ten(self):
        await self.grant()
        decisions = await asyncio.gather(*(self.store.consume_mariya_request(self.uid) for _ in range(25)))
        self.assertEqual(sum(decisions), 10)
        self.assertEqual(self.count(), 10)

    async def test_real_paid_renewal_immediately_removes_cap(self):
        await self.grant()
        for _ in range(10):
            await app.handle_text(self.message())
        with patch.object(app, '_sheets_log_payment', AsyncMock()):
            self.assertTrue(await app.mark_paid(self.uid, '1m', order_id='QUOTA_TEST_PAYMENT', amount=1290))
        for _ in range(12):
            await app.handle_text(self.message())
        self.assertEqual(self.chat.await_count, 22)
        self.assertEqual(self.count(), 10)

    async def test_existing_paid_and_regular_accounts_are_not_limited(self):
        await self.grant()
        await self.store.activate_payment(user_id=self.uid, tier='3m', paid_until=(datetime.now(timezone.utc)+timedelta(days=90)).isoformat(), renewal_next_at=None, amount=3490, commission_sum=0, order_id='EXISTING_PAYMENT')
        self.assertTrue(all([await self.store.consume_mariya_request(self.uid) for _ in range(15)]))
        self.assertTrue(all([await self.store.consume_mariya_request('ordinary') for _ in range(15)]))
        self.assertEqual(self.count(), 0)

    async def test_admin_is_exempt_even_with_a_free_grant(self):
        await self.grant()
        with patch.object(app, 'ADMIN_USER_IDS', {self.uid}):
            for _ in range(12):
                await app.handle_text(self.message())
        self.assertEqual(self.chat.await_count, 12)
        self.assertEqual(self.count(), 0)

    async def test_menus_recipes_and_intro_do_not_spend_requests(self):
        await self.grant()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=int(self.uid)),
                                   message=SimpleNamespace(chat=SimpleNamespace(id=int(self.uid)), delete=AsyncMock()), answer=AsyncMock())
        with patch.object(app, 'send_categories_screen', AsyncMock()), patch.object(app, 'send_favorites_screen', AsyncMock()), patch.object(app, 'send_mariya_screen', AsyncMock()):
            for _ in range(12):
                await app.nav_categories.__wrapped__(callback)
                await app.nav_favorites.__wrapped__(callback)
                await app.nav_mariya.__wrapped__(callback)
                await app.mariya_intro.__wrapped__(self.message())
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.chat.await_count, 0)
        await self.store.set_mariya_mode(self.uid, False)
        await app.handle_text(self.message())
        self.assertEqual(self.count(), 0)


class FakeSpreadsheet:
    def __init__(self):
        titles = [sheets.PAYMENTS_SHEET, sheets.DASHBOARD_SHEET, sheets.USERS_SHEET, sheets.DAILY_SHEET, sheets.SEGMENTS_SHEET, sheets.SOURCES_SHEET]
        self.worksheets = {title: SimpleNamespace(title=title, id=i, row_count=1400, col_count=20) for i,title in enumerate(titles)}
        self.batches = []
    def fetch_sheet_metadata(self, **kwargs):
        return {'sheets': [{'properties': {'title': title, 'sheetId': ws.id, 'gridProperties': {'rowCount': ws.row_count, 'columnCount': ws.col_count}}} for title,ws in self.worksheets.items()]}
    def batch_update(self, body):
        self.batches.append(body)
        return {'replies': [{} for _ in body['requests']]}


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.client = sheets.SheetsClient.__new__(sheets.SheetsClient)
        self.client.sh = FakeSpreadsheet()
        self.client._lock = threading.RLock()
        self.client._worksheets = self.client.sh.worksheets
        self.client._grid_sizes = {title: (1400,20) for title in self.client._worksheets}

    def test_all_report_tabs_commit_in_one_write_without_shrinking(self):
        self.client.write_dashboard_snapshot({}, {'daily': [], 'users': [], 'sources': []}, '2026-10-01', '2026-10-27', [])
        self.assertEqual(len(self.client.sh.batches), 1)
        requests = self.client.sh.batches[0]['requests']
        updates = [r['updateCells'] for r in requests if 'updateCells' in r]
        self.assertEqual(len(updates), 5)
        self.assertNotIn(self.client._worksheets[sheets.PAYMENTS_SHEET].id, {r['range']['sheetId'] for r in updates})
        self.assertTrue(all(r['fields'] == 'userEnteredValue' for r in updates))
        self.assertTrue(all(r['range']['endRowIndex'] == 1400 for r in updates))
        for request in requests:
            if 'updateSheetProperties' in request:
                grid = request['updateSheetProperties']['properties']['gridProperties']
                self.assertEqual((grid['rowCount'], grid['columnCount']), (1400,20))
        dash = next(r for r in updates if r['range']['sheetId'] == self.client._worksheets[sheets.DASHBOARD_SHEET].id)
        self.assertEqual(dash['rows'][1]['values'][1]['userEnteredValue']['numberValue'], (datetime(2026,10,1)-datetime(1899,12,30)).days)

    @staticmethod
    def error(code):
        return sheets.gspread.exceptions.APIError(SimpleNamespace(status_code=code, json=lambda: {'error': {'code': code, 'message': 'test'}}))

    def test_quota_error_retries_the_same_atomic_batch(self):
        operation = Mock(side_effect=[self.error(429), {'replies': []}])
        self.client.sh.batch_update = operation
        requests = [{'repeatCell': {}}]
        with patch.object(sheets.time, 'sleep') as sleep:
            self.client._batch_update(requests)
        self.assertEqual(operation.call_count, 2)
        self.assertEqual(operation.call_args_list[0], operation.call_args_list[1])
        sleep.assert_called_once()

    def test_invalid_requests_are_not_retried(self):
        self.client.sh.batch_update = Mock(side_effect=self.error(400))
        with patch.object(sheets.time, 'sleep') as sleep, self.assertRaises(sheets.gspread.exceptions.APIError):
            self.client._batch_update([{}])
        sleep.assert_not_called()

    def test_text_is_literal_and_not_a_spreadsheet_formula(self):
        self.assertEqual(self.client._cell('=IMPORTXML("x")'), {'userEnteredValue': {'stringValue': '=IMPORTXML("x")'}})


if __name__ == '__main__':
    real_bot = app.bot
    program = unittest.main(exit=False, verbosity=2)
    asyncio.run(real_bot.session.close())
    if not program.result.wasSuccessful():
        raise SystemExit(1)
    print('QUOTA_AND_SHEETS_TESTS_OK', program.result.testsRun)
