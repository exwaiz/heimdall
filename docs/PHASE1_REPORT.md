# Phase 1 MVP 구현 및 검증

2026-10-14, Asia/Seoul. 구현과 로컬/실제 Codex 검증을 완료했다.
전용 Telegram Bot token/user/chat 설정은 없으므로 실제 Telegram E2E는 미검증이다.
명세의 실제 Telegram 수신 및 재부팅 항목까지 모두 통과했다고 선언하지 않는다.

## 구현 범위

| 구성 요소 | 구현 |
|---|---|
| Python package / CLI | heimdall_bridge 0.1.0, 독립 .venv |
| config | 정확한 home cwd 추적, 소유자 private chat, 설정 누락 fail closed |
| Telegram transport | Bot API long polling, getMe/webhook 확인, 429 제한 retry, plain text |
| 명령어 | start/help/database/select/new/current/status/queue/cancel |
| 세션 registry | sourceKinds 유지, 20개 페이지, 30초 TTL, 5페이지 조회 예산, exact/unique prefix |
| SQLite | migrations, WAL/FK/busy_timeout, binding, job, event, update 및 command 중복 방지 |
| worker | FIFO 1개, enqueue 시 target 고정, lock, 외부 활동 검사, timeout/cancel |
| App Server | schema validation, correlation futures, notification stream, 서버 요청 거부 |
| final renderer | agentMessage의 authoritative completed item, final phase, delta/reasoning 제외 |
| recovery | dispatching/running→uncertain, turn ID 증거 조회, 자동 중복 실행 금지 |
| outbox | job 결과 독립 보존, 분할 순서, 중복 전송 방지, ambiguous delivery uncertain |

핵심 구현 파일: `src/heimdall_bridge/{config,db,transport,codex,bridge,__main__,fakes}.py`.
테스트 및 실행 스크립트: `tests/test_phase1.py`, `scripts/smoke_phase1.py`, `scripts/smoke_sandbox.py`.

## 실제 Pi 검증

- 기존 Codex ChatGPT 로그인을 그대로 사용했다. API 키나 별도 로그인 토큰은 생성하지 않았다.
- 제한 프로파일이 적용된 새 thread에서 실제 turn을 실행하고 정식 최종 응답을 받았다.
- App Server 종료 → 동일 thread로 codex exec resume → App Server 재시작/재개를 순차 수행했다.
- 마지막 응답에 양쪽의 테스트 문자열이 포함되는 것을 검증했다.
- 실제 Codex + FakeTelegramTransport + SQLite worker의 결과/전송 흐름이 성공했다.

제한 프로파일이 적용된 최종 성공 스모크:

| 항목 | 값 |
|---|---|
| thread ID | 개인 ID 생략 |
| 첫 Bridge turn ID | 개인 ID 생략 |
| 마지막 Bridge turn ID | 개인 ID 생략 |
| 첫 답변 | HEIMDALL_P1_OK 확인 |
| CLI 답변 | HEIMDALL_CLI_OK 확인 |
| 재개 문맥 | 두 문자열을 마지막 답변에서 모두 확인 |
| Telegram | FakeTelegramTransport |

권한 프로파일 도입 전 성공한 테스트 thread도 확인했다(개인 ID 생략).
두 테스트 대화는 삭제/수정하지 않고 보존했다. 프로토콜 디버깅 중 만든 빈 thread도 서버를 통해서만
생성했으며 `.codex` 파일을 직접 편집하거나 삭제하지 않았다.

실제 sandbox probe는 일반 파일 읽기 성공, harmless denied fixture 읽기 거부, 쓰기 거부,
직접 외부 network 차단 및 비밀 환경 변수 미전달을 모두 통과했다.
실제 auth/token 파일을 읽어 검사하지 않았으며, 지정된 deny 규칙 및 harmless fixture로 차단을 검증했다.

## 테스트

`python -m unittest discover -s tests` 총 42개 통과 (Phase 0 11개 + Phase 1 31개).
`pip check` 및 Python compile 검증도 통과했다.
주요 시나리오는 FIFO, immutable thread 선택, update/message 중복, 명령 중복, 비인증/forward/group 차단,
prefix ambiguity, 페이지네이션, queue limit, cancel, failed turn, crash uncertain, evidence reconciliation,
outbox 분할 순서·재전송 방지, migration 보존, 원문 입력, early/duplicate/unknown event,
approval 거부, 비밀 환경 필터 및 token 설정 누락 종료다.

Kakao-Radar unit 파일 SHA-256 및 선택한 runtime 속성/PID는 Phase 0 기준과 동일했다.
기존 서비스 코드/DB/env/token을 수정하거나 서비스를 재시작하지 않았다.
이 비교는 Telegram 업무 응답 등 기존 서비스의 전체 건강 검증을 대신하지 않는다.

## 설치 버전 대응

0.162.1에서는 첫 메시지가 없는 새 thread에 resume하면 rollout 관련 -32600으로 거부됐다.
thread/start로 이미 열린 신규 thread는 첫 turn까지 재개를 생략하고 이후 turn은 resume한다.
새 thread의 rollout 경로가 아직 존재하지 않는 경우에도 안전한 preflight snapshot을 지원한다.

기본 read-only sandbox만으로 비밀 경로의 읽기를 제한할 수 없어 공식 권한 문서와
설치된 experimental schema를 확인했다. 기본 schemas 317개와 experimental schemas 447개를 보존했다.
initialize.experimentalApi를 켜고 start/resume/turn에 named permissions 필드를 적용했다.
activePermissionProfile ID와 legacy sandbox 요약의 readOnly를 실제 응답에서 검증한다.
stable sandbox/sandboxPolicy 필드와 permissions를 동시에 보내지 않는다.
Codex 설치 파일이 `.codex/packages`에 있어 이 런타임은 읽을 수 있게 두고, 나머지 기존
CODEX_HOME 하위 경로, auth/session/config, SSH/config/기존 서비스 및 Bridge data 경로는 읽기를 거부한다.

## 미검증 및 남은 범위

- 실제 전용 Telegram 연결, 스마트폰에서 최종 메시지 수신, Telegram API의 실제 rate-limit 상황.
- 실제 reboot/power failure, migration과 WAL의 hardware-level 내구성.
- 모든 외부 CLI에 대한 완전한 글로벌 락. 현재 휴리스틱은 보수적이며 한계가 있다.
- 수동 uncertain 해소/재시도 및 outbox 재전송 UI, retention, 백업/복구 운영.
- 승인 UI, 쓰기 모드, history/rename/fork, systemd 배포는 다음 운영 단계.
- 설치 버전의 experimental 권한 계약은 변경 가능하며 업그레이드 시 재검증 필요.
- callback 승인 UI가 없으므로 P0에서는 모든 추가 승인을 거부한다.

실제 Telegram 연결에 필요한 후속 설정은 `.env.example` 및 `OPERATIONS.md`에 있다.
현재 전경 실행도 token/user/chat 누락으로 정상적으로 거부되며 운영 서비스는 활성화하지 않았다.

참고: [공식 App Server 문서](https://developers.openai.com/codex/app-server),
[공식 권한 프로파일 문서](https://developers.openai.com/codex/permissions),
[Telegram Bot API](https://core.telegram.org/bots/api).
OpenAI Docs 스킬의 공식 문서 확인 절차에 따라 권한 프로파일을 선정하고 설치 스키마/RPC/실행 검증으로 확인했다.
