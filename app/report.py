"""리포트 출력: detail.csv / summary.json / summary.txt (FR-08, NFR-01, NFR-02)."""
import csv
import json
import time

from . import config as cfgmod

COLUMNS = ["name", "model", "speaker", "status", "cer", "wer", "cs", "ci", "cd", "cn",
           "ws", "wi", "wd", "wn", "duration_s", "latency", "lat_excluded", "note"]


def _pct(v):
    return "-" if v is None else f"{v:.2f}%"


def _sec(v):
    return "-" if v is None else f"{v:.2f}s"


def write_all(run):
    out = run.out_dir
    out.mkdir(parents=True, exist_ok=True)
    recs = [run.records[k] for k in sorted(run.records)]
    with open(out / "detail.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in recs:
            w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in COLUMNS])
    snap = run.snapshot(trend_points=500)
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "state": snap["state"],
        "partial": snap["state"] == "stopped",
        "mode": "sentence" if run.cfg.get("sentence_mode") else "continuous",
        "models": run.models,
        "concurrency": snap["concurrency"],
        "realtime_pacing": run.cfg.get("realtime_pacing", True),
        "total": snap["total"], "done": snap["done"], "failed": snap["failed"],
        "cer_pct": snap["cer"], "wer_pct": snap["wer"], "cer_counts_SIDN": snap["cer_counts"],
        "speakers": snap["speakers"], "cer_distribution": snap["dist"],
        "latency": snap["latency"],
        "elapsed_s": snap["elapsed_s"],
        "config": cfgmod.public_snapshot(run.cfg),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lat = snap["latency"]
    lines = [
        "CER/WER 시험 요약" + (" (중지됨: 완료된 결과 기준)" if snap["state"] == "stopped" else ""),
        f"생성: {summary['generated_at']}   모드: {'문장 단위' if summary['mode'] == 'sentence' else '일반(장시간)'}",
        f"모델: {', '.join(run.models)}   동시 접속: {snap['concurrency']}",
        f"파일: 전체 {snap['total']} / 완료 {snap['ok']} / 실패 {snap['failed']}"
        + (f" / 미실행 {max(0, snap['total'] - snap['done'])}" if snap["state"] == "stopped" else ""),
        f"전체(가중) CER {_pct(snap['cer'])}   WER {_pct(snap['wer'])}",
    ]
    c = snap["cer_counts"]
    lines.append(f"  S/I/D/N = {c[0]}/{c[1]}/{c[2]}/{c[3]}")
    for sp, v in snap["speakers"].items():
        lines.append(f"  화자 {sp}: CER {_pct(None if v['cer'] is None else v['cer'] * 100)}"
                     f"  WER {_pct(None if v['wer'] is None else v['wer'] * 100)}  (N={v['n']})")
    if lat["on"]:
        lines += [f"응답 속도(문장 단위, 30초 이상 제외): 평균 {_sec(lat['mean'])}  중앙 {_sec(lat['median'])}"
                  f"  p95 {_sec(lat['p95'])}  최대 {_sec(lat['max'])}  (측정 {lat['count']}건, 제외 {lat['excluded']}건)"]
    dist = snap["dist"]
    dtot = sum(dist.values())
    if dtot:
        lines.append("파일별 CER 분포")
        for k, v in dist.items():
            pct = v / dtot * 100
            bar = "█" * round(pct / 5)          # 5%당 한 칸
            lab = f"{pct:.1f}%({v}건)"
            lines.append(f"  {lab:<14}  {bar:<20}  CER:{k}")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
