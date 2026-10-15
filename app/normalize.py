"""텍스트 정규화: 빈 줄 제거, 전사 태그(n/ 등)·특수문자(, . ! 등) 제거, 추임새 제외, 숫자 표기 통일."""
import re
import unicodedata

_DIGIT = {"0": "공", "1": "일", "2": "이", "3": "삼", "4": "사",
          "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구"}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
# 전사 표기용 태그: n/ b/ o/ l/ u/ 처럼 영문 한 글자 + 슬래시 (잡음·호흡 등). 비교에서 무시한다.
_TAG = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]/(?![A-Za-z0-9])")


def digits_to_hangul(text):
    return "".join(_DIGIT.get(c, c) for c in text)


def normalize(text, fillers=()):
    t = unicodedata.normalize("NFC", text)
    t = " ".join(line.strip() for line in t.splitlines() if line.strip())
    t = _TAG.sub(" ", t)
    t = _PUNCT.sub(" ", t.replace("_", " "))
    t = digits_to_hangul(t)
    fl = set(fillers)
    return " ".join(w for w in t.split() if w not in fl)


def to_chars(norm):
    """CER용: 공백 제거 문자 리스트."""
    return list(norm.replace(" ", ""))


def to_words(norm):
    """WER용: 공백 기준 어절."""
    return norm.split()


# (A)/(B) 형태의 병기 표기: 둘 중 하나가 인식 결과와 맞으면 정답으로 인정한다.
_ALT_GROUP = re.compile(r"\([^()]*\)(?:\s*/\s*\([^()]*\))+")
_ALT_ITEM = re.compile(r"\(([^()]*)\)")


def normalize_ref(text, fillers=()):
    """정답 전사를 구간 목록으로 변환한다.

    반환: [[대안 문자열, ...], ...]  (각 구간은 대안이 1개면 고정 텍스트)
    예) "저는 (5살)/(다섯 살) 입니다" → [["저는"], ["오살", "다섯 살"], ["입니다"]]
    """
    text = unicodedata.normalize("NFC", text)
    segs, pos = [], 0
    for m in _ALT_GROUP.finditer(text):
        plain = text[pos:m.start()]
        if plain.strip():
            segs.append([normalize(plain, fillers)])
        alts = [normalize(a, fillers) for a in _ALT_ITEM.findall(m.group(0))]
        segs.append(list(dict.fromkeys(alts)))
        pos = m.end()
    tail = text[pos:]
    if tail.strip() or not segs:
        segs.append([normalize(tail, fillers)])
    return segs


def lattice(segs, unit="char"):
    """구간 목록 → metrics.score_lattice 입력 (대안별 토큰 리스트)."""
    f = to_chars if unit == "char" else to_words
    return [[f(a) for a in alts] for alts in segs]


def ref_text(segs):
    """화면 표시용: 대안이 있으면 (A|B)로 표기."""
    return " ".join(a[0] if len(a) == 1 else "(" + "|".join(a) + ")" for a in segs if any(a))
