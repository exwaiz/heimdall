# Phase 4 지식 기능

Codex는 읽기·이해·OCR·답변을 담당한다. Pi는 원본, 추출 결과, 출처, 검색 인덱스를 보관한다.
로컬 OCR/ASR 모델, 임베딩 서버, 새 OpenAI API 키는 추가하지 않았다.

## 메모리는 세 가지로 구분

`/memory` (별칭 `/memories`)는 소유자 전용 조회 명령이다. 세션 선택 없이도 사용할 수 있다.

| 종류 | 의미 | 확인·사용 |
| --- | --- | --- |
| 현재 스레드 대화 | 선택한 Codex 대화의 문맥. 오래된 내용은 압축·문맥 한계가 있음 | `/current`, `/history`; 일반 질문은 이 스레드에서 실행 |
| Codex 자체 로컬 메모리 | Codex가 여러 대화에서 생성·활용하는 별도 기능 | `/memory`에서 제한된 설정값 조회. 실제 사용·생성 여부와 다름 |
| Heimdall 지식 저장소 | 명시적으로 저장한 메모와 등록·추출한 첨부 자료 | `/save`, `/search`, `/ask`, `/items` |

ChatGPT 웹/계정 메모리는 또 별개이며 여기서 상태나 동기화를 확인하지 않는다.
일반 대화는 Heimdall 저장소에 자동 복제되지 않으며 일반 질문은 저장소를 자동 검색하지 않는다.
`/forget`은 지식 자료의 검색 제외이지 Codex 메모리·대화·첨부 원본·백업 삭제가 아니다.

브리지는 현재 App Server의 `config/read` (`includeLayers=false`, 추적 cwd 기준)에서
`features.memories`, `memories.use_memories`, `memories.generate_memories`,
`memories.disable_on_external_context` 네 키의 정확한 불리언 값만 꺼낸다.
다른 설정값·경로·인증정보·원본 응답은 모델에 전달하거나 DB/로그에 저장하지 않는다.
필드 누락·잘못된 타입·조회 실패는 `미확인`이며 기본값이나 꺼짐으로 추정하지 않는다.
이 설정 조회는 스레드별 선택, 실제 메모리 주입, 백그라운드 생성 완료를 증명하지 않는다.
Telegram `/memories`는 Codex TUI의 동일 이름 설정 화면을 실행하지 않으며,
켜기/끄기/초기화 인자를 거절하고 전역 설정을 변경하지 않는다.

모든 모델 turn에는 사용자 원문을 첫 텍스트 입력으로 유지한 채, 별도 텍스트 입력으로
메모리 구분·보호 이유·허용된 설정 조회 결과를 덧붙인다. 새 세션·재개 세션·자료 질문 모두
동일하며 기존 developer instructions를 덮어쓰지 않는다. 답변 형식(JSON 등)은 유지한다.
이는 모델의 정확한 설명을 돕는 문맥이며 모든 자연어 답변의 정확성을 강제하는 검증기는 아니다.
`config.toml`, 인증, 세션 원본, 브리지 비공개 DB의 모델 도구 접근 차단은 그대로 유지한다.
`/mode write`도 이 차단을 해제하지 않는다. `/status`의 RAM cgroup은 AI 메모리가 아니다.

