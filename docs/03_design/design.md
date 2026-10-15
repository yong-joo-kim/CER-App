# 설계서 (P2) — CER App

입력: docs/02_requirements/requirements.md (FR-01~11, NFR-01~04), interface_summary.md

## 1. 기술 스택
| 항목 | 선택 | 근거 |
|---|---|---|
| 언어 | Python 3.14 | 개발 환경 설치됨, Spec 예제와 동일 |
| UI | 웹 화면(브라우저): Python 표준 `http.server` 기반 로컬 서버 + 바닐라 JS 프론트엔드 | 사용자 결정(웹), 추가 설치 최소화, FR-11 |
| WebSocket | `websocket-client` | Spec 4.4.2 예제와 동일 라이브러리 |
| WAV | 표준 `wave` | 샘플: 8kHz mono PCM16, 약 329s (2,636,000 frames) |
| 편집거리 | 자체 구현(Levenshtein, S/I/D 역추적) | 외부 의존 최소화, S/I/D 분리 산출 필요 |

## 2. 구조
```
app/
  main.py            # 진입점 (GUI 실행)
  config.py          # 설정 로드/저장 (FR-03)
  inputs.py          # WAV/TXT 쌍 탐색·검증 (FR-01, FR-02)
  stt_client.py      # /health, WebSocket ready→PCM→finalize (FR-04, FR-05)
  normalize.py       # 추임새 제외, 숫자 표기 통일, 빈 줄 처리
  metrics.py         # CER/WER, S/I/D/N (FR-06, FR-07)
  report.py          # CSV/JSON/요약 출력 (FR-08)
  runner.py          # 배치 실행·모델 비교, 진행 콜백 (FR-09, FR-10)
  web/server.py      # 로컬 웹 서버, JSON API, 진행 이벤트(SSE) (FR-11)
  web/static/        # index.html, app.js, style.css (탭 UI, 가상 스크롤 목록)
config.local.json    # API 키 등 (gitignore)
```
의존 방향: web/server → runner → (inputs, stt_client, normalize, metrics, report). GUI는 별도 스레드에서 runner를 실행하고 큐로 진행 상황을 전달(UI 멈춤 방지).

## 3. 처리 흐름 (파일 1건)
1. inputs: `<name>.wav`(입력 음성) + `<name>.txt`(정답 전사 원본) 쌍 확인(기본 이름이 같고 `_result.txt`로 끝나는 파일은 입력에서 제외), WAV 헤더 검증(8000Hz/1ch/16bit)
2. stt_client: `GET /health` → WS 연결(`?model=`) → `ready` JSON → 3200B 청크 전송(0.2s 페이싱) → `finalize`
3. 수신 메시지 누적: 최종 `transcript`, `segments[]` 저장. 연속 음성이므로 EPD 구간별 결과가 여러 개 올 수 있어 모든 최종 segment를 시간순 연결해 전체 hypothesis 구성
4. normalize(정답, 인식) → metrics → 결과 레코드
5. report: 파일별 레코드 + 화자(rx/tx)별·전체 집계

화자 구분: 기본 이름이 `_rx` / `_tx`로 끝나면 해당 화자로 표기(없으면 "미지정", 평가에는 영향 없음). 각 파일은 독립 평가(FR-06).

## 4. 정규화 규칙 (확정 방침의 상세)
적용 순서(정답·인식 동일):
1. 유니코드 NFC, 빈 줄 제거 후 전체를 공백으로 이어붙임
2. 구두점·특수문자 제거
3. 추임새 어절 제거: 기본 목록 `음, 어, 으음, 아, 네, 예, 응, 어어, 에`(설정 파일에서 편집 가능)
   - "네/예"는 응답어이지만 기본 목록에 포함하는 것으로 확정(사용자 결정). 설정에서 변경 가능
4. 숫자 표기 통일: 아라비아 숫자 → 한글 독음(공일이…) 변환 후 비교 (한글 숫자어는 그대로)
   - 한계: "삼하나오" 같은 혼용 독음은 완전 일치 불가 → P4 시험에서 샘플로 검증
5. CER: 공백 제거한 문자열 단위 / WER: 공백 기준 어절

## 5. 지표 (metrics)
- Levenshtein 정렬로 S, I, D 집계, N = 정답 길이
- CER/WER = (S+I+D)/N, N=0이면 정의 불가(오류 표시)
- 전체 평균: 파일별 평균이 아니라 S+I+D 합 / N 합 (가중)

## 6. 설정 (config.local.json)
```
{"server":{"scheme":"ws","host":"","port":8081},
 "api_key":"", "model":"model_v2",
 "epd":{"auto_epd":true,"speech_start_timeout":5000.0,"speech_end_silence_length":1000.0,"max_speech_length":600000.0},
 "result":{"partial_results":false,"word_alignment":false},
 "filler_words":[...], "retry":{"count":2,"timeout_s":30}}
```
- API 키: 파일에만 보관, 로그·리포트 출력 금지(마스킹), `.gitignore`에 추가 (NFR-02)
- 샘플 5.5분 파일을 실시간 페이싱하면 파일당 약 5.5분 소요 → "빠른 전송(페이싱 off)" 옵션 제공(서버가 허용하는 경우)

## 7. GUI 화면 (FR-11)
- 상단: 서버(host/port/scheme), 모델 선택(v1~v3 다중 선택=비교), API 키(마스킹), 입력 폴더
- 중앙: 파일 목록 테이블(파일, 화자, 상태, CER, WER, S/I/D/N)
- 하단: 진행 바, 로그, [실행] [중지] [리포트 저장]
- 요약 영역: 화자별·전체 평균 CER/WER, 모델 비교 시 모델별 열

