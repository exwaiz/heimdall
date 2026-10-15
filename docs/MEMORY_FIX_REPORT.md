# 메모리 구분·안전한 상태 조회 개선 (2026-10-15)

사용자가 Telegram에서 받은 “설정 파일이 차단되어 활성화 여부를 알 수 없음 / 차단 이유는 모름”
답변을 개선했다. 보호 정책은 완화하지 않고, 브리지가 알고 있는 이유와 제한된 설정 상태를 제공한다.

## 변경

- `/memory`, `/memories`: 소유자 전용, 조회 전용. 선택 세션이 없어도 사용 가능.
- 현재 스레드 대화, Codex 자체 로컬 메모리, Heimdall Knowledge를 명확히 분리.
  ChatGPT 웹/계정 메모리도 동기화된다고 주장하지 않음.
- `config/read`에서 메모리 관련 네 불리언만 전달. 누락·오류는 미확인.
  설정 값과 스레드별 선택·실제 주입·생성 완료는 별개라고 표시.
- 일반 대화·사진·자료 질문의 모델 turn에 별도 텍스트 문맥을 추가. 사용자 원문,
  기존 developer instructions, 자료 답변 JSON 형식, 작업 권한은 유지.
- 설정/인증/세션/브리지 DB 접근 차단 이유가 Heimdall 보호 정책임을 설명.
- 일반 질문의 Knowledge 자동 검색 부재와 `/forget`의 삭제 범위를 설명.
- `/status` RAM cgroup 문구를 AI 메모리와 혼동하지 않게 변경.

## 검증

- 전체 단위 테스트 196개 통과. 새 테스트 8개는 allowlist/잘못된 타입/조회 실패,
  권한·변경 거절·자료 개수·원문 보존·별도 문맥·원본 파일 차단 유지 검증.
- 설치 Codex 0.162.1 실제 config/read 성공:
  `features.memories=false`, 나머지 세 키는 응답상 미확인. 전역 설정 수정 없음.
- 별도 검증 스레드의 실제 Codex 새 turn과 재개 turn에서 JSON 응답 검증:
  세 메모리 구분, Knowledge 자동 검색하지 않음, 보호 주체 식별,
  설정 값과 실제 사용 증거 구분, Telegram `/memories` 조회 전용 확인.
- pip check 및 compileall 통과.

단위·실제 모델 검증은 통제된 입력의 결과이며 모든 미래 자연어 답변을 보증하지 않는다.
실제 모바일 Telegram의 `/memory` 표시 확인은 사용자 확인이 필요하다.
운영 반영 전 대기/실행 작업·준비 중 추출·승인 대기가 없음을 확인했다.
`heimdall-bridge.service` 재시작 후 `bridge_ready`, active/running, NRestarts=0 확인.
운영 DB quick_check=ok, 검색 가능한 기존 지식 자료 1개 유지.

설명: [KNOWLEDGE.md](KNOWLEDGE.md), 근거:
[OpenAI Memories](https://learn.chatgpt.com/docs/customization/memories).
