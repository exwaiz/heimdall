import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.operations import Operations, backup_database, restore_database
from heimdall_bridge.transport import DeliveryUncertain, TransportError, deliver


class OperationsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heimdall-operations-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.config = Config(self.root, self.data, self.root / "schemas", self.root / "codex",
                             chat_id=7, user_id=7, min_disk_bytes=0)
        self.store = Store(self.data / "bridge.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.store.cache({"id": "thread", "cwd": str(self.root)}, owned=True)
        self.store.bind(7, 7, "thread")
        self.ops = Operations(self.store, self.config)

    def job(self, state="failed", error="selected_thread_in_use", turn=None, *, chat=7, user=7):
        message = self.store.conn.execute("SELECT coalesce(max(telegram_message_id),0)+1 FROM jobs").fetchone()[0]
        job = self.store.enqueue(chat, user, message, "thread", "PRIVATE prompt", "old", 30)
        if state == "queued":
            return job
        self.store.transition(job, "dispatching")
        if turn:
            self.store.transition(job, "running", codex_turn_id=turn)
        if state not in {"running", "dispatching"}:
            self.store.transition(job, state, error_code=error)
        return job

    def columns(self):
        for table, column, declaration in (
                ("jobs", "permission_mode", "TEXT NOT NULL DEFAULT 'readonly'"),
                ("chat_bindings", "permission_mode", "TEXT NOT NULL DEFAULT 'readonly'"),
                ("outbox", "reply_markup", "TEXT")):
            if column not in {r[1] for r in self.store.conn.execute(f"PRAGMA table_info({table})")}:
                self.store.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def retry(self, job, message=100, **kwargs):
        return self.ops.retry(job, 7, 7, message, "fresh", 30, **kwargs)

    def delivery(self, state="failed", key="alert", parent=None):
        self.store.reply(7, "PRIVATE answer", key, parent_id=parent)
        row = self.store.conn.execute("SELECT delivery_id FROM outbox WHERE dedupe=?", (key,)).fetchone()[0]
        self.store.conn.execute("UPDATE outbox SET state=? WHERE delivery_id=?", (state, row))
        return row


class OperationsTests(OperationsFixture):
    def test_safe_retry_is_new_job_and_idempotent(self):
        old = self.job()
        new = self.retry(old)
        self.assertNotEqual(old, new)
        self.assertEqual(self.retry(old), new)
        self.assertEqual(self.store.job(old)["state"], "failed")
        copied = self.store.job(new)
        self.assertEqual((copied["prompt"], copied["baseline"], copied["state"]), ("PRIVATE prompt", "fresh", "queued"))
        self.assertIsNone(copied["codex_turn_id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
        events = [r[0] for r in self.store.conn.execute("SELECT event_type FROM job_events")]
        self.assertIn(f"retry_of:{old}", events)
        self.assertNotIn("PRIVATE", str(events))

    def test_retry_rejects_unknown_failure_or_uncertain_even_confirmed(self):
        for state, error, turn in (("failed", "unknown", None), ("uncertain", "worker_shutdown", None),
                                   ("uncertain", "worker_shutdown", "turn"), ("queued", None, None),
                                   ("dispatching", None, None), ("running", None, "turn"),
                                   ("cancelled", None, "turn")):
            with self.subTest(state=state, turn=turn):
                job = self.job(state, error, turn)
                with self.assertRaises(ValueError):
                    self.retry(job, confirm_repeat=True)

    def test_recorded_terminal_turn_requires_explicit_boolean(self):
        for state in ("completed", "failed"):
            job = self.job(state, "failed", "turn")
            for flag in (False, 1, "yes"):
                with self.assertRaisesRegex(ValueError, "repeat_confirmation"):
                    self.retry(job, 100 + job, confirm_repeat=flag)
            new = self.retry(job, 100 + job, confirm_repeat=True)
            self.assertEqual(self.store.job(new)["state"], "queued")

    def test_no_turn_id_with_running_event_is_not_safe(self):
        job = self.job()
        self.store.conn.execute("INSERT INTO job_events(job_id,event_type,created_at) VALUES(?,'running','test')", (job,))
        with self.assertRaises(ValueError):
            self.retry(job, confirm_repeat=True)

    def test_database_ownership_and_no_information_leak(self):
        foreign = self.job(chat=7, user=8)
        other_chat = self.job(chat=8, user=7)
        for job in (foreign, other_chat, 999):
            for action in (lambda: self.ops.inspect(job, 7, 7), lambda: self.retry(job),
                           lambda: self.ops.resolve(job, "failed", 7, 7)):
                with self.assertRaisesRegex(ValueError, "job_not_owned"):
                    action()
        with self.assertRaises(ValueError):
            self.ops.status(7, 8)
        self.assertEqual(self.ops.status(7, 7), [])

    def test_status_bounded_and_metadata_only(self):
        ids = [self.job() for _ in range(4)]
        rows = self.ops.status(7, 7, 2)
        self.assertEqual([r["job_id"] for r in rows], ids[-1:-3:-1])
        self.assertEqual([r["job_id"] for r in self.ops.status(7, 7, 2, before_job_id=3)], [2, 1])
        self.assertNotIn("PRIVATE", str(rows))
        self.assertNotIn("prompt", self.ops.inspect(ids[0], 7, 7))
        for limit in (0, 101, True, "2"):
            with self.assertRaises(ValueError):
                self.ops.status(7, 7, limit)

    def test_queue_full_and_message_collision_leave_no_partial_audit(self):
        job = self.job()
        queued = self.job("queued")
        before = self.store.conn.execute("SELECT count(*) FROM job_events").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "queue_full"):
            self.ops.retry(job, 7, 7, 100, "fresh", 1)
        with self.assertRaisesRegex(ValueError, "message_id_already_used"):
            self.retry(job, self.store.job(queued)["telegram_message_id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM job_events").fetchone()[0], before)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)

    def test_same_thread_uncertainty_blocks_safe_retry(self):
        job = self.job()
        self.job("uncertain")
        with self.assertRaisesRegex(ValueError, "thread_not_safe"):
            self.retry(job)

    def test_bridge_preclaims_are_supported_and_command_ids_cannot_cross_operations(self):
        job = self.job()
        delivery = self.delivery()
        self.store.conn.execute("INSERT INTO command_messages VALUES(7,100)")
        new = self.retry(job)
        self.assertEqual(self.retry(job), new)
        with self.assertRaisesRegex(ValueError, "message_id_already_used"):
            self.ops.resend(delivery, 7, 7, 100)
        self.store.conn.execute("INSERT INTO command_messages VALUES(7,200)")
        self.ops.resend(delivery, 7, 7, 200)
        with self.assertRaisesRegex(ValueError, "message_id_already_used"):
            self.retry(job, 200)

    def test_legacy_optional_columns_are_not_required(self):
        job = self.job()
        delivery = self.delivery()
        for table, column in (("jobs", "permission_mode"), ("chat_bindings", "permission_mode"),
                              ("outbox", "reply_markup")):
            if column in {r[1] for r in self.store.conn.execute(f"PRAGMA table_info({table})")}:
                self.store.conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

        # Emulate the existing enqueue implementation shipped with the legacy schema.
        def legacy_enqueue(chat, user, message, thread, prompt, baseline, maximum):
            return self.store.conn.execute(
                "INSERT INTO jobs(chat_id,user_id,telegram_message_id,thread_id,prompt,state,baseline,created_at) "
                "VALUES(?,?,?,?,?,'queued',?,'test')", (chat, user, message, thread, prompt, baseline)).lastrowid

        with patch.object(self.store, "enqueue", side_effect=legacy_enqueue):
            new = self.retry(job)
        self.assertEqual(self.store.job(new)["state"], "queued")
        self.assertNotIn("permission_mode", self.store.job(new))
        self.ops.resend(delivery, 7, 7, 200)

    def test_workspace_requires_current_database_binding_and_preserves_mode(self):
        self.columns()
        job = self.job()
        self.store.conn.execute("UPDATE jobs SET permission_mode='workspace' WHERE job_id=?", (job,))
        with self.assertRaisesRegex(ValueError, "workspace_binding_required"):
            self.retry(job)
        self.store.conn.execute("UPDATE chat_bindings SET permission_mode='workspace'")
        new = self.retry(job)
        self.assertEqual(self.store.job(new)["permission_mode"], "workspace")
        readonly = self.job()
        # enqueue currently inherits binding mode; explicitly model an older readonly job.
        self.store.conn.execute("UPDATE jobs SET permission_mode='readonly' WHERE job_id=?", (readonly,))
        new_readonly = self.retry(readonly, 200)
        self.assertEqual(self.store.job(new_readonly)["permission_mode"], "readonly")
        self.store.conn.execute("UPDATE chat_bindings SET user_id=8")
        with self.assertRaises(ValueError):
            self.retry(job, 102)

    def test_resolve_only_uncertain_with_manual_audit_and_no_retry(self):
        for decision in ("failed", "cancelled"):
            job = self.job("uncertain", turn="known")
            result = self.ops.resolve(job, decision, 7, 7)
            self.assertEqual(result["state"], decision)
            self.assertEqual(self.store.job(job)["codex_turn_id"], "known")
            self.assertIsNotNone(self.store.conn.execute(
                "SELECT 1 FROM job_events WHERE job_id=? AND event_type=?", (job, f"manual_resolve:{decision}")).fetchone())
        for state in ("running", "dispatching", "queued", "completed", "failed"):
            job = self.job(state, turn="turn" if state in {"running", "completed"} else None)
            with self.assertRaises(ValueError):
                self.ops.resolve(job, "failed", 7, 7)
        with self.assertRaisesRegex(ValueError, "manual_decision_required"):
            self.ops.resolve(1, "completed", 7, 7)

    def test_resend_single_row_and_command_idempotence(self):
        delivery = self.delivery()
        self.assertEqual(self.ops.resend(delivery, 7, 7, 100), delivery)
        self.assertEqual(self.ops.resend(delivery, 7, 7, 100), delivery)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT state FROM outbox").fetchone()[0], "pending")
        with self.assertRaises(ValueError):
            self.ops.resend(delivery, 7, 7, 101)
        self.store.conn.execute("UPDATE outbox SET state='sent'")
        self.ops.resend(delivery, 7, 7, 100)
        self.assertEqual(self.store.conn.execute("SELECT state FROM outbox").fetchone()[0], "sent")
        with self.assertRaises(ValueError):
            self.ops.resend(delivery, 7, 7, 102)

    def test_uncertain_resend_warning_confirmation(self):
        delivery = self.delivery("uncertain")
        for flag in (False, 1, "yes"):
            with self.assertRaisesRegex(ValueError, "may_duplicate"):
                self.ops.resend(delivery, 7, 7, 100, flag)
        self.ops.resend(delivery, 7, 7, 100, True)
        self.assertIsNotNone(self.store.conn.execute(
            "SELECT 1 FROM job_events WHERE event_type LIKE 'resend_duplicate_risk_confirmed:%'").fetchone())

    def test_resend_database_owner_and_approval_ancestry(self):
        self.columns()
        parent = self.delivery(key="approval")
        child = self.delivery(key="chunk", parent=parent)
        self.store.conn.execute("UPDATE outbox SET reply_markup='{}' WHERE delivery_id=?", (parent,))
        for delivery in (parent, child):
            with self.assertRaisesRegex(ValueError, "approval_delivery"):
                self.ops.resend(delivery, 7, 7, 100)
        self.store.conn.execute("UPDATE outbox SET reply_markup=NULL")
        self.store.conn.execute("UPDATE chat_bindings SET user_id=8")
        with self.assertRaisesRegex(ValueError, "delivery_not_owned"):
            self.ops.resend(child, 7, 7, 100)
        self.store.conn.execute("UPDATE chat_bindings SET user_id=7")
        foreign = self.job(user=8)
        delivery = self.delivery(key=f"job:{foreign}:final")
        with self.assertRaisesRegex(ValueError, "job_not_owned"):
            self.ops.resend(delivery, 7, 7, 100)


class DeliveryFailureTests(OperationsFixture, unittest.IsolatedAsyncioTestCase):
    async def test_worker_preflight_failure_retry_executes_only_once(self):
        from heimdall_bridge.bridge import Bridge, snapshot
        from heimdall_bridge.fakes import FakeCodexAdapter
        from heimdall_bridge.transport import FakeTelegramTransport

        adapter = FakeCodexAdapter(self.config.cwd)
        thread = await adapter.new_thread()
        self.store.cache(thread, owned=True)
        self.store.bind(7, 7, thread["id"])
        baseline = snapshot(thread, self.config.codex_home)
        old = self.store.enqueue(7, 7, 1, thread["id"], "PRIVATE prompt", baseline, 30)
        bridge = Bridge(self.config, self.store, adapter, FakeTelegramTransport(), conflict_check=lambda *_: True)
        await bridge.work_once()
        self.assertEqual(self.store.job(old)["state"], "failed")
        self.assertEqual(adapter.calls, [])
        new = self.ops.retry(old, 7, 7, 100, baseline, 30)
        self.assertEqual(self.ops.retry(old, 7, 7, 100, baseline, 30), new)
        bridge.conflict_check = lambda *_: False
        await bridge.work_once()
        self.assertFalse(await bridge.work_once())
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.store.job(new)["state"], "completed")

    async def test_real_transport_failures_can_be_requeued_without_duplicate_send(self):
        class Broken:
            def __init__(self, error):
                self.calls = 0
                self.error = error

            async def send(self, chat, body):
                self.calls += 1
                if self.error:
                    raise self.error
                return 999

        delivery = self.delivery("pending")
        transport = Broken(TransportError("test_rejection"))
        await deliver(self.store, transport)
        self.assertEqual(self.store.conn.execute("SELECT state FROM outbox").fetchone()[0], "failed")
        self.ops.resend(delivery, 7, 7, 100)
        transport.error = DeliveryUncertain()
        await deliver(self.store, transport)
        with self.assertRaises(ValueError):
            self.ops.resend(delivery, 7, 7, 101)
        self.ops.resend(delivery, 7, 7, 101, True)
        transport.error = None
        await deliver(self.store, transport)
        self.ops.resend(delivery, 7, 7, 101, True)
        await deliver(self.store, transport)
        self.assertEqual(transport.calls, 3)


