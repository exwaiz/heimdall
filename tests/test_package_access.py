import asyncio
from pathlib import Path
import tomllib
import unittest
from unittest.mock import AsyncMock

from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.memory import MEMORY_GUIDANCE

ROOT = Path(__file__).resolve().parents[1]


class PackageAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = CodexAdapter(Config.load(ROOT / 'config.toml', live=False))

    def test_narrow_read_rules_all_modes(self):
        for mode in ('readonly', 'workspace', 'network'):
            args = self.adapter.policy_args(mode)
            parsed = tomllib.loads(args[1])['permissions'][self.adapter.profile_name(mode)]
            rules = parsed['filesystem']
            for path in ('/var/log/apt', '/var/log/dpkg.log', '/var/lib/dpkg/status'):
                if Path(path).exists():
                    self.assertEqual(rules[path], 'read')
            if mode != 'network':
                self.assertNotIn('/var', rules)
                self.assertNotIn('/var/log', rules)
                self.assertFalse(parsed['network']['enabled'])
            self.assertEqual(rules[str(self.adapter.config.codex_home / 'auth.json')], 'deny')

    async def test_readonly_automatic_denial_is_not_owner_refusal(self):
        adapter = self.adapter
        adapter.process = type('Process', (), {'returncode': None})()
        adapter.approval_handler = AsyncMock(return_value={'decision': 'decline'})
        adapter.send = AsyncMock()
        adapter.thread_modes['fixture'] = 'readonly'
        adapter.subscribers['fixture'] = asyncio.Queue()
        await adapter.handle_approval({'id': 1, 'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 'fixture'}}, adapter.connection_id)
        adapter.send.assert_awaited_once_with({'id': 1, 'result': {'decision': 'decline'}})
        event = adapter.subscribers['fixture'].get_nowait()
        self.assertEqual(event['params']['reason'], 'readonly_policy')
        self.assertIn('사용자가 거부한 것이 아닙니다', adapter.denial_notice(event['params']['reason']))

    async def test_unsupported_permissions_stay_denied(self):
        self.adapter.send = AsyncMock()
        self.adapter.subscribers['fixture'] = asyncio.Queue()
        await self.adapter.deny_request({'id': 2, 'method': 'item/permissions/requestApproval',
                                        'params': {'threadId': 'fixture'}})
        self.adapter.send.assert_awaited_once_with({'id': 2, 'result': {'permissions': {}, 'scope': 'turn'}})
        self.assertEqual(self.adapter.subscribers['fixture'].get_nowait()['params']['reason'], 'unsupported_request')

    def test_context_explains_host_and_no_escalation(self):
        for fragment in ('Pi host', '/var/log/apt', '/var/lib/dpkg/status', 'not an actual user'):
            self.assertIn(fragment, MEMORY_GUIDANCE)
