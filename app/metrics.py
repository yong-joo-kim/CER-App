"""CER/WER 및 S/I/D/N 계산 (FR-06, FR-07). Levenshtein 정렬 + 역추적."""
from dataclasses import dataclass


@dataclass
class Score:
    s: int = 0
    i: int = 0
    d: int = 0
    n: int = 0

    @property
    def errors(self):
        return self.s + self.i + self.d

    @property
    def rate(self):
        """N=0이면 정의 불가(None)."""
        return None if self.n == 0 else self.errors / self.n

    def __add__(self, o):
        return Score(self.s + o.s, self.i + o.i, self.d + o.d, self.n + o.n)


def align(ref, hyp):
    """ops 리스트: ('=',r,h) ('S',r,h) ('I',None,h) ('D',r,None)."""
    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        dp[i][0] = i
        ri = ref[i - 1]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            row[j] = min(prev[j - 1] + (ri != hyp[j - 1]), prev[j] + 1, row[j - 1] + 1)
    ops, i, j = [], n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            ops.append(("=" if ref[i - 1] == hyp[j - 1] else "S", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append(("D", ref[i - 1], None))
            i -= 1
        else:
            ops.append(("I", None, hyp[j - 1]))
            j -= 1
    ops.reverse()
    return ops


def score(ref, hyp):
    ops = align(ref, hyp)
    sc = Score(n=len(ref))
    for op, _, _ in ops:
        if op == "S":
            sc.s += 1
        elif op == "I":
            sc.i += 1
        elif op == "D":
            sc.d += 1
    return sc, ops


def score_lattice(segs, hyp):
    """정답이 구간별 대안(lattice)인 경우의 편집거리.

    segs: [[토큰리스트(대안1), 토큰리스트(대안2), ...], ...]
    각 구간에서 인식 결과와 가장 잘 맞는 대안 하나가 선택된다(전체 오류 최소).
    N은 선택된 대안의 토큰 수 합이다. 반환: (Score, ops)
    """
    m = len(hyp)
    D = list(range(m + 1))
    bounds, stores = [D], []
    for alts in segs:
        best, seg_rows = None, []
        for tokens in alts:
            rows, prev = [D], D
            for tok in tokens:
                row = [prev[0] + 1] + [0] * m
                for j in range(1, m + 1):
                    row[j] = min(prev[j - 1] + (tok != hyp[j - 1]), prev[j] + 1, row[j - 1] + 1)
                rows.append(row)
                prev = row
            seg_rows.append((tokens, rows))
            best = prev if best is None else [min(a, b) for a, b in zip(best, prev)]
        if best is None:
            best = D
            seg_rows.append(([], [D]))
        stores.append(seg_rows)
        D = best
        bounds.append(D)
    ops, j, n = [], m, 0
    for k in range(len(segs) - 1, -1, -1):
        target = bounds[k + 1][j]
        tokens, rows = next(((t, r) for t, r in stores[k] if r[-1][j] == target), stores[k][0])
        i = len(tokens)
        n += i
        while i > 0:
            cur = rows[i][j]
            if j > 0 and cur == rows[i - 1][j - 1] + (tokens[i - 1] != hyp[j - 1]):
                ops.append(("=" if tokens[i - 1] == hyp[j - 1] else "S", tokens[i - 1], hyp[j - 1]))
                i, j = i - 1, j - 1
            elif cur == rows[i - 1][j] + 1:
                ops.append(("D", tokens[i - 1], None))
                i -= 1
            else:
                ops.append(("I", None, hyp[j - 1]))
                j -= 1
    while j > 0:
        ops.append(("I", None, hyp[j - 1]))
        j -= 1
    ops.reverse()
    sc = Score(n=n)
    for op, _, _ in ops:
        if op == "S":
            sc.s += 1
        elif op == "I":
            sc.i += 1
        elif op == "D":
            sc.d += 1
    return sc, ops
