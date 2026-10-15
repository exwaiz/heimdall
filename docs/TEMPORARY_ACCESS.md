# 30분 임시 일반 모드 권한 (사용자 승인, 2026-10-15)

## 현재 권장 사용: Telegram `/mode network`

```text
/select <세션 ID>
/mode network
<인터넷이나 브라우저가 필요한 질문>
/mode
/status
/mode read
```

- 소유자의 private chat만 가능하다. 세션 선택이 필요하며 켠 명령을 보낸 때부터 30분이다.
  새 메시지로 `/mode network`를 다시 보내면 신규 작업의 창을 30분으로 갱신한다.
  동일 Telegram update/message의 중복 수신은 창을 연장하지 않는다.
- 선택 세션의 다음 신규 작업만 network 권한과 절대 만료 시각을 DB에 고정한다.
  이미 대기 중인 일반 작업을 승격하지 않는다. 다른 세션 선택/새 세션은 기본 read로 시작한다.
  등록된 작업의 대상/권한은 원래대로 고정되며, `/ask`/`/extract`는 기존 제한을 유지한다.
- 네트워크와 일반 홈·`/tmp` 파일 쓰기를 허용한다. 기존 비밀정보/운영코드 보호 및
  `untrusted` 실행 승인·owner/nonce/replay 방지는 유지한다. 관리자가 root가 되는 모드는 아니다.
- `/mode read` 또는 `/mode write`는 네트워크 창을 해제하고 기존 네트워크 대기 작업을
  취소한다. 실행 중인 네트워크 작업은 interrupt/전용 App Server 종료를 통해 중단한다.
  이미 끝난 변경을 되돌리는 명령은 아니다. 일반 대기 작업은 유지한다.
- 만료된 대기 작업은 실행 전 거절한다. 실행 중인 네트워크 작업은 monotonic deadline으로
  중단하며 잠금을 해제하고 uncertain을 보존한다. 자동 재실행하지 않는다.
- 네트워크 작업에서 연결 단절 등 예외로 제어를 잃으면 무인 상태로 권한을 남기지 않도록
  해당 전용 서버를 종료한다. 일반 작업의 기존 uncertain 보존 정책은 바꾸지 않는다.
- 실행 승인 버튼은 명령 실행 승인이다. `/mode network`와 별개이며 승인 버튼만으로
  네트워크가 열리지는 않는다. `/mode`와 `/status`에 현재 모드와 남은 시간이 표시된다.
- DB migration 7의 `chat_bindings.network_until`, `jobs.network_until`에 고정 만료를 보관한다.
  서비스 재시작이 창을 연장하지 않는다. 만료는 서비스 전체 재시작 없이 해당 세션/작업에 적용한다.
  이전 config.toml 고정 timestamp는 제거했으며 평소 기본 프로필은 network=false다.
- `/retry`로 과거 네트워크 권한을 되살리지 않는다. `/mode network` 후 새 질문을 보내야 한다.

검증: 전체 242개 테스트, 새 네트워크 모드 회귀 12개 통과.
`python -m scripts.smoke_network_mode`는 격리 DB·가상 Telegram의 소유자 버튼과 실제 Codex를
사용해 localhost 접속·일반 홈 쓰기, 실제 approval RPC, 작업 종료 후 잠금 해제,
`/mode read` 이후 동일 대화의 제한 프로필 재개를 확인했다. 실제 공유기 설정은 변경하지 않았다.
명령을 추가·배포할 뿐 이번 개발 작업이 운영 채팅의 네트워크 모드를 자동 활성화하지는 않는다.
배포 전 전체 bundle `knowledge-backup-j1bucsbq.zip` 검증 백업 및 유휴 상태 확인 후
전용 서비스를 재시작했다. MainPID=832650, active/running, NRestarts=0, bridge_ready 확인.
운영 DB migration 7, quick_check=ok. 기존 workspace 선택 모드와 네트워크 OFF(0) 유지.

## 이전 운영자 수동 override (기록)

사용자가 일반 모드에도 인터넷 접속·일반 파일 읽기/쓰기를 30분간 허용하고,
인증/비밀 설정/운영코드 보호와 실행 승인을 유지하는 범위를 명시적으로 승인했다.
원래 SPEC의 제한을 영구 변경하지 않으며 `danger-full-access`를 사용하지 않는다.

## 범위

- 운영 `config.toml`의 `[bridge].temporary_access_until`에 timezone-aware 절대 시각을 기록한다.
  live 설정 로딩에서 최대 30분 이내의 미래 시각만 허용한다. 만료된 값은 무시한다.
  재시작해도 창을 새로 30분 연장하지 않는다. non-live 테스트는 이 운영 override를 무시한다.
