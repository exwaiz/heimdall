# 운영 안내 · Phase 2~4

지식 저장·검색·추출과 전체 백업은 [KNOWLEDGE.md](KNOWLEDGE.md)가 최신 기준이다.
추가 명령은 `/save`, `/items`, `/item`, `/tag`, `/search`, `/ask`, `/extract`, `/processing`, `/forget`이다.
Phase 4부터 `/backup`은 DB뿐 아니라 첨부 원본 ZIP도 포함한다. 아래 DB-only 설명은
`--include-assets` 없는 CLI 백업이나 기존 Phase 2 백업에 해당한다.

## Telegram

| 명령 | 동작 |
| --- | --- |
| `/database [페이지/검색어]` | `/home/haaland` 세션 조회 |
| `/select <ID/고유 접두사>` · `/new [이름]` | 세션 선택·생성, 읽기 전용 시작 |
| `/current` · `/status` | 현재 연결, 권한, 큐, 승인, 디스크 상태 |
| `/mode read` · `/mode write` | 이후 신규 작업 권한 지정, 기존 큐는 불변 |
| `/rename <이름>` | 로컬 별칭 및 Codex 이름 동기화 |
| `/history [1~20]` | 사용자와 정식 최종 답변만 조회 |
| `/fork [이름]` | 원본을 보존하고 새 분기 세션 선택 |
| `/release` | 이 브리지의 유휴 연결만 해제, 기록은 보존 |
| `/queue` · `/jobs [개수]` · `/job <ID>` | 실행/불확실 작업, 최근 작업, 저장 결과 |
| `/cancel` | 현재 작업 중단 요청 또는 첫 대기 작업 취소 |
| `/recover` | 실제 turn 이력으로 불확실 작업 대조; 자동 재실행 아님 |
| `/resolve <ID> failed 또는 cancelled` | 유휴 세션의 uncertain 상태를 수동 확정; 실제 실행/되돌리기 아님 |
| `/retry <ID> [repeat]` | 원본 보존 후 새 작업; 실행 이력이 있으면 `repeat`로 중복 위험 명시 |
| `/deliveries` · `/resend <ID> [duplicate]` | 답변 전송만 재시도; 불확실 전송은 `duplicate` 확인 필요 |
| `/backup` | SQLite online DB 백업·무결성 검사 |
| `/assets [개수]` · `/asset <ID>` | 저장 자료 목록·유형·크기·해시 |

일반 텍스트와 사진 캡션은 재작성하지 않는다. 캡션 없는 사진에는 기본 설명 요청을 사용한다.
사진은 최대 크기 버전 하나를 저장하고 이미지 ID를 작업에 고정한다. 다운로드 도중 세션을
바꾸더라도 원래 선택·권한을 유지한다. 문서·음성·영상은 모델 작업을 만들지 않는다.
모바일 실제 입력/클릭 확인과 자동화된 테스트는 별도 검증 범위다.

## 안전한 쓰기·승인

기본 읽기 전용이며 home 전체 쓰기를 허용하지 않는다. 설정된 쓰기 폴더는
`/home/haaland/heimdall/workspaces/default`다. 다른 프로젝트를 추가하려면
`config.toml`의 security.write_workspaces를 검토·검증 후 서비스에 반영한다.
Codex 인증·기록, personal-agent, Kakao-Radar, SSH/config, 브리지 코드·DB와 겹치면 거절한다.

쓰기 모드의 지원되는 명령/파일 변경만 Telegram 버튼으로 요청한다. 기본 120초 만료,
소유자·개인 chat·메시지·nonce·연결·thread·turn·요청을 대조한다. 요청당 한 번만 승인하며
세션 영구 승인이나 execpolicy 변경을 저장하지 않는다. 취소·종료·연결 종료 후 버튼은 무효다.
추가 파일 권한, 원격 환경, 네트워크 확대 및 미지원 요청은 거부한다.

