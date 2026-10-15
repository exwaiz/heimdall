# Phase 4 결과 · 2026-10-15

지식 저장·검색·근거 답변, Codex 기반 이미지 추출과 영상 프레임 분석을 구현하고 배포했다.
음성 전사는 실험적 Realtime 인증 오류 때문에 미지원으로 남는다. Phase 4 전체가
음성까지 완성됐다고 보고하지 않는다. 구체적인 명령과 제한은 [KNOWLEDGE.md](KNOWLEDGE.md).

## 변경

- `knowledge.py`: items/contents/sources/tags/item_tags, FTS5 trigram 및 짧은 한글 LIKE 보완,
  소유자 검증, 자료 ID, 저장 상한, source별 원문/AI 추출 구분, 질문 단어 주변 본문 검색.
- `bridge.py`, `commands.py`, `db.py`, `__main__.py`, `operations.py`: 새 명령,
  고정된 검색 문맥의 읽기 전용 /ask, 단일 모델 worker를 공유하는 /extract,
  durable processing 상태·불확실 복구, 기존 사진 답변의 비용 없는 backfill.
- `extract_worker.py`, `extraction.py`, `assets.py`: 원본 해시 재검증,
  offline sandbox의 제한된 PDF/DOCX/텍스트 디코딩 및 최대 3개 영상 프레임 생성.
  이미지의 글자·장면 이해는 기존 Codex가 처리한다.
- `knowledge_backup.py`, `scripts/backup_bridge.py`, 백업 unit:
  DB+첨부 원본의 검증된 ZIP 백업과 새 경로 복원. 기존 DB-only CLI도 유지한다.
- 전용 venv에 pypdf 6.20.1, PyAV 16.1.0, Pillow 12.3.0 설치.
  시스템 패키지, 로컬 OCR/음성 AI, 새 키/과금 경로는 추가하지 않았다.

새 명령: `/save`, `/items`, `/item`, `/tag`, `/search`, `/ask`, `/extract`,
`/processing`, `/forget`. 기존 `/backup`은 원본 포함 ZIP으로 확장했다.

## 검증 결과

- 전체 자동 테스트 **188개 통과**, pip check 및 compileall 통과.
- 한국어 2글자 검색, 장문 뒤쪽의 검색 본문 선택, 소유자/삭제 필터,
  잘못된 FTS/SQL 입력, idempotency, tag/용량 제한 기반 구조를 확인했다.
- 없는 검색 자료는 모델을 실행하지 않는다. 가짜 ID·불일치 인용·잘못된 JSON은
  그대로 답변하지 않으며, soft-delete한 대기 자료는 모델 실행 전에 거절한다.
- 사진 기존 답변 backfill은 모델 재호출 없이 이루어진다.
- 전역 모델 큐, 추출 작업 중복 방지, 원본 변조 검증, uncertain 이력 복구를 확인했다.
- ZIP traversal·symlink·기존 복원 대상·변조 원본·압축폭탄/XML entity 거절과
  복원 후 DB/원본 hash/경로/FTS 보존을 확인했다.

`python -m scripts.smoke_phase4`는 격리 DB·인공 fixture·실제 Codex로 전 항목을 통과했다.

1. 메모 저장 → 한글 검색 → 실제 근거 답변 및 자료 ID/원문 인용 검증.
2. 글자 이미지 → Codex OCR/장면 JSON → `73000` 저장 확인.
3. 저장한 이미지 추출 결과 재검색 → 실제 `/ask` 근거 답변.
4. 인공 영상 → 3개 프레임 → 실제 Codex 분석 → 검색 가능한 결과 저장.
5. DB·6개 원본/파생 이미지 파일 전체 bundle을 신규 경로로 복원.

또한 문서 텍스트 추출 sandbox의 실제 실행 및 PDF/DOCX 파서 테스트를 수행했다.
영상 결과는 일부 프레임 관찰일 뿐 소리·모든 장면을 분석한 것으로 표현하지 않는다.

## 운영 반영

- 배포 전 기존 DB+원본을 전체 백업했다.
- 자체 backup unit의 ExecStart 한 줄에 `--include-assets`만 추가하고 실제 timer에 반영했다.
- bridge 재시작 후 `bridge_ready`, active, NRestarts=0 확인.
- 기존 완료 5건·실패 6건, 선택 thread ID와 readonly binding 및 원본 1개를 보존했다.
- migration 1~5, knowledge 1건 및 원문/기존 Codex 설명의 FTS 인덱스 생성 확인.
- 실제 backup.service 실행으로 전체 ZIP 생성 성공.
- 실제 운영 ZIP을 별도의 임시 private 경로로 복원해 integrity_check=ok,
  원본 1개·지식 자료 1개·`카시트` FTS 검색 hit 1개를 확인했다. 원본/live DB는 덮어쓰지 않았다.
- bridge와 backup timer의 부팅 enable은 유지했다. 재부팅·Kakao-Radar 변경은 하지 않았다.

## 남은 항목

설치 0.162.1의 일반 UserInput에는 음성/영상 원본 타입이 없다.
실험적 Realtime start는 수락되지만 `thread/realtime/error`를 반환했고, 오류의
인증 관련 표시(auth)를 확인했다. 정확한 계정/플랜 원인까지 단정하지 않는다.
원본 음성은 보관하되 전사됐다고 표시하지 않고, 새 인증이나 별도 API로 우회하지 않았다.
따라서 **음성 전사 연동은 남은 작업**이다.

모바일에서 새 `/search`·`/ask`·`/extract` 명령을 사용하는 사용자 확인은 별도로 남는다.
ID/인용 검증은 의미적 사실 검증이나 완전한 hallucination 방지를 보장하지 않는다.
자료·원본은 plaintext private 보관이며 `/forget`은 검색 제외이지 영구 삭제가 아니다.
URL fetch, embeddings, 전체 영상 자막/음성 추출, 자동 retention은 제공하지 않는다.
Pi의 memory cgroup 미지원도 그대로이며 native decoder에만 별도 RLIMIT을 적용한다.
