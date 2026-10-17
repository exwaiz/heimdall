# Heimdall

Telegram에서 기존 Codex 세션을 조회·선택하고 대화하는 개인용 독립 브리지.
Kakao-Radar와 별도 봇·설정·DB·서비스를 사용한다.

Raspberry Pi 위에서 SSH와 Telegram을 오가며 같은 Codex 대화를 이어가고,
사진·파일·개인 지식 자료를 다루는 단일 사용자용 프로젝트다.
현재 배포 경로와 테스트는 원래 Pi 환경에 맞춰져 있으며, 범용 설치 프로그램은 아니다.
인증·개인 자료를 제외한 코드와 명세·개발 기록을 보관한다. [보안 및 배포 범위](SECURITY.md).

현재: Phase 0~3 및 Phase 4의 지식 저장·검색·근거 답변·Codex OCR·영상 프레임 분석 구현.
음성 전사는 실험적 Realtime 인증 오류로 미지원 상태다.
실제 쓰기 승인, 이미지 입력, Telegram 파일 전송, 지식 질문, 원본 포함 백업·복원을 검증했다.
상세 검증 범위와 남은 제한은 [Phase 2 보고서](docs/PHASE2_REPORT.md),
[Phase 3 보고서](docs/PHASE3_REPORT.md), [Phase 4 보고서](docs/PHASE4_REPORT.md)에 있다.

Auto-Memory는 [설계서 v0.1](docs/AUTO_MEMORY_DESIGN_v0.1.md)를 기준으로 개발 중이다.
PR-A/B/C 코드(기본 OFF 설정·스키마 v8·검색/참조 검증·일반 대화 참고 주입·설정 명령)를
구현했고 2026-10-17 운영에 기본 OFF로 반영했다. 자동 검색은 소유자의 명시적 명령으로
켤 수 있으며 자동 캡처는 미구현이다. [배포 확인·테스트 순서](docs/AUTO_MEMORY_DEPLOYMENT.md).
[구현 범위와 다음 단계](docs/AUTO_MEMORY_IMPLEMENTATION.md).

## 사용

1. `/database` → `/select <ID>` 또는 `/new [이름]`.
2. 질문 또는 사진+캡션을 전송한다. 사진은 저장하고 선택 세션에 전달한다.
3. `/status`, `/jobs`, `/job <ID>`로 상태·결과를 확인한다.

기본은 읽기 전용이다. `/mode write`는
`/home/haaland/heimdall/workspaces/default`만 쓰기를 허용한다.
추가 권한·네트워크 확대는 거부하고, 지원하는 승인 버튼은 요청당 한 번만 유효하다.
`/mode read` 또는 새 세션 선택으로 읽기 전용으로 돌아간다.
`/mode network`는 선택 세션의 신규 작업에 30분간 인터넷·일반 홈 파일 및 `/tmp` 쓰기를
허용한다. 비밀정보·운영코드 보호와 승인 절차는 유지한다. `/mode read` 또는 `/mode write`로
조기 해제하며, 만료 시 자동으로 읽기 전용·네트워크 차단으로 복귀한다. `/mode`, `/status`로
남은 시간을 확인한다. [네트워크 모드 상세](docs/TEMPORARY_ACCESS.md).

Pi 설치 이력·패키지 목록은 기본 읽기 모드에서도 조회한다. APT 로그와 dpkg 상태만
읽기 허용하며, 설치·삭제나 시스템 전체 접근을 허용하지 않는다.
권한 확대의 자동 차단은 사용자가 승인 버튼을 거부한 것과 구분해 안내한다.
[설치 기록 접근 수정](docs/PACKAGE_ACCESS_FIX.md).

완료·실패·중단이 확인된 모델 작업은 전용 App Server를 종료하여 스레드 점유를
자동 해제한다. 이후 SSH의 `codex resume <ID>`로 순차 재개할 수 있다.
결과 불확실 작업은 임의 종료하지 않는다. 첫 질문 전의 새 세션은 별도 연결을 유지한다.
[스레드 잠금 수정·검증](docs/HANDOFF_FIX_REPORT.md).

