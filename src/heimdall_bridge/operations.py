"""Owner-scoped operations. No network calls, prompt logging, or automatic replay."""

import os
import json
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from .db import now


SAFE_PREFLIGHT_ERRORS = frozenset({
    "preflight_blocked_not_dispatched", "thread_has_uncertain_job",
    "thread_changed_before_dispatch", "selected_thread_in_use", "thread_active",
    "thread_lock_busy", "thread_already_active",
})
JOB_FIELDS = ("job_id", "chat_id", "user_id", "telegram_message_id", "thread_id",
              "state", "codex_turn_id", "cancel_requested", "created_at",
              "started_at", "finished_at")


class Operations:
    """Synchronous, short SQLite transactions; callable by async command handlers."""

    def __init__(self, store, config):
        self.store, self.config = store, config

    def _authorize(self, chat, user):
        if (type(chat) is not int or type(user) is not int or user <= 0
                or chat != self.config.chat_id or user != self.config.user_id):
            raise ValueError("operation_not_authorized")

    def _job(self, job_id, chat, user):
        self._authorize(chat, user)
        job = self.store.job(job_id)
        if not job or job["chat_id"] != chat or job["user_id"] != user:
            raise ValueError("job_not_owned")
        return job

    def _event(self, job_id, event):
        self.store.conn.execute(
            "INSERT INTO job_events(job_id,event_type,created_at) VALUES(?,?,?)",
            (job_id, event, now()))

    def _claim_command(self, job_id, chat, user, message_id):
        # Bridge may have claimed command_messages before calling this helper.
        # Our audit marker is committed atomically with the actual operation.
        event = f"operation_command:{chat}:{user}:{message_id}"
        if self.store.conn.execute("SELECT 1 FROM job_events WHERE event_type=?", (event,)).fetchone():
            raise ValueError("message_id_already_used")
        self.store.conn.execute("INSERT OR IGNORE INTO command_messages(chat_id,message_id) VALUES(?,?)",
                                (chat, message_id))
        self._event(job_id, event)

    def inspect(self, job_id, chat, user):
        """Return job metadata only, excluding prompt, response, and baseline."""
        job = self._job(job_id, chat, user)
        return {key: job[key] for key in JOB_FIELDS}

    def status(self, chat, user, limit=20, *, before_job_id=None):
        """Newest first, bounded to 1..100, with an optional exclusive cursor."""
        self._authorize(chat, user)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_status_limit")
        if before_job_id is not None and (type(before_job_id) is not int or before_job_id < 1):
            raise ValueError("invalid_status_cursor")
        sql = "SELECT " + ",".join(JOB_FIELDS) + " FROM jobs WHERE chat_id=? AND user_id=?"
        args = [chat, user]
        if before_job_id is not None:
            sql += " AND job_id<?"
            args.append(before_job_id)
        rows = self.store.conn.execute(sql + " ORDER BY job_id DESC LIMIT ?", (*args, limit))
        return [dict(row) for row in rows]

    def retry(self, job_id, chat, user, message_id, new_baseline, maximum, *, confirm_repeat=False):
        """Enqueue a new job, preserving the source. Repeated command IDs are idempotent."""
        self._authorize(chat, user)
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("invalid_message_id")
        if not isinstance(new_baseline, str) or not new_baseline:
            raise ValueError("fresh_baseline_required")
        if type(maximum) is not int or not 1 <= maximum <= self.config.max_queue:
            raise ValueError("invalid_queue_maximum")
        with self.store.transaction():
            job = self._job(job_id, chat, user)
            if job.get("knowledge_context") and json.loads(job["knowledge_context"]).get("purpose") == "extract":
                raise ValueError("자료 재추출은 /extract HK-000001 형식으로 요청하세요.")
            if job.get("permission_mode", "readonly") == "workspace":
                binding = self.store.conn.execute(
                    "SELECT * FROM chat_bindings WHERE chat_id=?", (chat,)).fetchone()
                if (not binding or binding["user_id"] != user
                        or "permission_mode" not in binding.keys()
                        or binding["permission_mode"] != "workspace"):
                    raise ValueError("workspace_binding_required")
            existing = self.store.conn.execute(
                "SELECT job_id,user_id FROM jobs WHERE chat_id=? AND telegram_message_id=?",
                (chat, message_id)).fetchone()
            if existing:
                event = f"retry_created:{existing['job_id']}:{message_id}"
                if existing["user_id"] == user and self.store.conn.execute(
                        "SELECT 1 FROM job_events WHERE job_id=? AND event_type=?",
                        (job_id, event)).fetchone():
                    return existing["job_id"]
                raise ValueError("message_id_already_used")
            ran = self.store.conn.execute(
                "SELECT 1 FROM job_events WHERE job_id=? AND event_type='running'",
                (job_id,)).fetchone()
            safe = (job["state"] == "failed" and not job["codex_turn_id"] and not ran
                    and job["error_code"] in SAFE_PREFLIGHT_ERRORS)
            terminal = job["state"] in {"completed", "failed"} and bool(job["codex_turn_id"])
            if not safe and not (terminal and confirm_repeat is True):
                raise ValueError("repeat_confirmation_required" if terminal else "job_not_retryable")
            if self.store.conn.execute(
                    "SELECT 1 FROM jobs WHERE thread_id=? AND state IN ('uncertain','dispatching','running')",
                    (job["thread_id"],)).fetchone():
                raise ValueError("thread_not_safe_for_retry")
            self._claim_command(job_id, chat, user, message_id)
            new_id = self.store.enqueue(chat, user, message_id, job["thread_id"],
                                        job["prompt"], new_baseline, maximum)
            # Optional Phase 2 column: preserve the original permission scope.
            if job.get('permission_mode') == 'network':
                raise ValueError('network_job_requires_new_question_in_active_network_mode')
            if "permission_mode" in job:
                self.store.conn.execute("UPDATE jobs SET permission_mode=? WHERE job_id=?",
                                        (job["permission_mode"], new_id))
            if "image_assets" in job:
                self.store.conn.execute("UPDATE jobs SET image_assets=? WHERE job_id=?", (job["image_assets"], new_id))
            if "knowledge_context" in job:
                self.store.conn.execute("UPDATE jobs SET knowledge_context=? WHERE job_id=?", (job["knowledge_context"], new_id))
            self._event(job_id, f"retry_created:{new_id}:{message_id}")
            self._event(new_id, f"retry_of:{job_id}")
            if terminal:
                self._event(new_id, "repeat_confirmed")
            return new_id

    def resolve(self, job_id, decision, chat, user):
        """Record an explicit manual decision; this neither executes nor undoes a turn."""
        if decision not in {"failed", "cancelled"}:
            raise ValueError("manual_decision_required")
        with self.store.transaction():
            job = self._job(job_id, chat, user)
            if job["state"] != "uncertain":
                raise ValueError("only_uncertain_can_be_resolved")
            self.store.transition(job_id, decision, error_code="manual_resolution")
            self._event(job_id, f"manual_resolve:{decision}")
        return self.inspect(job_id, chat, user)

    def resend(self, delivery_id, chat, user, message_id, confirm_uncertain=False):
        """Requeue one delivery; uncertain requires acknowledgement of duplicate risk."""
        self._authorize(chat, user)
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("invalid_message_id")
        with self.store.transaction():
            row = self.store.conn.execute("SELECT * FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone()
            binding = self.store.conn.execute(
                "SELECT user_id FROM chat_bindings WHERE chat_id=?", (chat,)).fetchone()
            if not row or row["chat_id"] != chat or not binding or binding[0] != user:
                raise ValueError("delivery_not_owned")
            row = dict(row)
            job_id = None
            if row["dedupe"].startswith("job:"):
                try:
                    job_id = int(row["dedupe"].split(":")[1])
                except (ValueError, IndexError):
                    raise ValueError("delivery_not_owned") from None
                self._job(job_id, chat, user)
            # Check ancestors too: split children must never replay approval UI.
            ancestor, seen = row, set()
            while ancestor:
                if ancestor["delivery_id"] in seen:
                    raise ValueError("invalid_delivery_ancestry")
                seen.add(ancestor["delivery_id"])
                if ancestor["dedupe"].startswith("job:"):
                    try:
                        ancestor_job = int(ancestor["dedupe"].split(":")[1])
                    except (ValueError, IndexError):
                        raise ValueError("delivery_not_owned") from None
                    self._job(ancestor_job, chat, user)
                if ancestor.get("reply_markup") is not None:
                    raise ValueError("approval_delivery_not_replayable")
                parent = ancestor.get("parent_id")
                if parent is None:
                    break
                found = self.store.conn.execute("SELECT * FROM outbox WHERE delivery_id=?", (parent,)).fetchone()
                if not found or found["chat_id"] != chat:
                    raise ValueError("delivery_not_owned")
                ancestor = dict(found)
            event = f"resend:{delivery_id}:{chat}:{user}:{message_id}"
            if self.store.conn.execute("SELECT 1 FROM job_events WHERE event_type=?", (event,)).fetchone():
                return delivery_id
            if row["state"] not in {"failed", "uncertain"}:
                raise ValueError("delivery_not_resendable")
            if row["state"] == "uncertain" and confirm_uncertain is not True:
                raise ValueError("uncertain_delivery_may_duplicate_confirm_required")
            if self.store.conn.execute(
                    "SELECT 1 FROM jobs WHERE chat_id=? AND telegram_message_id=?",
                    (chat, message_id)).fetchone():
                raise ValueError("message_id_already_used")
            self._claim_command(job_id, chat, user, message_id)
            self.store.conn.execute("UPDATE outbox SET state='pending' WHERE delivery_id=?", (delivery_id,))
            self._event(job_id, event)
            if row["state"] == "uncertain":
                self._event(job_id, f"resend_duplicate_risk_confirmed:{delivery_id}:{message_id}")
            return delivery_id


def _no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink_path_forbidden")
    return path


def _safe_directory(config, directory):
    path = _no_symlinks(directory)
    protected = (config.codex_home, Path.home() / "personal-agent", Path.home() / ".config",
                 Path.home() / ".ssh", Path("/var/lib/kakao-radar"), Path("/etc/kakao-radar"))
    if path in {Path("/"), Path.home(), config.cwd} or any(
            path.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError("unsafe_backup_directory")
    if not path.is_dir():
        raise ValueError("private_directory_must_exist")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("private_directory_required")
    return path


def _readonly(path):
    path = _no_symlinks(path)
    if not path.is_file():
        raise ValueError("database_source_missing")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _check_database(conn):
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("database_integrity_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("database_foreign_keys_failed")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"jobs", "job_events", "outbox", "sessions", "chat_bindings"} <= tables:
        raise ValueError("not_a_bridge_database")


def _copy_database(config, source, directory, destination=None):
    """Private targets are reserved exclusively before opening SQLite."""
    with closing(_readonly(source)) as src:
        _check_database(src)
        size = src.execute("PRAGMA page_count").fetchone()[0] * src.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(directory).free < size + config.min_disk_bytes:
            raise ValueError("insufficient_backup_disk_space")
        if destination is None:
            fd, name = tempfile.mkstemp(prefix="bridge-backup-", suffix=".sqlite3", dir=directory)
            target = Path(name)
        else:
            target = destination
            if any(Path(str(target) + suffix).exists() or Path(str(target) + suffix).is_symlink()
                   for suffix in ("", "-wal", "-shm", "-journal")):
                raise ValueError("restore_destination_exists")
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst, pages=256)
                _check_database(dst)
                dst.commit()
            finally:
                dst.close()
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
            return target
        except BaseException:
            # Only the fresh target reserved by this invocation is removed.
            target.unlink()
            raise


def backup_database(config, destination_dir):
    """Snapshot only data_dir/bridge.sqlite3 using a read-only SQLite source."""
    directory = _safe_directory(config, destination_dir)
    _safe_directory(config, config.data_dir)
    return _copy_database(config, Path(config.data_dir) / "bridge.sqlite3", directory)


def restore_database(config, backup_path, destination, *, safe_directory=None):
    """Restore to a fresh path under data_dir or an explicit separate private root."""
    root = _safe_directory(config, config.data_dir if safe_directory is None else safe_directory)
    target = _no_symlinks(destination)
    if not target.is_relative_to(root) or target == root:
        raise ValueError("restore_destination_outside_safe_directory")
    directory = _safe_directory(config, target.parent)
    live = _no_symlinks(Path(config.data_dir) / "bridge.sqlite3")
    data = Path(config.data_dir).resolve()
    if target == live or (safe_directory is not None and
                         (root.is_relative_to(data) or data.is_relative_to(root))):
        raise ValueError("restore_live_database_forbidden")
    source = _no_symlinks(backup_path)
    # Restore consumes only a bridge DB artifact, never Codex histories or secrets.
    _safe_directory(config, source.parent)
    if source == live:
        raise ValueError("restore_requires_backup_artifact")
    return _copy_database(config, source, directory, target)
