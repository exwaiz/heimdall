# 구현 결정 기록

## D4-001 — Codex를 두뇌로, 로컬은 저장·검색·최소 디코딩으로 제한

사용자가 Phase 4에서 별도 로컬 OCR/음성 AI 설치 대신 기존 Codex 활용을 요청했다.
이미지 OCR과 영상 프레임 이해는 기존 로그인 Codex에 맡기고, PyAV/Pillow/pypdf는
전용 venv에서 최소 파일 디코딩만 수행한다. 음성의 실험적 Realtime은 인증 관련 오류로
미지원 처리하며 새 API 키·과금 경로·로컬 ASR로 우회하지 않는다.
지식 DB와 기존 Codex 대화 기록을 분리하고, /ask만 명시적인 검색 문맥을 구성한다.
자료 ID·실제 인용을 검증하되 의미적 사실 검증을 완전히 보장한다고 주장하지 않는다.

현재 운영 상태는 Phase 2/3 보고서 및 OPERATIONS.md가 기준이다.
아래 Phase 0/1 항목의 미구현·미배포 표현은 해당 시점의 역사적 기록이다.

## D2-001 — 요청·연결에 결합된 일회 승인과 제한 폴더 쓰기

쓰기 모드는 사용자가 선택한 이후 신규 작업에만 고정한다. 승인 nonce는 DB에 해시로만
저장하고 지원하는 요청만 이번 한 번 승인한다. 추가 권한·네트워크 확대·영구 정책 변경은
허용하지 않는다. 만료/취소/재시작 후 승인 버튼을 재사용하지 않는다.
실제 RPC는 environmentId="local"을 사용하므로 null과 local만 허용한다.

## D2-002 — loaded actor의 권한 변경은 유휴 연결 재로드 후 검증

실제 0.162.1은 로드된 workspace actor를 readonly로 resume해도 이전 프로파일을
반환했다. 이를 그대로 허용하지 않고 fail closed한 뒤, 이 브리지가 소유한 유휴 연결만
unsubscribe 후 새 권한으로 resume한다. 실제 profile/sandbox 전환을 모델 호출 없이
양방향 검증했다. 다른 클라이언트를 종료하거나 활성 turn을 강제로 해제하지 않는다.

## D2-003 — 계정 사용량 제한을 실행 실패와 구별

추가 스모크에서 실제 usageLimitExceeded를 확인했다. 코드에 정의된 codexErrorInfo
라벨만 저장/안내하고 원시 backend 메시지는 전달하지 않는다. 한도 회복 시각을 추정하지
않으며 자동 재실행이나 새 API 키·다른 과금 경로로 우회하지 않는다.

## D3-001 — 원본 보관과 모델 입력·Knowledge 처리를 분리

사진만 검증 후 localImage로 전달하고 문서/음성/영상은 저장한다. caption/thread/mode/asset ID를
고정하고 dispatch 직전에 파일 해시·형식을 재확인한다. private assets는 모델 filesystem
도구에 개방하지 않는다. App Server가 localImage 파일을 읽는 경로와 도구 권한은 별개다.
URL fetch, 문서 실행·압축 해제·전사·OCR·검색은 이번 단계에 추가하지 않았다.

## D0-001 — Phase 0는 Python 표준 라이브러리로 수행

Python 3.13.5에서 asyncio/subprocess/unittest만 사용한다.
기존 서비스 venv나 시스템 패키지를 수정할 필요가 없다.
Phase 1 Python 의존성은 별도 프로젝트 venv에 설치하고 ARM64 호환성을 검증한다.

## D0-002 — metadata-only, state DB-only 조회

0.162.1의 ThreadListParams는 useStateDbOnly를 지원한다.
Phase 0에서는 true로 고정하여 rollout 스캔과 metadata repair를 회피한다.
thread/read는 includeTurns=false로 고정한다. resume/start/turn은 진단 allowlist에 없다.
state DB에 누락된 세션은 이 모드에서 발견되지 않을 수 있다. 현재 cli 출처가 없는 원인은 미확정이다.

## D0-003 — 실행 권한 계약을 각 RPC에 맞게 적용

명세의 readOnly 개념을 그대로 모든 요청 필드에 넣지 않는다.
thread/start/resume은 sandbox="read-only", turn/start는 sandboxPolicy.type="readOnly"가 실제 계약이다.
P0 어댑터는 resume과 turn 양쪽에 권한을 명시해 과거 세션의 넓은 권한을 물려받지 않도록 한다.
approvalsReviewer="user", approvalPolicy="on-request"와 요청별 fail-closed 처리를 적용한다.
실제 sandbox 효과와 연결된 도구/플러그인 권한은 Phase 1 테스트에서 검증한다.

## D0-004 — stderr와 메시지 콘텐츠 비노출

