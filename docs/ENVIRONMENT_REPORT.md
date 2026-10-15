# Phase 0 환경 및 프로토콜 보고서

현재 상태: Phase 0 완료. 사용자가 Termius에서 새로 만든
테스트 세션(개인 ID 생략, 2026-10-14 22:34:30 KST)을
App Server 목록과 thread/read로 확인하고 사용자 확인으로 대조를 마쳤다.
출처 문자열은 vscode로 보고되므로 출처명만으로 CLI 세션 여부를 판단하지 않는다.
아래는 최초 진단 당시의 기록이며, 이후 구현 검증은 PHASE1_REPORT.md를 참조한다.

## 후속 변경 — 사용자 지정 세션 추적 범위

사용자 요청으로 `config.toml`의 기본 추적 cwd를 `/home/haaland`로 지정했다.
thread/list에 정확한 cwd 필터를 적용하고 목록 및 thread/read 결과도 검증한다.
적용 후 실제 세션 2개를 조회하고 metadata read에 성공했다. 두 세션의 출처는 vscode이다.
재검증 테스트는 총 11개 통과했다. 결과는 `tracking-result.json`에 보관한다.
아래의 9개 조회 결과와 `smoke-result.json`은 변경 전 전체 목록 조회의 역사적 근거다.
현재 추적은 조회 명령 실행 시 갱신하며 상시 polling은 아직 구현하지 않았다.
세션 파일은 기존 CODEX_HOME에 보관되고 personal-agent 기록은 이동/변경하지 않았다.

확인일: 2026-10-14, Asia/Seoul. 상세 관측 시각은 `diagnostic.json`의 UTC 값 참조.

환경 진단, 스키마 생성, 기존 세션 발견과 메타데이터 read 스모크는 성공했다.
최초 진단 당시 **기존 Termius/CLI 세션 발견** 항목은 미충족이었고 cli 출처가 없었다.
실제 대화 재개, 모델 실행, Telegram 연동 및 양방향 기록 일관성은 후속 단계에서 검증해야 한다.

## 장비 및 인증

| 항목 | 실제 관측 |
|---|---|
| 하드웨어 | Raspberry Pi 4 Model B Rev 1.5 |
| 호스트 / 사용자 | haaland-agent / haaland (uid/gid 1000) |
| OS | Debian GNU/Linux 13 (trixie), 13.7 |
| 커널 / CPU | 6.18.50+rpt-rpi-v8 / aarch64 |
| Python | 3.13.5 |
| Codex | codex-cli 0.162.1 |
| 실행 파일 | /home/haaland/.local/bin/codex |
| HOME / 유효 CODEX_HOME | /home/haaland / /home/haaland/.codex |
| 로그인 | Logged in using ChatGPT |
| RAM | 총 3.7GiB, 진단 시 available 약 2.7GiB |
| Swap | 총 2.0GiB, 사용 약 283MiB |
| 파일시스템 | /dev/mmcblk0p2, 약 115GiB, 여유 약 99GiB |
| 세션 기록 | JSONL 파일 9개, 약 39.6MiB |

명세의 64GB 저장장치 가정과 실제 파일시스템 용량은 다르다. 실제 관측값을 사용한다.
ChatGPT 로그인 성공은 모델별 사용 권한, 잔여 사용량 또는 무제한 사용의 증거가 아니다.
자격 증명 원문이나 account 응답은 읽거나 기록하지 않았다.

## 실제 App Server 스모크

생성 명령:

```sh
codex app-server generate-json-schema --out /home/haaland/heimdall/schemas
```

기본 스키마 317개, 합계 3,602,403 bytes. `--experimental`은 사용하지 않았다.
ClientRequest에는 105개 method가 선언되어 있다. 선언 여부는 실제 호출 성공과 구분한다.

스모크 프로세스는 `--strict-config --listen stdio://`와 다음 호출별 config override로 실행했다:
`sandbox_mode="read-only"`, `approval_policy="on-request"`, `approvals_reviewer="user"`.
기존 config 파일은 편집하지 않았다.

| 작업 | 실행 결과 |
|---|---|
| initialize → 응답 → initialized | 성공 |
| thread/list | 성공, 9개, 다음 페이지 없음 |
| thread/read, includeTurns=false | 성공, 요청/응답 ID 일치 |
| thread/resume / thread/start / turn/start | 실행하지 않음 |

목록은 `sourceKinds=[cli,vscode,exec,appServer]`, `updated_at desc`, `archived=false`,
`limit=50`, `useStateDbOnly=true`로 조회했다.
출처는 vscode 8개, exec 1개이며 cli/appServer 출처는 없었다.
read 대상은 기존 테스트 세션(개인 ID 생략)이고 cwd는 `/home/haaland`이다.
이 ID가 포함된 기존 rollout 파일의 존재도 filename으로 확인했다. 원문 대화 파싱은 하지 않았다.
이 세션은 vscode 출처이므로 Termius에서 생성됐다고 주장할 수 없다.

모든 thread가 이 진단 프로세스에서 `notLoaded`로 보고됐다.
이는 별도 CLI 프로세스의 실행 여부를 보장하지 않는다. 실제로 사용자 소유 Codex 프로세스가
여러 개 실행 중인 것을 프로세스명만으로 확인했다. 특정 thread와의 연관성은 확인하지 않았다.
프로세스 인자와 환경 변수는 수집하지 않았다.

## 설치 버전에서 확인한 프로토콜 계약

