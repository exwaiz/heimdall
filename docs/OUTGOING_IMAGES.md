# Codex 답변 이미지를 Telegram 사진으로 보내기

일반 대화의 성공한 최종 답변에 `![설명](/home/haaland/image.png)` 형태의
로컬 이미지 링크가 있으면, 브리지가 실제 바이트를 multipart `sendPhoto`로 업로드한다.
경로의 공백은 `![설명](</home/haaland/image name.png>)` 형식도 지원한다.
단순 텍스트 경로, 일반 링크, 코드 예제, 외부 URL은 자동 전송하지 않는다.
자료 기반 `/ask`와 `/extract`의 검색·출처 텍스트에서 파일 전송을 유발하지 않는다.

이미 완료된 작업은 `/images 31`처럼 사진만 전송 등록할 수 있다.
소유자 자신의 일반 완료 작업만 가능하며 Codex를 재실행하지 않는다.
명령 update/message 중복은 기존 gateway가 차단한다. 새 명령으로 다시 요청하면
의도한 재전송으로 취급한다. 사진 전송 자체의 실패는 `/deliveries`, `/resend`로 관리한다.

## 경계와 제한

- 추적 작업 디렉토리 아래의 절대 경로 PNG/JPEG만 지원한다. 현재 루트는 `/home/haaland`.
- `.codex`, `.ssh` 등 모든 숨김 경로, 브리지 비공개 데이터, personal-agent는 차단한다.
- 모든 부모 디렉토리를 descriptor와 `O_NOFOLLOW`로 열고, 일반 파일·소유자·hardlink
  개수·크기·변경 여부를 확인한다. 심볼릭 링크/FIFO/상위 폴더 이동은 허용하지 않는다.
- 확장자만 믿지 않고 기존 bounded PNG/JPEG 내용 검증을 재사용한다.
  20M 픽셀, 파일 10,000,000바이트, 가로+세로 10,000, 비율 20 이내.
- 답변당 중복 경로 제외 최대 3장. 전체 보관 사진 바이트 한도 40,000,000.
  자동 삭제/retention은 없다. 한도를 초과하면 텍스트 답변은 유지하고 미첨부 안내한다.
- 파일 접근·이미지 검증은 worker thread에서 수행한다. 모델 도구 권한은 확대하지 않는다.
- 전송할 원본 바이트·MIME·SHA-256을 private SQLite outbox에 고정한다 (migration 6).
  전송 직전에 원래 경로를 다시 읽지 않는다. 현재 DB 백업은 이 바이트도 포함한다.
  `/forget`은 이 전송 이력/이미지나 Telegram 사진을 지우지 않는다.
- 송신 전 `sending`, 성공 시 `sent`, 명확한 거절은 `failed`로 기록한다.
  네트워크/응답 해석 불가/5xx는 `uncertain`이며 자동 재전송하지 않는다.
  재시작 중 `sending`도 기존 복구 정책에 따라 `uncertain`으로 남긴다.
  명시적 429만 bounded retry한다. 봇 토큰과 원본 경로를 전송 오류에 넣지 않는다.
- Telegram 사진은 서버에서 압축될 수 있다. 원본 파일 문서 첨부 기능은 아직 없다.

기준: [Telegram Bot API sendPhoto](https://core.telegram.org/bots/api#sendphoto).

## 검증

회귀 테스트 12개: PNG/JPEG, 변조/내용/크기/비율, private·symlink·hardlink·FIFO·traversal,
markdown 파싱/중복/개수, DB 바이트 고정, Gateway 중복/권한, 전송 불확실성·복구·hash,
multipart 고정 host, 429와 5xx/네트워크 오류 처리, 사진 바이트의 백업/복원 및 수동 재전송.
캡처의 실제 작업 #31은 완료 상태이며 원본 PNG 257,701바이트를 검증했다.
배포 전 `knowledge-backup-uxqxv8lj.zip` 검증 백업 후, 작업/추출/승인 및 uncertain
작업이 없는 상태에서 전용 bridge 서비스를 재시작했다. bridge_ready, active/running,
NRestarts=0 확인. migration 6, quick_check=ok.
요청된 작업 #31의 이미지를 중복 방지 키로 한 번만 등록했고 실제 Telegram `sendPhoto`
성공 응답(message_id=158), outbox delivery #110 `sent` 확인. 모델은 재실행하지 않았다.
사용자의 휴대폰 실제 표시 확인과 API 전송 성공 확인은 구분한다.
최종 전체 테스트 221개 통과 (전송 사진의 백업·복원과 수동 재전송 포함).
