import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.fakes import FakeCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport
from heimdall_bridge.codex import CodexAdapter, RpcRejected, TurnUncertain, turn_error_code

ROOT = Path(__file__).resolve().parents[1]


class Phase2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-phase2-test-")
        self.config = replace(Config.load(ROOT / "config.toml", live=False),
            data_dir=Path(self.temp.name), user_id=7, chat_id=7, approval_timeout=.15)
        self.store = Store(self.config.data_dir / "bridge.sqlite3")
        self.adapter = FakeCodexAdapter(self.config.cwd)
        self.transport = FakeTelegramTransport()
        self.bridge = Bridge(self.config, self.store, self.adapter, self.transport, conflict_check=lambda *_: False)
        await self.bridge.handle(update(1, "/new Test"))

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def request(self):
        await self.bridge.handle(update(2, "/mode write"))
        await self.bridge.handle(update(3, "harmless fixture"))
        job = self.store.claim()
        self.store.transition(job["job_id"], "running", codex_turn_id="turn-fixture")
        return {"id": "rpc-fixture", "method": "item/commandExecution/requestApproval",
                "params": {"threadId": job["thread_id"], "turnId": "turn-fixture",
                "itemId": "item-fixture", "command": "printf harmless", "cwd": str(self.config.cwd)}}

    async def launch(self, message):
        task = asyncio.create_task(self.bridge.approvals.handle(message, "connection-fixture", {}))
        for _ in range(50):
            if self.transport.buttons or task.done():
                break
            await asyncio.sleep(.001)
        return task

    def callback(self, decision="yes"):
        chat, message_id, markup = self.transport.buttons[-1]
        data = markup["inline_keyboard"][0][0 if decision == "yes" else 1]["callback_data"]
        return {"id": "click", "from": {"id": 7}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}

    async def test_once_only_owner_approval(self):
        task = await self.launch(await self.request())
        query = self.callback()
        await self.bridge.handle({"update_id": 4, "callback_query": query})
        self.assertEqual(await task, {"decision": "accept"})
        self.assertFalse(await self.bridge.approvals.callback(query))
        row = self.store.conn.execute("SELECT * FROM approvals").fetchone()
        self.assertEqual(row["state"], "resolved")
        self.assertNotIn(query["data"].split(":")[2], json.dumps(dict(row)))

    async def test_wrong_owner_nonce_message_and_turn(self):
        task = await self.launch(await self.request())
        original = self.callback()
        for wrong in ({**original, "from": {"id": 8}},
                      {**original, "data": original["data"].replace(":yes", "x:yes")},
                      {**original, "message": {**original["message"], "message_id": 999}}):
            self.assertFalse(await self.bridge.approvals.callback(wrong))
            self.assertFalse(task.done())
        self.store.conn.execute("UPDATE jobs SET codex_turn_id='other'")
        self.assertFalse(await self.bridge.approvals.callback(original))
        self.assertEqual(await task, {"decision": "decline"})

    async def test_timeout_and_expired_replay(self):
        task = await self.launch(await self.request())
        query = self.callback()
        self.assertEqual(await task, {"decision": "decline"})
        self.assertFalse(await self.bridge.approvals.callback(query))
        self.assertEqual(self.store.conn.execute("SELECT state FROM approvals").fetchone()[0], "expired")

    async def test_cancel_invalidates_pending(self):
        task = await self.launch(await self.request())
        await self.bridge.command("/cancel", 7, 7)
        self.assertEqual(await task, {"decision": "decline"})
        self.assertFalse(await self.bridge.approvals.callback(self.callback()))

    async def test_deny_no_future_for_network_additional_permissions(self):
        message = await self.request()
        for key, value in (("additionalPermissions", {"filesystem": {}}),
                           ("networkApprovalContext", {"host": "test"}),
                           ("environmentId", "remote"), ("kind", "network")):
            altered = {**message, "params": {**message["params"], key: value}}
            self.assertEqual(await self.bridge.approvals.handle(altered, "c", {}), {"decision": "decline"})
        self.assertFalse(self.transport.buttons)

    async def test_file_changes_bound_to_whitelisted_paths(self):
        message = await self.request()
        message["method"] = "item/fileChange/requestApproval"
        key = ("fake-thread-1", "turn-fixture", "item-fixture")
        for path in (str(ROOT / "config.toml"), "relative.txt", str(self.config.codex_home / "auth.json")):
            self.assertIsNone(self.bridge.approvals.scope(message, {key: {"changes": [{"path": path}]}}))
        path = str(self.config.write_roots[0] / "harmless.txt")
        self.assertIsNotNone(self.bridge.approvals.scope(message, {key: {"changes": [{"path": path}]}}))

    async def test_readonly_default_frozen_permissions_reset_select(self):
        await self.bridge.handle(update(2, "read"))
        await self.bridge.handle(update(3, "/mode write"))
        await self.bridge.handle(update(4, "write"))
        await self.bridge.handle(update(5, "/select fake-thread-1"))
        await self.bridge.handle(update(6, "read again"))
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT permission_mode FROM jobs ORDER BY job_id")],
                         ["readonly", "workspace", "readonly"])

    async def test_history_rename_fork_release_and_jobs(self):
        self.assertIn("변경", await self.bridge.command("/rename New", 7, 7))
        self.assertEqual(self.adapter.threads["fake-thread-1"]["name"], "New")
        self.assertIn("없습니다", await self.bridge.command("/history 5", 7, 7))
        with self.assertRaises(ValueError):
            await self.bridge.command("/history 21", 7, 7)
        self.assertIn("새 분기", await self.bridge.command("/fork Branch", 7, 7))
        self.assertEqual(self.store.binding(7), "fake-thread-2")
        self.assertIn("해제", await self.bridge.command("/release", 7, 7))
        self.assertIn("아닙니다", await self.bridge.command("/release", 7, 7))
        await self.bridge.handle(update(2, "question"))
        await self.bridge.work_once()
        self.assertIn("completed", await self.bridge.command("/jobs", 7, 7))
        self.assertIn("Codex: question", await self.bridge.command("/job 1", 7, 7))
        with self.assertRaises(ValueError):
            await self.bridge.command("/retry 1", 7, 7, 90)
        self.assertIn("새 작업 #2", await self.bridge.command("/retry 1 repeat", 7, 7, 91))

    async def test_recovery_expires_approvals_without_replay(self):
        task = await self.launch(await self.request())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.store.recover()
        self.assertEqual(self.store.job(1)["state"], "uncertain")
        self.assertFalse(self.store.claim())
        self.assertEqual(self.store.conn.execute("SELECT state FROM approvals").fetchone()[0], "expired")

    async def test_active_uncertain_blocks_other_threads_globally(self):
        await self.bridge.handle(update(2, "first"))
        self.store.claim()
        self.store.transition(1, "uncertain", error_code="connection_lost")
        self.adapter.threads["fake-thread-1"]["status"] = {"type": "active"}
        await self.bridge.handle(update(3, "/new Other"))
        await self.bridge.handle(update(4, "second"))
        self.assertFalse(await self.bridge.work_once())
        self.assertEqual(self.store.job(2)["state"], "queued")
        self.adapter.threads["fake-thread-1"]["status"] = {"type": "idle"}
        self.assertTrue(await self.bridge.work_once())
        self.assertEqual(self.store.job(2)["state"], "completed")
        self.assertEqual(self.store.job(1)["state"], "uncertain")

    async def test_quota_error_preserved_and_reported_without_raw_backend_error(self):
        await self.bridge.handle(update(2, "question"))
        job = self.store.claim()
        self.store.transition(1, "running", codex_turn_id="limit-turn")
        code = turn_error_code({"error": {"codexErrorInfo": "usageLimitExceeded", "message": "SECRET"}})
        self.bridge.finish(job, {"status": "failed", "text": "", "error_code": code})
        self.assertEqual(self.store.job(1)["error_code"], "usageLimitExceeded")
        body = self.store.conn.execute("SELECT body FROM outbox WHERE dedupe='job:1:final'").fetchone()[0]
        self.assertIn("사용량", body)
        self.assertNotIn("SECRET", body)
        self.assertIsNone(turn_error_code({"error": {"codexErrorInfo": "SECRET"}}))

    async def test_mode_change_unloads_only_our_idle_actor_before_resume(self):
        adapter = CodexAdapter(self.config)
        adapter.loaded_threads.add("fixture")
        adapter.thread_modes["fixture"] = "workspace"
        calls = []
        async def request(method, params):
            calls.append(method)
            if method == "thread/turns/list":
                return {"data": [{"status": "completed"}]}
            if method == "thread/unsubscribe":
                return {"status": "unsubscribed"}
            return {"thread": {"id": "fixture", "cwd": str(self.config.cwd)},
                    "sandbox": {"type": "readOnly"}, "activePermissionProfile": {"id": "heimdall_readonly"}}
        adapter.request = request
        await adapter.resume_thread("fixture", "readonly")
        self.assertEqual(calls, ["thread/turns/list", "thread/unsubscribe", "thread/resume"])
        self.assertEqual(adapter.thread_modes["fixture"], "readonly")

    async def test_mode_change_never_releases_active_actor(self):
        adapter = CodexAdapter(self.config)
        adapter.loaded_threads.add("fixture")
        adapter.thread_modes["fixture"] = "workspace"
        async def request(method, params):
            self.assertEqual(method, "thread/turns/list")
            return {"data": [{"status": "inProgress"}]}
        adapter.request = request
        with self.assertRaises(RpcRejected):
            await adapter.resume_thread("fixture", "readonly")

    async def test_unknown_first_turn_acceptance_is_not_assumed_idle(self):
        adapter = CodexAdapter(self.config)
        adapter.new_threads.add("fixture")
        async def request(method, params):
            raise asyncio.TimeoutError()
        adapter.request = request
        with self.assertRaises(TurnUncertain):
            await adapter.run_turn("fixture", "question", lambda _: None, lambda _: None)
        self.assertNotIn("fixture", adapter.new_threads)