스모크 stdout 결과는 thread ID/cwd/source/timestamps/status만 저장한다.
raw stderr, preview/title, 대화 내용, initialize 응답 원문 및 RPC 오류 원문은 출력하지 않는다.
예상치 못한 서버 요청은 RPC error로 응답하고 진단을 중단한다.
운영용 P0 어댑터에는 승인 method별 decline 및 종료 확인 처리가 별도로 필요하다.

## D0-005 — 기존 세션 조회 성공과 CLI/Termius 검증을 구분

실제 세션 9개와 metadata read는 확인했다. 출처는 vscode 8, exec 1이다.
기존 Termius/CLI 출처 발견과 양방향 순차 재개는 미검증 항목으로 남긴다.
외부 Codex 프로세스가 존재하므로 진단에서 기존 thread에 turn을 추가하지 않는다.

## D0-006 — 생성된 기본 스키마를 그대로 보존

schemas/는 Codex CLI 0.162.1의 기본 generate-json-schema 결과다.
미지원 experimental 필드를 추가하지 않았다. CLI 업그레이드 시 새 출력과 비교하고 재스모크한다.
schemas에 위험 method가 선언되어 있어도 브리지에서 노출한다는 의미가 아니다.

## D0-007 — 신규 세션 추적 기준을 /home/haaland로 지정

사용자 요청에 따라 기본 세션 조회는 `config.toml`의 session_tracking.cwd인
`/home/haaland`를 정확히 매칭한다. App Server thread/list의 cwd 필터를 적용하고,
목록 및 thread/read 응답의 cwd도 재검사한다. cli만 필터링하지 않고 기존 명세의
cli/vscode/exec/appServer 출처를 유지한다.

이는 세션 작업 디렉터리 기준이다. 기록 저장소인 CODEX_HOME을 변경하거나 과거
personal-agent 세션의 cwd/기록을 재작성하지 않는다. 하위 디렉터리는 기본 추적에서 제외한다.
Phase 1의 `/database` 및 `/select`는 이 추적 계약을 이어받고 `/new`의 기본 cwd도
사용자가 정한 `/home/haaland`를 적용한다. 실행 권한과 보호 경로 검증은 별도로 구현한다.
현재 조회 클라이언트는 매 실행 시 갱신하며 상시 추적 프로세스는 아직 없다.

## D1-001 — 표준 SQLite와 직접 Bot API transport

Python 3.13 표준 sqlite3, httpx 및 jsonschema를 사용한다. 독립 venv에서 ARM64 설치를 확인했다.
Telegram 전송과 Bridge core를 분리해 token 없이 FakeTelegramTransport로 동일 명령/worker를 검증한다.
SQLite transaction 안에서는 await하지 않는다. 단일 worker와 프로세스 flock으로 실행을 직렬화한다.

## D1-002 — 새 thread의 첫 turn은 resume 생략

실제 0.162.1에서 메시지가 없는 thread/resume은 rollout 관련 -32600으로 거부된다.
같은 App Server의 thread/start로 만든 thread는 첫 turn까지 이미 로드된 상태를 사용한다.
first turn ID를 저장한 뒤 신규 표시를 지우며, 이후 및 재연결 시에는 resume한다.
첫 turn 전에 프로세스가 죽어 생성 thread가 재개되지 않으면 실패/uncertain으로 보존한다.

## D1-003 — named read-only 권한으로 비밀 읽기도 제한

기본 readOnly만으로 home의 비밀 파일 읽기를 막았다고 주장할 수 없다.
공식 권한 문서와 0.162.1의 experimental permissions 필드를 검증해 heimdall_readonly를 적용했다.
이는 D0-003의 stable sandbox-only 계획을 강화한 구현 변경이다.
`permissions`는 sandbox/sandboxPolicy와 함께 보내지 않고 각 start/resume/turn에서 명시한다.
initialize의 experimentalApi를 켜며 생성된 experimental JSON Schema로 요청을 검증한다.
Codex 버전과 schemas/VERSION 불일치는 fail closed다.

프로파일은 cwd 읽기만 허용하고 auth/session/config 등 CODEX_HOME의 기존 하위 경로와
SSH/config, personal-agent, Kakao-Radar 운영 경로, Bridge DB 및 /proc를 deny한다.
`.codex` 전체 deny 후 packages를 reopen하는 구성은 이 Linux 런타임의 실행 파일 접근을
막았으므로, 설치된 packages 런타임은 제외하고 나머지 즉시 하위 경로를 각각 deny한다.
실제 harmless fixture의 읽기/쓰기/네트워크 차단과 active profile ID를 검증했다.
Telegram/API 비밀 환경 변수와 SSH agent socket도 child env에서 제외한다.

## D1-004 — 보수적 uncertain 및 별도 outbox

Codex turn 전달 여부 및 Telegram 전송 수락 여부가 모호하면 자동 반복하지 않는다.
job과 outbox를 독립적으로 저장해 모델 결과는 전송 실패에도 보존한다.
답변 분할을 영속화하고 parent delivery 순서로 전송한다.
startup은 queued만 처리하며 turn ID의 명확한 완료 이력으로만 uncertain을 복구한다.
command update와 chat/message ID를 함께 기억해 /new 중복 생성과 crash replay를 방지한다.