- 임시 창에서 두 named profile 모두 추적 홈(`/home/haaland`)과 `/tmp`의 일반 파일을
  쓰기 가능하게 하고 명령 네트워크를 허용한다. OS 사용자 권한을 넘어서는 쓰기는 허용하지 않는다.
- 기존 Codex 인증/설정/세션, 봇 키, 브리지 private data, SSH/config/AWS/GPG,
  personal-agent/Kakao-Radar는 기존 deny를 유지한다.
- 프로젝트 전체와 Codex 실행 파일 디렉토리·Playwright runtime·브라우저 binary는
  read-only로 보호하고, 프로젝트 내 명시적 workspaces만 쓰기를 다시 허용한다.
  존재하는 dotenv/credential 파일은 차단하며 shell startup 파일은 read-only다.
- Codex 0.162.1 bubblewrap는 writable home 아래 존재하지 않는 read/deny 대상 때문에
  기동 실패할 수 있다. 임시 profile은 존재하는 경로만 mount rule로 넘긴다.
  빈 credential 파일/폴더를 만들지 않는다. 보호 경로는 각 App Server 시작 때 다시 계산한다.
  임시 창에 다른 프로그램이 새 비밀 파일을 일반 작업 경로에 생성하지 않도록 주의한다.
- 일반 모드도 `untrusted` 승인 정책을 적용한다. 기존 owner·private chat·nonce·일회성·
  만료·재클릭 차단을 유지하고 추가 sandbox/network 권한 요청은 계속 거절한다.
- `/status`는 임시 권한과 남은 초를 표시한다. 일반 모드라는 저장된 명칭이 읽기 전용을
  의미한다고 답하지 않도록 실제 모델 입력에 임시 권한 문맥을 덧붙인다.
- Chromium용 TMPDIR은 짧은 writable workspace로, XDG_CONFIG_HOME/CACHE_HOME은
  그 아래 browser-config/browser-cache로 명시한다. `~/.config`를 열거나 HOME을 바꾸지 않는다.

## 자동 복귀

운영 서비스는 deadline의 남은 시간만큼 monotonic timer를 기다린 뒤 만료 안내를 outbox에
남기고 `TemporaryAccessExpired`로 종료한다. 종료 경로에서 worker를 취소하고 실제 turn을
중단 요청하며 브리지 소유 App Server들을 닫는다. systemd `Restart=on-failure`, `RestartSec=5`
설정에 의해 다시 시작하고, 고정된 만료값은 이미 지났으므로 기본 읽기 전용/네트워크 차단으로
복귀한다. 만료 중 작업은 uncertain 등 기존 보수적 상태 처리로 남으며 자동 재실행하지 않는다.
종료에는 짧은 graceful-shutdown 시간이 필요할 수 있다. 새 작업과 새 승인은 만료 후 거절한다.
서비스가 재부팅/장애 뒤 늦게 시작돼도 만료된 설정으로 임시 권한이 부활하지 않는다.

## 검증

`python -m scripts.smoke_temporary`는 라우터가 아닌 localhost 테스트 서버에서 실제 sandbox,
Playwright/Chromium, 페이지 버튼 클릭 및 PNG 저장, 일반 홈 파일 쓰기,
private sentinel 읽기 차단, 실제 운영코드의 O_WRONLY 열기 차단(내용은 쓰지 않음),
App Server의 일반 모드 profile 검증, 만료 시 쓰기/네트워크 차단을 확인한다.
추가 단위 테스트는 deadline/30분 상한·승인·만료·브라우저 환경 및 실제 서비스 종료 경로를 검사한다.
공유기 접속/로그인/설정 변경은 이 권한 적용 과정에서 실행하지 않는다.

기준: [OpenAI 공식 permission profiles](https://learn.chatgpt.com/docs/permissions).

## 이번 활성화 기록

- 만료: 2026-10-15 19:23:15 KST (이전 고정 UTC 값 2026-10-15T10:23:15.355281+00:00).
- 활성화 전 전체 테스트 230개, pip check, compileall 및 실제 browser/만료 sandbox 스모크 통과.
- 검증 백업: `knowledge-backup-nv9_phtp.zip`.
- 대기/실행/uncertain 작업·추출·승인이 없는 상태에서 전용 bridge 재시작.
  MainPID=824278, active/running, NRestarts=0, bridge_ready 확인.
- 실행 중인 실제 App Server 프로필: network=true, home=write, bridge code=read,
  auth=deny, private data=deny. DB quick_check=ok, 기존 완료 25건/실패 9건 유지.
- 30분 실시간 경과 자체를 기다려 확인한 것은 아니며, 만료 정책 스모크와 서비스 종료 경로
  단위 테스트로 자동 복귀를 검증했다. 실제 만료 시 systemd 재시작 로그로 추가 확인 가능.
