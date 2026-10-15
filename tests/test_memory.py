import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.codex import CodexAdapter, RpcRejected, ConnectionLost
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.memory import KEYS, MEMORY_GUIDANCE, sanitize_memory_config
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-memory-")
        self.config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(self.temp.name), chat_id=7, user_id=7)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.bridge = Bridge(self.config, self.store, self.adapter, FakeTelegramTransport(), conflict_check=lambda *_: False)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_allowlist_only_exact_booleans_no_secrets(self):
        result = sanitize_memory_config({"config": {"api_key": "SECRET",
            "features": {"memories": True, "other": "SECRET"},
            "memories": {"use_memories": False, "generate_memories": "SECRET",
                         "disable_on_external_context": 1, "extra": "SECRET"}},
            "origins": {"SECRET": "SECRET"}})
        self.assertEqual(result, dict(zip(KEYS, [True, False, None, None])))
        self.assertNotIn("SECRET", str(result))
        for invalid in (None, [], {}, {"config": None}, {"config": {"features": []}}):
            self.assertTrue(all(v is None for v in sanitize_memory_config(invalid).values()))

    async def test_rpc_returns_only_allowlisted_config_without_layers(self):
        adapter = CodexAdapter(self.config)
        adapter.request = AsyncMock(return_value={"config": {"features": {"memories": True}, "token": "SECRET"}})
        result = await adapter.memory_config()
        adapter.request.assert_awaited_once_with("config/read", {"includeLayers": False, "cwd": str(self.config.cwd)})
        self.assertIs(result["features.memories"], True)
        self.assertNotIn("SECRET", str(result))

    async def test_rpc_failure_is_unknown_not_disabled(self):
        adapter = CodexAdapter(self.config)
        for error in (RpcRejected("SECRET"), ConnectionLost("SECRET"), asyncio.TimeoutError()):
            adapter.request = AsyncMock(side_effect=error)
            result = await adapter.memory_config()
            self.assertEqual(set(result), set(KEYS))
            self.assertTrue(all(v is None for v in result.values()))

    async def test_commands_distinguish_memory_without_model_job_or_binding(self):
        for cmd in ("/memory", "/memories"):
            report = await self.bridge.command(cmd, 7, 7)
            for text in ("현재 스레드 대화", "Codex 자체 로컬 메모리", "Heimdall 지식 저장소",
                         "ChatGPT", "미확인은 꺼짐이 아닙니다", "자동 검색하지 않습니다", "조회 전용"):
                self.assertIn(text, report)
            self.assertIn("선택: 없음", report)
        self.assertFalse(self.adapter.calls)
        self.assertFalse(self.store.jobs())
        self.assertIsNone(self.store.binding(7))

    async def test_commands_owner_only_and_no_setting_mutation(self):
        for chat, user in ((8, 7), (7, 8)):
            with self.assertRaises(ValueError):
                await self.bridge.command("/memory", chat, user)
        for cmd in ("/memory on", "/memories off", "/memories reset"):
            with self.assertRaises(ValueError):
                await self.bridge.command(cmd, 7, 7)
        for i, text in enumerate(("/memory", "/memories"), 10):
            await self.bridge.handle(update(i, text, owner=8))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    async def test_knowledge_count_not_codex_memory_and_deleted_excluded(self):
        await self.bridge.handle(update(1, "/new A"))
        await self.bridge.handle(update(2, "/save 저장한 자료"))
        report = await self.bridge.command("/memory", 7, 7)
        self.assertIn("fake-thread-1", report)
        self.assertIn("검색 가능한 자료 1개", report)
        await self.bridge.command("/forget HK-000001", 7, 7)
        self.assertIn("검색 가능한 자료 0개", await self.bridge.command("/memory", 7, 7))

    async def test_normal_turn_keeps_original_text_and_separate_policy_context(self):
        adapter = CodexAdapter(self.config)
        adapter.new_threads.add("fixture")
        calls = []
        async def request(method, params):
            calls.append((method, params))
            adapter.validators[method].validate(params)
            if method == "config/read":
                return {"config": {"features": {"memories": True}, "auth": "SECRET"}}
            adapter.subscribers["fixture"].put_nowait({"method": "turn/completed", "params": {
                "threadId": "fixture", "turn": {"id": "t", "status": "completed", "items": []}}})
            return {"turn": {"id": "t"}}
        adapter.request = request
        text = "  메모리 켜져 있어?\n그대로  "
        await adapter.run_turn("fixture", text, lambda _: None, AsyncMock())
        turn = calls[-1][1]
        self.assertEqual(turn["input"][0], {"type": "text", "text": text})
        context = turn["input"][1]["text"]
        self.assertTrue(context.startswith(MEMORY_GUIDANCE))
        self.assertIn("features.memories: 설정상 켜짐", context)
        self.assertIn("NOT automatically", context)
        self.assertNotIn("SECRET", context)
        self.assertEqual(turn["permissions"], "heimdall_readonly")

    def test_protected_config_remains_denied(self):
        for mode in ("readonly", "workspace"):
            args = " ".join(CodexAdapter(self.config).policy_args(mode))
            for path in ("config.toml", "auth.json", "sessions"):
                self.assertIn(f'"{self.config.codex_home / path}"="deny"', args)
