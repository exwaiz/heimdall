import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import now
from .transport import sanitize


class ApprovalManager:
    """Once-only approvals, bound to live job/turn/request/connection/owner."""

    def __init__(self, store, config, transport):
        self.store, self.config, self.transport = store, config, transport
        self.pending = {}

    def scope(self, message, items, config=None):
        config = config or self.config
        params = message.get("params", {})
        method = message["method"]
        if params.get("environmentId") not in {None, "local"}:
            return None  # Only local execution is supported.
        if method == "item/commandExecution/requestApproval":
            if (params.get("kind", "command") != "command" or params.get("additionalPermissions")
                or params.get("networkApprovalContext") or params.get("proposedNetworkPolicyAmendments")):
                return None
            command, cwd = params.get("command"), params.get("cwd")
            if not isinstance(command, str) or not command or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                return None
            cwd_path = Path(cwd).resolve()
            if cwd_path != config.cwd and not config.writable(cwd):
                return None
            decisions = params.get("availableDecisions")
            if decisions is not None and "accept" not in decisions:
                return None
            return f"명령: {command[:1800]}\ncwd: {cwd_path}"
        if method == "item/fileChange/requestApproval":
            if params.get("grantRoot"):
                return None
            item = items.get((params.get("threadId"), params.get("turnId"), params.get("itemId")), {})
            paths = [change.get("path") for change in item.get("changes", [])]
            if not paths or any(not isinstance(path, str) or not config.writable(path) for path in paths):
                return None
            return "변경 파일:\n" + "\n".join(paths)[:1800]
        return None

    async def handle(self, message, connection_id, items):
        params = message.get("params", {})
        thread, turn = params.get("threadId"), params.get("turnId")
        method = message["method"]
        denied = {"decision": "decline"}
        if not thread or not turn:
            return denied
        job = None
        # Early server requests can precede the turn/start response. Reader must keep running.
        for _ in range(20):
            row = self.store.conn.execute("SELECT * FROM jobs WHERE thread_id=? AND codex_turn_id=? AND state='running'",
                                          (thread, turn)).fetchone()
            if row:
                job = dict(row)
                break
            await asyncio.sleep(0.05)
        if (not job or (job["permission_mode"] not in {"workspace", 'network'} and not self.config.temporary_active()) or job["user_id"] != self.config.user_id
            or job["chat_id"] != self.config.chat_id):
            return denied
        config = self.config
        if job['permission_mode'] == 'network':
            if job.get('network_until', 0) <= time.time():
                return denied
            config = replace(config, temporary_access_until=job['network_until'])
        scope = self.scope(message, items, config)
        if scope is None:
            return denied
        nonce = secrets.token_urlsafe(12)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.config.approval_timeout)).isoformat()
        with self.store.transaction():
            cursor = self.store.conn.execute("""INSERT OR IGNORE INTO approvals
                (job_id,thread_id,turn_id,rpc_request_id,connection_id,kind,nonce_hash,state,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,'pending',?,?)""", (job["job_id"], thread, turn, json.dumps(message["id"]),
                connection_id, method, hashlib.sha256(nonce.encode()).hexdigest(), now(), expires))
            if cursor.rowcount != 1:
                return denied
            approval_id = cursor.lastrowid
        future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = (future, connection_id, thread, turn, message["id"])
        markup = {"inline_keyboard": [[
            {"text": "승인 · 이번만", "callback_data": f"a:{approval_id}:{nonce}:yes"},
            {"text": "거부", "callback_data": f"a:{approval_id}:{nonce}:no"}]]}
        risk = ("임시 권한: 일반 파일 쓰기와 인터넷 접속이 허용됩니다. 인증·운영코드 보호 및 추가 권한 거부는 유지합니다."
                if config.temporary_active() else
                "위험: 허용 작업 폴더 안에서 명령 실행 또는 파일 변경. 추가 권한·네트워크 확대는 승인하지 않습니다.")
        body = sanitize(f"🛡 작업 #{job['job_id']} · 승인 요청\n{scope}\n"
            f"{risk}\n"
            f"유효 시간: {self.config.approval_timeout:g}초. 기본 거부.", (self.config.token,))
        try:
            message_id = await self.transport.send(job["chat_id"], body, reply_markup=markup)
            self.store.conn.execute("UPDATE approvals SET telegram_message_id=? WHERE approval_id=?", (message_id, approval_id))
            accepted = await asyncio.wait_for(future, self.config.approval_timeout)
            # Revalidate state after await; cancel/turn completion makes any approval invalid.
            live = self.store.job(job["job_id"])
            accepted = accepted and live["state"] == "running" and live["codex_turn_id"] == turn and not live["cancel_requested"]
            if config.temporary_access_until and not config.temporary_active():
                accepted = False
            self.store.conn.execute("UPDATE approvals SET state='resolved',decided_at=? WHERE approval_id=? AND state IN ('approved','denied')", (now(), approval_id))
            return {"decision": "accept" if accepted else "decline"}
        except asyncio.TimeoutError:
            self.store.conn.execute("UPDATE approvals SET state='expired',decided_at=? WHERE approval_id=? AND state='pending'", (now(), approval_id))
            return denied
        except asyncio.CancelledError:
            self.store.conn.execute("UPDATE approvals SET state='expired',decided_at=? WHERE approval_id=? AND state IN ('pending','approved')", (now(), approval_id))
            raise
        except Exception:
            self.store.conn.execute("UPDATE approvals SET state='denied',decided_at=? WHERE approval_id=? AND state='pending'", (now(), approval_id))
            return denied
        finally:
            self.pending.pop(approval_id, None)

    async def callback(self, query):
        answer = "잘못된 승인 요청입니다."
        accepted = False
        try:
            if len(query.get("data", "")) > 64:
                raise ValueError()
            prefix, identifier, nonce, decision = query.get("data", "").split(":")
            if prefix != "a" or decision not in {"yes", "no"}:
                raise ValueError()
            approval_id = int(identifier)
            entry = self.pending.get(approval_id)
            row = self.store.conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not entry or not row or row["state"] != "pending" or row["expires_at"] <= now():
                raise ValueError()
            future, connection, thread, turn, request_id = entry
            message = query.get("message", {})
            job = self.store.job(row["job_id"])
            if job['permission_mode'] == 'network' and job.get('network_until', 0) <= time.time():
                raise ValueError()
            if (query.get("from", {}).get("id") != self.config.user_id
                or message.get("chat", {}).get("id") != self.config.chat_id
                or message.get("chat", {}).get("type") != "private"
                or message.get("message_id") != row["telegram_message_id"]
                or not hmac.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), row["nonce_hash"])
                or row["connection_id"] != connection or row["thread_id"] != thread or row["turn_id"] != turn
                or row["rpc_request_id"] != json.dumps(request_id) or future.done()
                or job["state"] != "running" or job["codex_turn_id"] != turn or job["cancel_requested"]):
                raise ValueError()
            accepted = decision == "yes"
            self.store.conn.execute("UPDATE approvals SET state=?,decided_at=? WHERE approval_id=? AND state='pending'",
                                    ("approved" if accepted else "denied", now(), approval_id))
            future.set_result(accepted)
            answer = "이번 요청만 승인했습니다." if accepted else "거부했습니다."
        except (ValueError, TypeError, KeyError, AttributeError):
            answer = "만료됐거나 잘못된 승인 버튼입니다."
        try:
            await self.transport.answer_callback(query["id"], answer)
        except Exception:
            pass
        return accepted

    def expire_job(self, job_id):
        rows = self.store.conn.execute("SELECT approval_id FROM approvals WHERE job_id=? AND state IN ('pending','approved')", (job_id,)).fetchall()
        self.store.conn.execute("UPDATE approvals SET state='expired',decided_at=? WHERE job_id=? AND state IN ('pending','approved')", (now(), job_id))
        for row in rows:
            entry = self.pending.get(row[0])
            if entry and not entry[0].done():
                entry[0].set_result(False)