class BackupTests(OperationsFixture):
    def directory(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        return path

    def test_wal_backup_restore_integrity_private_unique_and_source_unchanged(self):
        job = self.job()
        self.store.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.store.reply(7, "WAL-only answer", "wal-test")
        source = self.data / "bridge.sqlite3"
        source_bytes = source.read_bytes()
        wal_bytes = Path(str(source) + "-wal").read_bytes()
        directory = self.directory("backups")
        first = backup_database(self.config, directory)
        second = backup_database(self.config, directory)
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(Path(str(source) + "-wal").read_bytes(), wal_bytes)
        restored = restore_database(self.config, first, self.data / "restored.sqlite3")
        with contextlib.closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT prompt FROM jobs WHERE job_id=?", (job,)).fetchone()[0], "PRIVATE prompt")
            self.assertEqual(conn.execute("SELECT body FROM outbox WHERE dedupe='wal-test'").fetchone()[0], "WAL-only answer")
        separate = self.directory("restore")
        self.assertEqual(restore_database(self.config, second, separate / "fresh.sqlite3", safe_directory=separate), separate / "fresh.sqlite3")

    def test_restore_refuses_overwrite_live_symlink_and_sidecars(self):
        backup = backup_database(self.config, self.directory("backups"))
        dest = self.data / "existing.sqlite3"
        dest.write_bytes(b"keep")
        before = dest.read_bytes()
        with self.assertRaises(ValueError):
            restore_database(self.config, backup, dest)
        self.assertEqual(dest.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "live_database"):
            restore_database(self.config, backup, self.data / "bridge.sqlite3")
        link = self.data / "link.sqlite3"
        link.symlink_to(dest)
        with self.assertRaisesRegex(ValueError, "symlink"):
            restore_database(self.config, backup, link)
        dangling = self.data / "dangling.sqlite3"
        dangling.symlink_to(self.data / "missing")
        with self.assertRaises(ValueError):
            restore_database(self.config, backup, dangling)
        fresh = self.data / "fresh.sqlite3"
        Path(str(fresh) + "-wal").write_bytes(b"keep-sidecar")
        with self.assertRaises(ValueError):
            restore_database(self.config, backup, fresh)
        self.assertFalse(fresh.exists())

    def test_private_and_protected_directories_and_source_symlink(self):
        public = self.directory("public")
        public.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private_directory"):
            backup_database(self.config, public)
        protected = self.config.codex_home
        protected.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "unsafe_backup"):
            backup_database(self.config, protected)
        private = self.directory("backups")
        link = self.root / "linked"
        link.symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            backup_database(self.config, link)
        source = self.data / "bridge.sqlite3"
        original = self.data / "original.sqlite3"
        source.rename(original)
        source.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symlink"):
            backup_database(self.config, private)

    def test_disk_failure_and_bad_database_produce_no_artifact(self):
        private = self.directory("backups")
        with patch("heimdall_bridge.operations.shutil.disk_usage", return_value=shutil_usage(0)):
            with self.assertRaisesRegex(ValueError, "insufficient_backup_disk"):
                backup_database(self.config, private)
        self.assertEqual(list(private.iterdir()), [])
        corrupt = private / "corrupt.sqlite3"
        corrupt.write_bytes(b"not SQLite")
        with self.assertRaises(sqlite3.DatabaseError):
            restore_database(self.config, corrupt, self.data / "fresh.sqlite3")
        self.assertFalse((self.data / "fresh.sqlite3").exists())
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        self.store.conn.execute("INSERT INTO job_events(job_id,event_type,created_at) VALUES(999,'bad','test')")
        with self.assertRaisesRegex(ValueError, "foreign_keys_failed"):
            backup_database(self.config, private)
        self.assertEqual(list(private.iterdir()), [corrupt])

    def test_restore_requires_explicit_safe_directory_and_backup_artifact(self):
        private = self.directory("backups")
        backup = backup_database(self.config, private)
        separate = self.directory("restore")
        with self.assertRaisesRegex(ValueError, "outside_safe"):
            restore_database(self.config, backup, separate / "fresh.sqlite3")
        with self.assertRaisesRegex(ValueError, "requires_backup_artifact"):
            restore_database(self.config, self.data / "bridge.sqlite3", self.data / "fresh.sqlite3")
        with self.assertRaises(ValueError):
            restore_database(self.config, backup, self.data / "fresh.sqlite3", safe_directory=self.data)

    def test_cli_loads_offline_config_and_suppresses_exception_secrets(self):
        spec = importlib.util.spec_from_file_location("backup_bridge", Path(__file__).resolve().parents[1] / "scripts/backup_bridge.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config) as load, contextlib.redirect_stdout(output):
            result = cli.main(["backup", "--destination-dir", str(self.directory("backups"))])
        self.assertEqual(result, 0)
        self.assertEqual(load.call_args.kwargs, {"live": False})
        with patch.object(cli.Config, "load", side_effect=ValueError("SECRET token")), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["backup", "--destination-dir", str(self.root)]), 1)
        self.assertNotIn("SECRET", output.getvalue())


def shutil_usage(free):
    import shutil
    return type(shutil.disk_usage("/"))(1000, 1000 - free, free)


if __name__ == "__main__":
    unittest.main()
