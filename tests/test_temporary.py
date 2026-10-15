import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from heimdall_bridge.config import Config, ConfigError
from heimdall_bridge.codex import CodexAdapter, RpcRejected
from heimdall_bridge.temporary import expiry_guard, TemporaryAccessExpired
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport
import test_phase2

ROOT = Path(__file__).resolve().parents[1]


class TemporaryTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return replace(Config.load(ROOT / 'config.toml', live=False), temporary_access_until=time.time() + 600)

    def test_policy_opens_normal_mode_network_and_general_files_not_secrets_or_code(self):
        config = self.config()
        for mode in ('readonly', 'workspace'):
            args = ' '.join(CodexAdapter(config).policy_args(mode))
            expected = 'heimdall_readonly' if mode == 'readonly' else 'heimdall_workspace'
            self.assertIn(f'permissions.{expected}=', args)
            for fragment in ('network={enabled=true}', f'"{config.cwd}"="write"', '"/tmp"="write"',
                             f'"{config.codex_home / "auth.json"}"="deny"',
                             f'"{config.data_dir}"="deny"', f'"{config.schema_dir.parent}"="read"',
                             f'"{config.write_roots[0]}"="write"'):
                self.assertIn(fragment, args)
            self.assertNotIn('danger-full-access', args)
        self.assertTrue(config.writable(config.cwd / 'ordinary.png'))
        self.assertTrue(config.writable('/tmp/ordinary'))
        self.assertTrue(config.writable(config.write_roots[0] / 'ordinary.png'))
        for path in (config.codex_home / 'auth.json', config.data_dir / 'bridge.sqlite3',
                     config.schema_dir.parent / 'src/test.py', Path.home() / '.env', Path('/etc/hosts')):
            self.assertFalse(config.writable(path))

    def test_expiry_reverts_policy_and_denies_stale_adapter_turn_start(self):
        config = self.config()
        adapter = CodexAdapter(config)
        self.assertEqual(adapter.approval_policy('readonly'), 'untrusted')
        with patch('time.time', return_value=config.temporary_access_until + 1):
            self.assertIn('network={enabled=false}', ' '.join(adapter.policy_args()))
            self.assertFalse(config.writable(config.cwd / 'ordinary.png'))
            with self.assertRaises(RpcRejected): adapter.check_temporary_expiry()

    def test_browser_environment_does_not_use_protected_config(self):
        config = self.config()
        env = CodexAdapter(config).child_env()
        self.assertEqual(env['TMPDIR'], str(config.write_roots[0]))
        self.assertEqual(env['XDG_CONFIG_HOME'], str(config.write_roots[0] / 'browser-config'))
        self.assertEqual(env['XDG_CACHE_HOME'], str(config.write_roots[0] / 'browser-cache'))

    async def test_guard_uses_remaining_time_and_requests_restart(self):
        callback = unittest.mock.Mock()
        with patch('heimdall_bridge.temporary.time.time', return_value=100), patch('heimdall_bridge.temporary.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(TemporaryAccessExpired):
                await expiry_guard(120, callback)
            sleep.assert_awaited_once_with(20)
            callback.assert_called_once_with()

    async def test_guard_cancellation_does_not_claim_expiry(self):
        callback = unittest.mock.Mock()
        with patch('heimdall_bridge.temporary.asyncio.sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError): await expiry_guard(time.time() + 60, callback)
        callback.assert_not_called()

    def test_config_fixed_deadline_expired_and_future_limit(self):
        text = re.sub(r'^temporary_access_until\s*=.*\n', '', (ROOT / 'config.toml').read_text(), flags=re.M)
        env = {'TELEGRAM_ALLOWED_USER_ID':'7','TELEGRAM_ALLOWED_CHAT_ID':'7',
               'TELEGRAM_BOT_TOKEN':'123456:FIXTURE_NOT_A_REAL_SECRET_TOKEN'}
        with tempfile.TemporaryDirectory(prefix='heimdall-temporary-config-', dir=ROOT) as directory:
            path = Path(directory) / 'config.toml'
            for delta, valid in ((60, True), (-1, True), (1801, False)):
                expiry = datetime.fromtimestamp(time.time() + delta, timezone.utc).isoformat()
                path.write_text(text.replace('[bridge]', '[bridge]\ntemporary_access_until = "' + expiry + '"'))
                with patch.dict(os.environ, env):
                    if valid:
                        cfg = Config.load(path)
                        self.assertEqual(cfg.temporary_active(), delta > 0)
                        self.assertEqual(Config.load(path, live=False).temporary_access_until, 0)
                    else:
                        with self.assertRaises(ConfigError): Config.load(path)

    async def test_service_expiry_closes_adapter_and_leaves_notification_for_restart(self):
        from heimdall_bridge.__main__ import run
        class Transport(FakeTelegramTransport):
            async def initialize(self): pass
            async def updates(self, offset): await asyncio.Event().wait()
        with tempfile.TemporaryDirectory(prefix='heimdall-expiry-service-') as directory:
            config = replace(self.config(), data_dir=Path(directory), user_id=7, chat_id=7,
                temporary_access_until=time.time() + .1)
            adapter = FakeCodexAdapter(config.cwd)
            with patch('heimdall_bridge.__main__.HandoffCodexAdapter', return_value=adapter), \
                 patch('heimdall_bridge.__main__.TelegramTransport', return_value=Transport()), \
                 patch.object(asyncio.get_running_loop(), 'add_signal_handler'):
                with self.assertRaises(TemporaryAccessExpired):
                    await asyncio.wait_for(run(config), 3)
            self.assertFalse(adapter.ready)
            with closing(sqlite3.connect(config.data_dir / 'bridge.sqlite3')) as db:
                self.assertIsNotNone(db.execute("SELECT 1 FROM outbox WHERE dedupe LIKE 'temporary-expired:%'").fetchone())


class TemporaryApprovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_phase2.Phase2Tests.asyncSetUp
    asyncTearDown = test_phase2.Phase2Tests.asyncTearDown
    request = test_phase2.Phase2Tests.request
    launch = test_phase2.Phase2Tests.launch
    callback = test_phase2.Phase2Tests.callback

    async def test_normal_mode_still_requires_owner_click_in_temporary_window(self):
        message = await self.request()
        self.store.conn.execute("UPDATE jobs SET permission_mode='readonly'")
        config = replace(self.config, temporary_access_until=time.time() + 60)
        self.bridge.approvals.config = config
        task = await self.launch(message)
        self.assertFalse(task.done())
        await self.bridge.approvals.callback(self.callback())
        self.assertEqual(await task, {'decision': 'accept'})

    async def test_expired_window_rejects_readonly_job_approval(self):
        message = await self.request()
        self.store.conn.execute("UPDATE jobs SET permission_mode='readonly'")
        self.bridge.approvals.config = replace(self.config, temporary_access_until=time.time() - 1)
        self.assertEqual(await self.bridge.approvals.handle(message, 'expired', {}), {'decision':'decline'})
