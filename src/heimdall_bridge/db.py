import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 thread_id TEXT PRIMARY KEY, alias TEXT, metadata TEXT NOT NULL, owned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_bindings(
 chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
 active_thread_id TEXT REFERENCES sessions(thread_id), selected_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
 job_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 telegram_message_id INTEGER NOT NULL, thread_id TEXT NOT NULL REFERENCES sessions(thread_id),
 prompt TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('queued','dispatching','running','completed','failed','cancelled','uncertain')),
 request_id TEXT, codex_turn_id TEXT, response_text TEXT, error_code TEXT,
 baseline TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 UNIQUE(chat_id,telegram_message_id));
CREATE INDEX IF NOT EXISTS jobs_fifo ON jobs(state,job_id);
CREATE TABLE IF NOT EXISTS job_events(
 event_id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(job_id),
 event_type TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_updates(
 update_id INTEGER PRIMARY KEY, state TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_messages(
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS outbox(
 delivery_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
 dedupe TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 parent_id INTEGER REFERENCES outbox(delivery_id));
CREATE TABLE IF NOT EXISTS approvals(
 approval_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, rpc_request_id TEXT NOT NULL,
 connection_id TEXT NOT NULL, kind TEXT NOT NULL, nonce_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','approved','denied','expired','resolved')),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT,
 telegram_message_id INTEGER,
 UNIQUE(connection_id,thread_id,turn_id,rpc_request_id));
"""

TRANSITIONS = {
    "queued": {"dispatching", "cancelled"},
    "dispatching": {"running", "failed", "uncertain", "cancelled"},
    "running": {"completed", "failed", "cancelled", "uncertain"},
    "uncertain": {"completed", "failed", "cancelled"},
}


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("symlink_database_forbidden")
        os.chmod(path.parent, 0o700)
        self.conn = sqlite3.connect(path, isolation_level=None)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(DDL)
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (now(),))
        # Upgrade early P0 databases without dropping any rows.
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(outbox)")}
        if "parent_id" not in columns:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN parent_id INTEGER REFERENCES outbox(delivery_id)")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(2,?)", (now(),))
        for table, name, definition in (
            ("jobs", "permission_mode", "TEXT NOT NULL DEFAULT 'readonly'"),
            ("chat_bindings", "permission_mode", "TEXT NOT NULL DEFAULT 'readonly'"),
            ("outbox", "reply_markup", "TEXT")):
            columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if name not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3,?)", (now(),))
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "image_assets" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN image_assets TEXT NOT NULL DEFAULT '[]'")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(4,?)", (now(),))
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "knowledge_context" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN knowledge_context TEXT")
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(outbox)")}
        for name, definition in (("photo", "BLOB"), ("photo_mime", "TEXT"), ("photo_sha256", "TEXT")):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6,?)", (now(),))
        for table in ('chat_bindings', 'jobs'):
            columns = {r[1] for r in self.conn.execute(f'PRAGMA table_info({table})')}
            if 'network_until' not in columns:
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN network_until REAL NOT NULL DEFAULT 0')
        self.conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES(7,?)', (now(),))
        # Foundation only: no retrieval or capture is enabled by this migration.
        # Keep the column, settings and version marker atomic on legacy databases.
        with self.transaction():
            self.conn.execute("""CREATE TABLE IF NOT EXISTS memory_settings(
                chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                auto_retrieve INTEGER NOT NULL DEFAULT 0 CHECK(auto_retrieve IN (0,1)),
                capture_mode TEXT NOT NULL DEFAULT 'off' CHECK(capture_mode IN ('off','suggest')),
                updated_at TEXT NOT NULL)""")
            columns = {r[1] for r in self.conn.execute('PRAGMA table_info(jobs)')}
            if 'auto_memory_refs' not in columns:
                self.conn.execute('ALTER TABLE jobs ADD COLUMN auto_memory_refs TEXT')
            self.conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES(8,?)', (now(),))

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def close(self):
        self.conn.close()

    def cache(self, thread, owned=False, alias=None):
        # Store metadata only; do not duplicate conversation history/previews.
        meta = {k: thread.get(k) for k in ("id", "cwd", "source", "updatedAt", "status", "name", "path")}
        self.conn.execute("""INSERT INTO sessions(thread_id,metadata,owned,alias) VALUES(?,?,?,?)
          ON CONFLICT(thread_id) DO UPDATE SET metadata=excluded.metadata,
          owned=max(sessions.owned,excluded.owned),alias=coalesce(excluded.alias,sessions.alias)""",
                          (thread["id"], json.dumps(meta), int(owned), alias))

    def bind(self, chat, user, thread):
        self.conn.execute("INSERT INTO chat_bindings(chat_id,user_id,active_thread_id,selected_at) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET active_thread_id=excluded.active_thread_id,selected_at=excluded.selected_at,permission_mode='readonly',network_until=0",
                          (chat, user, thread, now()))

    def binding(self, chat):
        row = self.conn.execute("SELECT active_thread_id FROM chat_bindings WHERE chat_id=?", (chat,)).fetchone()
        return row[0] if row else None

    def permission_mode(self, chat):
        row = self.conn.execute("SELECT permission_mode,network_until FROM chat_bindings WHERE chat_id=?", (chat,)).fetchone()
        if row and row[0] == 'network' and row[1] <= time.time():
            return 'readonly'
        return row[0] if row else "readonly"

    def network_deadline(self, chat):
        row = self.conn.execute("SELECT network_until FROM chat_bindings WHERE chat_id=? AND permission_mode='network'", (chat,)).fetchone()
        return row[0] if row and row[0] > time.time() else 0

    def set_permission_mode(self, chat, mode):
        if mode not in {"readonly", "workspace", "network"} or not self.binding(chat):
            raise ValueError("select_thread_before_mode")
        self.conn.execute("UPDATE chat_bindings SET permission_mode=?,network_until=? WHERE chat_id=?",
            (mode, time.time() + 1800 if mode == 'network' else 0, chat))

    def job(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def jobs(self, chat=None):
        sql = "SELECT * FROM jobs WHERE state IN ('queued','dispatching','running','uncertain')"
        rows = self.conn.execute(sql + (" AND chat_id=?" if chat is not None else "") + " ORDER BY job_id",
                                 (chat,) if chat is not None else ())
        return [dict(r) for r in rows]

    def accept_update(self, update):
        return self.conn.execute("INSERT OR IGNORE INTO telegram_updates VALUES(?,'processing',?)",
                                 (update, now())).rowcount == 1

    def handled(self, update):
        self.conn.execute("UPDATE telegram_updates SET state='handled' WHERE update_id=?", (update,))

    def enqueue(self, chat, user, message, thread, prompt, baseline, maximum, *, auto_memory_refs=None):
        existing = self.conn.execute("SELECT job_id FROM jobs WHERE chat_id=? AND telegram_message_id=?", (chat, message)).fetchone()
        if existing:
            return existing[0]
        count = self.conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','dispatching','running')").fetchone()[0]
        if count >= maximum:
            raise ValueError("queue_full")
        mode = self.permission_mode(chat)
        cursor = self.conn.execute("INSERT INTO jobs(chat_id,user_id,telegram_message_id,thread_id,prompt,state,baseline,created_at,permission_mode,network_until,auto_memory_refs) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)",
                                   (chat, user, message, thread, prompt, baseline, now(), mode,
                                    self.network_deadline(chat) if mode == 'network' else 0, auto_memory_refs))
        return cursor.lastrowid

    def claim(self):
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM jobs WHERE state IN ('dispatching','running')").fetchone():
                return None
            row = self.conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY job_id LIMIT 1").fetchone()
            if not row:
                return None
            self.transition(row["job_id"], "dispatching", request_id=f"job-{row['job_id']}", started_at=now())
            return self.job(row["job_id"])

    def transition(self, job_id, state, **fields):
        current = self.job(job_id)["state"]
        if state not in TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid_transition:{current}:{state}")
        if set(fields) - {"codex_turn_id", "response_text", "error_code", "request_id", "started_at"}:
            raise ValueError("invalid_job_field")
        if state in {"completed", "failed", "cancelled", "uncertain"}:
            fields["finished_at"] = now()
        fields["state"] = state
        self.conn.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE job_id=?",
                          (*fields.values(), job_id))
        self.conn.execute("INSERT INTO job_events(job_id,event_type,created_at) VALUES(?,?,?)", (job_id, state, now()))

    def recover(self):
        with self.transaction():
            for job in self.jobs():
                if job["state"] in {"dispatching", "running"}:
                    self.transition(job["job_id"], "uncertain", error_code="restart_during_execution")
            self.conn.execute("UPDATE telegram_updates SET state='uncertain' WHERE state='processing'")
            self.conn.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
            self.conn.execute("UPDATE approvals SET state='expired',decided_at=? WHERE state IN ('pending','approved','denied')", (now(),))

    def resolve_known_preflight_blocks(self):
        """Correct legacy misclassification only when the recorded failure predates turn/start."""
        resolved = []
        with self.transaction():
            rows = self.conn.execute("""SELECT job_id FROM jobs
                WHERE state='uncertain' AND error_code='external_activity_suspected'
                  AND codex_turn_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM job_events e WHERE e.job_id=jobs.job_id
                                  AND e.event_type='running')""").fetchall()
            for row in rows:
                self.transition(row[0], "failed", error_code="preflight_blocked_not_dispatched")
                resolved.append(row[0])
        return resolved

    def reply(self, chat, body, key, parent_id=None):
        self.conn.execute("INSERT OR IGNORE INTO outbox(chat_id,body,dedupe,parent_id) VALUES(?,?,?,?)", (chat, body, key, parent_id))

    def offset(self):
        return self.conn.execute("SELECT coalesce(max(update_id)+1,0) FROM telegram_updates").fetchone()[0]