`/rename`, `/history`, `/fork`, `/release`, `/cancel`, `/recover`, `/retry`,
`/resolve`, `/deliveries`, `/resend`, `/backup`, `/assets`, `/asset`을 제공한다.
Codex 최종 답변의 로컬 PNG/JPEG 이미지 링크는 검증 후 Telegram 사진으로 첨부한다.
이미 끝난 일반 작업은 `/images <작업 ID>`로 사진만 다시 요청할 수 있다.
Codex 질문은 재실행하지 않는다. [사진 답변 전송](docs/OUTGOING_IMAGES.md).
`/save <메모>`, `/items`, `/item GB-000001`, `/tag GB-000001 태그`, `/search <검색어>`,
`/ask <질문>`, `/extract GB-000001`, `/processing`, `/forget GB-000001`을 추가했다.
사진 답변은 검색 자료로도 저장한다. `/extract`는 Codex로 이미지 글자·장면을 추출하고,
문서의 텍스트를 읽거나 영상의 최대 3개 프레임을 분석한다. 음성은 원본·캡션만 보관한다.
별도 로컬 OCR·음성인식 AI는 설치하지 않는다. 자세한 사용법은 [지식 기능 안내](docs/KNOWLEDGE.md).
`/memory 저장해줘: 내용`, `/memory GB-000001 읽어줘`,
`/memory GB-000001 수정해줘: 새 전체 내용`으로 Heimdall 지식을 직접 관리한다.
원본과 수정 이력은 보존하며 애매한 요청은 변경하지 않는다. 추가 모델 호출은 없다.
인자 없는 `/memory` (별칭 `/memories`)로 현재 스레드 대화·Codex 자체 메모리·Heimdall 지식 저장소를
구분하고 제한된 설정 상태를 조회한다. 이 조회 명령은 설정을 바꾸지 않는다.
일반 질문의 자동 검색은 기본 OFF이며 `/knowledge auto on`으로 켠 일반 읽기 전용 텍스트만 최대 3개 자료를
추가 참고 입력으로 받는다. `/knowledge auto off`는 대기 작업도 중지한다.
`/knowledge status`, `/knowledge test <질문>`은 모델 호출 없이 상태·검색 결과를 확인한다.
쓰기·network·임시 권한 및 사진 캡션은 자동 검색하지 않는다.
[메모리 구분](docs/MEMORY_FIX_REPORT.md), [Auto-Memory 개발·배포 범위](docs/AUTO_MEMORY_IMPLEMENTATION.md).
URL 원문은 질문으로 전달하지만 브리지가 URL을 가져오지는 않는다.

## 운영

실행 중인 system 서비스는 `heimdall-bridge.service`, 일일 DB 백업 타이머는
`heimdall-backup.timer`이다. 부팅 자동 시작을 설정했으며 실제 재부팅은 하지 않았다.
과거 임시 user 서비스를 동시에 시작하지 않는다.

```sh
systemctl status heimdall-bridge.service --no-pager
systemctl list-timers heimdall-backup.timer --no-pager
journalctl -u heimdall-bridge.service -n 30 --no-pager
```

Pi 커널에 memory cgroup controller가 없어 설정된 MemoryHigh/MemoryMax는
실제 강제되지 않는다. 메모리 여유를 모니터링해야 한다. 부팅 설정은 변경하지 않았다.
Phase 4의 `/backup`과 일일 타이머는 DB·지식 자료·첨부 원본 ZIP을 보관한다.
이전 DB-only 백업에는 첨부 원본이 없다. 어느 방식도 Codex 기록·인증은 포함하지 않는다.

운영 절차: [명령 안내](docs/OPERATIONS.md), [배포](docs/DEPLOYMENT.md),
[백업·복구](docs/BACKUP.md), [첨부 보안·제한](docs/ATTACHMENTS.md).

## 검증

```sh
cd /home/haaland/heimdall
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python -m heimdall_bridge --demo
.venv/bin/python -m scripts.smoke_sandbox
```

실제 모델을 호출하는 별도 스모크는 계정 사용량을 소비한다.
`python -m scripts.smoke_phase2`, `python -m scripts.smoke_phase3`는 새 테스트 세션을
사용한다. 후자에 `--telegram`을 붙이면 설정된 소유자에게 테스트 사진 한 장을 보낸다.
사용량 제한 중에는 반복 실행하지 않는다. 토큰은 명령줄·로그에 출력하지 않는다.

## 세션과 호환성

추적 cwd는 `/home/haaland` 정확 일치다. 새 CLI는 이 경로에서 시작한다.
기존 `personal-agent` 세션·`.codex/sessions` 기록을 이동하거나 직접 편집하지 않는다.
동일 thread를 CLI와 Telegram에서 동시에 사용하지 않는다. 다른 thread의 CLI는 유지할 수 있다.
다른 연결이 같은 세션을 점유하면 원래 연결만 닫거나 `/fork`로 새 분기를 만든다.

Codex CLI 0.162.1과 생성된 stable/experimental 스키마에 고정한다.
업그레이드 시 스키마·샌드박스·RPC 검증을 먼저 갱신해야 하며 불일치 시 실행을 거부한다.
기존 ChatGPT 로그인을 재사용하고 새 OpenAI API 키를 요구하지 않는다.
명세는 [SPEC.md](SPEC.md), 과거 기록은 [Phase 1 보고서](docs/PHASE1_REPORT.md)와
[결정 기록](docs/DECISIONS.md)에 보존한다.