0.162.1은 이미 로드한 세션의 resume에서 이전 권한을 반환할 수 있다. 모드 변경 시
브리지 자신의 유휴 actor만 해제·재로드하고 반환된 프로파일을 재검증한다.
다른 CLI를 종료하거나 활성 turn을 강제로 해제하지 않는다.

## 장애·재시도

실행 Worker는 전역 1개다. 같은 cwd의 다른 CLI만 있다는 이유로 막지 않지만,
같은 thread의 실행/명시적 CLI 사용·기록 변경·협조 락 충돌은 차단한다.
협조하지 않는 외부 CLI의 동시 쓰기를 완벽히 감지하지는 못한다.

실행 수락 여부가 불확실하면 uncertain으로 보존하고 자동 반복하지 않는다.
15초 간격 및 재연결 시 알려진 turn ID의 최근 완료 이력을 대조한다.
불확실 turn이 아직 실행 중이거나 상태를 확인할 수 없으면 다음 전역 작업도 대기한다.
같은 thread는 uncertain 정리 전 새 작업을 실행하지 않는다.

`/retry`와 `/resend`는 각각 모델 재실행과 답변 재전송으로 전혀 다르다.
이미 실행된 모델 작업의 `repeat`, 전송 수락 여부가 불확실한 답변의 `duplicate`는
명시적으로 중복 위험을 확인하는 옵션이다. 승인 버튼은 재전송하지 않는다.
`/cancel`은 기존 파일 변경을 되돌리지 않는다.

`usageLimitExceeded` 등은 계정 사용량·속도 제한으로 안내한다. 브리지는 한도 회복을
추정하거나 자동으로 질문을 재실행하지 않는다. 새 API 키/과금 경로로 우회하지 않는다.
이때 `/status`, `/database`, `/assets` 등 모델을 호출하지 않는 운영 명령은 사용할 수 있다.

## 서비스·데이터

system `heimdall-bridge.service`만 사용한다. 기존 임시 user unit은 중지돼 있다.

```sh
systemctl status heimdall-bridge.service --no-pager
systemctl is-enabled heimdall-bridge.service heimdall-backup.timer
systemctl list-timers heimdall-backup.timer --no-pager
journalctl -u heimdall-bridge.service -n 30 --no-pager
```

재시작 전 봇 입력을 잠시 멈추고 대기·실행 작업이 없는지 확인한다.
명시적인 운영 재시작은 `sudo systemctl restart heimdall-bridge.service`다.
재시작은 선택/큐/결과를 보존하고 실행 중 작업을 불확실 상태로 만든다.
`Restart=on-failure`와 시작 횟수 제한을 사용한다. 버전/인증 오류를 무한히 우회하지 않는다.
부팅 자동 시작은 enable돼 있지만 실제 재부팅은 별도 사용자 승인 후 테스트한다.

설정은 `/home/haaland/.config/heimdall/bridge.env`(0600), 데이터는
`/home/haaland/.local/share/heimdall`(0700)다. 토큰을 출력하거나 journal에 남기지 않는다.
DB는 WAL·FK·migration을 사용하며 프롬프트/결과 및 자료 메타데이터를 평문으로 저장한다.
첨부는 assets/의 0600 파일로 저장한다. 운영 로그에는 본문·키·원시 RPC 오류를 기록하지 않는다.

일일 백업 타이머는 03시 이후 무작위 지연으로 DB를 online backup한다.
복원은 신규 경로만 허용하며 live DB·기존 파일을 덮어쓰지 않는다.
DB 백업에는 첨부 원본·Codex 기록·인증이 포함되지 않는다.
백업/자료의 자동 삭제 정책은 없으므로 디스크와 보존 기간을 직접 관리한다.

CPU/IO 가중치·task 제한 및 journal rate limit을 설정했다. 이 Pi 커널에는 memory
cgroup controller가 없어 MemoryHigh/MemoryMax는 강제되지 않는다. 부팅 설정을
변경하지 않았으며, 장시간/대용량 작업의 메모리 압박은 계속 모니터링해야 한다.

세부 사항: [배포](DEPLOYMENT.md), [백업·복구](BACKUP.md), [첨부](ATTACHMENTS.md).
