"""Owner-only operational commands; the gateway authorizes before dispatch."""
import asyncio
import json
import time
from datetime import datetime

from .operations import Operations
from .knowledge_backup import backup_bundle
from .db import now
from .memory import memory_report
from .memory_commands import handle_memory
from .knowledge import title_suffix
from .outgoing import prepare_images, queue_images


class CommandHandler:
    def __init__(self, bridge):
        self.bridge = bridge
        self.ops = Operations(bridge.store, bridge.config)

    async def handle(self, command, arg, chat, user, message_id):
        b = self.bridge
        active = b.store.binding(chat)
        if command == '/knowledge':
            b.knowledge.authorize(chat, user)
            verb, _, rest = arg.strip().partition(' ')
            if verb == 'auto':
                verb, _, rest = rest.strip().partition(' ')
                if not verb:
                    raise ValueError('/knowledge auto on|off · /knowledge status · /knowledge test <질문>')
                if verb in {'on', 'off'} and not rest.strip():
                    with b.store.transaction():
                        b.auto_memory.settings.set_auto_retrieve(chat, user, verb == 'on')
                    return ('자동 검색 ON · 다음 일반 읽기 전용 텍스트 질문부터 적용됩니다. '
                            '쓰기·network·임시 권한에서는 중지됩니다. 자동 캡처는 켜지지 않습니다.' if verb == 'on' else
                            '자동 검색 OFF · 대기 작업의 추가 참고 문맥도 제외합니다. '
                            '이미 Codex에 전달된 참고자료·기존 대화·백업은 삭제하지 않습니다.')
            if verb in {'', 'status'} and not rest.strip():
                return b.auto_memory.status(chat, user)
            if verb == 'test' and rest.strip():
                result = b.auto_memory.preview(rest, chat, user, permission_mode=b.store.permission_mode(chat))
                reasons = {'hits': '고신뢰 관련 자료', 'unfocused_query': '인사·단일 일반 키워드 등 검색 생략',
                           'non_readonly': '현재 권한에서는 자동 검색 중지',
                           'no_high_confidence_hits': '충분히 관련된 자료 없음'}
                body = '\n'.join(f'{ref.id} · 점수 {score}' for ref, score in zip(result.refs, result.scores))
                return (f'자동 검색 미리보기 · {len(result.refs)}개 · {reasons.get(result.reason, "검색 생략")}\n'
                        + (body + '\n' if body else '')
                        + '설정 변경·모델 호출·자료 원문 출력 없이 로컬 검색만 했습니다. 점수는 사실 검증이 아닙니다.')
            raise ValueError('/knowledge auto on|off · /knowledge status · /knowledge test <질문>')
        if command == '/images':
            if not arg.isdecimal() or message_id is None:
                raise ValueError('/images <완료된 작업 ID>')
            number = int(arg)
            self.ops.inspect(number, chat, user)
            job = b.store.job(number)
            if job['state'] != 'completed' or job.get('knowledge_context'):
                raise ValueError('일반 대화의 완료된 작업만 이미지를 다시 전송할 수 있습니다.')
            prepared, rejected = await asyncio.to_thread(prepare_images, job['response_text'] or '', b.config)
            with b.store.transaction():
                queued, limited = queue_images(b.store, b.config, job, prepared,
                    f'job:{number}:images-command:{message_id}')
            return f'작업 #{number} 사진 {queued}장 전송 등록 · 제외 {rejected + limited}장. Codex 질문은 재실행하지 않았습니다.'
        if command in {"/memory", "/memories"}:
            b.knowledge.authorize(chat, user)
            if arg:
                return handle_memory(b.knowledge, arg, chat, user, message_id)
            values = await b.adapter.memory_config()
            count = b.store.conn.execute("SELECT count(*) FROM items WHERE chat_id=? AND user_id=? AND deleted_at IS NULL", (chat, user)).fetchone()[0]
            return memory_report(values, active, count,
                auto_retrieve=b.auto_memory.settings.get(chat, user).auto_retrieve,
                auto_allowed=b.store.permission_mode(chat) == 'readonly' and not b.config.temporary_active())
        if command == "/save":
            if message_id is None:
                raise ValueError("메시지 ID가 필요합니다.")
            with b.store.transaction():
                value = b.knowledge.save(chat, user, message_id, arg)
            return f"{value} · 메모 저장 완료. /search 로 검색할 수 있습니다."
        if command in {"/items", "/item", "/search", "/tag", "/forget", "/extract", "/processing", "/ask"}:
            b.knowledge.authorize(chat, user)
            if command == "/items":
                hits = b.knowledge.recent(chat, user, int(arg or "10"))
                return "\n\n".join(f"{h['id']} · {h['kind']} · {h['title']}" for h in hits) or "저장 자료가 없습니다. /save 로 메모를 저장하세요."
            if command == "/item":
                item = b.knowledge.get(arg.upper(), chat, user)
                content = "\n\n".join(f"[{c['extractor']}]\n{c['text']}" for c in item["contents"])
                body = '\n'.join(c['text'] for c in item['contents'])
                return f"{item['id']}{title_suffix(item, body)}\n태그: {', '.join(item['tags']) or '-'}\n\n{content[:16000]}"
            if command == "/search":
                hits = b.knowledge.search(arg, chat, user)
                blocks = []
                for item in hits:
                    body = '\n'.join(c['text'] for c in item['contents'])
                    excerpt = '\n'.join(c['text'][:350] for c in item['contents'])
                    blocks.append(f"{item['id']}{title_suffix(item, body)}\n{excerpt}")
                return '\n\n'.join(blocks) or "기록에서 찾지 못했습니다."
            if command == "/tag":
                parts = arg.split()
                if len(parts) < 2:
                    raise ValueError("/tag GB-000001 태그1 태그2")
                b.knowledge.tag(parts[0].upper(), parts[1:], chat, user)
                return "자료 태그를 저장했습니다."
            if command == "/forget":
                item = b.knowledge.get(arg.upper(), chat, user)
                with b.store.transaction():
                    b.store.conn.execute("UPDATE items SET deleted_at=? WHERE item_id=?", (now(), item['item_id']))
                    b.knowledge._index(item['item_id'])
                return "검색에서 제외했습니다. 첨부 원본·기존 대화·백업을 영구 삭제한 것은 아닙니다."
            if command == "/processing":
                rows = b.store.conn.execute("SELECT processing_id,item_id,state,error_code FROM processing_jobs WHERE chat_id=? AND user_id=? ORDER BY processing_id DESC LIMIT 20", (chat,user))
                return "\n".join(f"#{r[0]} · GB-{r[1]:06d} · {r[2]} · {r[3] or '-'}" for r in rows) or "추출 작업이 없습니다."
            if command == "/extract":
                if message_id is None:
                    raise ValueError("메시지 ID가 필요합니다.")
                number = b.knowledge.schedule(arg.upper(), chat, user, message_id)
                return f"자료 추출 #{number} 등록. 글자·장면 이해는 Codex를 사용하며 사용량에 포함됩니다. /processing"
            if not active or message_id is None:
                raise ValueError("먼저 /select 또는 /new 로 답변을 받을 세션을 선택하세요.")
            context = b.knowledge.context(arg, chat, user)
            if not context:
                return "기록에서 찾지 못했습니다. Codex는 실행하지 않았습니다. /save 또는 /extract 로 검색 자료를 준비하세요."
            from .bridge import snapshot
            baseline = snapshot(await b.adapter.read_thread(active), b.config.codex_home)
            with b.store.transaction():
                job_id = b.store.enqueue(chat,user,message_id,active,arg,baseline,b.config.max_queue)
                metadata = {"purpose":"ask","sources":context}
                b.store.conn.execute("UPDATE jobs SET permission_mode='readonly',knowledge_context=? WHERE job_id=?", (json.dumps(metadata,ensure_ascii=False),job_id))
            return f"📥 자료 질문 작업 #{job_id} 등록 · 근거: " + ", ".join(s['id'] for s in context)
        if command == "/assets":
            records = b.assets.list(chat, user, int(arg or "20"))
            return "\n".join(f"#{r['asset_id']} · {r['kind']} · {r['state']} · {r['size_bytes']} bytes" for r in records) or "저장 자료가 없습니다."
        if command == "/asset":
            record = b.assets.get(int(arg), chat, user)
            return (f"자료 #{record['asset_id']}: {record['state']}\n유형: {record['kind']}\n"
                    f"MIME: {record['mime_type'] or '-'}\n크기: {record['size_bytes']} bytes\n"
                    f"SHA-256: {record['sha256'] or '-'}\n{record['guidance']}")
        if command == "/mode":
            b.knowledge.authorize(chat, user)
            if not active:
                return "먼저 세션을 선택하세요."
            modes = {"read": "readonly", "write": "workspace", 'network':'network'}
            if arg not in modes:
                deadline = b.store.network_deadline(chat)
                remaining = f' · 남은 {max(0, int(deadline - time.time()))}초' if deadline else ''
                return f"현재 모드: {b.store.permission_mode(chat)}{remaining}\n/mode read · /mode write · /mode network (30분)"
            if arg in {"write", 'network'} and not b.config.write_roots:
                return "허용된 쓰기 작업 폴더가 없습니다."
            b.store.set_permission_mode(chat, modes[arg])
            if arg == 'network':
                expiry = datetime.fromtimestamp(b.store.network_deadline(chat)).astimezone().strftime('%H:%M:%S %Z')
                return (f'🌐 네트워크 모드 30분 허용 · 만료 {expiry}\n'
                    '선택 세션의 다음 신규 작업에 인터넷·일반 홈 파일 및 /tmp 쓰기를 허용합니다. '
                    '비밀정보·운영코드 보호와 실행 승인 유지. 기존 대기 작업은 권한을 바꾸지 않습니다.\n'
                    '만료 시 읽기 전용·네트워크 차단으로 자동 복귀합니다. /mode read 로 조기 해제할 수 있습니다.')
            # An explicit non-network mode revokes queued/running network work.
            # It must not execute later using a revoked frozen grant.
            with b.store.transaction():
                for job in b.store.jobs(chat):
                    if job['state'] == 'queued' and job['permission_mode'] == 'network':
                        b.store.transition(job['job_id'], 'cancelled')
                        b.store.reply(chat, f"작업 #{job['job_id']} 네트워크 모드 해제로 실행 전 취소", f"job:{job['job_id']}:network-revoked")
            await b.adapter.revoke_network()
            return (f"다음 신규 작업 모드: {modes[arg]}. 일반 대기 작업은 유지합니다.\n"
                    + ("쓰기 범위:\n" + "\n".join(map(str, b.config.write_roots)) if arg == "write" else "읽기 전용")
                    + '\n네트워크는 차단됩니다. 기존 네트워크 대기 작업은 취소하고 실행 중 네트워크 작업은 중단합니다.')
        if command in {"/rename", "/history", "/fork", "/release"}:
            if not active:
                return "먼저 세션을 선택하세요."
            if command == "/rename":
                if not arg or len(arg) > 100 or "\n" in arg:
                    raise ValueError("/rename <100자 이하 이름>")
                thread = await b.adapter.read_thread(active)
                b.config.check_thread(thread)
                b.store.cache(thread, alias=arg)
                b.registry.cache.clear()
                try:
                    await b.adapter.rename_thread(active, arg)
                except Exception:
                    return "브리지 별칭을 저장했습니다. Codex 이름 동기화는 실패했습니다."
                return "별칭과 Codex 이름을 변경했습니다."
            if command == "/history":
                count = int(arg or "5")
                if not 1 <= count <= 20:
                    raise ValueError("/history [1~20]")
                turns = await b.adapter.history(active, count)
                lines = []
                for turn in reversed(turns):
                    for item in turn.get("items", []):
                        if item.get("type") == "userMessage":
                            content = "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")
                            lines.append("사용자: " + content[:4000])
                        elif item.get("type") == "agentMessage" and item.get("phase") in {None, "final_answer"}:
                            lines.append("Codex: " + item.get("text", "")[:4000])
                return "\n\n".join(lines)[-20000:] or "표시할 사용자·최종 답변 기록이 없습니다."
            if command == "/fork":
                if len(arg) > 100:
                    raise ValueError("별칭은 100자 이하입니다.")
                thread = await b.adapter.fork_thread(active)
                with b.store.transaction():
                    b.store.cache(thread, owned=True, alias=arg or None)
                    b.store.bind(chat, user, thread["id"])
                b.registry.cache.clear()
                return f"새 분기 세션 연결: {thread['id']}\n원본은 유지합니다. 읽기 전용으로 시작합니다."
            if any(j["thread_id"] == active for j in b.store.jobs()):
                return "대기·실행·불확실 작업을 먼저 정리하세요."
            if not await b.adapter.release_thread(active):
                return "이미 해제됐거나 이 브리지의 유휴 연결이 아닙니다. 완료된 작업은 자동 해제하며 다른 CLI 연결은 종료하지 않습니다."
            return "브리지의 세션 연결을 해제했습니다. 기록과 선택 ID는 유지합니다."
        if command == "/jobs":
            jobs = self.ops.status(chat, user, int(arg or "20"))
            return "\n".join(f"#{j['job_id']} · {j['state']} · {j['thread_id'][:12]}" for j in jobs) or "작업이 없습니다."
        if command in {"/job", "/retry", "/resolve"}:
            parts = arg.split()
            if not parts or not parts[0].isdigit():
                raise ValueError(f"{command} <작업 ID>")
            identifier = int(parts[0])
            info = self.ops.inspect(identifier, chat, user)
            job = b.store.job(identifier)
            if command == "/job":
                return (f"작업 #{identifier}: {info['state']}\n세션: {info['thread_id']}\n"
                        f"turn: {info['codex_turn_id'] or '-'}\n오류: {job['error_code'] or '-'}\n"
                        + (job['response_text'] or "정식 최종 답변이 없습니다."))
            if command == "/retry":
                if parts[1:] not in ([], ["repeat"]):
                    raise ValueError("/retry <ID> [repeat]. repeat는 같은 질문을 새로 실행하며 변경이 중복될 수 있습니다.")
                if message_id is None:
                    raise ValueError("메시지 ID가 필요합니다.")
                from .bridge import snapshot
                thread = await b.adapter.read_thread(info["thread_id"])
                if b.conflict_check(info["thread_id"], b.config.cwd) or await b.adapter.thread_is_active(info["thread_id"]):
                    raise ValueError("세션이 사용 중입니다. 실행하지 않았습니다.")
                new_id = self.ops.retry(identifier, chat, user, message_id,
                    snapshot(thread, b.config.codex_home), b.config.max_queue, confirm_repeat=parts[1:] == ["repeat"])
                return f"새 작업 #{new_id} 등록. 원본 작업 #{identifier}는 유지합니다."
            if len(parts) != 2 or parts[1] not in {"failed", "cancelled"}:
                raise ValueError("/resolve <ID> failed|cancelled — 수동 상태 확정, 실행·취소·되돌리기는 하지 않습니다.")
            await b.adapter.read_thread(info["thread_id"])
            if await b.adapter.thread_is_active(info["thread_id"]):
                raise ValueError("세션 실행 중에는 수동 상태 확정할 수 없습니다.")
            self.ops.resolve(identifier, parts[1], chat, user)
            return "불확실 작업 상태를 수동 확정했습니다. 실제 변경을 되돌리거나 질문을 재실행하지 않았습니다."
        if command == "/recover":
            await b.reconcile()
            return "불확실 작업의 실제 turn 결과를 다시 조회했습니다. 자동 재실행하지 않습니다. /jobs 로 확인하세요."
        if command == "/deliveries":
            rows = b.store.conn.execute("SELECT delivery_id,state FROM outbox WHERE chat_id=? AND state IN ('failed','uncertain') ORDER BY delivery_id DESC LIMIT 30", (chat,))
            return "\n".join(f"#{r[0]} · {r[1]}" for r in rows) or "실패·불확실 전송이 없습니다."
        if command == "/resend":
            parts = arg.split()
            if not parts or not parts[0].isdigit() or parts[1:] not in ([], ["duplicate"]) or message_id is None:
                raise ValueError("/resend <전송 ID> [duplicate] — 불확실 전송은 중복 위험 확인이 필요합니다.")
            self.ops.resend(int(parts[0]), chat, user, message_id, confirm_uncertain=parts[1:] == ["duplicate"])
            return "기존 답변 전송을 재등록했습니다. Codex 질문을 재실행하지 않습니다."
        if command == "/backup":
            directory = b.config.data_dir / "backups"
            directory.mkdir(mode=0o700, exist_ok=True)
            result = await asyncio.to_thread(backup_bundle, b.config, directory)
            return f"DB·지식 자료·첨부 원본 백업 완료: {result.name}\nCodex 대화 기록·인증은 포함하지 않습니다."
        return None