근거: [OpenAI 공식 메모리 문서](https://learn.chatgpt.com/docs/customization/memories).

## 사용

```text
/save 서울 출장 식비 예산은 73000원으로 확정했습니다.
/search 출장
/ask 서울 출장 식비 예산은 얼마야?
/items
/item HK-000001
/tag HK-000001 출장 비용
```

일반 대화 전체를 자동으로 Knowledge에 복제하지 않는다. 메모는 `/save`로 명시적으로 저장한다.
첨부는 자동으로 자료 ID를 받으며 캡션과 기존에 보관된 사진 답변도 검색할 수 있다.
저장한 사진의 Codex 답변은 `codex-description-v1`, 명시적으로 추출한 결과는
`codex-extract-v1`, 문서의 원문 텍스트는 `document-v1`로 출처를 구분한다.
AI 추출 내용은 잘못됐을 수 있고 검증된 객관적 사실과 같지 않다.

사진의 모든 글자나 문서 본문을 검색하려면:

```text
/extract HK-000002
/processing
/search 영수증
/ask HK-000002 영수증 금액은 얼마야?
```

`/extract`는 기존 선택을 바꾸지 않고 별도 읽기 전용 테스트/추출 세션을 만든다.
Codex 추출도 일반 질문과 같은 전역 단일 worker와 영속 큐를 사용한다.
`/ask`는 현재 선택한 세션에 질문하되, 쓰기 모드여도 강제로 읽기 전용을 적용한다.
본문·자료 ID·대상·권한을 등록 시 고정하고 삭제된 자료는 dispatch/결과 검증 때 거절한다.

검색 자료가 없으면 모델을 실행하지 않는다. FTS5 trigram 검색에 두 글자 한글의 literal
LIKE 보완을 결합한다. 검색 문법/SQL을 사용자에게 실행시키지 않는다. 최대 5개 자료에서
질문 단어 주변의 짧은 본문을 골라 `/ask` 컨텍스트로 보낸다. 장문 전체를 보내지 않는다.
검색 순위와 키워드 일치에 한계가 있으므로 정확한 HK-ID로 특정 자료를 질문할 수도 있다.

답변은 JSON으로 받고 알려진 자료 ID와 실제 검색 문맥의 10~300자 정확한 인용을
대조한다. 없는 ID·가짜 인용·구조가 잘못된 응답은 사용자에게 그대로 표시하지 않고 실패 처리한다.
이는 ID/인용의 구조적 검증이지 인용이 답변을 논리적으로 뒷받침하는지 완벽히 판정하는 검증은 아니다.
검색 원문을 비신뢰 데이터로 구분하고 도구 사용을 금지하는 지시를 주며, 기존 sandbox는 유지한다.

## 입력별 처리

| 자료 | `/extract` 처리 |
| --- | --- |
| 사진, 이미지 문서 | 기존 Codex가 글자와 장면을 읽음 |
| UTF-8 텍스트 | 텍스트 인덱싱, 모델 호출 없음 |
| PDF | 최대 50페이지 텍스트 추출. 텍스트 없는 페이지의 내장 이미지는 최대 3개를 Codex로 전달 |
| DOCX | 압축을 풀지 않고 `word/document.xml` 텍스트만 읽음 |
| 영상 | 첫 최대 60초 범위의 최대 3개 프레임을 Codex가 분석. 소리·자막의 완전한 전사는 아님 |
| 음성 | 원본/캡션 보관, 전사 미지원 안내 |
| 일반 ZIP/구형 DOC/RTF 등 | 보관하되 본문 추출은 지원하지 않음 |

설치 Codex 0.162.1의 일반 UserInput에는 음성/영상 타입이 없다. 실험적
`thread/realtime/start`도 시험했지만 요청 수락 후 인증 관련 `thread/realtime/error`가
발생했다. 이 경로를 운영 기능으로 노출하거나 새 키·API/로컬 음성 모델로 우회하지 않았다.
음성 전사에는 별도 연동 검증/인증 결정이 남아 있다.

프로젝트 venv에 PyAV, Pillow, pypdf만 추가했다. 이들은 디코딩/텍스트 추출용이며 AI 모델이 아니다.
시스템 apt 패키지, Tesseract, Whisper 등을 설치하지 않았다.
디코딩은 네트워크 없는 별도 Codex sandbox에서 검증된 원본 사본 하나만 읽는다.
프로세스에는 768MiB 주소 공간, CPU 35초, wall timeout 50초, 출력 4MB 제한을 적용한다.
이 RLIMIT은 호스트의 memory cgroup 미지원과 별개다. 코덱 thread는 1개이며 낮은 우선순위로 실행한다.
DOCX 엔티티·압축폭탄, 과도한 PDF 페이지, 1080p 초과 영상 등을 거절한다.
지원 범위를 벗어나거나 실패해도 원본을 지우지 않는다. 재추출은 `/extract`로 명시적으로 요청한다.

## 저장·복구

`items`, `contents`, `sources`, `tags`, `item_tags`, FTS5 인덱스,
`processing_jobs`를 기존 DB에 추가한다. 자료 최대 10,000개, 추출 본문당 100,000자,
본문 전체 20,000,000자, 태그 최대 10,000개로 제한한다. AI 추출 본문은 최대 20,000자다.
첨부는 기존 200MB/1,000개 저장 상한을 공유하며 영상 프레임도 이 예산에 포함된다.
모든 작업은 개인 소유자 범위로 조회한다. 로그에 원문·추출 내용·토큰을 쓰지 않는다.

`/forget HK-000001`은 soft-delete로 검색에서만 제외한다. 첨부 원본, 이미 보낸 Codex
문맥, Telegram 메시지, 기존 백업까지 지우는 영구 삭제가 아니다. 비용 발생 모델 작업은
중복 자동 실행하지 않으며 불확실 모델 작업은 수동 확인 대상으로 유지한다.

`/backup`과 새 일일 타이머는 DB와 원본을 함께 ZIP으로 만든다. SQLite online snapshot의
저장 완료 파일 목록을 기준으로 immutable 파일을 해시 검증해 복사하므로 추가 수신과
동시에 실행해도 snapshot에 해당하는 파일 집합을 보관한다. Codex 기록·인증은 포함하지 않는다.

```sh
.venv/bin/python -m scripts.backup_bridge backup --destination-dir /home/haaland/.local/share/heimdall/backups --include-assets
# 부모는 미리 만든 개인 0700 디렉터리, destination 자체는 존재하지 않는 새 경로여야 함
.venv/bin/python -m scripts.backup_bridge restore-bundle --backup /private/backups/knowledge-backup-EXAMPLE.zip --destination /private/restore/new-copy
```

복원은 기존/live 데이터와 겹치는 경로를 거부한다. allowlist된 ZIP member 이름, 크기,
SHA-256, DB/manifest 일치, SQLite 무결성/FK를 검증하고 assets 경로를 복원 위치로 바꾼다.
DB 100MB, 첨부 총 200MB, 1,000개 상한이며 인증/`.codex` 데이터는 건드리지 않는다.
복원 사본을 바로 서비스에 덮어쓰지 말고 운영 큐와 uncertain 상태를 먼저 검토한다.
