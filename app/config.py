"""설정 로드/저장 (FR-03). API 키는 config.local.json에만 보관한다."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.local.json"

DEFAULT = {
    "server": {"scheme": "ws", "host": "", "port": 8081},
    "api_key": "",
    "auth_header": "X-API-Key",     # 정상 동작 확인된 클라이언트 기준 (Authorization이면 Bearer 방식)
    "chunk_bytes": 640,            # 정상 클라이언트와 동일(0.04s). 페이싱 sleep = len/16000
    "call_info": {"call_id": "test", "customer_phone": "010-2345-6789",
                  "in_out_bound": "IB", "consultant_id": "test01"},
    "models": ["model_v2"],
    "wav_dir": str(ROOT / "data" / "input"),   # 입력 음성(.wav) 폴더
    "txt_dir": "",                             # 정답 전사(.txt) 폴더. 비우면 wav_dir와 같은 위치
    "output_dir": str(ROOT / "data" / "results"),
    "sentence_mode": False,        # 문장 단위 시험 (FR-12)
    "measure_latency": True,       # 응답 속도 측정 (문장 단위 모드에서만 동작, FR-13)
    "latency_max_audio_s": 30.0,   # 이 길이 이상 파일은 속도 집계 제외
    "concurrency": 4,              # 속도 측정 ON이면 GUI가 1로 설정 (FR-14)
    "realtime_pacing": True,
    "resume": False,               # 이어서 하기 (FR-15)
    "window_size": 500,            # 실시간 구간 CER 윈도우 (FR-17)
    "epd": {
        "auto_epd": True,
        "speech_start_timeout": 5000.0,
        "speech_end_silence_length": 1000.0,
        "max_speech_length": 600000.0,
    },
    "result": {"partial_results": False, "word_alignment": False},
    "filler_words": ["음", "어", "으음", "아", "네", "예", "응", "어어", "에"],
    "retry": {"count": 2, "timeout_s": 30, "settle_s": 1.5},
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=CONFIG_PATH):
    cfg = copy.deepcopy(DEFAULT)
    p = Path(path)
    if p.exists():
        saved = json.loads(p.read_text(encoding="utf-8"))
        if "input_dir" in saved:  # 이전 버전 설정 호환
            saved.setdefault("wav_dir", saved["input_dir"])
            saved.pop("input_dir")
        _merge(cfg, saved)
    return cfg


def save(cfg, path=CONFIG_PATH):
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def mask(key):
    """화면/로그/리포트용 마스킹 (NFR-02)."""
    return "" if not key else key[:3] + "*" * 8


def public_snapshot(cfg):
    """결과에 남길 설정 스냅샷: API 키 제외 (NFR-01, NFR-02)."""
    s = copy.deepcopy(cfg)
    s["api_key"] = mask(cfg.get("api_key", ""))
    return s


def effective(cfg):
    """실행 시 실제 적용되는 값(속도 측정 가능 여부 등)을 계산."""
    latency_on = bool(cfg["sentence_mode"] and cfg["measure_latency"] and cfg["realtime_pacing"])
    return {"latency_on": latency_on}
