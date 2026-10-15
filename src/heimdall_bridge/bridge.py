import asyncio
import base64
import fcntl
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import __version__
from .codex import ConnectionLost, RpcRejected, TurnUncertain
from .config import ConfigError
from .db import now
from .approvals import ApprovalManager
from .commands import CommandHandler
from .assets import AssetStore, AssetError
from .knowledge import Knowledge
from .outgoing import prepare_images, queue_images

HELP = """Heimdall · Codex 세션 브리지
/database [페이지 또는 검색어] — 세션 목록
/select <전체 ID 또는 고유 접두사> — 연결
/new [이름] — 새 세션 생성·연결
/current — 현재 연결
/status — 연결·권한·큐 상태
/memory · /memories — 스레드 대화 / Codex 메모리 / Heimdall 저장소 구분·조회
/queue — 내 작업 상태
/cancel — 실행 중 작업 또는 첫 대기 작업 취소
/mode read|write|network — 다음 작업 권한 (network: 30분 임시 인터넷·일반 파일 쓰기)
/rename <이름> · /history [개수] · /fork [이름] · /release
/jobs · /job <ID> · /recover · /retry <ID> [repeat]
/resolve <ID> failed|cancelled — 불확실 상태 수동 확정
/deliveries · /resend <ID> [duplicate] · /backup
/assets [개수] · /asset <ID> — 저장 자료 확인
/images <작업 ID> — 완료 답변의 로컬 이미지를 사진으로 전송
/save <메모> · /items · /item HK-000001 · /tag HK-000001 태그
/search <검색어> · /ask <자료 질문> · /extract HK-000001 · /processing
/forget HK-000001 — 검색에서 제외 (원본 삭제 아님)
/help — 도움말
일반 텍스트 원문은 선택 세션에 전달하고, 별도 입력으로 메모리·보호 정책 안내를 덧붙입니다.
사진은 선택 세션에 캡션+이미지 전달. 문서·영상은 /extract로 추출, 음성은 보관만 합니다.
기본 읽기 전용. 쓰기 모드 승인 버튼은 이번 요청만, 만료 시 거부.
같은 세션을 CLI와 Telegram에서 동시에 사용하지 마세요."""


def snapshot(thread, codex_home):
    result = {"updatedAt": thread.get("updatedAt")}
    path = thread.get("path")
    if path:
        path = Path(path).resolve()
        if not path.is_relative_to((codex_home / "sessions").resolve()):
            raise ConfigError("unexpected_rollout_path")
        try:
            info = path.stat()
        except FileNotFoundError:
            # Newly-created threads can have a rollout path before the first item is written.
            result["rollout_exists"] = False
        else:
            result.update(rollout_exists=True, size=info.st_size, mtime_ns=info.st_mtime_ns)
    return json.dumps(result, sort_keys=True)


class PreflightBlocked(RuntimeError):
    """A refusal known to occur before turn/start; no execution ambiguity."""


