import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import AsyncMock, patch

from heimdall_bridge.__main__ import update
from heimdall_bridge.codex import CodexAdapter, RpcRejected, TurnUncertain
import test_phase2
import test_handoff


class NetworkModeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_phase2.Phase2Tests.asyncSetUp
    asyncTearDown = test_phase2.Phase2Tests.asyncTearDown
    request = test_phase2.Phase2Tests.request
    launch = test_phase2.Phase2Tests.launch
    callback = test_phase2.Phase2Tests.callback

    async def test_owner_only_network_command_and_duplicate_update_does_not_extend(self):
        with self.assertRaises(ValueError):
            await self.bridge.command('/mode network', 7, 8, 2)
        item = update(2, '/mode network')
        await self.bridge.handle(item)
        deadline = self.store.network_deadline(7)
        self.assertLessEqual(deadline - time.time(), 1800)
        self.assertGreater(deadline - time.time(), 1798)
        await self.bridge.handle({**item, 'update_id':3})
        self.assertEqual(deadline, self.store.network_deadline(7))
        self.assertEqual(self.store.permission_mode(7), 'network')
        self.assertIn('30분', await self.bridge.command('/mode', 7, 7))

    async def test_queue_permission_frozen_and_expired_grant_never_dispatches(self):
        await self.bridge.handle(update(2, 'normal before network'))
        await self.bridge.command('/mode network', 7, 7)
        await self.bridge.handle(update(3, 'network question'))
        normal, network = self.store.job(1), self.store.job(2)
        self.assertEqual(normal['permission_mode'], 'readonly')
        self.assertEqual(normal['network_until'], 0)
        self.assertEqual(network['permission_mode'], 'network')
        self.assertGreater(network['network_until'], time.time())
        self.store.conn.execute("UPDATE jobs SET state='cancelled' WHERE job_id=1")
        with patch('time.time', return_value=network['network_until'] + 1):
            await self.bridge.work_once()
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.store.job(2)['error_code'], 'network_window_expired')
        self.assertEqual(self.store.permission_mode(7), 'readonly')

    async def test_read_revokes_network_queue_but_does_not_cancel_normal_queue(self):
        await self.bridge.handle(update(2, 'normal'))
        await self.bridge.command('/mode network', 7, 7)
        await self.bridge.handle(update(3, 'network'))
        await self.bridge.command('/mode read', 7, 7)
        self.assertEqual(self.store.job(1)['state'], 'queued')
        self.assertEqual(self.store.job(2)['state'], 'cancelled')
        self.assertEqual(self.store.network_deadline(7), 0)

    async def test_binding_selection_resets_network_mode_and_deadline(self):
        await self.bridge.command('/mode network', 7, 7)
        await self.bridge.handle(update(2, '/new second'))
        self.assertEqual(self.store.permission_mode(7), 'readonly')
        self.assertEqual(self.store.network_deadline(7), 0)

    async def test_expiry_notice_idempotent_and_status_reports_network(self):
        await self.bridge.command('/mode network', 7, 7)
        self.assertIn('Network mode: ON', await self.bridge.command('/status', 7, 7))
        deadline = self.store.network_deadline(7)
        with patch('time.time', return_value=deadline + 1):
            self.bridge.expire_network_mode()
            self.bridge.expire_network_mode()
        self.assertEqual(self.store.permission_mode(7), 'readonly')
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM outbox WHERE dedupe LIKE 'network-expired:%'").fetchone()[0], 1)

    async def test_network_job_approval_still_requires_live_owner_click(self):
        message = await self.request()
        deadline = time.time() + 60
        self.store.conn.execute("UPDATE jobs SET permission_mode='network',network_until=?", (deadline,))
        task = await self.launch(message)
        self.assertFalse(task.done())
        await self.bridge.approvals.callback(self.callback())
        self.assertEqual(await task, {'decision':'accept'})

    async def test_late_network_approval_is_denied(self):
        message = await self.request()
        self.store.conn.execute("UPDATE jobs SET permission_mode='network',network_until=?", (time.time()-1,))
        self.assertEqual(await self.bridge.approvals.handle(message,'c',{}), {'decision':'decline'})

    def test_network_profile_does_not_broaden_default_profile(self):
        config = replace(self.config, enable_network_profile=True)
        adapter = CodexAdapter(config)
        self.assertIn('network={enabled=false}', ' '.join(adapter.policy_args('readonly')))
        self.assertIn('network={enabled=true}', ' '.join(adapter.policy_args('network')))
        self.assertIn('permissions.heimdall_network=', ' '.join(adapter.policy_args('network')))
        self.assertEqual(adapter.approval_policy('network'), 'untrusted')


class NetworkOwnerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_handoff.HandoffTests.asyncSetUp
    asyncTearDown = test_handoff.HandoffTests.asyncTearDown

    async def test_deadline_stops_owner_and_preserves_uncertain_not_replay(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        async def running(thread_id, prompt, started, progress, **kwargs):
            started('fixture-turn')
            await asyncio.Event().wait()
        owner.run_turn = running
        with self.assertRaisesRegex(TurnUncertain, 'network_window_expired'):
            await self.adapter.run_turn(thread['id'], 'probe', lambda _:None, AsyncMock(),
                permission_mode='network', network_until=time.time()+.02)
        owner.close.assert_awaited_once()
        self.assertIn('fixture-turn', owner.interrupted)
        self.assertNotIn(thread['id'], self.adapter.owners)

    async def test_explicit_revocation_stops_only_network_owner(self):
        thread = await self.adapter.new_thread()
        other = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        entered = asyncio.Event()
        async def running(thread_id, prompt, started, progress, **kwargs):
            started('fixture-turn'); entered.set(); await asyncio.Event().wait()
        owner.run_turn = running
        task = asyncio.create_task(self.adapter.run_turn(thread['id'],'probe',lambda _:None,AsyncMock(),
            permission_mode='network',network_until=time.time()+60))
        await entered.wait()
        self.assertTrue(await self.adapter.revoke_network())
        with self.assertRaisesRegex(TurnUncertain, 'network_mode_revoked'): await task
        self.adapter.owners[other['id']].close.assert_not_awaited()

    async def test_uncertain_network_owner_is_not_left_with_unwatched_access(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        owner.run_turn = AsyncMock(side_effect=TurnUncertain('unknown'))
        with self.assertRaises(TurnUncertain):
            await self.adapter.run_turn(thread['id'],'probe',lambda _:None,AsyncMock(),
                permission_mode='network',network_until=time.time()+60)
        owner.close.assert_awaited_once()

    async def test_expired_or_unbounded_grant_is_rejected_before_start(self):
        thread = await self.adapter.new_thread()
        owner = self.adapter.owners[thread['id']]
        owner.run_turn = AsyncMock()
        for deadline in (time.time()-1, time.time()+3600, float('nan')):
            with self.assertRaises(RpcRejected):
                await self.adapter.run_turn(thread['id'],'probe',lambda _:None,AsyncMock(),
                    permission_mode='network',network_until=deadline)
        owner.run_turn.assert_not_awaited()
