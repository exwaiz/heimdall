# Phase 2 결과 · 2026-10-15

견고한 운영 기능 구현 및 실제 system 서비스 배포를 완료했다.
최종 Phase 2/3 통합 테스트는 164개 통과했다.

## 구현

- stdio RPC 재연결·자식 종료/재생성, startup/15초 주기 turn reconciliation.
- 실행 수락 불확실성 보존, 자동 재실행 금지, 실제 turn ID 대조, 수동 inspect/recover/resolve/retry.
- Telegram 승인 버튼: 소유자·private chat·메시지·nonce·연결·thread·turn·요청 결합,
  기본 거부·120초 만료·재클릭 차단·취소/재시작 무효화. 추가 권한·네트워크 확대 불허.
- 읽기 전용 기본 및 명시적 쓰기 모드, 허용 폴더만 쓰기, 등록 당시 권한 고정.
- rename/history/fork/release, outbox 실패 조회·중복 위험을 명시한 수동 재전송.
- 독립 systemd service, 일일 DB online backup timer, 별도 경로 복원·무결성 검사.
- 단일 전역 worker, uncertain turn의 실제 실행이 끝나기 전 다른 thread도 대기.

Termius 협조 락 래퍼는 명세의 선택 항목으로 이번에는 추가하지 않았다.
Bridge 자체 flock과 thread별 preflight를 유지하며 비협조 외부 CLI의 전역 락을 보장하지 않는다.

## 실제 Pi 검증

`scripts/smoke_phase2.py`의 성공한 실행에서 다음을 확인했다.

- 제한 폴더에 disposable 파일 쓰기 성공, 폴더 밖 쓰기 및 보호 파일 읽기 차단.
- 실제 Codex turn, history, fork, idle release, app-server 재생성 후 thread read.
- 실제 command approval RPC → 테스트용 가상 Telegram 소유자 버튼 → 승인 → 지정 파일 생성.
- 같은 세션의 workspace resume 후 실제 두 번째 turn 완료.

실제 휴대폰에서 사용자가 승인 버튼을 누르는 E2E는 별도 미검증이다.
코드의 가상 Telegram 승인 클릭을 사용자의 실제 클릭으로 표현하지 않는다.

모드 복귀 검사에서 0.162.1의 loaded-actor 캐시 문제를 발견했다.
workspace actor에 readonly resume을 보내도 응답은 기존 workspace profile이었다.
fail-closed 검사로 실행을 막았고, 자신의 idle actor만 unsubscribe 후 재로드하도록 수정했다.
수정 후 모델 호출 없이 실제 RPC로 workspace→readonly→workspace→readonly의
active profile과 sandbox type 검증을 모두 통과했다. 활성 actor 해제 거절 회귀 테스트도 통과했다.

최초 추가 모델 테스트는 `usageLimitExceeded`로 실패했다. 해당 실패를 성공으로 집계하지
않았으며, 호출을 중단하고 오류 분류·사용자 안내를 구현했다.

이후 사용자가 사용량 제한 해결을 알리고 재검증을 요청했다. 동일 로그인·모델 설정으로
`python -m scripts.smoke_phase2`를 다시 실행해 전체 `status=passed`를 확인했다.
실제 쓰기 승인·파일 생성, 같은 세션의 workspace resume, `/mode read` 이후의
`PHASE2_READONLY_OK` 정식 최종 답변까지 통과했다. 이로써 사용량 제한으로 남았던
모드 복귀 후 모델 답변 검증을 완료했다. 전체 로컬 테스트 164개도 다시 통과했다.
운영 bridge와 backup timer가 active이고 bridge의 NRestarts=0인 것도 확인했다.
격리된 테스트 DB·새 테스트 세션을 사용했으며 운영 세션 선택이나 기존 작업을 변경하지 않았다.

## 배포·데이터

- `/etc/systemd/system/heimdall-bridge.service`, `heimdall-backup.service`,
  `heimdall-backup.timer` 세 파일만 신규 설치.
- system bridge와 backup timer active/enabled, `bridge_ready` 확인.
- 이전 정확한 transient user service만 중지, 기존 CLI·Kakao-Radar 서비스 변경 없음.
- 실제 SQLite online backup 및 0600 확인, 새 private 경로에 복원 후 integrity_check=ok.
- 실제 backup.service 수동 실행 성공, daily timer 예약 확인.
- 기존 완료 4건/실패 6건 및 선택 binding을 보존한 상태로 migration 1~4 적용.
- 실제 서비스 재시작 후 같은 binding·작업 건수 유지 및 integrity_check=ok 확인.

호스트의 systemd는 frozen unit stop을 거절했다. installer 실패 정리에서 기존 unit을
thaw하여 유지했고, 이후 freeze→상태 확인→명시적 thaw→동기 stop 순서로 성공했다.
이 과정은 입력 중지 전제이며 check/stop 경쟁을 완전히 제거하지는 않는다.

## 제한·미검증

실제 재부팅은 수행하지 않았다. enable 상태 확인과 실제 부팅 복원 검증은 다르다.
커널 cgroup controller에 memory가 없어 MemoryHigh/MemoryMax는 설정만 있고
실제 강제되지 않는다. CPU/IO/task 제한과 journal rate limit은 별도로 설정했다.
부팅 설정·global journal 정책·기존 인증 설정을 변경하지 않았다.
DB 백업은 첨부 원본·Codex 세션·인증의 백업이 아니며 자동 retention/삭제도 없다.

운영 절차는 [OPERATIONS.md](OPERATIONS.md), [DEPLOYMENT.md](DEPLOYMENT.md),
[BACKUP.md](BACKUP.md)를 참조한다.
