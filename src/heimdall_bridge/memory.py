"""Memory vocabulary and strictly allowlisted, read-only Codex diagnostics."""

MEMORY_GUIDANCE = """Heimdall bridge context (application-provided, not the user's message):
Distinguish these independent kinds of memory whenever relevant:
1. Current Codex thread context: conversation in the selected thread; /history shows recent
   messages. Resuming a thread continues it, but does not guarantee every old token is in
   context (compaction/context limits apply). Other threads are not automatically loaded.
2. Codex native local memories: optional cross-chat memory generation/use controlled by
   Codex global settings and per-chat choices. A configured boolean is not proof that this
   turn received memories, that generation ran, or that memories contain any particular fact.
3. Heimdall Knowledge: a separate local database. /save explicitly stores notes; attachments
   are indexed with captions/available extraction. /search searches it; /ask retrieves matching
   sources for a grounded answer. Ordinary chat is NOT automatically copied here, and ordinary
   questions do NOT automatically retrieve Knowledge. /forget excludes a Knowledge item from
   search, not Codex memories, thread history, original attachments, or backups.
ChatGPT web/account memory is separate again; do not claim it is synchronized here.
The Heimdall bridge deliberately denies model tool access to Codex config/auth/session files,
private bridge data and other protected paths, to protect credentials, settings and history.
This is a bridge policy, not evidence that memories are disabled. /mode write does not lift it.
Use the sanitized diagnostic values below only as reported configuration, not runtime proof.
Unknown means unverified, NOT disabled. Do not bypass protection or invent UI/account state.
Tools run on the Pi host with a restricted filesystem view, not a separate machine.
APT history (/var/log/apt), dpkg logs (/var/log/dpkg.log and existing rotations), and
/var/lib/dpkg/status and arch are explicitly readable in every bridge mode. For recent
package installs, read these paths directly using normal sandboxed tools; do not request
sudo, require_escalated, or extra permissions. This does not permit installing packages.
Missing other paths can reflect the restricted view, not absence on the host. An escalation
error saying 'rejected by user' can be automatic bridge policy denial, not an actual user
button press; do not attribute it to the user without evidence. Report actual tool outcomes.
Telegram /memory and /memories are read-only Heimdall diagnostics, NOT the Codex TUI memory
control. Direct the user there for status. Do not claim a memory setting was changed or a note
was saved unless a corresponding operation actually succeeded. Preserve any requested output
format (including Knowledge JSON); this context does not change the user's task.
"""

KEYS = ("features.memories", "memories.use_memories", "memories.generate_memories",
        "memories.disable_on_external_context")


def sanitize_memory_config(response):
    """Never return config contents, origins, paths, strings, or arbitrary feature flags."""
    config = response.get("config") if isinstance(response, dict) else None
    config = config if isinstance(config, dict) else {}
    result = {}
    for key in KEYS:
        section, field = key.split(".")
        table = config.get(section)
        value = table.get(field) if isinstance(table, dict) else None
        result[key] = value if type(value) is bool else None
    return result


def config_lines(values):
    return "\n".join(f"{key}: " + ("설정상 켜짐" if values.get(key) is True else
        "설정상 꺼짐" if values.get(key) is False else "미확인") for key in KEYS)


def memory_report(values, thread_id, count):
    return ("메모리 구분 · 조회 전용 (설정을 바꾸지 않습니다)\n\n"
        f"1. 현재 스레드 대화\n선택: {thread_id or '없음'}\n"
        "선택 스레드의 대화를 이어갑니다. /history 로 최근 기록 확인. "
        "오래된 내용은 압축·문맥 한계가 있고 다른 스레드 전체를 자동으로 읽지는 않습니다.\n\n"
        "2. Codex 자체 로컬 메모리\n" + config_lines(values) + "\n"
        "출처: 현재 브리지 App Server의 config/read 응답에서 허용된 불리언만 조회. "
        "미확인은 꺼짐이 아닙니다. 스레드별 메모리 선택·이번 답변의 실제 주입 여부·"
        "생성 완료 여부는 확인하지 않았습니다.\n\n"
        f"3. Heimdall 지식 저장소 · 검색 가능한 자료 {count}개\n"
        "/save 저장 · /search 검색 · /ask 자료 기반 질문 · /items 목록. "
        "첨부는 자료로 등록되지만 일반 대화 전체는 자동 저장되지 않습니다. "
        "일반 질문은 저장소를 자동 검색하지 않습니다. /forget 은 검색 제외이며 "
        "Codex 메모리·대화·첨부 원본·백업 삭제가 아닙니다.\n\n"
        "ChatGPT 웹/계정 메모리는 별개이며 여기서 상태·동기화를 확인하지 않았습니다.\n"
        "설정·인증·기록 보호를 위한 Heimdall 정책으로 모델의 원본 파일 접근은 제한됩니다. "
        "이는 메모리 꺼짐을 뜻하지 않으며 /mode write 로 해제되지 않습니다.\n"
        "Telegram /memory · /memories 는 이 상태 안내입니다. Codex CLI의 "
        "/memories 설정 화면을 실행하거나 메모리를 켜고 끄지 않습니다.")
