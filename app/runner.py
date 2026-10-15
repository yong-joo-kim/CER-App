"""배치 실행·병렬·실시간 통계·이어서 하기 (FR-05, FR-09~FR-17)."""
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import inputs, metrics, normalize, stt_client


def safe_name(name):
    return name.replace("/", "__")


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class Run:
    """시험 1회의 상태. 스레드 안전."""

    def __init__(self, cfg, items, out_dir, transcribe_fn=None):
        self.cfg = cfg
        self.items = items
        self.out_dir = Path(out_dir)
        self.transcribe = transcribe_fn or stt_client.transcribe
        self.stop_ev = threading.Event()
        self.pause_ev = threading.Event()
        self.lock = threading.Lock()
        self.records = {}          # (name, model) -> record
        self.order = []            # 완료 순서 key
        self.cer_tot = metrics.Score()
        self.wer_tot = metrics.Score()
        self.window = deque(maxlen=int(cfg.get("window_size", 500)))
        self.trend = []            # (done, cum_cer_pct, window_cer_pct)
        self.latencies = []
        self.lat_excluded = 0
        self.failed = 0
        self.total = 0
        self.state = "idle"        # idle/running/paused/done/stopped
        self.started = None
        self.finished = None
        self.by_speaker = {}
        self.eff = cfgmod.effective(cfg)
        self.models = list(cfg.get("models") or ["model_v2"])
        self.thread = None
        self.audio_total_s = 0.0

    # ---- 저장 경로 ----
    def result_path(self, name, model):
        base = self.out_dir if len(self.models) == 1 else self.out_dir / model
        return base / f"{safe_name(name)}_result.txt"

    # ---- 실행 ----
    def start(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state = "running"
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _load_resume(self):
        sp = self.out_dir / "state.jsonl"
        done = {}
        if self.cfg.get("resume") and sp.exists():
            for line in sp.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("status") == "완료":
                    done[(r["name"], r["model"])] = r
        return done

    def _run(self):
        done = self._load_resume()
        jobs = []
        for it in self.items:
            for m in self.models:
                jobs.append((it, m))
        self.total = len(jobs)
        for (it, m) in list(jobs):
            r = done.get((it.name, m))
            if r:
                self._accept(r, persist=False)
        pending = [(it, m) for it, m in jobs if (it.name, m) not in self.records]
        self.audio_total_s = sum(it.duration_s for it, _ in pending)
        n = max(1, min(int(self.cfg.get("concurrency", 4)), 16))
        try:
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(self._one, it, m) for it, m in pending]
                for f in futs:
                    f.result()
        finally:
            self.finished = time.time()
            if self.state != "stopped":
                self.state = "stopped" if self.stop_ev.is_set() else "done"
            try:
                from . import report
                report.write_all(self)
            except Exception:
                pass

    def _one(self, it, model):
        while self.pause_ev.is_set() and not self.stop_ev.is_set():
            time.sleep(0.2)
        if self.stop_ev.is_set():
            return
        rec = {"name": it.name, "model": model, "speaker": it.speaker,
               "duration_s": round(it.duration_s, 2), "status": "실패", "note": "",
               "cer": None, "wer": None, "cs": 0, "ci": 0, "cd": 0, "cn": 0,
               "ws": 0, "wi": 0, "wd": 0, "wn": 0, "latency": None, "lat_excluded": False}
        try:
            ref_raw = inputs.read_text(it.txt)
            fill = self.cfg.get("filler_words", [])
            ref_segs = normalize.normalize_ref(ref_raw, fill)
            attempts = 1 + int(self.cfg["retry"].get("count", 2))
            res, last_err = None, ""
            for a in range(attempts):
                try:
                    res = self.transcribe(it.wav, self.cfg, model, "tr_" + safe_name(it.name)[:40], stop=self.stop_ev)
                    break
                except stt_client.SttError as e:
                    last_err = str(e)
                    if not e.retry or self.stop_ev.is_set():
                        break
            if res is None:
                if self.stop_ev.is_set():
                    return  # 사용자 중지로 끊긴 건은 실패로 집계하지 않는다(미완료로 남김)
                rec["note"] = last_err or "실패"
                return self._accept(rec)
            hyp_n = normalize.normalize(res["transcript"], fill)
            p = self.result_path(it.name, model)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(res["transcript"], encoding="utf-8")
            (p.parent / (p.name[:-len("_result.txt")] + "_messages.jsonl")).write_text(
                "\n".join(json.dumps(m, ensure_ascii=False) for m in res.get("messages", [])),
                encoding="utf-8")
            cs, _ = metrics.score_lattice(normalize.lattice(ref_segs, "char"), normalize.to_chars(hyp_n))
            ws, _ = metrics.score_lattice(normalize.lattice(ref_segs, "word"), normalize.to_words(hyp_n))
            rec.update(status="완료", cs=cs.s, ci=cs.i, cd=cs.d, cn=cs.n,
                       ws=ws.s, wi=ws.i, wd=ws.d, wn=ws.n,
                       cer=None if cs.rate is None else round(cs.rate * 100, 2),
                       wer=None if ws.rate is None else round(ws.rate * 100, 2))
            if self.eff["latency_on"]:
                if it.duration_s >= float(self.cfg.get("latency_max_audio_s", 30.0)):
                    rec["lat_excluded"] = True
                    rec["note"] = "속도 제외(30초 이상)"
                elif res.get("latency") is None:
                    rec["note"] = "속도 불명"
                else:
                    rec["latency"] = round(res["latency"], 3)
        except inputs.InputError as e:
            rec["note"] = str(e)
        except Exception as e:  # 한 파일의 오류가 배치를 멈추지 않게 한다
            rec["note"] = f"{type(e).__name__}: {e}"
        return self._accept(rec)

    def _accept(self, rec, persist=True):
        key = (rec["name"], rec["model"])
        with self.lock:
            self.records[key] = rec
            self.order.append(key)
            if rec["status"] == "완료":
                c = metrics.Score(rec["cs"], rec["ci"], rec["cd"], rec["cn"])
                w = metrics.Score(rec["ws"], rec["wi"], rec["wd"], rec["wn"])
                self.cer_tot += c
                self.wer_tot += w
                self.window.append((c.errors, c.n))
                sp = self.by_speaker.setdefault(rec["speaker"], [metrics.Score(), metrics.Score()])
                sp[0] += c
                sp[1] += w
                if rec.get("latency") is not None:
                    self.latencies.append(rec["latency"])
                if rec.get("lat_excluded"):
                    self.lat_excluded += 1
                we = sum(e for e, _ in self.window)
                wn = sum(n for _, n in self.window)
                self.trend.append((len(self.order),
                                   None if self.cer_tot.n == 0 else round(self.cer_tot.errors / self.cer_tot.n * 100, 3),
                                   None if wn == 0 else round(we / wn * 100, 3)))
            else:
                self.failed += 1
            if persist:
                with open(self.out_dir / "state.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if persist and len(self.order) % 100 == 0:
            try:
                (self.out_dir / "live_snapshot.json").write_text(
                    json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # ---- 조회 ----
    def stop(self):
        self.stop_ev.set()
        self.pause_ev.clear()

    def pause(self, on=True):
        if on:
            self.pause_ev.set()
            if self.state == "running":
                self.state = "paused"
        else:
            self.pause_ev.clear()
            if self.state == "paused":
                self.state = "running"

    def snapshot(self, trend_points=300):
        with self.lock:
            done = len(self.order)
            lat = sorted(self.latencies)
            elapsed = ((self.finished or time.time()) - self.started) if self.started else 0
            remaining = None
            ok_done = done - self.failed
            if self.total and done and self.state in ("running", "paused"):
                remaining = elapsed / done * (self.total - done)
            tr = self.trend
            if len(tr) > trend_points:
                step = len(tr) / trend_points
                tr = [tr[int(i * step)] for i in range(trend_points)] + [tr[-1]]
            win_e = sum(e for e, _ in self.window)
            win_n = sum(n for _, n in self.window)
            dist = {"0%": 0, "~5%": 0, "~10%": 0, "10%+": 0}
            for r in self.records.values():
                if r["status"] != "완료" or r["cer"] is None:
                    continue
                c = r["cer"]
                dist["0%" if c == 0 else "~5%" if c <= 5 else "~10%" if c <= 10 else "10%+"] += 1
            return {
                "state": self.state, "total": self.total, "done": done, "failed": self.failed,
                "ok": ok_done,
                "cer": None if self.cer_tot.n == 0 else self.cer_tot.errors / self.cer_tot.n * 100,
                "wer": None if self.wer_tot.n == 0 else self.wer_tot.errors / self.wer_tot.n * 100,
                "cer_counts": [self.cer_tot.s, self.cer_tot.i, self.cer_tot.d, self.cer_tot.n],
                "window_cer": None if win_n == 0 else win_e / win_n * 100,
                "window_size": self.window.maxlen,
                "latency": {
                    "on": self.eff["latency_on"], "count": len(lat), "excluded": self.lat_excluded,
                    "mean": sum(lat) / len(lat) if lat else None,
                    "median": percentile(lat, .5), "p95": percentile(lat, .95),
                    "max": lat[-1] if lat else None,
                },
                "speakers": {k: {"cer": v[0].rate, "wer": v[1].rate, "n": v[0].n}
                             for k, v in self.by_speaker.items()},
                "dist": dist, "trend": tr,
                "elapsed_s": elapsed, "remaining_s": remaining,
                "concurrency": int(self.cfg.get("concurrency", 4)),
                "models": self.models, "out_dir": str(self.out_dir),
            }

    def query(self, offset=0, limit=100, q="", status="", sort="", asc=True, min_cer=None):
        with self.lock:
            rows = list(self.records.values())
        q = (q or "").strip().lower()
        if q:
            rows = [r for r in rows if q in r["name"].lower()]
        if status:
            if status == "제외":
                rows = [r for r in rows if r.get("lat_excluded")]
            else:
                rows = [r for r in rows if r["status"] == status]
        if min_cer is not None:
            rows = [r for r in rows if r["cer"] is not None and r["cer"] >= min_cer]
        if sort in ("name", "cer", "wer", "latency", "status", "model", "duration_s"):
            nn = [r for r in rows if r.get(sort) is not None]
            nl = [r for r in rows if r.get(sort) is None]
            nn.sort(key=lambda r: r[sort], reverse=not asc)
            rows = nn + nl
        else:
            rows.sort(key=lambda r: r["name"])
        return len(rows), rows[offset:offset + limit]

    def detail(self, name, model):
        rec = self.records.get((name, model))
        if not rec:
            return None
        it = next((i for i in self.items if i.name == name), None)
        fill = self.cfg.get("filler_words", [])
        ref_n = hyp_n = ""
        ops = []
        if it is not None:
            try:
                ref_segs = normalize.normalize_ref(inputs.read_text(it.txt), fill)
                ref_n = normalize.ref_text(ref_segs)
                p = self.result_path(name, model)
                if p.exists():
                    hyp_n = normalize.normalize(p.read_text(encoding="utf-8"), fill)
                    _, ops = metrics.score_lattice(normalize.lattice(ref_segs, "word"), normalize.to_words(hyp_n))
            except Exception:
                pass
        return {"record": rec, "ref": ref_n, "hyp": hyp_n,
                "ops": [list(o) for o in ops],
                "result_file": str(self.result_path(name, model))}


def new_out_dir(cfg):
    base = Path(cfg["output_dir"])
    if cfg.get("resume"):
        return base / "last_run"
    return base / datetime.now().strftime("%Y%m%d_%H%M%S")
