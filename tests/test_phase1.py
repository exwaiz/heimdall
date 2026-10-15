import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge, external_cli_busy
from heimdall_bridge.codex import CodexAdapter, final_text
from heimdall_bridge.config import Config, ConfigError
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.transport import (DeliveryUncertain, FakeTelegramTransport,
    TelegramTransport, chunks, deliver, sanitize)

ROOT = Path(__file__).resolve().parents[1]


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="heimdall-tests-")
        self.config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(self.directory.name), user_id=7, chat_id=7)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.transport = FakeTelegramTransport()
        self.bridge = Bridge(self.config, self.store, self.adapter, self.transport, conflict_check=lambda *_: False)
        await self.bridge.handle(update(1, "/new A"))

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_prompt_exactly_once_and_fifo(self):
        first = update(2, "  한글\n코드 `x` 그대로  ")
        await self.bridge.handle(first)
        await self.bridge.handle(first)
        await self.bridge.handle({**first, "update_id": 20})
        await self.bridge.handle(update(3, "두 번째"))
        while await self.bridge.work_once():
            pass
        self.assertEqual(self.adapter.calls, [("fake-thread-1", first["message"]["text"]), ("fake-thread-1", "두 번째")])
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT state FROM jobs")], ["completed", "completed"])

    async def test_selection_does_not_retarget_queue(self):
        await self.bridge.handle(update(2, "A에서 실행"))
        await self.bridge.handle(update(3, "/new B"))
        await self.bridge.handle(update(4, "B에서 실행"))
        while await self.bridge.work_once():
            pass
        self.assertEqual([c[0] for c in self.adapter.calls], ["fake-thread-1", "fake-thread-2"])

    async def test_unauthorized_group_forwarded_are_silent(self):
        for number, change in enumerate(("user", "group", "forward", "anonymous"), 10):
            item = update(number, "/database")
            if change == "user":
                item["message"]["from"]["id"] = 8
            elif change == "group":
                item["message"]["chat"]["type"] = "group"
            elif change == "forward":
                item["message"]["forward_origin"] = {}
            else:
                item["message"].pop("from")
            await self.bridge.handle(item)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    async def test_unknown_command_and_bot_suffix(self):
        await self.bridge.handle(update(2, "/exec rm -rf"))
        await self.bridge.handle(update(3, "/help@HeimdallTestBot"))
        await self.bridge.handle(update(4, "/new@OtherBot"))
        self.assertEqual(len(self.adapter.threads), 1)
        self.assertFalse(self.store.jobs())
        self.assertIn("알 수 없는", self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='update:2'").fetchone()[0])

    async def test_duplicate_command_message_id_does_not_create_twice(self):
        item = update(2, "/new B")
        await self.bridge.handle(item)
        await self.bridge.handle({**item, "update_id": 20})
        self.assertEqual(len(self.adapter.threads), 2)

    async def test_prefix_ambiguity_and_exact_selection(self):
        await self.bridge.handle(update(2, "/new B"))
        await self.bridge.handle(update(3, "/select fake-thread-"))
        self.assertEqual(self.store.binding(7), "fake-thread-2")
        await self.bridge.handle(update(4, "/select fake-thread-1"))
        self.assertEqual(self.store.binding(7), "fake-thread-1")

    async def test_page_two_and_missing_page(self):
        for _ in range(21):
            await self.adapter.new_thread()
        self.bridge.registry.cache.clear()
        page = await self.bridge.registry.page(2)
        self.assertEqual(len(page["data"]), 2)
        with self.assertRaises(ValueError):
            await self.bridge.registry.page(3)

    async def test_unique_prefix(self):
        thread = await self.bridge.registry.resolve("fake-thread-")
        self.assertEqual(thread["id"], "fake-thread-1")
        with self.assertRaises(ValueError):
            await self.bridge.registry.resolve("does-not-exist")

    async def test_queue_limit(self):
        self.bridge.config = replace(self.config, max_queue=1)
        await self.bridge.handle(update(2, "1"))
        await self.bridge.handle(update(3, "2"))
        self.assertEqual(len(self.store.jobs()), 1)

    async def test_cancel_queued(self):
        await self.bridge.handle(update(2, "대기"))
        await self.bridge.handle(update(3, "/cancel"))
        self.assertEqual(self.store.job(1)["state"], "cancelled")
        self.assertFalse(await self.bridge.work_once())

    async def test_cancel_running_waits_for_turn_end(self):
        self.adapter.delay = 0.1
        await self.bridge.handle(update(2, "실행"))
        task = asyncio.create_task(self.bridge.work_once())
        await asyncio.sleep(0.01)
        await self.bridge.handle(update(3, "/cancel"))
        self.assertEqual(self.store.job(1)["state"], "running")
        await task
        self.assertEqual(self.store.job(1)["state"], "cancelled")

    async def test_failed_turn_not_success(self):
        self.adapter.fail = True
        await self.bridge.handle(update(2, "실패"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertNotIn("✅", self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='job:1:final'").fetchone()[0])

    async def test_disconnect_is_uncertain_no_retry(self):
        self.adapter.disconnected = True
        await self.bridge.handle(update(2, "실행"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["state"], "uncertain")
        self.assertFalse(await self.bridge.work_once())
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_external_change_stops_dispatch(self):
        await self.bridge.handle(update(2, "실행"))
        self.adapter.threads["fake-thread-1"]["updatedAt"] += 1
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["error_code"], "thread_changed_before_dispatch")
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertEqual(self.adapter.calls, [])

    async def test_known_preflight_block_can_be_resolved_without_retry(self):
        await self.bridge.handle(update(2, "실행"))
        self.store.claim()
        self.store.transition(1, "uncertain", error_code="external_activity_suspected")
        self.assertEqual(self.store.resolve_known_preflight_blocks(), [1])
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(await self.bridge.work_once())

    async def test_same_thread_conflict_refuses_before_execution_without_poisoning_thread(self):
        self.bridge.conflict_check = lambda *_: True
        await self.bridge.handle(update(2, "blocked"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertEqual(self.store.job(1)["error_code"], "selected_thread_in_use")
        self.assertEqual(self.adapter.calls, [])
        self.bridge.conflict_check = lambda *_: False
        await self.bridge.handle(update(3, "allowed"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(2)["state"], "completed")

    async def test_external_in_progress_turn_is_refused(self):
        async def active(_):
            return True
        self.adapter.thread_is_active = active
        await self.bridge.handle(update(2, "blocked"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["error_code"], "thread_active")
        self.assertEqual(self.store.job(1)["state"], "failed")
        self.assertEqual(self.adapter.calls, [])

    async def test_resume_ownership_rejection_explains_without_poisoning_thread(self):
        from heimdall_bridge.codex import RpcRejected
        async def busy(*args):
            raise RpcRejected("thread_already_active")
        self.adapter.run_turn = busy
        await self.bridge.handle(update(2, "blocked"))
        await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["state"], "failed")
        body = self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='job:1:final'").fetchone()[0]
        self.assertIn("다른 Codex 연결", body)
        self.assertIn("/new", body)

    async def test_thread_changed_during_activity_check_is_refused(self):
        from unittest.mock import patch
        await self.bridge.handle(update(2, "blocked"))
        with patch("heimdall_bridge.bridge.snapshot", side_effect=[self.store.job(1)["baseline"], "changed"]):
            await self.bridge.work_once()
        self.assertEqual(self.store.job(1)["error_code"], "thread_changed_before_dispatch")
        self.assertEqual(self.adapter.calls, [])

    async def test_unknown_dispatch_with_turn_id_is_not_resolved(self):
        await self.bridge.handle(update(2, "실행"))
        self.store.claim()
        self.store.transition(1, "running", codex_turn_id="actual-turn")
        self.store.transition(1, "uncertain", error_code="external_activity_suspected")
        self.assertEqual(self.store.resolve_known_preflight_blocks(), [])
        self.assertEqual(self.store.job(1)["state"], "uncertain")

    async def test_restart_restores_selection_but_not_running(self):
        await self.bridge.handle(update(2, "실행"))
        self.store.claim()
        self.store.close()
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.store.recover()
        self.assertEqual(self.store.binding(7), "fake-thread-1")
        self.assertEqual(self.store.job(1)["state"], "uncertain")
        self.assertIsNone(self.store.claim())

    async def test_reconciliation_uses_matching_turn_only(self):
        await self.bridge.handle(update(2, "실행"))
        job = self.store.claim()
        self.store.transition(job["job_id"], "running", codex_turn_id="known-turn")
        self.store.recover()
        self.adapter.results["known-turn"] = {"status": "completed", "text": "복구 답변"}
        await self.bridge.reconcile()
        self.assertEqual(self.store.job(1)["state"], "completed")
        self.assertEqual(self.adapter.calls, [])

    async def test_outbox_no_replay_on_ambiguous_send(self):
        class Uncertain(FakeTelegramTransport):
            async def send(self, chat, text):
                self.messages.append((chat, text))
                raise DeliveryUncertain()
        transport = Uncertain()
        await deliver(self.store, transport)
        await deliver(self.store, transport)
        self.assertEqual(len(transport.messages), 1)
        self.store.recover()
        self.assertEqual(self.store.conn.execute("SELECT state FROM outbox").fetchone()[0], "uncertain")

    async def test_invalid_transition_and_atomic_update_rollback(self):
        await self.bridge.handle(update(2, "실행"))
        with self.assertRaises(ValueError):
            self.store.transition(1, "completed")
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.accept_update(99)
                raise RuntimeError()
        self.assertIsNone(self.store.conn.execute("SELECT * FROM telegram_updates WHERE update_id=99").fetchone())

    async def test_migration_upgrade_preserves_outbox(self):
        self.store.reply(7, "preserve", "preserve")
        self.store.conn.execute("ALTER TABLE outbox DROP COLUMN parent_id")
        self.store.close()
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.assertEqual(self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='preserve'").fetchone()[0], "preserve")
        self.assertIn("parent_id", {r[1] for r in self.store.conn.execute("PRAGMA table_info(outbox)")})

    async def test_long_answer_persisted_and_split(self):
        text = "한글🙂\n```python\n" * 1000
        self.store.reply(7, text, "long")
        await deliver(self.store, self.transport)
        await deliver(self.store, self.transport)
        pieces = [t for _, t in self.transport.messages if t.startswith("(")]
        self.assertEqual("".join(p.partition(") ")[2] for p in pieces), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 < 4096 for p in pieces))

    async def test_split_chunks_arrive_before_later_answer(self):
        self.store.reply(7, "a" * 9000, "long")
        self.store.reply(7, "later", "later")
        await deliver(self.store, self.transport)
        await deliver(self.store, self.transport)
        self.assertEqual(self.transport.messages[-1][1], "later")
        self.assertTrue(all(t.startswith("(") for _, t in self.transport.messages[1:-1]))

    async def test_recovered_answer_after_uncertain_notification(self):
        await self.bridge.handle(update(2, "실행"))
        job = self.store.claim()
        self.store.transition(job["job_id"], "running", codex_turn_id="known-turn")
        self.store.recover()
        self.store.reply(7, "uncertain", "job:1:final")
        self.adapter.results["known-turn"] = {"status": "completed", "text": "recovered"}
        await self.bridge.reconcile()
        self.assertIn("recovered", self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='job:1:recovered'").fetchone()[0])


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config.load(ROOT / "config.toml", live=False)

    def test_schema_validates_readonly_and_forbids_unknown_fields(self):
        adapter = CodexAdapter(self.config)
        adapter.validators["turn/start"].validate({"threadId": "id", "input": [{"type": "text", "text": "原文"}],
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "on-request", "approvalsReviewer": "user"})
        with self.assertRaises(Exception):
            adapter.validators["turn/start"].validate({"threadId": "id", "input": [], "invented": True})
        adapter.validators["turn/start"].validate({"threadId": "id", "input": [{"type": "text", "text": "原文"}],
            "permissions": adapter.PERMISSION_PROFILE, "approvalPolicy": "on-request", "approvalsReviewer": "user"})

    def test_child_env_and_secret_path_policy(self):
        adapter = CodexAdapter(self.config)
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "OPENAI_API_KEY": "test", "PGPASSWORD": "test"}):
            env = adapter.child_env()
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PGPASSWORD", env)
        policy = " ".join(adapter.policy_args())
        self.assertIn('"/home/haaland/.codex/auth.json"="deny"', policy)
        self.assertIn('"/home/haaland/personal-agent"="deny"', policy)
        self.assertIn("network={enabled=false}", policy)

    async def test_new_thread_first_turn_skips_resume(self):
        adapter = CodexAdapter(self.config)
        adapter.new_threads.add("new-id")
        async def request(method, params):
            if method == "config/read":
                return {"config": {}}
            self.assertEqual(method, "turn/start")
            adapter.subscribers["new-id"].put_nowait({"method": "turn/completed", "params": {
                "threadId": "new-id", "turn": {"id": "turn", "status": "completed", "items": []}}})
            return {"turn": {"id": "turn"}}
        adapter.request = request
        async def progress(_):
            pass
        result = await adapter.run_turn("new-id", "hello", lambda _: None, progress)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("new-id", adapter.new_threads)

    def test_final_answer_excludes_delta_commentary_and_dedicated_reasoning(self):
        items = [{"type": "agentMessage", "phase": "commentary", "text": "준비"},
                 {"type": "agentMessage", "phase": "partial_answer", "text": "중간"},
                 {"type": "reasoning", "text": "추론"},
                 {"type": "agentMessage", "phase": "final_answer", "text": "정식 답변"}]
        self.assertEqual(final_text(items), "정식 답변")

    async def test_stream_early_duplicate_unknown_events(self):
        adapter = CodexAdapter(self.config)
        requests = []
        async def request(method, params):
            requests.append((method, params))
            if method == "thread/resume":
                return {"thread": {"id": "id", "cwd": str(self.config.cwd)}, "sandbox": {"type": "readOnly"},
                        "activePermissionProfile": {"id": adapter.PERMISSION_PROFILE}}
            if method == "turn/start":
                queue = adapter.subscribers["id"]
                for event in [
                    {"method": "item/agentMessage/delta", "params": {"threadId": "id", "turnId": "turn", "delta": "중복되면 안 됨"}},
                    {"method": "unknown/newEvent", "params": {"threadId": "id", "turnId": "turn"}},
                    *[{"method": "item/completed", "params": {"threadId": "id", "turnId": "turn",
                       "item": {"id": "msg", "type": "agentMessage", "phase": "final_answer", "text": "완성"}}}] * 2,
                    {"method": "turn/completed", "params": {"threadId": "id", "turn": {"id": "turn", "status": "completed"}}},
                ]:
                    queue.put_nowait(event)
                return {"turn": {"id": "turn"}}
        adapter.request = request
        started = []
        async def progress(_):
            pass
        result = await adapter.run_turn("id", "프롬프트 원문", started.append, progress)
        self.assertEqual(result["text"], "완성")
        self.assertEqual(started, ["turn"])
        turn = next(params for method, params in requests if method == "turn/start")
        self.assertEqual(turn["input"][0]["text"], "프롬프트 원문")

    async def test_approval_requests_never_accepted(self):
        adapter = CodexAdapter(self.config)
        replies = []
        async def send(message):
            replies.append(message)
        adapter.send = send
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                       "item/permissions/requestApproval", "unexpected/method"):
            await adapter.deny_request({"id": "nonce", "method": method})
        self.assertEqual(replies[0]["result"]["decision"], "decline")
        self.assertEqual(replies[1]["result"]["decision"], "decline")
        self.assertEqual(replies[2]["result"]["permissions"], {})
        self.assertIn("error", replies[3])

    async def test_activity_uses_latest_turn_status_not_open_files(self):
        adapter = CodexAdapter(self.config)
        for status, active in (("completed", False), ("interrupted", False), ("failed", False),
                               ("inProgress", True), ("unknownFutureStatus", True)):
            async def request(method, params):
                self.assertEqual(method, "thread/turns/list")
                self.assertEqual(params["limit"], 1)
                self.assertEqual(params["itemsView"], "notLoaded")
                return {"data": [{"id": "turn", "status": status}]}
            adapter.request = request
            self.assertEqual(await adapter.thread_is_active("id"), active)
        async def empty(method, params):
            return {"data": []}
        adapter.request = empty
        self.assertFalse(await adapter.thread_is_active("new-id"))
        adapter.new_threads.add("created-id")
        async def must_not_read(method, params):
            self.fail("New threads have no persisted rollout to inspect yet")
        adapter.request = must_not_read
        self.assertFalse(await adapter.thread_is_active("created-id"))

    async def test_telegram_429_retry_and_plain_text(self):
        transport = TelegramTransport("123456:never-a-real-secret-000000000")
        await transport.client.aclose()
        count = 0
        def respond(request):
            nonlocal count
            count += 1
            params = json.loads(request.content)
            self.assertNotIn("parse_mode", params)
            if count == 1:
                return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        transport.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with patch("heimdall_bridge.transport.asyncio.sleep", return_value=None):
                self.assertEqual(await transport.send(7, "안녕"), 9)
            self.assertEqual(count, 2)
        finally:
            await transport.close()

    def test_secret_sanitizer_and_astral_size(self):
        self.assertEqual(sanitize("token SECRET", ("SECRET",)), "token [redacted]")
        self.assertNotIn("sk-proj-", sanitize("sk-proj-" + "x" * 30))
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 < 4096 for p in chunks("🙂" * 4000)))

    def test_live_start_fails_closed_without_owner_or_token(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_USER_ID": "0", "TELEGRAM_ALLOWED_CHAT_ID": "0"}):
            with self.assertRaises(ConfigError):
                Config.load(ROOT / "config.toml")


if __name__ == "__main__":
    unittest.main()
