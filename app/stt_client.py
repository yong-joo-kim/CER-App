"""ZIEUM 스트리밍 STT 클라이언트 (FR-04, FR-05, FR-13)."""
import json
import threading
import time
import urllib.request
import wave

import websocket

BYTES_PER_SEC = 16000  # 8kHz * 16bit * mono


class SttError(Exception):
    def __init__(self, msg, retry=True):
        super().__init__(msg)
        self.retry = retry


def _http_base(server):
    return f"{'https' if server['scheme'] == 'wss' else 'http'}://{server['host']}:{server['port']}"


def health(server, timeout=5):
    """GET /health → (ok, 메시지)."""
    try:
        with urllib.request.urlopen(_http_base(server) + "/health", timeout=timeout) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def build_ready(tr_id, cfg):
    return {
        "cmd": "ready",
        "tr_id": tr_id,
        "audio_encoding": {"sample_rate_hz": 8000, "format": "pcm16"},
        "epd_config": cfg["epd"],
        "result_config": cfg["result"],
        "call_info": cfg.get("call_info", {}),
    }


def merge_messages(msgs):
    """수신 JSON 메시지들에서 최종 인식 텍스트를 구성한다.
    - partial/is_final=false 표시가 있으면 제외
    - segments가 있으면 (start,end)로 중복 제거 후 시간순 연결
    - 없으면 transcript들을 연결하되, 누적형(앞 결과를 포함)이면 마지막 것만 사용"""
    finals = [m for m in msgs
              if isinstance(m, dict) and not m.get("partial") and m.get("is_final", True) is not False]
    segs = {}
    for m in finals:
        for sg in m.get("segments") or []:
            if isinstance(sg, dict) and sg.get("transcript"):
                segs[(sg.get("start"), sg.get("end"))] = sg
    if segs:
        ordered = sorted(segs.values(), key=lambda s: (s.get("start") or 0, s.get("end") or 0))
        return " ".join(s["transcript"].strip() for s in ordered)
    texts = [m.get("transcript", "") for m in finals if m.get("transcript")]
    if not texts:
        return ""
    out = [texts[0]]
    for t in texts[1:]:
        if t.startswith(out[-1]):
            out[-1] = t
        else:
            out.append(t)
    return " ".join(x.strip() for x in out)


def _scrub(text, key):
    return text.replace(key, "***") if key else text


def transcribe(wav_path, cfg, model, tr_id, on_progress=None, stop=None):
    """WAV 1건을 인식. dict(transcript, messages, t_end, last_msg, latency) 반환.

    latency = (finalize 이후 수신한 마지막 메시지 시각) - (finalize 전송 완료 시각).
    finalize 이후 수신 메시지가 없으면 None.
    종료 판정: 서버가 {"cmd":"finished"}를 보내면 즉시 종료(정상 경로).
    그 외에는 연결 종료, finalize 이후 settle_s 동안 무수신, timeout_s 무응답.
    인증 헤더는 cfg["auth_header"] (기본 X-API-Key, 정상 동작 확인된 클라이언트 기준).
    """
    server = cfg["server"]
    key = cfg.get("api_key", "")
    url = f"{server['scheme']}://{server['host']}:{server['port']}/v1/streaming?model={model}"
    timeout = cfg["retry"]["timeout_s"]
    settle = cfg["retry"].get("settle_s", 1.5)
    try:
        hname = cfg.get("auth_header", "X-API-Key")
        hval = f"Bearer {key}" if hname.lower() == "authorization" else key
        ws = websocket.create_connection(url, header=[f"{hname}: {hval}"], timeout=timeout)
    except websocket.WebSocketBadStatusException as e:
        code = getattr(e, "status_code", None)
        raise SttError(f"서버 거부({code})", retry=code not in (400, 401, 404))
    except Exception as e:
        raise SttError(_scrub(f"연결 실패: {type(e).__name__}: {e}", key))

    msgs = []  # (수신시각, 객체)
    st = {"err": None, "done": False, "finished_at": None}

    def reader():
        try:
            while True:
                data = ws.recv()
                if data == "" or data is None:
                    break
                try:
                    now = time.perf_counter()
                    obj = json.loads(data)
                    msgs.append((now, obj))
                    if isinstance(obj, dict) and obj.get("cmd") == "finished":
                        st["finished_at"] = now  # 서버의 종료 통지: 최종 결과 출력 완료
                        break
                except ValueError:
                    pass
        except Exception as e:
            if not st["done"]:
                st["err"] = e
        finally:
            st["done"] = True

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t_end = None
    try:
        ws.send(json.dumps(build_ready(tr_id, cfg)))
        with wave.open(str(wav_path), "rb") as w:
            total = max(1, w.getnframes() * 2)
            sent = 0
            chunk = int(cfg.get("chunk_bytes", 640))
            while True:
                if stop is not None and stop.is_set():
                    raise SttError("사용자 중지", retry=False)
                data = w.readframes(chunk // 2)
                if not data:
                    break
                ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                sent += len(data)
                if on_progress:
                    on_progress(sent / total)
                if cfg.get("realtime_pacing", True):
                    time.sleep(len(data) / BYTES_PER_SEC)
        ws.send(json.dumps({"cmd": "finalize"}))
        t_end = time.perf_counter()
        while not st["done"]:
            if stop is not None and stop.is_set():
                raise SttError("사용자 중지", retry=False)
            now = time.perf_counter()
            if st["finished_at"] is not None:
                break
            after = [t for t, _ in msgs if t > t_end]
            if after and now - after[-1] > settle:
                break
            if now - (after[-1] if after else t_end) > timeout:
                break
            time.sleep(0.05)
    except SttError:
        raise
    except Exception as e:
        raise SttError(_scrub(f"전송 실패: {e}", key))
    finally:
        st["done"] = True
        try:
            ws.close(timeout=1)
        except Exception:
            pass
    objs = [m for _, m in msgs]
    for m in objs:
        if isinstance(m, dict) and m.get("status") in (400, 401, 404, 500):
            raise SttError(f"서버 오류 {m.get('status')}", retry=m.get("status") == 500)
    if not objs:
        raise SttError("응답 없음" + (f" ({_scrub(str(st['err']), key)})" if st["err"] else ""))
    if st["finished_at"] is not None and t_end is not None:
        latency = max(0.0, st["finished_at"] - t_end)
    else:
        after = [t for t, _ in msgs if t_end is not None and t > t_end]
        latency = (after[-1] - t_end) if after else None
    return {"transcript": merge_messages(objs), "messages": objs, "latency": latency}
