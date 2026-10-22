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


MAX_CONCURRENCY = 16


class Run:
    """시험 1회의 상태. 스레드 안전."""

    def __init__(self, cfg, items, out_dir, transcribe_fn=None):
        self.cfg = cfg
        self.items = items
        self.view_only = False       # 이전 시험을 읽기 전용으로 연 경우
        self.view_info = None
        self.files_count = None
        self.txt_search_dirs = []
        self._stem_cache = {}
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
        self.paused_total = 0.0    # 일시정지로 보낸 누적 시간(경과·남은 시간 계산에서 제외)
        self.paused_since = None
        self.by_speaker = {}
        self.eff = cfgmod.effective(cfg)
        self.models = list(cfg.get("models") or ["model_v2"])
        self.thread = None
        self.audio_total_s = 0.0
        self.audio_all_s = 0.0      # 시험 대상 전체 음성 길이(초)
        self.excluded_files = 0     # 검색 단계에서 제외된 파일 수(정답 없음 등)
        self.unique_audio_s = sum(i.duration_s for i in items)   # 파일 기준 음성 합계(모델 수와 무관)
        self.done_audio_s = 0.0     # 처리가 끝난 음성 길이(초)
        self.sum_proc = 0.0         # 완료 파일의 처리 시간 합(초)
        self.sum_proc_dur = 0.0     # 위 파일들의 음성 길이 합(초)
        self.limit = max(1, min(int(cfg.get("concurrency", 4)), MAX_CONCURRENCY))
        self.conc_seen = {self.limit}
        self.active = 0
        self.cv = threading.Condition()

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
        self.audio_all_s = sum(it.duration_s for it, _ in jobs)
        try:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as ex:
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

    def set_concurrency(self, n):
        """실행 중 동시 접속 수 변경. 새로 시작하는 파일부터 적용된다."""
        n = max(1, min(int(n), MAX_CONCURRENCY))
        with self.cv:
            self.limit = n
            self.cfg["concurrency"] = n
            self.conc_seen.add(n)
            self.cv.notify_all()
        return n

    def _acquire(self):
        with self.cv:
            while self.active >= self.limit and not self.stop_ev.is_set():
                self.cv.wait(0.2)
            if self.stop_ev.is_set():
                return False
            self.active += 1
            return True

    def _release(self):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def _one(self, it, model):
        while self.pause_ev.is_set() and not self.stop_ev.is_set():
            time.sleep(0.2)
        if self.stop_ev.is_set() or not self._acquire():
            return
        try:
            self._process(it, model)
        finally:
            self._release()

    def _process(self, it, model):
        rec = {"name": it.name, "model": model, "speaker": it.speaker,
               "duration_s": round(it.duration_s, 2), "status": "실패", "note": "",
               "cer": None, "wer": None, "cs": 0, "ci": 0, "cd": 0, "cn": 0,
               "ws": 0, "wi": 0, "wd": 0, "wn": 0, "latency": None, "lat_excluded": False}
        try:
            ref_raw = inputs.read_text(it.txt)
            fill = self.cfg.get("filler_words", [])
            ref_segs = normalize.normalize_ref2(ref_raw, fill)
            attempts = 1 + int(self.cfg["retry"].get("count", 2))
            res, last_err = None, ""
            t0 = time.perf_counter()
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
            rec["proc_s"] = round(time.perf_counter() - t0, 3)
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
            self.done_audio_s += float(rec.get("duration_s") or 0)
            if rec.get("status") == "완료" and rec.get("proc_s"):
                self.sum_proc += float(rec["proc_s"])
                self.sum_proc_dur += float(rec.get("duration_s") or 0)
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
    def _close_pause(self):
        if self.paused_since is not None:
            self.paused_total += time.time() - self.paused_since
            self.paused_since = None

    def stop(self):
        self.stop_ev.set()
        self.pause_ev.clear()
        with self.lock:
            self._close_pause()

    def pause(self, on=True):
        with self.lock:
            if on:
                self.pause_ev.set()
                if self.state == "running":
                    self.state = "paused"
                    self.paused_since = time.time()
            else:
                self.pause_ev.clear()
                if self.state == "paused":
                    self.state = "running"
                self._close_pause()

    def snapshot(self, trend_points=300):
        with self.lock:
            done = len(self.order)
            lat = sorted(self.latencies)
            now = self.paused_since or self.finished or time.time()   # 일시정지 중에는 시계를 멈춘다
            elapsed = max(0.0, now - self.started - self.paused_total) if self.started else 0
            remaining = None
            ok_done = done - self.failed
            if self.total and done and self.state in ("running", "paused"):
                rem_audio = max(0.0, self.audio_all_s - self.done_audio_s)
                if self.sum_proc_dur > 0:
                    rate = self.sum_proc / self.sum_proc_dur   # 음성 1초당 처리에 걸린 시간
                    remaining = rate * rem_audio / max(1, self.limit)
                else:
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
                "state": ("stopping" if self.stop_ev.is_set() and self.state in ("running", "paused") else self.state),
                "total": self.total, "done": done, "failed": self.failed,
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
                "files": {"count": self.files_count if self.files_count is not None else len(self.items),
                          "excluded": self.excluded_files,
                          "audio_s": self.unique_audio_s},
                "realtime_pacing": bool(self.cfg.get("realtime_pacing", True)),
                "view": self.view_info if self.view_only else None,
                "mode": "sentence" if self.cfg.get("sentence_mode") else "continuous",
                "concurrency": self.limit,
                "concurrency_seen": sorted(self.conc_seen),
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

    def resolve_txt(self, name):
        """이전 시험을 열 때 정답 원본(.txt) 경로를 찾는다. 없으면 None."""
        base = Path(name)
        cands = []
        for d in self.txt_search_dirs:
            if d:
                cands.append(Path(d) / (name + ".txt"))
        for c in cands:
            if c.exists():
                return c
        for d in self.txt_search_dirs:
            if not d or not Path(d).is_dir():
                continue
            idx = self._stem_cache.get(d)
            if idx is None:
                idx = self._stem_cache[d] = inputs._txts_by_stem(Path(d), 1)
            if base.name in idx:
                return idx[base.name]
        return None

    def detail(self, name, model):
        rec = self.records.get((name, model))
        if not rec:
            return None
        it = next((i for i in self.items if i.name == name), None)
        txt = it.txt if it is not None else self.resolve_txt(name)
        fill = self.cfg.get("filler_words", [])
        ref_n = hyp_n = ref_raw = ""
        ops = []
        ref_missing = txt is None or not Path(txt).exists()
        ref_segs = None
        if not ref_missing:
            try:
                raw_text = inputs.read_text(txt)
                ref_raw = " ".join(l.strip() for l in raw_text.splitlines() if l.strip())
                ref_segs = normalize.normalize_ref2(raw_text, fill)
                ref_n = normalize.ref_text(ref_segs)
            except Exception:
                ref_missing = True
        p = self.result_path(name, model)
        try:
            if p.exists():
                hyp_c, hyp_d = normalize.normalize2(p.read_text(encoding="utf-8"), fill)
                hyp_n = hyp_d
                if ref_segs is not None:
                    _, ops = metrics.score_lattice(normalize.lattice(ref_segs, "word"),
                                                   normalize.to_words_disp(hyp_c, hyp_d))
        except Exception:
            pass
        def disp(x):
            return getattr(x, "disp", x)

        return {"record": rec, "ref": ref_n, "ref_raw": ref_raw, "hyp": hyp_n, "ref_missing": ref_missing,
                "ops": [[o[0], disp(o[1]), disp(o[2]), getattr(o[1], "seg", None)] for o in ops],
                "alts": [[a[1] if isinstance(a, tuple) else a for a in alts] for alts in ref_segs] if ref_segs else [],
                "result_file": str(p)}

    @classmethod
    def from_folder(cls, folder, fallback_cfg):
        """이전 시험 결과 폴더를 읽기 전용으로 연다 (state.jsonl 기준)."""
        import copy
        folder = Path(folder)
        sp = folder / "state.jsonl"
        if not sp.exists():
            raise ValueError("시험 결과 폴더가 아닙니다 (state.jsonl이 없습니다)")
        summ = {}
        smp = folder / "summary.json"
        if smp.exists():
            try:
                summ = json.loads(smp.read_text(encoding="utf-8"))
            except ValueError:
                summ = {}
        cfg = copy.deepcopy(fallback_cfg)
        snap = summ.get("config") or {}
        for k in ("wav_dir", "txt_dir", "models", "sentence_mode", "measure_latency", "realtime_pacing",
                  "filler_words", "window_size", "latency_max_audio_s"):
            if k in snap:
                cfg[k] = snap[k]
        cfg["resume"] = False
        recs = {}
        for line in sp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "name" in r and "model" in r:
                recs[(r["name"], r["model"])] = r
        if not recs:
            raise ValueError("이 폴더에는 시험 결과가 없습니다")
        models = list(summ.get("models") or sorted({m for _, m in recs}))
        cfg["models"] = models
        cfg["concurrency"] = int(summ.get("concurrency") or fallback_cfg.get("concurrency", 4))
        run = cls(cfg, [], folder)
        run.view_only = True
        for rec in recs.values():
            run._accept(rec, persist=False)
        run.total = int(summ.get("total") or len(recs))
        st = summ.get("state")
        run.state = st if st in ("done", "stopped") else "stopped"
        run.started = 1.0
        run.finished = 1.0 + float(summ.get("elapsed_s") or 0)
        names = {n for n, _ in recs}
        durs = {}
        for (n, _), r in recs.items():
            durs.setdefault(n, float(r.get("duration_s") or 0))
        run.files_count = len(names)
        run.unique_audio_s = sum(durs.values())
        run.excluded_files = int((summ.get("files") or {}).get("excluded", 0))
        run.conc_seen = set(summ.get("concurrency_seen") or [run.limit])
        run.txt_search_dirs = [cfg.get("txt_dir") or cfg.get("wav_dir"), cfg.get("wav_dir"),
                               fallback_cfg.get("txt_dir"), fallback_cfg.get("wav_dir")]
        gen = summ.get("generated_at")
        if not gen:
            import time as _t
            gen = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(sp.stat().st_mtime))
        run.view_info = {"folder": str(folder), "generated_at": gen,
                         "has_summary": smp.exists(), "txt_dir": cfg.get("txt_dir") or cfg.get("wav_dir")}
        return run


