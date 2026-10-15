import json
import tempfile
import time
import unittest
import wave
from pathlib import Path

from app import config, inputs, metrics, normalize, runner, stt_client


def make_wav(path, seconds=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


class NormalizeTest(unittest.TestCase):
    def test_fillers_digits_punct(self):
        t = normalize.normalize("\n음 네 안녕하세요,\n\n 3 1 번입니다.", ["음", "네"])
        self.assertEqual(t, "안녕하세요 삼 일 번입니다")

    def test_tags_and_special_chars(self):
        raw = "n/ 안녕하세요, 반갑습니다! b/ 네. 오늘은 맑습니다? (웃음) a/b ~ 끝…"
        got = normalize.normalize(raw, ["네"])
        self.assertEqual(got, "안녕하세요 반갑습니다 오늘은 맑습니다 웃음 a b 끝")
        self.assertEqual(normalize.normalize("안녕하세요", []), normalize.normalize("n/ 안녕하세요.", []))

    def test_chars_words(self):
        self.assertEqual(normalize.to_chars("가 나"), ["가", "나"])
        self.assertEqual(normalize.to_words("가 나"), ["가", "나"])


class MetricsTest(unittest.TestCase):
    def test_sid(self):
        sc, _ = metrics.score(list("가나다라"), list("가마다라바"))
        self.assertEqual((sc.s, sc.i, sc.d, sc.n), (1, 1, 0, 4))
        self.assertAlmostEqual(sc.rate, 0.5)

    def test_delete_and_empty(self):
        sc, _ = metrics.score(list("가나"), [])
        self.assertEqual((sc.d, sc.rate), (2, 1.0))
        sc, _ = metrics.score([], list("가"))
        self.assertIsNone(sc.rate)


class AlternativeTest(unittest.TestCase):
    def _cer(self, ref, hyp):
        segs = normalize.normalize_ref(ref, ["네"])
        sc, _ = metrics.score_lattice(normalize.lattice(segs, "char"), normalize.to_chars(normalize.normalize(hyp, ["네"])))
        return sc

    def test_parse(self):
        segs = normalize.normalize_ref("저는 (5살)/(다섯 살) 입니다 (웃음) 끝")
        self.assertEqual(segs[0], ["저는"])
        self.assertEqual(segs[1], ["오살", "다섯 살"])
        self.assertEqual(segs[2], ["입니다 웃음 끝"])

    def test_either_alternative_matches(self):
        ref = "저는 (5살)/(다섯 살) 입니다"
        for hyp in ("저는 5살 입니다", "저는 다섯 살 입니다", "저는 다섯살 입니다"):
            sc = self._cer(ref, hyp)
            self.assertEqual(sc.errors, 0, hyp)
        self.assertEqual(self._cer(ref, "저는 5살 입니다").n, len("저는오살입니다"))

    def test_second_group_and_error(self):
        ref = "(5살)/(다섯 살) 정도 (정도예요)/(정도에요)"
        self.assertEqual(self._cer(ref, "다섯 살 정도 정도에요").errors, 0)
        self.assertEqual(self._cer(ref, "다섯 살 정도 정도에용").errors, 1)

    def test_no_group_same_as_plain(self):
        sc = self._cer("가나다라", "가마다라바")
        self.assertEqual((sc.s, sc.i, sc.d, sc.n), (1, 1, 0, 4))

    def test_word_level(self):
        segs = normalize.normalize_ref("저는 (5살)/(다섯 살) 입니다")
        sc, _ = metrics.score_lattice(normalize.lattice(segs, "word"), ["저는", "다섯", "살", "입니다"])
        self.assertEqual((sc.errors, sc.n), (0, 4))


class MergeTest(unittest.TestCase):
    def test_segments_dedup_and_partial(self):
        msgs = [{"partial": True, "transcript": "안"},
                {"segments": [{"start": 0, "end": 1, "transcript": "안녕"}]},
                {"segments": [{"start": 0, "end": 1, "transcript": "안녕"},
                              {"start": 1, "end": 2, "transcript": "하세요"}]}]
        self.assertEqual(stt_client.merge_messages(msgs), "안녕 하세요")

    def test_cumulative_transcript(self):
        msgs = [{"transcript": "안녕"}, {"transcript": "안녕 하세요"}]
        self.assertEqual(stt_client.merge_messages(msgs), "안녕 하세요")

    def test_separate_transcripts(self):
        msgs = [{"transcript": "안녕"}, {"transcript": "하세요"}]
        self.assertEqual(stt_client.merge_messages(msgs), "안녕 하세요")


class InputsTest(unittest.TestCase):
    def test_scan_depth_and_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            make_wav(d / "a.wav")
            (d / "a.txt").write_text("가", encoding="utf-8")
            make_wav(d / "nopair.wav")
            (d / "s").mkdir()
            make_wav(d / "s" / "b_rx.wav")
            (d / "s" / "b_rx.txt").write_text("나", encoding="utf-8")
            (d / "s" / "t").mkdir()
            make_wav(d / "s" / "t" / "c.wav")
            (d / "s" / "t" / "c.txt").write_text("다", encoding="utf-8")
            items, warns = inputs.scan(d, depth=1)
            names = [i.name for i in items]
            self.assertEqual(names, ["a", "s/b_rx"])
            self.assertEqual(items[1].speaker, "rx")
            self.assertEqual(len(warns), 1)


class SeparateDirsTest(unittest.TestCase):
    def test_wav_and_txt_in_different_folders(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "wav" / "sub").mkdir(parents=True)
            (d / "txt" / "sub").mkdir(parents=True)
            (d / "txt" / "flat").mkdir()
            make_wav(d / "wav" / "a.wav")
            (d / "txt" / "a.txt").write_text("가", encoding="utf-8")
            make_wav(d / "wav" / "sub" / "b.wav")
            (d / "txt" / "sub" / "b.txt").write_text("나", encoding="utf-8")
            make_wav(d / "wav" / "c.wav")                    # 상대 경로가 달라도 txt 폴더에서 이름으로 찾음
            (d / "txt" / "flat" / "c.txt").write_text("다", encoding="utf-8")
            make_wav(d / "wav" / "none.wav")                 # 정답 없음 → 제외
            (d / "wav" / "a.txt").write_text("무시", encoding="utf-8")  # wav 폴더의 txt는 사용하지 않음
            items, warns = inputs.scan(d / "wav", txt_folder=d / "txt")
            self.assertEqual([i.name for i in items], ["a", "c", "sub/b"])
            self.assertEqual(Path(items[0].txt), d / "txt" / "a.txt")
            self.assertEqual(len(warns), 1)

    def test_missing_txt_folder(self):
        with tempfile.TemporaryDirectory() as d:
            items, warns = inputs.scan(d, txt_folder=Path(d) / "nope")
            self.assertEqual(items, [])
            self.assertIn("전사(txt) 폴더가 없습니다", warns[0])


class RunnerTest(unittest.TestCase):
    def test_run_resume_and_latency(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "in").mkdir()
            for n, ref in (("a", "안녕 하세요"), ("b", "감사 합니다")):
                make_wav(d / "in" / f"{n}.wav", 1.0)
                (d / "in" / f"{n}.txt").write_text(ref, encoding="utf-8")
            make_wav(d / "in" / "long.wav", 31.0)
            (d / "in" / "long.txt").write_text("길다", encoding="utf-8")
            calls = []

            def fake(wav, cfg, model, tr_id, on_progress=None, stop=None):
                calls.append(Path(wav).stem)
                txt = {"a": "음 안녕 하세요", "b": "감사 합니다 요", "long": "길다"}[Path(wav).stem]
                return {"transcript": txt, "messages": [], "latency": 1.5}

            cfg = config.load(d / "none.json")
            cfg.update(sentence_mode=True, measure_latency=True, concurrency=2,
                       wav_dir=str(d / "in"), output_dir=str(d / "out"), resume=True)
            cfg["server"]["host"] = "x"
            items, _ = inputs.scan(d / "in")
            out = d / "out" / "last_run"
            run = runner.Run(cfg, items, out, transcribe_fn=fake)
            run.start()
            run.thread.join(10)
            s = run.snapshot()
            self.assertEqual((s["total"], s["done"], s["failed"]), (3, 3, 0))
            self.assertEqual(s["latency"]["count"], 2)       # long(31s)은 제외
            self.assertEqual(s["latency"]["excluded"], 1)
            self.assertAlmostEqual(s["latency"]["mean"], 1.5)
            self.assertEqual(run.records[("a", "model_v2")]["cer"], 0.0)  # 추임새 제외
            self.assertGreater(run.records[("b", "model_v2")]["cer"], 0)
            self.assertTrue((out / "a_result.txt").exists())
            self.assertTrue((out / "detail.csv").exists())
            self.assertNotIn("sk-", (out / "summary.json").read_text(encoding="utf-8"))
            # 이어서 하기: 완료 건은 다시 호출하지 않는다
            calls.clear()
            run2 = runner.Run(cfg, items, out, transcribe_fn=fake)
            run2.start()
            run2.thread.join(10)
            self.assertEqual(calls, [])
            self.assertEqual(run2.snapshot()["done"], 3)

    def test_failure_does_not_stop_batch(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "in").mkdir()
            for n in ("a", "b"):
                make_wav(d / "in" / f"{n}.wav")
                (d / "in" / f"{n}.txt").write_text("가", encoding="utf-8")

            def fake(wav, cfg, model, tr_id, on_progress=None, stop=None):
                if Path(wav).stem == "a":
                    raise stt_client.SttError("연결 실패", retry=False)
                return {"transcript": "가", "messages": [], "latency": None}

            cfg = config.load(d / "none.json")
            cfg.update(concurrency=1)
            items, _ = inputs.scan(d / "in")
            run = runner.Run(cfg, items, d / "out", transcribe_fn=fake)
            run.start()
            run.thread.join(10)
            s = run.snapshot()
            self.assertEqual((s["done"], s["failed"]), (2, 1))
            self.assertEqual(run.records[("a", "model_v2")]["status"], "실패")


class StopTest(unittest.TestCase):
    def test_stop_keeps_completed_results(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "in").mkdir()
            for n in ("a", "b", "c", "d"):
                make_wav(d / "in" / f"{n}.wav")
                (d / "in" / f"{n}.txt").write_text("가나다라", encoding="utf-8")
            started = []

            def fake(wav, cfg, model, tr_id, on_progress=None, stop=None):
                name = Path(wav).stem
                started.append(name)
                if name in ("a", "b"):
                    return {"transcript": "가나다마", "messages": [], "latency": None}
                while not stop.is_set():          # c, d는 오래 걸리는 중
                    time.sleep(0.01)
                raise stt_client.SttError("사용자 중지", retry=False)

            cfg = config.load(d / "none.json")
            cfg.update(concurrency=2)
            items, _ = inputs.scan(d / "in")
            run = runner.Run(cfg, items, d / "out", transcribe_fn=fake)
            run.start()
            for _ in range(200):
                if run.snapshot()["done"] >= 2:
                    break
                time.sleep(0.02)
            run.stop()
            self.assertIn(run.snapshot()["state"], ("stopping", "stopped"))
            run.thread.join(10)
            s = run.snapshot()
            self.assertEqual(s["state"], "stopped")
            self.assertEqual((s["done"], s["failed"]), (2, 0))       # 중지된 건은 실패로 세지 않음
            self.assertAlmostEqual(s["cer"], 25.0)                   # 완료 2건(오류 2 / N 8)
            txt = (d / "out" / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("중지됨", txt)
            self.assertTrue((d / "out" / "detail.csv").exists())


class BrowseTest(unittest.TestCase):
    def test_list_dir(self):
        from app.web import server
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "b").mkdir()
            (d / "A").mkdir()
            (d / ".hidden").mkdir()
            make_wav(d / "x.wav")
            (d / "x.txt").write_text("가", encoding="utf-8")
            r = server.list_dir(str(d))
            self.assertEqual([x["name"] for x in r["dirs"]], ["A", "b"])
            self.assertEqual((r["wav"], r["txt"]), (1, 1))
            self.assertEqual(r["parent"], str(d.parent))
            r2 = server.list_dir(str(d / "없는폴더" / "하위"))   # 없는 경로는 가장 가까운 상위로
            self.assertEqual(r2["path"], str(d))

    def test_drives(self):
        from app.web import server
        r = server.list_dir("")
        self.assertIsNone(r["parent"])
        self.assertTrue(all(x["path"].endswith("\\") for x in r["dirs"]) and r["dirs"])


class PauseClockTest(unittest.TestCase):
    def test_elapsed_freezes_while_paused(self):
        cfg = config.load(Path("none.json"))
        run = runner.Run(cfg, [], Path(tempfile.gettempdir()) / "cer_pause_test")
        run.started = time.time() - 100
        run.total = 10
        run.order = [1, 2]
        run.state = "running"
        run.pause(True)
        a = run.snapshot()
        time.sleep(0.3)
        b = run.snapshot()
        self.assertEqual(a["state"], "paused")
        self.assertAlmostEqual(a["elapsed_s"], b["elapsed_s"], places=3)
        self.assertAlmostEqual(a["remaining_s"], b["remaining_s"], places=3)
        run.pause(False)
        time.sleep(0.2)
        c = run.snapshot()
        self.assertGreater(c["elapsed_s"], b["elapsed_s"])
        self.assertLess(c["elapsed_s"], b["elapsed_s"] + 1.0)   # 정지한 시간은 더해지지 않음


class ConfigTest(unittest.TestCase):
    def test_mask_and_snapshot(self):
        cfg = config.load(Path("nonexistent.json"))
        cfg["api_key"] = "sk-abcdefghijkl"
        snap = config.public_snapshot(cfg)
        self.assertNotIn("abcdefghijkl", json.dumps(snap))


if __name__ == "__main__":
    unittest.main()