## D1-005 — Phase 1의 완료 보고 범위

MVP 구현, fake Telegram 통합, 실제 Codex의 순차 CLI↔Bridge 재개는 검증했다.
token/user/chat 설정이 없으므로 실제 Telegram 수신 및 reboot DoD는 미검증으로 유지한다.
Phase 0의 Termius thread 발견은 사용자와 실제 ID 대조로 확인됐다.
systemd 설치, 승인 UI 및 쓰기 모드를 이 단계의 구현으로 확장하지 않는다.

## D1-006 — 사용자의 요청으로 CLI 경합 조건을 thread 단위로 완화

같은 cwd의 미확인 CLI 전체를 보수적으로 차단하는 정책이 실제 개발을 방해했다.
사용자가 개발 CLI를 유지하면서 Telegram을 사용하도록 조건 완화를 요청했다.
cwd 일치만으로는 차단하지 않고, 선택 thread ID를 명시하는 외부 Codex 인자 또는
선택 thread의 열린 rollout 파일로 경합을 추정한다. 자체 App Server의 열린 파일은 제외한다.
enqueue 후 기록 변경, 동일 thread active/lock, 실행 불확실한 기존 turn의 보호는 유지한다.
정확한 외부 CLI의 thread를 식별하지 못하면 허용하므로 완전한 동시 쓰기 감지를 보장하지 않는다.

preflight 거절은 turn/start 전에 발생하므로 failed로 처리한다. 실제 수락 여부가 불명확한
turn/start/실행 중 연결 장애는 여전히 uncertain이며 자동 재실행하지 않는다.
구버전 preflight-only uncertain은 turn ID와 running event가 없는 경우에 한해 failed로 정정할 수 있다.
실제 작업 #1은 그 증거를 확인하고 정정했으며 자동 재실행하지 않았다.
다른 thread/미확인 CLI 허용, 같은 thread explicit/fd 경합 거절, 자체 App Server 제외 및
preflight 거절 후 다음 요청 처리 등 총 53개 테스트를 검증한다.

## D1-007 — App Server의 열린 파일과 활성 turn을 구분

사용자의 실제 Telegram 작업 #2/#3은 완료됐지만, 다른 세션을 선택한 작업 #4는 외부
App Server의 열린 rollout 파일 때문에 거절됐다. 실제 최근 turn은 interrupted였으므로 오탐이다.
App Server의 열린 파일을 실행 락으로 취급하지 않고, thread/turns/list(limit=1,
itemsView=notLoaded, desc)로 실제 마지막 turn 상태를 확인한다.
terminal 상태는 허용하고 inProgress 및 모르는 상태는 거절한다. 생성 직후 첫 turn이 없는
자체 신규 thread는 이력이 아직 없어 API 조회를 생략한다.
실행 전후의 기록 비교 및 CLI의 명시적인 같은 thread 사용 검사는 유지한다.
App Server의 메모리 상태와 영속 기록 간 시차가 있을 수 있으므로 완전한 글로벌 락은 아니다.
idle App Server 허용, active turn 차단, 검사 도중 기록 변경 차단 등을 포함한 테스트 56개 통과.

## D1-008 — 무응답 복구를 위한 독립 임시 supervisor

00:09/00:10 Telegram 메시지가 등록되지 않았고 브리지 프로세스 및 서비스가 모두 없었다.
DB 처리 시각도 이전 23:57에 멈춰 있었다. 정확한 종료 원인은 기존 로그만으로 확인하지 못했다.
현재 터미널 테스트의 수명과 분리하기 위해 임시 user systemd unit으로 시작했다.
독립 프로세스 감독, on-failure 재시작, 시작 횟수 제한 및 안전한 lifecycle 로그를 적용한다.
영구 unit 설치/enable/linger 변경은 하지 않았다. user manager Linger=no라는 운영 한계를 문서화한다.
실제 Telegram startup 네트워크 실패 후 supervisor의 자동 재시작과 누락된 3건 수신을 확인했다.

## D1-009 — 브리지의 충돌 추정과 Codex 자체 resume 거절을 구분

임시 supervisor 복구 이후 실제 job #7은 thread/resume -32600으로 거절됐다.
대상은 기존 선택 스레드(개인 ID 생략)이며, 새 진단 연결에서도 thread/already/active
내용의 동일한 resume 거절을 재현했다. 열린 파일 추정 검사를 완화해도 Codex 자체의
thread 소유/활성 연결 제한을 우회하지 않는다. 사용자에게 해당 세션만 다른 연결에서 닫거나
다른 thread를 선택하도록 안내한다. 전체 home CLI 종료는 요구하지 않는다.
이 실패는 질문을 turn/start로 보내기 전이므로 failed로 보존하며 자동 재실행하지 않는다.