def list_runs(base):
    """base 폴더(와 그 바로 아래 폴더) 중 시험 결과(state.jsonl)가 있는 폴더 목록. 최근 것이 먼저."""
    base = Path(base)
    out = []
    cands = []
    if base.is_dir():
        if (base / "state.jsonl").exists():
            cands.append(base)
        try:
            cands += [p for p in base.iterdir() if p.is_dir() and (p / "state.jsonl").exists()]
        except OSError:
            pass
    for p in cands:
        try:
            mt = (p / "state.jsonl").stat().st_mtime
        except OSError:
            continue
        info = {"name": p.name, "path": str(p), "mtime": mt,
                "generated_at": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S"),
                "state": None, "done": None, "total": None, "cer": None}
        smp = p / "summary.json"
        if smp.exists():
            try:
                s = json.loads(smp.read_text(encoding="utf-8"))
                info.update(generated_at=s.get("generated_at") or info["generated_at"], state=s.get("state"),
                            done=s.get("done"), total=s.get("total"), cer=s.get("cer_pct"))
            except ValueError:
                pass
        out.append(info)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _norm_dir(p):
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


def find_resume_dir(base, cfg):
    """이어서 하기 대상: 결과 저장 폴더 안에서 같은 음성 폴더로 실행한 가장 최근 시험 폴더."""
    base = Path(base)
    want = _norm_dir(cfg.get("wav_dir"))
    for r in list_runs(base):
        p = Path(r["path"])
        if p == base:
            continue
        smp = p / "summary.json"
        if smp.exists():
            try:
                got = _norm_dir((json.loads(smp.read_text(encoding="utf-8")).get("config") or {}).get("wav_dir"))
            except ValueError:
                got = ""
            if got and got != want:
                continue
        return p
    return None


def new_out_dir(cfg, now=None):
    """시험 결과 폴더: 결과 저장 폴더 아래에 년월일시분(YYYYMMDD_HHMM) 하위 폴더를 만든다.
    이어서 하기를 켜면 같은 입력의 가장 최근 시험 폴더를 이어서 사용한다."""
    base = Path(cfg["output_dir"])
    if cfg.get("resume"):
        d = find_resume_dir(base, cfg)
        if d is not None:
            return d
    now = now or datetime.now()
    p = base / now.strftime("%Y%m%d_%H%M")
    if p.exists():                                  # 같은 분에 다시 시작한 경우: 초, 번호를 덧붙여 구분
        p = base / now.strftime("%Y%m%d_%H%M%S")
        n = 2
        while p.exists():
            p = base / (now.strftime("%Y%m%d_%H%M%S") + f"_{n}")
            n += 1
    return p
