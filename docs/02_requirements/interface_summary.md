# ZIEUM 스트리밍 STT 인터페이스 요약

출처: docs/spec/zieum_기술문서.pdf (4장). API 키는 Spec에 평문으로 있으나 본 문서에 복사하지 않으며, 환경변수/설정 파일로 분리한다.

## 접속
| 구분 | URL |
|---|---|
| HTTPS | https://deepmodal.ai:8443 |
| WSS | wss://deepmodal.ai:8443 |
| HTTP | http://deepmodal.ai:8081 |
| WS | ws://deepmodal.ai:8081 |

온프레미스 시험 서버: ip/port(8081)/model(model_v2)는 Spec 10p 참조.

- 인증: `Authorization: Bearer <api-key>` (`/health` 제외)
- Health: `GET /health` → 200 "ok"
- 스트리밍: `WS /v1/streaming?model=model_v1|model_v2|model_v3` (기본 model_v2)

## STT 수행 절차
1. WebSocket 연결
2. 제어 메시지(`cmd: ready`) 전송
3. 오디오 바이너리 전송 (16bit PCM, 8kHz, 3200B = 0.2s 청크, 실시간 페이싱)
4. 종료 메시지(`cmd: finalize`) 전송

## 제어 메시지 필드
cmd, tr_id(필수), audio_encoding.{sample_rate_hz=8000, format=pcm16},
epd_config.{auto_epd, speech_start_timeout=5000, speech_end_silence_length=1000, max_speech_length=600000},
result_config.{partial_results, word_alignment},
call_info.{call_id, customer_phone, in_out_bound, consultant_id}

## 응답
id, transcript_id, session_id, file_path, transcript, metadata, segments[]{start,end,transcript,score}, created_at(ISO8601)

## 오류
401 인증 실패 / 400 잘못된 요청 / 404 없음 / 500 서버 오류

## 결과 조회
`GET /v1/recordings?transcript_id=` 또는 `?session_id=` (`audio=true` 시 wav)

## 지표 (Spec 2장)
CER = (S+I+D)/N, xRT = Processing Time / Audio Length
