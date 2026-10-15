# Phase 3 결과 · 2026-10-15

첨부 수신·보관과 사진의 Codex localImage 전달을 구현하고 실제 연동을 검증했다.
Phase 4의 OCR·전사·검색·Knowledge DB 확장은 포함하지 않는다.

## 구현·보안

- 사진·문서·음성·영상 수신, private 0700 디렉터리와 0600 원본 저장.
- Telegram getFile의 고정 HTTPS origin만 다운로드, redirect·임의 URL·경로 탈출 거부.
- 스트리밍 실측 용량 제한, 메시지/getFile 크기 교차 확인, SHA-256 저장.
- 파일당 20,000,000 bytes, 총 예약 포함 200,000,000 bytes, 기록 최대 1,000개,
  그룹당 10개, 이미지 20,000,000 pixels, 수신 timeout 60초.
- 원래 이름은 메타데이터만 보존, 생성 경로·symlink·owner·mode·inode·hardlink 검사.
- PNG 구조/CRC/경계 있는 raster 검증, JPEG 구조·크기 검증. JPEG는 완전한 픽셀 디코더가 아니다.
- 저장 사진만 localImage로 전달. 등록 시 thread·권한·asset ID와 원문 캡션 고정.
  dispatch 직전에 소유권·해시·크기·형식을 다시 확인한다.
- 중복 update/message로 파일 다운로드·작업 생성·기존 권한 변경을 반복하지 않는다.
- 파일 hashing/parsing은 별도 스레드로 옮겨 승인 callback 처리를 막지 않는다.
- `/assets`, `/asset`으로 저장 상태와 해시 조회.

문서·음성·영상은 저장만 한다. 압축 해제·실행·자동 내용 추출은 하지 않는다.
사진을 문서로 보내면 문서로만 보관한다. URL은 원문 질문으로 전달하고 브리지가 fetch하지 않는다.
원본 자료 저장은 장기 검색·추출·완전한 백업을 의미하지 않는다.

## 검증

Phase 2/3 전체 테스트 164개 통과. 중복·선택 변경·권한 고정·변조 차단·비소유자 차단,
다운로드 경로/redirect/실측 상한/기존 파일 보존, 이미지 위장·압축 경계 등을 포함한다.

`python -m scripts.smoke_phase3`:

- 임시 fixture 수신·해시 저장 성공.
- 실제 Codex App Server localImage에 빨간 정사각형 전달, RED 최종 답변 확인.

`python -m scripts.smoke_phase3 --telegram`:

- 설정된 소유자 chat에 명시적인 테스트 사진 한 장 업로드.
- 실제 Telegram file_id → getFile → 고정 origin 다운로드 → 검증·SHA-256 보관 성공.
- 다운로드된 이미지를 실제 Codex localImage로 전달, RED 최종 답변 확인.

후자는 실제 Telegram 파일 API를 사용하지만 수신 이벤트는 격리된 테스트 DB에 구성한
fixture다. 사용자 휴대폰에서 업로드한 update를 live poller가 처리하는 E2E와 구분한다.
테스트 원본·복원 fixture는 임시 디렉터리 종료 시 정리했고, 테스트 세션 기록은 보존했다.
토큰·인증 원문을 출력하지 않았다.

## 다음 확인·제한

사용자가 실제 봇에 사진+캡션을 보내 모바일 수신 E2E를 확인하면 된다.
사용량 제한 해결 후 Phase 2의 실제 모델 검증은 재실행해 통과했다(Phase 2 보고서 참조).
Codex 사용량 제한 중에도 파일 보관 및 운영 조회는 가능하나 모델 답변은 실패 안내한다.
DB 백업에는 첨부 원본이 포함되지 않는다. 원본까지 복구하려면 별도 일관성 있는 자료 백업이 필요하다.
강제 종료 시 다운로드 중 기록·부분 파일은 자동 재시도/삭제하지 않으며 예약 용량을 유지한다.
보존/정리는 명시적인 운영 작업으로 남겨뒀다. 상세 계약은 [ATTACHMENTS.md](ATTACHMENTS.md).
