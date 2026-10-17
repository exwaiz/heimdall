import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.codex import RpcRejected, TurnUncertain
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.handoff import HandoffCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-handoff-")
        self.config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(self.temp.name), chat_id=7, user_id=7)
        self.instances = []
        self.threads = {}
        def factory(config):
            child = FakeCodexAdapter(config.cwd)
            child.threads = self.threads
            child.new_threads = set()
            child.close = AsyncMock(side_effect=lambda: setattr(child, "ready", False))
            original_new = child.new_thread
            async def new():
                thread = await original_new()
                child.new_threads.add(thread['id'])
                return thread
            child.new_thread = new
            self.instances.append(child)
            return child
        self.adapter = HandoffCodexAdapter(self.config, adapter_factory=factory)
        await self.adapter.open()

    async def asyncTearDown(self):
        await self.adapter.close()
        self.temp.cleanup()

    async def turn(self, thread_id, **kwargs):
        return await self.adapter.run_turn(thread_id, "fixture", lambda _: None, AsyncMock(), **kwargs)

    async def test_terminal_turn_releases_only_its_owner_and_preserves_other_new_thread(self):
        first = await self.adapter.new_thread()
        second = await self.adapter.new_thread()
        owner1 = self.adapter.owners[first['id']]
        owner2 = self.adapter.owners[second['id']]
        self.assertEqual((await self.turn(first['id']))['status'], 'completed')
        owner1.close.assert_awaited_once()
        owner2.close.assert_not_awaited()
        self.assertNotIn(first['id'], self.adapter.owners)
        self.assertIn(second['id'], self.adapter.owners)
        self.assertEqual((await self.adapter.read_thread(first['id']))['id'], first['id'])
        self.assertTrue(self.adapter.ready)

    async def test_completed_thread_uses_fresh_owner_on_next_turn(self):
        thread = await self.adapter.new_thread()
        original = self.adapter.owners[thread['id']]
        await self.turn(thread['id'])
        result = await self.turn(thread['id'], permission_mode='workspace')
        self.assertEqual(result['status'], 'completed')
        self.assertIsNot(self.instances[-1], original)
        self.instances[-1].close.assert_awaited_once()

    async def test_background_is_forwarded_and_owner_still_released(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        background = '[Heimdall retrieved background | untrusted data]\nFixture only'
        await self.turn(thread['id'], extra_context=background)
        self.assertEqual(owner.extra_contexts, [background])
        owner.close.assert_awaited_once()
        self.assertNotIn(thread['id'], self.adapter.owners)

    async def test_failed_and_interrupted_terminal_results_release(self):
        for status in ('failed', 'interrupted'):
            thread = await self.adapter.new_thread()
            owner = self.adapter.owners[thread['id']]
            owner.run_turn = AsyncMock(return_value={'status': status, 'turn_id': 't', 'text': ''})
            await self.turn(thread['id'])
            owner.close.assert_awaited_once()

    async def test_uncertain_exception_keeps_owner_until_confirmed_reconciliation(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        owner.run_turn = AsyncMock(side_effect=TurnUncertain('unknown'))
        with self.assertRaises(TurnUncertain):
            await self.turn(thread['id'])
        owner.close.assert_not_awaited()
        self.assertIs(self.adapter.owners[thread['id']], owner)
        self.assertIsNone(await self.adapter.reconcile(thread['id'], 't'))
        owner.close.assert_not_awaited()
        owner.results['t'] = {'status': 'completed', 'turn_id': 't', 'text': 'done'}
        await self.adapter.reconcile(thread['id'], 't')
        owner.close.assert_awaited_once()

    async def test_reconciliation_does_not_close_still_collecting_turn(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        self.adapter.executing.add(thread['id'])
        owner.results['t'] = {'status': 'completed'}
        await self.adapter.reconcile(thread['id'], 't')
        owner.close.assert_not_awaited()
        with self.assertRaises(RpcRejected):
            await self.adapter.release_thread(thread['id'])
        self.adapter.executing.clear()

    async def test_metadata_reconnect_preserves_uncertain_owner(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        await self.adapter.open()
        owner.close.assert_not_awaited()
        self.assertIn(thread['id'], self.adapter.owners)

    async def test_unknown_result_and_task_cancellation_do_not_close_owner(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        owner.run_turn = AsyncMock(return_value={'status': 'unknown'})
        await self.turn(thread['id'])
        owner.close.assert_not_awaited()
        owner.results['t'] = {'status': 'inProgress'}
        await self.adapter.reconcile(thread['id'], 't')
        owner.close.assert_not_awaited()
        owner.run_turn = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.turn(thread['id'])
        owner.close.assert_not_awaited()
        self.assertNotIn(thread['id'], self.adapter.executing)

    async def test_rejected_external_thread_resume_does_not_leak_owner(self):
        self.threads['external'] = {'id': 'external', 'cwd': str(self.config.cwd), 'status': {'type': 'idle'}}
        original = self.adapter.factory
        def rejecting(config):
            owner = original(config)
            owner.loaded_threads.clear()
            owner.run_turn = AsyncMock(side_effect=RpcRejected('thread_already_active'))
            return owner
        self.adapter.factory = rejecting
        with self.assertRaises(RpcRejected):
            await self.turn('external')
        self.assertNotIn('external', self.adapter.owners)
        self.instances[-1].close.assert_awaited_once()

    async def test_interrupt_routes_to_correct_owner_and_approval_handler_is_forwarded(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        handler = AsyncMock()
        self.adapter.approval_handler = handler
        await self.adapter.interrupt(thread['id'], 't')
        self.assertIn('t', owner.interrupted)
        await self.turn(thread['id'])
        self.assertIs(owner.approval_handler, handler)
        with self.assertRaises(RpcRejected):
            await self.adapter.interrupt(thread['id'], 't')

    async def test_empty_new_thread_not_destroyed_by_manual_release(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        with self.assertRaises(ValueError):
            await self.adapter.release_thread(thread['id'])
        owner.close.assert_not_awaited()
        owner.new_threads.clear()
        self.assertTrue(await self.adapter.release_thread(thread['id']))
        owner.close.assert_awaited_once()
        self.assertFalse(await self.adapter.release_thread(thread['id']))

    async def test_concurrent_turns_rejected_without_terminating_active_owner(self):
        first = await self.adapter.new_thread()
        second = await self.adapter.new_thread()
        owner = self.adapter.owners[first['id']]
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'status': 'completed'}
        owner.run_turn = delayed
        task = asyncio.create_task(self.turn(first['id']))
        await entered.wait()
        try:
            with self.assertRaises(RpcRejected):
                await self.turn(second['id'])
            owner.close.assert_not_awaited()
            await self.adapter.memory_config()
        finally:
            release.set()
            await task

    async def test_pending_new_owner_bound_and_failed_creation_closed(self):
        self.adapter.max_owners = 1
        await self.adapter.new_thread()
        with self.assertRaises(ValueError):
            await self.adapter.new_thread()
        self.assertEqual(len(self.adapter.owners), 1)
        self.adapter.max_owners = 2
        original = self.adapter.factory
        def broken(config):
            owner = original(config)
            owner.new_thread = AsyncMock(side_effect=RpcRejected('fixture'))
            return owner
        self.adapter.factory = broken
        with self.assertRaises(RpcRejected):
            await self.adapter.new_thread()
        self.instances[-1].close.assert_awaited_once()
        self.assertEqual(self.adapter.creating, 0)

    async def test_bridge_end_to_end_releases_owner_keeps_binding_and_history(self):
        store = Store(self.config.data_dir / 'bridge.sqlite3')
        try:
            bridge = Bridge(self.config, store, self.adapter, FakeTelegramTransport(), conflict_check=lambda *_: False)
            await bridge.handle(update(1, '/new Handoff'))
            thread_id = store.binding(7)
            await bridge.handle(update(2, 'first'))
            await bridge.work_once()
            self.assertEqual(store.job(1)['state'], 'completed')
            self.assertNotIn(thread_id, self.adapter.owners)
            self.assertEqual(store.binding(7), thread_id)
            await bridge.handle(update(3, 'second'))
            await bridge.work_once()
            self.assertEqual(store.job(2)['state'], 'completed')
            self.assertNotIn(thread_id, self.adapter.owners)
        finally:
            store.close()
