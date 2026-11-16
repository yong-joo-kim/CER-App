"""④ 상세 비교 화면용: 글자 단위 정렬(CER과 같은 기준) + 어절 묶음 + 재생 시각.

CER은 공백을 뺀 글자 단위로 계산하므로, 화면도 글자 단위로 정렬해야 표시된 대체·삭제·삽입이
요약 표의 숫자와 일치한다(예: 정답 "입금받을" / 인식 "입금 받을"은 띄어쓰기만 다르므로 오류 없음).
비교는 비교용 글자(숫자는 한글 독음)로 하고, 화면에는 원래 표기(disp)를 보여 준다.
숫자 한 글자는 한글 한 글자로 바뀌므로 두 형태의 글자 위치는 1:1로 대응한다.
"""
from . import metrics, normalize


class Ch(str):
    """비교용 글자. disp: 화면 표기, seg/alt/w: 정답 구간·대안·어절 번호(인식 쪽은 w만)."""

    def __new__(cls, c, disp, seg=None, alt=None, w=None):
        o = super().__new__(cls, c)
        o.disp, o.seg, o.alt, o.w = disp, seg, alt, w
        return o


def _chars(cmp, disp, seg=None, alt=None):
    out = []
    cw, dw = cmp.split(), disp.split()
    if len(cw) != len(dw):              # 안전장치: 어긋나면 비교용 글자를 그대로 표시
        dw = cw
    for wi, (c, d) in enumerate(zip(cw, dw)):
        if len(c) != len(d):
            d = c
        for a, b in zip(c, d):
            out.append(Ch(a, b, seg, alt, wi))
    return out


def ref_lattice(segs):
    """정답 구간 목록 → 글자 단위 lattice (각 글자가 구간·대안·어절 번호를 가진다)."""
    rows = []
    for k, alts in enumerate(segs):
        row = []
        for ai, a in enumerate(alts):
            cmp, disp = a if isinstance(a, tuple) else (a, a)
            row.append(_chars(cmp, disp, k, ai))
        rows.append(row)
    return rows


def hyp_chars(cmp, disp):
    return _chars(cmp, disp)


def _times(ops, n_hyp, utts, utt_mode, duration, fillers):
    """글자 연산별 시각(초)과 방식. utts: [(시작, 끝, 텍스트)] 또는 None."""
    t = [None] * len(ops)
    mode = None
    utt_of = None
    if utts:
        utt_of = []
        for k, (_s, _e, text) in enumerate(utts):
            utt_of += [k] * len(normalize.to_chars(normalize.normalize(text, fillers)))
        if len(utt_of) != n_hyp:
            utt_of = None
    if utt_of:
        mode = utt_mode or "rx"
        u_of, j = [], 0
        for op in ops:
            if op[0] == "D":
                u_of.append(utt_of[j - 1] if j > 0 else utt_of[0])
            else:
                u_of.append(utt_of[min(j, len(utt_of) - 1)])
                j += 1
        for k, (s, e, _x) in enumerate(utts):
            idx = [i for i, u in enumerate(u_of) if u == k and ops[i][0] != "I"]
            for r, i in enumerate(idx):
                t[i] = round(s + (e - s) * r / max(1, len(idx)), 2)
            if not idx:                  # 정답 글자가 없는 구간(삽입만): 구간 시작 시각
                for i, u in enumerate(u_of):
                    if u == k:
                        t[i] = round(s, 2)
    else:
        mode = "estimate"
        idx = [i for i, op in enumerate(ops) if op[0] != "I"]
        for r, i in enumerate(idx):
            t[i] = round((duration or 0) * r / max(1, len(idx)), 2)
    prev = 0.0
    for i in range(len(t)):              # 삽입 글자: 바로 앞 글자와 같은 시각
        if t[i] is None:
            t[i] = prev
        prev = t[i]
    return t, mode


def _runs(items):
    """[(표시, 정답글자, 인식글자)] → 같은 표시끼리 묶은 [[표시, 정답문자열, 인식문자열]]."""
    out = []
    for m, a, b in items:
        if out and out[-1][0] == m:
            out[-1][1] += a or ""
            out[-1][2] += b or ""
        else:
            out.append([m, a or "", b or ""])
    return out


def build(ref_segs, hyp_cmp, hyp_disp, utts=None, utt_mode=None, duration=0.0, fillers=()):
    """반환: dict(ref=[어절], hyp=[어절], time_mode, counts).
    어절 = {"t": 시작초, "seg": 구간, "alt": 대안, "runs": [[표시, 정답, 인식], ...]}
    표시: "" 일치, "S" 대체, "D" 삭제, "I" 삽입."""
    hyp = hyp_chars(hyp_cmp, hyp_disp)
    sc, ops = metrics.score_lattice(ref_lattice(ref_segs), hyp)
    times, mode = _times(ops, len(hyp), utts, utt_mode, duration, fillers)

    ref_words, hyp_words = [], []
    rkey, hkey = None, None
    rbuf, hbuf = None, None

    def disp(x):
        return getattr(x, "disp", x) if x is not None else None

    last_h = None
    pend_d = []                           # 인식 쪽 어절이 아직 없을 때의 삭제 표시
    for (op, a, b), t in zip(ops, times):
        if op != "I":                     # 정답 쪽
            key = (a.seg, a.alt, a.w)
            if key != rkey:
                rbuf = {"t": t, "seg": a.seg, "alt": a.alt, "items": []}
                ref_words.append(rbuf)
                rkey = key
            m = "" if op == "=" else op
            rbuf["items"].append((m, disp(a), disp(b) if op == "S" else None))
        if op != "D":                     # 인식 쪽
            if b.w != hkey:
                hbuf = {"t": t, "items": list(pend_d)}
                pend_d = []
                hyp_words.append(hbuf)
                hkey = b.w
            m = "" if op == "=" else op
            hbuf["items"].append((m, disp(a) if op == "S" else None, disp(b)))
            last_h = hbuf
        else:                             # 삭제: 인식 쪽에는 빠진 자리 표시
            if last_h is not None:
                last_h["items"].append(("D", disp(a), None))
            else:
                pend_d.append(("D", disp(a), None))
    if pend_d:
        hyp_words.append({"t": 0.0, "items": pend_d})
    for w in ref_words + hyp_words:
        w["runs"] = _runs(w.pop("items"))
    return {"ref": ref_words, "hyp": hyp_words, "time_mode": mode,
            "counts": {"s": sc.s, "d": sc.d, "i": sc.i, "n": sc.n}}
