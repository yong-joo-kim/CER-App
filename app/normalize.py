"""텍스트 정규화: 빈 줄 제거, 전사 태그(n/ 등)·특수문자(, . ! 등) 제거, 추임새 제외, 숫자 표기 통일.

비교는 숫자를 한글 독음으로 바꾼 형태(cmp)로 하고, 화면 표시는 원래 표기(disp)를 쓴다.
숫자 한 글자는 한글 한 글자로 1:1 치환되므로 두 형태의 어절 수와 위치는 항상 같다.
"""
import re
import unicodedata

_DIGIT = {"0": "공", "1": "일", "2": "이", "3": "삼", "4": "사",
          "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구"}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# 전사 표기용 태그: n/ b/ o/ l/ u/ 처럼 영문 한 글자 + 슬래시 (잡음·호흡 등). 비교에서 무시한다.
_TAG = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]/(?![A-Za-z0-9])")


# 고유어 숫자 "네"(넷) 앞에 오는 수: 열 네 살, 스물 네 개 등
_TENS = {"열", "스물", "스무", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"}


class Tok(str):
    """비교용 문자열(cmp)과 화면 표시용 원문(disp)을 함께 가진 어절. 비교는 cmp로 한다."""

    def __new__(cls, cmp, disp=None):
        o = super().__new__(cls, cmp)
        o.disp = cmp if disp is None else disp
        return o


def digits_to_hangul(text):
    return "".join(_DIGIT.get(c, c) for c in text)


def _tokens(text):
    t = unicodedata.normalize("NFC", text)
    t = " ".join(line.strip() for line in t.splitlines() if line.strip())
    t = _TAG.sub(" ", t)
    t = _PUNCT.sub(" ", t.replace("_", " "))
    return t.split()


def normalize2(text, fillers=()):
    """(비교용, 표시용) 문자열 쌍. 두 문자열의 어절 수는 같다."""
    fl = set(fillers)
    cmp, disp = [], []
    for w in _tokens(text):
        c = digits_to_hangul(w)
        if c in fl and not (c == "네" and cmp and cmp[-1] in _TENS):   # "열 네 살"의 네는 숫자라 추임새가 아님
            continue
        cmp.append(c)
        disp.append(w)
    return " ".join(cmp), " ".join(disp)


def normalize(text, fillers=()):
    return normalize2(text, fillers)[0]


def to_chars(norm):
    """CER용: 공백 제거 문자 리스트."""
    return list(norm.replace(" ", ""))


def to_words(norm):
    """WER용: 공백 기준 어절."""
    return norm.split()


def to_words_disp(cmp, disp):
    """화면 표시용 원문을 함께 가진 어절 목록."""
    return [Tok(c, d) for c, d in zip(cmp.split(), disp.split())]


# (A)/(B) 형태의 병기 표기: 둘 중 하나가 인식 결과와 맞으면 정답으로 인정한다.
_ALT_GROUP = re.compile(r"\([^()]*\)(?:\s*/\s*\([^()]*\))+")
_ALT_ITEM = re.compile(r"\(([^()]*)\)")


def normalize_ref2(text, fillers=()):
    """정답 전사를 구간 목록으로 변환한다. 대안은 (비교용, 표시용) 쌍이다.

    병기 표기 바로 앞뒤에 공백 없이 붙은 글자(조사 등)는 각 대안에 붙여서 한 어절로 본다.
    예) "(17살)/(열 일곱 살)이랑" → 대안 "17살이랑", "열 일곱 살이랑"
    """
    text = unicodedata.normalize("NFC", text)
    segs, pos = [], 0
    for m in _ALT_GROUP.finditer(text):
        if m.start() < pos:
            continue
        plain = text[pos:m.start()]
        prefix = ""
        if plain and not plain[-1].isspace():
            mm = re.search(r"\S+$", plain)
            prefix, plain = mm.group(0), plain[:mm.start()]
        if plain.strip():
            segs.append([normalize2(plain, fillers)])
        tail = text[m.end():]
        suffix = ""
        if tail and not tail[0].isspace() and not _ALT_GROUP.match(tail):
            suffix = re.match(r"\S+", tail).group(0)
        alts = []
        for a in _ALT_ITEM.findall(m.group(0)):
            pair = normalize2(prefix + a + suffix, fillers)
            if pair not in alts:
                alts.append(pair)
        segs.append(alts)
        pos = m.end() + len(suffix)
    tail = text[pos:]
    if tail.strip() or not segs:
        segs.append([normalize2(tail, fillers)])
    return segs


def normalize_ref(text, fillers=()):
    """normalize_ref2의 비교용 문자열만 반환 (호환용)."""
    return [[a[0] for a in alts] for alts in normalize_ref2(text, fillers)]


def lattice(segs, unit="char"):
    """구간 목록 → metrics.score_lattice 입력 (대안별 토큰 리스트).
    대안이 (비교용, 표시용) 쌍이면 어절은 표시용 원문을 가진 Tok이 된다."""
    out = []
    for k, alts in enumerate(segs):
        row = []
        for a in alts:
            cmp, disp = a if isinstance(a, tuple) else (a, a)
            toks = to_chars(cmp) if unit == "char" else to_words_disp(cmp, disp)
            if unit != "char":
                for t in toks:
                    t.seg = k
            row.append(toks)
        out.append(row)
    return out


def ref_text(segs):
    """화면 표시용: 대안이 있으면 (A|B)로 표기. 숫자는 원래 표기를 쓴다."""
    def d(a):
        return a[1] if isinstance(a, tuple) else a
    return " ".join(d(alts[0]) if len(alts) == 1 else "(" + "|".join(d(a) for a in alts) + ")"
                    for alts in segs if any(d(a) for a in alts))