- InitializeParams는 v1, thread/turn Params는 v2 디렉터리에 생성된다.
- thread/start 및 thread/resume은 `sandbox: "read-only"`를 사용한다.
- turn/start는 `sandboxPolicy: {"type":"readOnly","networkAccess":false}`를 사용한다.
- 승인은 `approvalPolicy: "on-request"`, `approvalsReviewer: "user"`로 고정할 수 있다.
- thread/resume의 `excludeTurns`, thread/read의 `includeTurns`가 지원된다.
- thread/turns/list, thread/items/list, thread/name/set, thread/fork, turn/interrupt가 스키마에 있다.
- agentMessage phase는 commentary, partial_answer, final_answer가 있고 phase 미지정도 고려해야 한다.
- command/file approval의 거부 응답은 `decision: "decline"` 또는 `"cancel"`이다.
- 서버 요청에는 permissions approval, tool user input/call, MCP elicitation, auth refresh 등이 추가로 있다.
  P0에서 미지원 요청이 무기한 대기하지 않도록 method별 처리 정책이 필요하다.
- thread/list의 useStateDbOnly=true는 rollout 스캔에 따른 metadata repair를 회피한다.
  state DB에 아직 없는 세션은 누락될 수 있으므로 cli 출처 누락의 원인은 아직 단정할 수 없다.

위 실행 권한 값은 스키마 확인과 프로세스 config 수용까지 검증했다.
실제 도구 실행의 sandbox 강제, 비밀 파일 읽기 차단, 네트워크 제한은 모델 turn을 실행하지 않아 미검증이다.
RPC 호출의 전체 JSON Schema 검증은 아직 구현하지 않았다. 진단 클라이언트는 제한된 method/field,
필수 필드, source/sort enum 및 metadata-only 조건을 검사한다.

## Kakao-Radar 보호 범위

보호 경로: `/home/haaland/personal-agent`, `/etc/kakao-radar`, `/var/lib/kakao-radar`.
기존 Codex 기록/인증 보호 경로: `/home/haaland/.codex`.
기존 자격 증명 파일과 runtime env 내용은 열지 않았다.

| 기존 unit | 사용자 | 진단 전/후 상태 |
|---|---|---|
| kakao-radar-api.service | kakaoradar | active/running |
| kakao-radar-local-https.service | kakaoradar | active/running |
| kakao-radar-analysis.service | kakaoradar | active/running |
| kakao-radar-delivery.service | kakaoradar | active/running |
| kakao-radar-telegram-commands.service | kakaoradar | active/running |
| kakao-radar-adb@haaland.service | haaland | active/exited |

위 unit 파일의 SHA-256, PID 및 선택한 runtime 속성은 진단 전후 동일했다.
이 검사는 애플리케이션 데이터나 Telegram 응답의 완전한 건강 검증은 아니다.
기존 서비스/코드/DB/env 변경 및 재시작 명령은 실행하지 않았다.
정상 작동 중인 기존 서비스와 현재 Codex 세션은 자체적으로 상태를 갱신할 수 있다.
App Server가 내부적으로 자신의 state DB/WAL 등을 사용하는 것을 포함해 `.codex` 전체가
byte 단위로 불변이라고 주장하지 않는다. 파일을 직접 편집하지 않았다.

## 백업 점검

저장 공간은 충분하다. 실제 백업은 수행하지 않았다.
Codex는 sessions뿐 아니라 state_5.sqlite와 WAL/SHM을 사용 중이다.
대화가 실행 중인 상태에서 임의 파일 복사만으로 일관된 복구를 보장할 수 없다.
P1에서 사용자 승인 하에 스냅샷 시점 조율과 SQLite online backup을 포함한 계획을 적용해야 한다.
기존 `.codex` 및 sessions 디렉터리 모드는 0775였다. 현재 단계에서는 권한을 바꾸지 않았다.

## 검증 및 남은 작업

`bash -n scripts/diagnose.sh`, 원본 SPEC의 byte 비교, unittest 9개와 실제 App Server 스모크가 통과했다.
테스트는 notification 선행, 응답 ID 불일치, EOF/timeout, RPC 에러의 민감 메시지 비노출,
예상치 못한 서버 요청 거절, metadata-only 조건 및 결과 콘텐츠 필터를 검증했다.

Phase 1은 FakeTelegramTransport와 제한된 workspace 정책, SQLite 큐부터 진행할 수 있다.
새 봇의 token/user/chat 설정은 아직 수집하지 않았고 기존 Kakao-Radar 설정을 재사용하지 않았다.
home 전체를 write 허용 workspace로 채택하지 말고 보호 대상 프로젝트와 비밀 경로를 고려해야 한다.

후속 실제 검증:

1. Termius/CLI 테스트 thread의 실제 ID를 확인하고 목록/read와 비교.
2. 독립 테스트 thread에서 read-only turn, 최종 메시지 및 sandbox 차단 검증.
3. CLI ↔ App Server의 순차 재개와 기록 일관성 검증.
4. Telegram 실제 연결과 승인 거부, FIFO, 중복, 장애 복구 검증.

App Server가 정상 동작하므로 exec fallback은 help 계약만 확인했다.
`codex exec ... resume <FULL_ID> -`의 stdin 및 JSON 옵션이 CLI help에 명시돼 있으나 실행은 미검증이다.

근거 파일: `diagnostic.json`, `smoke-result.json`, `schema-audit.json`, `isolation-check.json`, `../schemas/`.
OpenAI Docs 스킬에 따라 [공식 App Server 문서](https://developers.openai.com/codex/app-server)를 확인하고,
이 장비의 생성 스키마와 실제 RPC 응답을 구현 계약의 우선 근거로 사용했다.

