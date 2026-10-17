from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge, HELP
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-commands-')
        self.root = Path(self.temp.name)
        self.config = Config(self.root, self.root / 'private', ROOT / 'schemas', self.root / 'codex',
                             user_id=7, chat_id=7, write_roots=(self.root,))
        self.store = Store(self.config.data_dir / 'bridge.sqlite3')
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.bridge = Bridge(self.config, self.store, self.adapter, FakeTelegramTransport(), conflict_check=lambda *_:False)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def command(self, text, mid=100):
        return await self.bridge.command(text, 7, 7, mid)

    async def test_status_defaults_off_without_thread_or_model_calls(self):
        result = await self.command('/knowledge status')
        self.assertIn('OFF', result)
        self.assertIn('자동 캡처: 미구현·비활성', result)
        self.assertIn('3,500', result)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.jobs(), [])
        self.assertIsNone(self.store.binding(7))
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM memory_settings').fetchone()[0], 0)

    async def test_enable_disable_and_preview_do_not_use_model_or_change_capture(self):
        await self.command('/save 8DT1 외부 노이즈 본문표식_AAA', 2)
        result = await self.command('/knowledge test 8DT1 외부 노이즈')
        self.assertIn('GB-000001', result)
        self.assertIn('점수', result)
        self.assertNotIn('본문표식_AAA', result)
        self.assertFalse(self.bridge.auto_memory.settings.get(7, 7).auto_retrieve)
        self.assertIn('ON', await self.command('/knowledge auto on'))
        self.assertTrue(self.bridge.auto_memory.settings.get(7, 7).auto_retrieve)
        self.assertEqual(self.bridge.auto_memory.settings.get(7, 7).capture_mode, 'off')
        self.assertIn('OFF', await self.command('/knowledge auto off'))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.jobs(), [])

    async def test_no_hits_and_greeting_preview(self):
        for text in ('/knowledge test 안녕', '/knowledge test 8DT1'):
            self.assertIn('0개', await self.command(text))
        self.assertIn('인사', await self.command('/knowledge test 감사합니다'))

    async def test_owner_only_even_for_status_and_preview(self):
        for text in ('/knowledge status', '/knowledge auto on', '/knowledge auto off', '/knowledge test 8DT1'):
            for chat, user in ((7,8), (8,7)):
                with self.assertRaises(ValueError):
                    await self.bridge.command(text, chat, user, 100)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM memory_settings').fetchone()[0], 0)

    async def test_group_and_forwarded_commands_cannot_change_settings(self):
        group = update(1, '/knowledge auto on')
        group['message']['chat']['type'] = 'group'
        forwarded = update(2, '/knowledge auto on')
        forwarded['message']['forward_origin'] = {'type':'user'}
        await self.bridge.handle(group)
        await self.bridge.handle(forwarded)
        self.assertFalse(self.bridge.auto_memory.settings.get(7, 7).auto_retrieve)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    async def test_malformed_commands_never_change_preferences(self):
        for text in ('/knowledge auto', '/knowledge auto on extra', '/knowledge auto delete',
                     '/knowledge status extra', '/knowledge test', '/knowledge other'):
            with self.assertRaises(ValueError):
                await self.command(text)
        self.assertFalse(self.bridge.auto_memory.settings.get(7, 7).auto_retrieve)

    async def test_aliases_and_help(self):
        self.assertIn('OFF', await self.command('/knowledge'))
        self.assertIn('OFF', await self.command('/knowledge auto status'))
        self.assertIn('0개', await self.command('/knowledge auto test 8DT1'))
        self.assertIn('/knowledge auto on|off', HELP)

    async def test_replayed_on_message_cannot_undo_later_off(self):
        first = update(1, '/knowledge auto on')
        await self.bridge.handle(first)
        await self.bridge.handle(update(2, '/knowledge auto off'))
        await self.bridge.handle(first)
        await self.bridge.handle({**first, 'update_id':30})
        self.assertFalse(self.bridge.auto_memory.settings.get(7, 7).auto_retrieve)

    async def test_dynamic_memory_report_and_mode_suspension(self):
        self.assertIn('설정 OFF', await self.command('/memory'))
        await self.command('/knowledge auto on')
        self.assertIn('자동 검색합니다', await self.command('/memories'))
        await self.command('/new Fixture')
        await self.command('/mode write')
        self.assertIn('중지', await self.command('/memory'))
        self.assertIn('자동 검색·주입 중지', await self.command('/knowledge status'))
        self.assertIn('현재 권한', await self.command('/knowledge test 8DT1'))
        await self.command('/mode read')
        self.assertIn('자동 검색합니다', await self.command('/memory'))

    async def test_stats_are_owner_scoped_and_not_claimed_as_model_use(self):
        await self.command('/new Fixture')
        await self.command('/save 8DT1 외부 노이즈 차폐', 2)
        await self.command('/knowledge auto on')
        await self.bridge.handle(update(3, '8DT1 외부 노이즈'))
        await self.bridge.work_once()
        result = await self.command('/knowledge status')
        self.assertIn('검색 선택 1건', result)
        self.assertIn('문맥 준비 자료 1개', result)
        self.assertIn('입증하지 않습니다', result)
        self.assertNotIn('차폐', result)

    async def test_off_suppresses_queued_refs_but_does_not_delete_notes_or_history(self):
        await self.command('/new Fixture')
        await self.command('/save 8DT1 외부 노이즈 차폐', 2)
        await self.command('/knowledge auto on')
        await self.bridge.handle(update(3, '8DT1 외부 노이즈'))
        response = await self.command('/knowledge auto off')
        self.assertIn('이미 Codex에 전달된', response)
        await self.bridge.work_once()
        self.assertEqual(self.adapter.extra_contexts, [None])
        self.assertEqual(len(self.bridge.knowledge.search('8DT1', 7, 7)), 1)

    async def test_preview_in_legacy_temporary_access_is_suspended(self):
        import time
        self.bridge.auto_memory.knowledge.config = replace(self.config, temporary_access_until=time.time()+60)
        self.assertIn('중지', await self.command('/knowledge test 8DT1'))