## 8. 출력 (data/results/<실행시각>/)
- `detail.csv`(파일×모델), `summary.json`, `summary.txt`, 실행 설정 스냅샷(키 제외, NFR-01)
- 인식 결과 원문: `<name>_result.txt` (입력 `<name>.wav`, 정답 `<name>.txt`와 같은 기본 이름). 단일 모델 실행 시 `data/results/<실행시각>/<name>_result.txt`, 모델 비교 시 `data/results/<실행시각>/<model>/<name>_result.txt`

## 9. 오류 처리 (FR-05, NFR-04)
- 401/400/404/500·연결 끊김: 파일 상태를 실패로 기록하고 사유 표시, 재시도 후 다음 파일 진행
- TXT 인코딩 오류: UTF-8 실패 시 파일명과 함께 오류 표시 (NFR-03)

## 10. 요구사항 추적
FR-01,02→inputs / FR-03→config / FR-04,05→stt_client / FR-06,07→normalize+metrics / FR-08→report / FR-09,10→runner / FR-11→gui / NFR-01→report 스냅샷 / NFR-02→config·logging / NFR-03→inputs / NFR-04→stt_client·config

## 11. 확인 필요(구현 전)
- 연속 음성 응답 방식: 서버가 EPD 구간별로 여러 최종 메시지를 보내는지, finalize 후 1건만 보내는지는 실제 호출로 확인 (설계는 둘 다 처리)

## 12. 개정: 시험 모드·병렬·응답 속도·대량 결과 (FR-12~16)
| 항목 | 설계 |
|---|---|
| 시험 모드 | GUI 옵션 `문장 단위 시험`(체크). OFF=일반(장시간), ON=문장 단위. config `test_mode: "continuous"|"sentence"` |
| 응답 속도 | `stt_client`가 `finalize` 전송 시각(t_end)과 최종 결과 수신 시각(t_final)을 기록, `latency = t_final - t_end`. 문장 단위 모드에서만 기록. 음성 길이 ≥ 30s는 `latency_excluded=true`로 집계 제외(CER/WER은 평가) |
| 최종 결과 시각 | 서버가 연결을 닫기 전에 마지막 유효 메시지를 수신한 시각을 t_final로 사용(실서버로 검증, 구현 단계 확인 항목) |
| 집계 | 평균/중앙값/최대/p95, 제외 건수, 동시 접속 수 함께 기록 |
| 병렬 | `runner`: `ThreadPoolExecutor(max_workers=concurrency)`, 응답 속도 측정 ON이면 기본 1, OFF(CER만)면 기본 4. 파일 1건 = 연결 1개. 결과는 큐로 GUI에 전달 |
| 이어서 하기 | 출력 폴더의 `<name>_result.txt` + 상태 파일(`state.jsonl`, 건별 append)로 완료 건 판별 후 건너뜀 |
| 대량 조회 | 결과를 메모리에 필드만 보관, Treeview는 일괄 갱신(배치 100건/0.5s). 검색·필터·정렬은 메모리 인덱스에서 수행. CER 분포 히스토그램, 상위 N개 |
| 입력 탐색 | `os.scandir`로 입력 폴더 + 1단계 하위 폴더까지만 탐색(깊이 제한) |
| 페이싱 | 문장 단위 모드 기본 ON(속도 측정 유효성). OFF 시 속도 측정 비활성 경고 |
| 소요 시간 추정 | 페이싱 ON 기준 7초×10,000건 ÷ 동시 수: 동시 4 ≈ 4.9시간, 동시 1 ≈ 19.4시간. GUI에 예상 시간 표시 |
| 실시간 결과 | 완료 건 수신 시 `LiveStats`(누적 S/I/D/N, 최근 N건 윈도우, CER 추이 포인트)를 갱신하고 GUI는 0.5초 주기로 폴링해 그래프·요약 갱신. 100건마다 `live_snapshot.json` 저장 |

## 13. 개정: 웹 UI (사용자 결정)
- 실행: `python -m app.main` → `http://127.0.0.1:<port>` 자동 오픈. 기본 바인딩은 로컬(127.0.0.1), 외부 접속은 설정으로만 허용(API 키는 항상 마스킹 응답)
- API: `GET/PUT /api/config`, `POST /api/scan`, `POST /api/run`, `POST /api/stop`, `GET /api/status`(진행·실시간 통계, 화면이 1초마다 폴링), `GET /api/results?offset=&limit=&q=&status=&sort=`, `GET /api/detail?name=`, `GET /api/report`
- 탭: ① 설정·실행 ② 실시간 결과(누적/구간 CER 그래프, 응답속도) ③ 파일별 결과(검색·필터·정렬, 스크롤 방식 가상 목록: 화면 행만 DOM 렌더, 서버에서 구간 조회) ④ 상세(정답·인식 비교) ⑤ 리포트(시험 전체 결과 저장)
- 상태줄: 모든 탭 하단 고정(완료/실패/누적 CER/남은 시간)
- 응답 속도: 문장 단위 시험은 전체 파일(30초 이상 제외) 평균을 대표값으로 사용, 동시 접속 1
- 시안 파일: `docs/03_design/ui/mockup.html` (브라우저로 열기)
- 앞선 7장(GUI/tkinter) 서술은 본 13장으로 대체한다
- 입력 경로: 설정 `wav_dir`(음성), `txt_dir`(정답 전사, 비우면 `wav_dir`). 기존 `input_dir`는 `wav_dir`로 자동 인식
- 결과 폴더: `<결과 저장 폴더>/<YYYYMMDD_HHMM>/`(같은 분 충돌 시 `_HHMMSS`, 번호). 이어서 하기는 `find_resume_dir`로 같은 `wav_dir`의 최근 시험 폴더를 선택. 이전 시험은 `/api/runs`로 목록, `/api/view`로 읽기 전용 열기
