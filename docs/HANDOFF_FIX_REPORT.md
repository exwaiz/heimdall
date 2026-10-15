# Telegram → SSH 스레드 점유 해제 수정 (2026-10-15)

## 원인과 재현

기존 단일 App Server는 답변이 끝나도 로드한 thread를 계속 소유했다.
`run_turn`의 finally는 Python 이벤트 큐만 정리했고, `/release`의 unsubscribe도
즉시 lease 해제를 보장하지 않았다. 설치 Codex 0.162.1 별도 테스트에서:

1. 답변 완료 후 다른 App Server의 resume: `thread_already_active`.
2. unsubscribe 성공 직후 다른 App Server의 resume: 동일 거절.
3. 소유 App Server 프로세스 종료 후 다른 App Server의 resume: 성공.

[공식 App Server 문서](https://learn.chatgpt.com/docs/app-server)도 unsubscribe의
유휴 유예 기간을 설명한다. 이전 Phase 2의 unsubscribe/RPC 프로필 검증만으로
SSH와의 순차 인계가 충분히 검증됐다는 판단은 정정한다.

## 수정

- 운영 엔트리포인트는 `HandoffCodexAdapter` 사용. 기존 `CodexAdapter`는
  저수준 프로토콜 구현으로 유지하며, 기존 테스트/권한 경계를 재사용한다.
- 목록·설정 등 metadata 조회 연결은 별도로 유지하며 thread/resume을 하지 않는다.
- 각 스레드의 모델 작업은 전용 App Server에서 수행. `completed`, `failed`,
  `interrupted` 정식 결과를 받은 뒤 해당 프로세스가 실제 종료될 때까지 기다린다.
- 대화 ID, 선택 binding, DB 작업 결과와 대화 원본은 삭제하거나 이동하지 않는다.
- 첫 질문 전의 빈 새 thread는 생성 서버를 닫으면 재개되지 않는 것을 실제 확인했다.
  따라서 다른 새 thread를 통째로 닫지 않고 해당 생성 서버만 첫 작업까지 보존한다.
- 불확실/알 수 없는 결과, timeout/연결 단절/취소 예외의 소유 서버는 임의 종료하지 않는다.
  reconcile로 정식 종료 결과를 확인한 경우에만 해제한다. 수동 상태 확정 뒤의
  `/release`도 실행 중 여부를 확인하며 다른 CLI 프로세스를 종료하지 않는다.
- 승인 handler·interrupt는 해당 작업 서버로 전달한다. metadata 재연결은 작업
  서버를 닫지 않는다. 전역 단일 모델 실행 정책은 유지한다.
- 보존 연결은 생성 중 포함 최대 8개로 제한한다. 미사용 새 세션을 계속 만들면
  안내 오류를 반환한다. 첫 질문 없는 세션의 `/release`는 유실 방지를 위해 거절한다.
- 인증·샌드박스·쓰기 allowlist·원문과 메모리 안내 입력·근거 검증 정책은 유지한다.

## 검증

- 새 회귀 테스트 13개: 실제 close 호출, 다른 새 thread 보존, 재진입,
  완료/실패/중단, 불확실·취소 보존, reconcile, metadata 재연결, interrupt/승인 전달,
  동시 실행 거부, 한도/생성 실패 정리, 외부 점유 거절 정리, Bridge 통합.
- `python -m scripts.smoke_handoff`: 별도 DB와 가상 Telegram, 실제 Codex로
  질문 완료 → 독립 Codex 재개/기록 조회 → Telegram 재질문 → 독립 Codex 재개 성공.
  중간에 만든 첫 질문 전 새 thread도 보존됐다. 기존 선택 ID와 2회 대화 기록 유지.
- 이 검증의 독립 클라이언트는 실제 App Server RPC이다. 모바일 SSH 터미널 화면을
  직접 확인한 것으로 표현하지 않는다.
- pip check 및 compileall 통과. 전체 테스트와 운영 배포 결과는 아래 기록한다.

## 운영 반영 결과

- 전체 단위·통합 테스트 209개 통과.
- 작업/추출/승인 대기 및 uncertain 작업이 없음을 확인한 뒤 DB+원본 bundle 백업:
  `knowledge-backup-orqiisuo.zip` (검증 성공).
- `heimdall-bridge.service` 재시작 후 bridge_ready, active/running,
  MainPID=804338, NRestarts=0 확인. 다른 서비스·CLI는 종료하지 않았다.
- 재시작 전에 브리지가 점유했던 스레드 3개 모두 독립 Codex의 thread/resume 성공:
  개인 스레드 ID는 공유용 문서에서 생략했다.
  모델 질문 없이 재개만 확인한 뒤 검증 서버도 종료해 점유를 남기지 않았다.
- 재시작 전후 완료 16건/실패 7건, 선택 ID/readonly binding, 지식 자료 유지.
  quick_check=ok, 재시작 전후 기존 세 대화 JSONL SHA-256 일치.
- 실제 SSH 터미널에서 사용자 재개 화면 확인은 남지만, 같은 로컬 Codex 점유
  경로의 독립 재개 성공을 확인했다. SSH와 Telegram 동시 사용은 여전히 지원하지 않는다.