def external_cli_busy(thread_id, cwd, *, proc_root=Path("/proc"), own_pid=None):
    # Session-specific evidence only. Sharing a cwd does not imply sharing a thread.
    # Uncooperative clients can still race; never log command lines or environment values.
    own_pid = os.getpid() if own_pid is None else own_pid
    sessions = (Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions").resolve()
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid() or (proc / "comm").read_text().strip() != "codex":
                continue
            args = (proc / "cmdline").read_bytes().split(b"\x00")
            if b"app-server" in args:
                # Persistent servers keep idle rollouts open. Worker checks authoritative
                # persisted turn status instead of treating these handles as execution locks.
                continue
            if thread_id.encode() in args:
                return True
            for fd in (proc / "fd").iterdir():
                try:
                    path = Path(os.readlink(fd).removesuffix(" (deleted)")).resolve()
                    if path.is_relative_to(sessions) and path.name.endswith("-" + thread_id + ".jsonl"):
                        return True
                except OSError:
                    continue
        except (OSError, ValueError, IndexError):
            continue
    return False


class Registry:
    def __init__(self, adapter, config, store):
        self.adapter, self.config, self.store = adapter, config, store
        self.cache = {}

    async def page(self, number=1, search=None):
        if not 1 <= number <= 5:
            raise ValueError("목록은 1~5페이지까지 조회 가능합니다. 검색어로 범위를 줄여주세요.")
        cursor = None
        for i in range(1, number + 1):
            key = (cursor, search)
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                result = cached[1]
            else:
                result = await self.adapter.list_threads(cursor, search)
                self.cache[key] = (time.monotonic(), result)
                for thread in result["data"]:
                    self.config.check_thread(thread)
                    self.store.cache(thread)
            if i == number:
                return result
            cursor = result.get("nextCursor")
            if not cursor:
                raise ValueError("해당 페이지가 없습니다.")

    async def resolve(self, identifier):
        if not identifier or len(identifier) > 200 or any(c.isspace() for c in identifier):
            raise ValueError("세션 ID를 확인해주세요.")
        # Exact IDs are opaque server strings, not hard-coded UUIDs.
        try:
            thread = await self.adapter.read_thread(identifier)
        except RpcRejected:
            thread = None
        if thread:
            self.config.check_thread(thread)
            return thread
        matches = {}
        for number in range(1, 6):
            page = await self.page(number)
            for item in page["data"]:
                if item["id"].startswith(identifier):
                    matches[item["id"]] = item
            if len(matches) > 1:
                raise ValueError("접두사가 여러 세션과 일치합니다. 전체 ID를 입력해주세요.")
            if not page.get("nextCursor"):
                break
        else:
            raise ValueError("조회 범위를 초과해 고유 접두사를 확인할 수 없습니다. 전체 ID를 입력해주세요.")
        if not matches:
            raise ValueError("세션을 찾지 못했습니다.")
        return await self.adapter.read_thread(next(iter(matches)))


class Bridge:
    def __init__(self, config, store, adapter, transport, *, conflict_check=external_cli_busy):
        self.config, self.store, self.adapter, self.transport = config, store, adapter, transport
        self.registry = Registry(adapter, config, store)
        self.conflict_check = conflict_check
        self.running_job = None
        self.approvals = ApprovalManager(store, config, transport)
        self.adapter.approval_handler = self.approvals.handle
        self.commands = CommandHandler(self)
        self.assets = AssetStore(store, config)
        self.knowledge = Knowledge(store, config)

    def authorized(self, message):
        user, chat = message.get("from", {}), message.get("chat", {})
        return (type(user.get("id")) is int and type(chat.get("id")) is int
            and user["id"] == self.config.user_id and chat["id"] == self.config.chat_id
            and chat.get("type") == "private" and not user.get("is_bot", False)
            and not any(k in message for k in ("forward_origin", "forward_from", "forward_sender_name", "sender_chat")))

    async def handle(self, update):
        uid = update.get("update_id")
        if type(uid) is not int:
            return
        if "callback_query" in update:
            with self.store.transaction():
                if not self.store.accept_update(uid):
                    return
                self.store.handled(uid)
            await self.approvals.callback(update["callback_query"])
            return
        message = update.get("message", {})
        if not self.authorized(message):
            with self.store.transaction():
                self.store.accept_update(uid)
                self.store.handled(uid)
            return
        text = message.get("text")
        chat, user = message["chat"]["id"], message["from"]["id"]
        if any(key in message for key in ("photo", "document", "voice", "audio", "video", "video_note", "animation")):
            await self.receive_asset(uid, message, chat, user)
            return
        if not isinstance(text, str) or not text:
            with self.store.transaction():
                if self.store.accept_update(uid):
                    self.store.reply(chat, "지원하지 않는 메시지입니다. /help 를 확인하세요.", f"update:{uid}")
                    self.store.handled(uid)
            return
        if not text.startswith("/"):
            thread_id = self.store.binding(chat)
            baseline = None
            if thread_id:
                try:
                    baseline = snapshot(await self.adapter.read_thread(thread_id), self.config.codex_home)
                except Exception:
                    baseline = None
            # Update ID, immutable target job and receipt are committed together.
            with self.store.transaction():
                if not self.store.accept_update(uid):
                    return
                if not thread_id:
                    body = "먼저 /database 와 /select 또는 /new 를 사용하세요."
                elif baseline is None:
                    body = "선택된 세션을 확인할 수 없습니다. 실행하지 않았습니다."
                elif shutil.disk_usage(self.config.data_dir).free < self.config.min_disk_bytes:
                    body = "저장 공간이 부족해 신규 작업을 받지 않습니다."
                else:
                    try:
                        job = self.store.enqueue(chat, user, message["message_id"], thread_id, text,
                                                 baseline, self.config.max_queue)
                        body = f"📥 작업 #{job} 대기 등록"
                    except ValueError:
                        body = "대기 큐가 가득 찼습니다."
                self.store.reply(chat, body, f"update:{uid}")
                self.store.handled(uid)
            return
        # Side-effecting command claims survive crashes: never replay /new ambiguously.
        with self.store.transaction():
            if not self.store.accept_update(uid):
                return
            if self.store.conn.execute("INSERT OR IGNORE INTO command_messages VALUES(?,?)",
                (chat, message["message_id"])).rowcount == 0:
                self.store.handled(uid)
                return
        try:
            body = await self.command(text, chat, user, message["message_id"])
        except (ValueError, ConfigError) as exc:
            body = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, ConfigError) else "허용된 작업 경로의 세션만 사용할 수 있습니다."
        except Exception:
            body = "명령 처리 실패 또는 결과 불확실. /status로 확인하세요. 자동 재시도하지 않습니다."
        with self.store.transaction():
            self.store.reply(chat, body, f"update:{uid}")
            self.store.handled(uid)

    async def receive_asset(self, uid, message, chat, user):
        # Claim before download; never repeat an ambiguous reception after a crash.
        thread_id = self.store.binding(chat)
        mode = self.store.permission_mode(chat)
        with self.store.transaction():
            if not self.store.accept_update(uid):
                return
        try:
            record = await self.assets.receive(message, self.transport, chat, user)
            body = f"자료 #{record['asset_id']} · {record['state']}\n{record['guidance']}"
            if record["state"] == "stored":
                try:
                    with self.store.transaction():
                        knowledge_id = self.knowledge.ingest_asset(record, chat, user)
                    body += f"\n지식 자료: {knowledge_id} · /extract {knowledge_id} 로 본문·글자·장면 추출"
                except ValueError:
                    body += "\n지식 인덱스 한도 등으로 검색 등록은 실패했습니다. 첨부 원본은 보존합니다."
            if record["can_local_image"] and thread_id:
                baseline = snapshot(await self.adapter.read_thread(thread_id), self.config.codex_home)
                await self.assets.local_image_async(record["asset_id"], chat, user)
                with self.store.transaction():
                    existing = self.store.conn.execute("SELECT job_id FROM jobs WHERE chat_id=? AND telegram_message_id=?",
                                                       (chat, message["message_id"])).fetchone()
                    if existing:
                        job_id = existing[0]
                    else:
                        job_id = self.store.enqueue(chat, user, message["message_id"], thread_id,
                            record["caption"] or "첨부 이미지를 설명해주세요.", baseline, self.config.max_queue)
                        self.store.conn.execute("UPDATE jobs SET image_assets=?,permission_mode=? WHERE job_id=?",
                            (json.dumps([record["asset_id"]]), mode, job_id))
                    body += f"\n📥 이미지 작업 #{job_id} 대기 등록"
            elif record["can_local_image"]:
                body += "\n선택된 세션이 없어 질문은 실행하지 않았습니다. /select 후 사진을 새로 보내주세요."
        except (AssetError, ValueError):
            body = "첨부 저장 또는 작업 등록 실패. /assets 와 /jobs 로 확인하세요. 용량·형식을 확인한 뒤 새 메시지로 보내주세요."
        except Exception:
            body = "첨부 처리 결과를 확인할 수 없습니다. /assets 로 확인하세요. 자동 재실행하지 않습니다."
        with self.store.transaction():
            self.store.reply(chat, body, f"update:{uid}")
            self.store.handled(uid)

    async def command(self, text, chat, user, message_id=None):
        first, _, arg = text.partition(" ")
        command, at, target = first.partition("@")
        if at and target.casefold() != (self.transport.username or "").casefold():
            return "다른 봇을 지정한 명령입니다."
        arg = arg.strip()
        result = await self.commands.handle(command, arg, chat, user, message_id)
        if result is not None:
            return result
        if command in {"/start", "/help"}:
            return HELP
        if command == "/database":
            number = int(arg) if arg.isdecimal() else 1
            page = await self.registry.page(number, arg if arg and not arg.isdecimal() else None)
            lines = ["🛡 Heimdall · Codex Threads"]
            for thread in page["data"]:
                cached = self.store.conn.execute("SELECT alias FROM sessions WHERE thread_id=?", (thread["id"],)).fetchone()
                name = (cached[0] or thread.get("name") or "이름 없음").replace("\n", " ")[:70]
                mark = "●" if self.store.binding(chat) == thread["id"] else "○"
                lines.append(f"{mark} {name}\n{thread['id']}")
            if not page["data"]:
                lines.append("세션이 없습니다. /new 로 생성하세요.")
            lines.append("/select <ID>")
            if page.get("nextCursor"):
                lines.append(f"다음: /database {number + 1}")
            return "\n\n".join(lines)
        if command == "/select":
            thread = await self.registry.resolve(arg)
            self.config.check_thread(thread)
            self.store.cache(thread)
            self.store.bind(chat, user, thread["id"])
            return f"✅ 연결: {thread['id']}\n{self.config.cwd}"
        if command == "/new":
            if len(arg) > 100:
                raise ValueError("별칭은 100자 이하로 입력해주세요.")
            thread = await self.adapter.new_thread()
            with self.store.transaction():
                self.store.cache(thread, owned=True, alias=arg or None)
                self.store.bind(chat, user, thread["id"])
            self.registry.cache.clear()
            return f"✅ 새 세션 연결: {arg or '이름 없음'}\n{thread['id']}"
        if command == "/current":
            active = self.store.binding(chat)
            if not active:
                return "선택된 세션이 없습니다."
            try:
                thread = await self.adapter.read_thread(active)
                status = thread.get("status", {}).get("type", "unknown")
            except Exception:
                status = "unavailable"
            return f"선택 세션: {active}\n경로: {self.config.cwd}\n상태: {status}"
        if command == "/queue":
            return "\n".join(f"#{j['job_id']} · {j['state']} · {j['thread_id'][:12]}" for j in self.store.jobs(chat)) or "대기 작업이 없습니다."
        if command == "/status":
            delivery = self.store.conn.execute("SELECT count(*) FROM outbox WHERE state IN ('failed','uncertain')").fetchone()[0]
            jobs = self.store.jobs(chat)
            counts = {state: sum(j["state"] == state for j in jobs) for state in ("queued", "dispatching", "running", "uncertain")}
            memory = "available" if "memory" in Path("/sys/fs/cgroup/cgroup.controllers").read_text().split() else "unavailable (kernel)"
            knowledge_count = self.store.conn.execute("SELECT count(*) FROM items WHERE chat_id=? AND user_id=? AND deleted_at IS NULL", (chat,user)).fetchone()[0]
            processing_count = self.store.conn.execute("SELECT count(*) FROM processing_jobs WHERE state IN ('queued','preparing','model_queued','uncertain') AND chat_id=? AND user_id=?", (chat,user)).fetchone()[0]
            return (f"Heimdall v{__version__}\nCodex: {'ready' if self.adapter.ready else 'disconnected'} · {self.adapter.version}"
                f"\nTelegram: {'connected' if self.transport.connected else 'unknown'}"
                f"\nActive thread: {self.store.binding(chat) or '-'}"
                f"\nPermissions: {self.store.permission_mode(chat)}\nQueue: {counts}\nDelivery failed/uncertain: {delivery}"
                + (f"\nNetwork mode: ON · 남은 {max(0, int(self.store.network_deadline(chat) - time.time()))}초"
                   if self.store.network_deadline(chat) else '\nNetwork mode: OFF')
                + (f"\nTemporary access: 일반 파일 쓰기·인터넷 허용 · 남은 {max(0, int(self.config.temporary_access_until - time.time()))}초 · 승인/비밀 보호 유지"
                   if self.config.temporary_active() else "\nTemporary access: off")
                + f"\nApproval pending: {len(self.approvals.pending)}"
                f"\nKnowledge: {knowledge_count} · processing/uncertain: {processing_count}"
                f"\nRAM cgroup (AI 메모리 아님): {memory}"
                "\n대화·Codex 메모리·지식 저장소 구분: /memory"
                f"\nDisk available: {shutil.disk_usage(self.config.data_dir).free // (1024**2)} MiB"
                "\nKakao-Radar: independent")
        if command == "/cancel":
            jobs = self.store.jobs(chat)
            active = next((j for j in jobs if j["state"] in {"dispatching", "running"}), None)
            if active:
                self.store.conn.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (active["job_id"],))
                self.approvals.expire_job(active["job_id"])
                if active["codex_turn_id"]:
                    await self.adapter.interrupt(active["thread_id"], active["codex_turn_id"])
                return f"작업 #{active['job_id']} 중단 요청. 실제 종료 확인을 기다립니다."
            queued = next((j for j in jobs if j["state"] == "queued"), None)
            if queued:
                self.store.transition(queued["job_id"], "cancelled")
                return f"작업 #{queued['job_id']} 실행 전 취소"
            return "취소 가능한 작업이 없습니다. uncertain 작업은 자동 재실행하지 않습니다."
        return "알 수 없는 명령입니다. /help 를 확인하세요."

    async def reconcile(self):
        for job in self.store.jobs():
            if job["state"] != "uncertain" or not job["codex_turn_id"]:
                continue
            try:
                await self.adapter.read_thread(job["thread_id"])
                result = await self.adapter.reconcile(job["thread_id"], job["codex_turn_id"])
                if result:
                    await self.finish_with_images(job, result, recovered=True)
            except Exception:
                continue

    async def finish_with_images(self, job, result, recovered=False):
        prepared, rejected = [], 0
        if result.get('status') == 'completed' and not job.get('knowledge_context'):
            prepared, rejected = await asyncio.to_thread(prepare_images, result.get('text', ''), self.config)
        self.finish(job, result, recovered=recovered, prepared=prepared, image_rejected=rejected)

    def finish(self, job, result, recovered=False, prepared=(), image_rejected=0):
        state = {"completed": "completed", "failed": "failed", "interrupted": "cancelled"}.get(result["status"], "uncertain")
        text = result.get("text", "")
        with self.store.transaction():
            knowledge_error = False
            if state == "completed" and job.get("knowledge_context"):
                try:
                    text = self.knowledge.finish(job, text)
                except (ValueError, KeyError, TypeError):
                    state, text, knowledge_error = "failed", "자료 추출 또는 답변의 근거 검증에 실패했습니다. 자동 재실행하지 않습니다.", True
            elif state == "completed" and json.loads(job.get("image_assets", "[]")):
                for asset_id in json.loads(job["image_assets"]):
                    row = self.store.conn.execute("SELECT item_id FROM items WHERE asset_id=? AND chat_id=? AND user_id=? AND deleted_at IS NULL",
                                                  (asset_id, job["chat_id"], job["user_id"])).fetchone()
                    if row:
                        try:
                            self.knowledge.add_content(row[0], "codex-description-v1", text[:20000])
                        except ValueError:
                            pass  # Retrieval failure must not discard an ordinary successful answer.
            self.store.transition(job["job_id"], state, response_text=text,
                                  error_code=None if state == "completed" else "knowledge_validation_failed" if knowledge_error else result.get("error_code") or result["status"])
            body = f"✅ 작업 #{job['job_id']}\n{text or '(정식 최종 텍스트 없음)'}" if state == "completed" else f"작업 #{job['job_id']}: {state}. 취소는 기존 변경을 되돌리지 않습니다."
            if knowledge_error:
                body += "\n" + text
            if result.get("error_code") in {"usageLimitExceeded", "sessionBudgetExceeded", "rateLimitExceeded"}:
                body = (f"작업 #{job['job_id']}: Codex 사용량·속도 제한 ({result['error_code']}). "
                        "한도 회복 후 새 질문을 보내주세요. 자동 재실행하지 않습니다.")
            key = f"job:{job['job_id']}:recovered" if recovered else f"job:{job['job_id']}:final"
            self.store.reply(job["chat_id"], body, key)
            if state == 'completed' and not job.get('knowledge_context'):
                parent = self.store.conn.execute('SELECT delivery_id FROM outbox WHERE dedupe=?', (key,)).fetchone()[0]
                queued, rejected = queue_images(self.store, self.config, job, prepared,
                    f"job:{job['job_id']}:images", parent)
                if queued:
                    for photo in prepared:
                        body = body.replace(photo['reference'], '🖼 이미지 첨부: ' + (photo['caption'] or '이미지'))
                if image_rejected + rejected:
                    body += '\n일부 이미지는 경로·형식·용량 제한으로 첨부하지 못했습니다.'
                self.store.conn.execute('UPDATE outbox SET body=? WHERE delivery_id=?', (body, parent))
            self.sync_processing(job["job_id"])

    def sync_processing(self, job_id):
        job = self.store.job(job_id)
        if job and job["state"] in {"failed", "cancelled", "uncertain"}:
            state = "uncertain" if job["state"] == "uncertain" else "failed"
            self.store.conn.execute("UPDATE processing_jobs SET state=?,error_code=?,finished_at=? WHERE job_id=? AND state='model_queued'",
                                    (state, job["error_code"] or job["state"], now(), job_id))

    async def process_knowledge_once(self):
        from .extraction import prepare
        row = self.store.conn.execute("SELECT * FROM processing_jobs WHERE state='queued' ORDER BY processing_id LIMIT 1").fetchone()
        if not row:
            return False
        task = dict(row)
        number = task["processing_id"]
        self.store.conn.execute("UPDATE processing_jobs SET state='preparing' WHERE processing_id=?", (number,))
        try:
            item = self.knowledge.get(task["item_id"], task["chat_id"], task["user_id"])
            record, data = await self.assets.validated_data(item["asset_id"], task["chat_id"], task["user_id"])
            images = []
            if record["can_local_image"]:
                images = [record["asset_id"]]
                prepared = {"text": ""}
            else:
                prepared = await prepare(self.config, record, data)
                if prepared.get("unsupported"):
                    self.store.conn.execute("UPDATE processing_jobs SET state='unsupported',error_code=?,finished_at=? WHERE processing_id=?",
                        (prepared["unsupported"], now(), number))
                    self.store.reply(task["chat_id"], f"{item['id']} · 지원하지 않는 원본 입력 형식입니다. 음성 전사는 현재 Codex 일반 입력에서 지원하지 않습니다. 원본·캡션은 보관했습니다.", f"processing:{number}:final")
                    return True
                payloads = prepared.get("images", [])
                if not isinstance(payloads, list) or len(payloads) > 3:
                    raise ValueError("invalid_prepared_images")
                for slot, payload in enumerate(payloads, 1):
                    decoded = base64.b64decode(payload, validate=True)
                    images.append(self.assets.import_image(record["asset_id"], slot, decoded, task["chat_id"], task["user_id"]))
            # Revalidate after await: a concurrently forgotten source stays excluded.
            self.knowledge.get(item["item_id"], task["chat_id"], task["user_id"])
            if not images:
                text = prepared.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("no_extractable_text")
                with self.store.transaction():
                    self.knowledge.add_content(item["item_id"], "document-v1", text)
                    self.store.conn.execute("UPDATE processing_jobs SET state='completed',finished_at=? WHERE processing_id=?", (now(), number))
                    self.store.reply(task["chat_id"], f"{item['id']} · 문서 본문 추출 완료. /search 로 검색할 수 있습니다.", f"processing:{number}:final")
                return True
            thread = await self.adapter.new_thread()
            with self.store.transaction():
                self.store.cache(thread, owned=True, alias=f"자료 추출 {item['id']}")
                if prepared.get("text"):
                    self.knowledge.add_content(item["item_id"], "document-v1", prepared["text"])
                job_id = self.store.enqueue(task["chat_id"], task["user_id"], -number, thread["id"],
                    f"{item['id']} 자료 내용 추출", snapshot(thread, self.config.codex_home), self.config.max_queue)
                metadata = {"purpose": "extract", "item_id": item["item_id"], "processing_id": number, "source_kind": record["kind"]}
                self.store.conn.execute("UPDATE jobs SET permission_mode='readonly',image_assets=?,knowledge_context=? WHERE job_id=?",
                    (json.dumps(images), json.dumps(metadata), job_id))
                self.store.conn.execute("UPDATE processing_jobs SET state='model_queued',job_id=? WHERE processing_id=?", (job_id, number))
                self.store.reply(task["chat_id"], f"{item['id']} · Codex 추출 작업 #{job_id} 등록 (읽기 전용)", f"processing:{number}:queued")
        except asyncio.CancelledError:
            # Local preparation has no model side effects; only this stage is safe to redo.
            self.store.conn.execute("UPDATE processing_jobs SET state='queued' WHERE processing_id=? AND state='preparing'", (number,))
            raise
        except Exception:
            self.store.conn.execute("UPDATE processing_jobs SET state='failed',error_code='preparation_failed',finished_at=? WHERE processing_id=? AND state='preparing'", (now(), number))
            self.store.reply(task["chat_id"], f"자료 추출 #{number}: 준비 실패. 원본은 보존했습니다. /processing 으로 확인하세요.", f"processing:{number}:final")
        return True

    async def work_once(self):
        self.expire_network_mode()
        # An uncertain previous turn might still be executing. Preserve the global
        # single-turn guarantee even when a queued job belongs to another thread.
        if self.store.conn.execute("SELECT 1 FROM jobs WHERE state='queued'").fetchone():
            for previous in self.store.jobs():
                if previous["state"] != "uncertain":
                    continue
                try:
                    await self.adapter.read_thread(previous["thread_id"])
                    if await self.adapter.thread_is_active(previous["thread_id"]):
                        return False
                except Exception:
                    return False
        job = self.store.claim()
        if not job:
            return False
        self.running_job = job["job_id"]
        try:
            thread = await self.adapter.read_thread(job["thread_id"])
            if any(j["thread_id"] == job["thread_id"] and j["state"] == "uncertain" for j in self.store.jobs()):
                raise PreflightBlocked("thread_has_uncertain_job")
            if snapshot(thread, self.config.codex_home) != job["baseline"]:
                raise PreflightBlocked("thread_changed_before_dispatch")
            if job['permission_mode'] == 'network' and job['network_until'] <= time.time():
                raise PreflightBlocked('network_window_expired')
            if self.conflict_check(job["thread_id"], self.config.cwd):
                raise PreflightBlocked("selected_thread_in_use")
            if thread.get("status", {}).get("type") == "active" or await self.adapter.thread_is_active(job["thread_id"]):
                raise PreflightBlocked("thread_active")
            if snapshot(thread, self.config.codex_home) != job["baseline"]:
                raise PreflightBlocked("thread_changed_before_dispatch")
            lock_path = self.config.data_dir / ("thread-" + hashlib.sha256(job["thread_id"].encode()).hexdigest() + ".lock")
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise PreflightBlocked("thread_lock_busy") from None
                if self.store.job(job["job_id"])["cancel_requested"]:
                    self.store.transition(job["job_id"], "cancelled")
                    self.store.reply(job["chat_id"], f"작업 #{job['job_id']} 실행 전 취소", f"job:{job['job_id']}:final")
                    return True
                self.store.reply(job["chat_id"], f"⏳ Codex 실행 중 · 작업 #{job['job_id']}", f"job:{job['job_id']}:progress")
                def started(turn_id):
                    self.store.transition(job["job_id"], "running", codex_turn_id=turn_id)
                    if self.store.job(job["job_id"])["cancel_requested"]:
                        asyncio.create_task(self.adapter.interrupt(job["thread_id"], turn_id))
                async def progress(text):
                    self.store.reply(job["chat_id"], text, f"job:{job['job_id']}:denied")
                kwargs = {"permission_mode": "workspace"} if job["permission_mode"] == "workspace" else {}
                if job['permission_mode'] == 'network':
                    kwargs = {'permission_mode':'network', 'network_until':job['network_until']}
                asset_ids = json.loads(job["image_assets"])
                if not isinstance(asset_ids, list) or len(asset_ids) > 10 or any(type(i) is not int for i in asset_ids):
                    raise ConfigError("invalid_image_assets")
                if asset_ids:
                    try:
                        paths = []
                        for identifier in asset_ids:
                            item = await self.assets.local_image_async(identifier, job["chat_id"], job["user_id"])
                            paths.append(item["path"])
                        kwargs["image_paths"] = tuple(paths)
                    except (AssetError, OSError):
                        raise PreflightBlocked("image_asset_unavailable_or_changed") from None
                try:
                    prompt = self.knowledge.prompt(job) if job.get("knowledge_context") else job["prompt"]
                except (ValueError, KeyError, TypeError):
                    raise PreflightBlocked("knowledge_source_unavailable") from None
                result = await self.adapter.run_turn(job["thread_id"], prompt, started, progress, **kwargs)
                await self.finish_with_images(job, result)
                # Our own completed turn is expected history activity for subsequent queued prompts.
                fresh = snapshot(await self.adapter.read_thread(job["thread_id"]), self.config.codex_home)
                self.store.conn.execute("UPDATE jobs SET baseline=? WHERE state='queued' AND thread_id=? AND baseline=?",
                                        (fresh, job["thread_id"], job["baseline"]))
        except asyncio.CancelledError:
            current = self.store.job(job["job_id"])
            if current["codex_turn_id"]:
                try:
                    await self.adapter.interrupt(job["thread_id"], current["codex_turn_id"])
                except Exception:
                    pass
            if current["state"] in {"dispatching", "running"}:
                self.store.transition(job["job_id"], "uncertain", error_code="worker_shutdown")
            raise
        except Exception as exc:
            current = self.store.job(job["job_id"])
            if current["state"] in {"dispatching", "running"}:
                state = "failed" if isinstance(exc, (PreflightBlocked, RpcRejected, ConfigError)) and current["state"] == "dispatching" else "uncertain"
                code = str(exc) if isinstance(exc, (PreflightBlocked, TurnUncertain, RpcRejected)) else type(exc).__name__
                with self.store.transaction():
                    self.store.transition(job["job_id"], state, error_code=code)
                    if isinstance(exc, PreflightBlocked):
                        body = f"작업 #{job['job_id']}: 실행 전 거절 ({code}). Codex에 질문을 전달하지 않았습니다. 해당 세션 상태를 확인한 뒤 새 질문을 보내주세요."
                        if code == 'network_window_expired':
                            body = f"작업 #{job['job_id']}: 네트워크 허용 시간이 만료되어 실행하지 않았습니다. /mode network 후 새 질문을 보내주세요. 기존 작업은 자동 재실행하지 않습니다."
                    elif isinstance(exc, RpcRejected) and code == "thread_already_active":
                        body = (f"작업 #{job['job_id']}: 이 세션은 다른 Codex 연결에서 열려 있어 재개가 거절됐습니다. "
                            "해당 세션만 다른 앱에서 닫거나, /new 또는 /select로 다른 세션을 선택해주세요. "
                            "질문은 실행하지 않았습니다.")
                    else:
                        body = f"작업 #{job['job_id']}: {state} ({code}). 자동 재실행하지 않습니다."
                    self.store.reply(job["chat_id"], body, f"job:{job['job_id']}:final")
        finally:
            self.sync_processing(job["job_id"])
            self.approvals.expire_job(job["job_id"])
            self.running_job = None
        return True

    def expire_network_mode(self):
        row = self.store.conn.execute("SELECT network_until FROM chat_bindings WHERE chat_id=? AND user_id=? AND permission_mode='network' AND network_until<=?",
            (self.config.chat_id, self.config.user_id, time.time())).fetchone()
        if row:
            with self.store.transaction():
                self.store.set_permission_mode(self.config.chat_id, 'readonly')
                self.store.reply(self.config.chat_id,
                    '네트워크 모드 30분이 만료되어 읽기 전용·네트워크 차단으로 복귀했습니다. 다시 허용하려면 /mode network',
                    f'network-expired:{self.config.chat_id}:{row[0]}')
