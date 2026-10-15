"""로컬 웹 서버: 정적 화면 + JSON API (FR-11). 기본은 127.0.0.1 바인딩."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config as cfgmod
from .. import inputs, report, runner, stt_client

STATIC = Path(__file__).resolve().parent / "static"


class App:
    def __init__(self, config_path=cfgmod.CONFIG_PATH):
        self.config_path = config_path
        self.cfg = cfgmod.load(config_path)
        self.run = None
        self.scan_cache = None
        self.lock = threading.Lock()

    def public_cfg(self):
        c = dict(self.cfg)
        c["api_key_masked"] = cfgmod.mask(self.cfg.get("api_key", ""))
        c["api_key_set"] = bool(self.cfg.get("api_key"))
        c.pop("api_key", None)
        return c

    def update_cfg(self, new):
        new = dict(new)
        key = new.pop("api_key", None)
        if key:  # 빈 값이면 기존 키 유지
            self.cfg["api_key"] = key
        new.pop("api_key_masked", None)
        new.pop("api_key_set", None)
        cfgmod._merge(self.cfg, new)
        cfgmod.save(self.cfg, self.config_path)

    def scan(self, wav_dir=None, txt_dir=None):
        wav_dir = wav_dir or self.cfg["wav_dir"]
        txt_dir = self.cfg["txt_dir"] if txt_dir is None else txt_dir
        items, warns = inputs.scan(wav_dir, depth=1, txt_folder=txt_dir or None)
        self.scan_cache = ((wav_dir, txt_dir), items)
        self.scan_excluded = len(warns)
        total_s = sum(i.duration_s for i in items)
        return {"count": len(items), "warnings": warns[:50], "warning_count": len(warns),
                "audio_total_s": total_s, "wav_dir": wav_dir, "txt_dir": txt_dir or wav_dir}

    def start_run(self):
        with self.lock:
            if self.run and self.run.state in ("running", "paused"):
                return False, "이미 실행 중입니다"
            if not self.cfg["server"]["host"]:
                return False, "서버 주소를 입력하세요"
            if not self.cfg.get("api_key"):
                return False, "API 키를 입력하세요"
            key = (self.cfg["wav_dir"], self.cfg["txt_dir"])
            if not self.scan_cache or self.scan_cache[0] != key:
                self.scan(*key)
            items = self.scan_cache[1]
            if not items:
                return False, "시험할 파일이 없습니다"
            cfg = json.loads(json.dumps(self.cfg))
            if not cfg["sentence_mode"] or not cfg["measure_latency"] or not cfg["realtime_pacing"]:
                pass  # 속도 측정 비활성: runner가 effective()로 판단
            self.run = runner.Run(cfg, items, runner.new_out_dir(cfg))
            self.run.excluded_files = getattr(self, "scan_excluded", 0)
            self.run.start()
            return True, str(self.run.out_dir)


APP = None

MAX_DIRS = 2000


def list_drives():
    try:
        return [d.rstrip("\\") + "\\" if not d.endswith("\\") else d for d in os.listdrives()]
    except AttributeError:  # Python 3.11 이하
        import string
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


def list_dir(path):
    """폴더 선택 화면용 목록. path가 비어 있으면 드라이브 목록을 반환한다."""
    if not path:
        return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in list_drives()],
                "wav": 0, "txt": 0, "truncated": False}
    p = Path(path)
    while not p.is_dir():           # 없는 경로면 존재하는 가장 가까운 상위로
        if p.parent == p:
            return {"error": "폴더를 찾을 수 없습니다"}
        p = p.parent
    dirs, wav, txt = [], 0, 0
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir():
                        if not e.name.startswith((".", "$")):
                            dirs.append({"name": e.name, "path": e.path})
                    elif e.name.lower().endswith(".wav"):
                        wav += 1
                    elif e.name.lower().endswith(".txt"):
                        txt += 1
                except OSError:
                    continue
    except OSError as ex:
        return {"error": f"폴더를 열 수 없습니다: {ex.strerror or ex}"}
    dirs.sort(key=lambda d: d["name"].lower())
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs[:MAX_DIRS], "wav": wav, "txt": txt,
            "truncated": len(dirs) > MAX_DIRS}


def _int(q, k, d):
    try:
        return int(q.get(k, [d])[0])
    except (TypeError, ValueError):
        return d


class Handler(BaseHTTPRequestHandler):
    server_version = "CERApp/1.0"

    def log_message(self, *a):  # 로그에 요청 URL(키 없음)만 남는 것도 생략
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        run = APP.run
        if p in ("/", "/index.html"):
            data = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/api/config":
            self._json(APP.public_cfg())
        elif p == "/api/status":
            if not run:
                self._json({"state": "idle", "total": 0, "done": 0, "failed": 0, "ok": 0})
            else:
                self._json(run.snapshot())
        elif p == "/api/results":
            if not run:
                return self._json({"total": 0, "rows": []})
            mc = q.get("min_cer", [""])[0]
            total, rows = run.query(
                offset=_int(q, "offset", 0), limit=min(_int(q, "limit", 100), 500),
                q=q.get("q", [""])[0], status=q.get("status", [""])[0],
                sort=q.get("sort", [""])[0], asc=q.get("asc", ["1"])[0] != "0",
                min_cer=float(mc) if mc else None)
            self._json({"total": total, "rows": rows})
        elif p == "/api/browse":
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return self._json({"error": "이 PC에서만 폴더를 선택할 수 있습니다"}, 403)
            self._json(list_dir(q.get("path", [""])[0]))
        elif p == "/api/detail":
            d = run.detail(q.get("name", [""])[0], q.get("model", [""])[0]) if run else None
            self._json(d or {"error": "결과 없음"}, 200 if d else 404)
        elif p == "/api/report":
            if not run:
                return self._json({"error": "실행 결과 없음"}, 404)
            fname = q.get("file", [""])[0]
            if not run.view_only:
                report.write_all(run)          # 이전 시험(읽기 전용)은 파일을 다시 쓰지 않는다
            if fname not in ("detail.csv", "summary.json", "summary.txt"):
                return self._json({"dir": str(run.out_dir),
                                   "files": ["detail.csv", "summary.json", "summary.txt"]})
            if not (run.out_dir / fname).exists():
                return self._json({"error": "이 시험 폴더에는 해당 리포트 파일이 없습니다"}, 404)
            data = (run.out_dir / fname).read_bytes()
            ctype = {"detail.csv": "text/csv", "summary.json": "application/json",
                     "summary.txt": "text/plain"}[fname]
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if urlparse(self.path).path == "/api/config":
            APP.update_cfg(self._body())
            self._json(APP.public_cfg())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        if p == "/api/scan":
            self._json(APP.scan(b.get("wav_dir") or None, b.get("txt_dir")))
        elif p == "/api/health":
            ok, msg = stt_client.health(b.get("server") or APP.cfg["server"])
            self._json({"ok": ok, "message": msg})
        elif p == "/api/run":
            ok, msg = APP.start_run()
            self._json({"ok": ok, "message": msg})
        elif p == "/api/stop":
            if APP.run:
                APP.run.stop()
            self._json({"ok": True})
        elif p == "/api/concurrency":
            try:
                n = int(b.get("value"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "message": "동시 접속 수가 올바르지 않습니다"}, 400)
            run = APP.run
            if run and run.state in ("running", "paused"):
                n = run.set_concurrency(n)
            else:
                n = max(1, min(n, runner.MAX_CONCURRENCY))
            APP.cfg["concurrency"] = n
            cfgmod.save(APP.cfg, APP.config_path)
            self._json({"ok": True, "value": n})
        elif p == "/api/view":
            run = APP.run
            if run and not run.view_only and run.snapshot()["state"] in ("running", "paused", "stopping"):
                return self._json({"ok": False, "message": "시험이 진행 중입니다. 끝나거나 중지한 뒤 열어 주세요."})
            folder = (b.get("folder") or "").strip()
            if not folder:
                return self._json({"ok": False, "message": "결과 폴더를 선택하세요"})
            try:
                APP.run = runner.Run.from_folder(folder, APP.cfg)
            except (ValueError, OSError) as ex:
                return self._json({"ok": False, "message": str(ex)})
            snap = APP.run.snapshot()
            self._json({"ok": True, "done": snap["done"], "total": snap["total"], "view": snap["view"]})
        elif p == "/api/view_close":
            if APP.run and APP.run.view_only:
                APP.run = None
            self._json({"ok": True})
        elif p == "/api/pause":
            if APP.run:
                APP.run.pause(bool(b.get("on", True)))
            self._json({"ok": True})
        elif p == "/api/open_folder":
            run = APP.run
            if run and os.name == "nt":
                os.startfile(str(run.out_dir))  # noqa: S606 (로컬 전용)
            self._json({"ok": bool(run)})
        else:
            self._json({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=8765, open_browser=True):
    global APP
    APP = App()
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"CER App 실행 중: {url}  (종료: Ctrl+C)")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
