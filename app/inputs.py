"""입력 WAV/TXT 쌍 탐색·검증 (FR-01, FR-02, NFR-03).

입력 폴더와 그 아래 한 단계 하위 폴더까지만 탐색한다.
"""
import os
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass
class InputItem:
    name: str          # 입력 폴더 기준 상대 경로(확장자 제외, '/' 구분)
    wav: Path
    txt: Path
    speaker: str       # 'rx' / 'tx' / '-'
    duration_s: float


class InputError(Exception):
    pass


def speaker_of(name):
    base = name.rsplit("/", 1)[-1].lower()
    for sp in ("rx", "tx"):
        if base.endswith("_" + sp):
            return sp
    return "-"


def check_wav(path):
    """8kHz / mono / PCM16 검증. 불일치 시 InputError. 길이(초) 반환."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != 8000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise InputError(
                    f"{Path(path).name}: 8kHz/mono/PCM16 필요 "
                    f"(현재 {w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth() * 8}bit)")
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError) as e:
        raise InputError(f"{Path(path).name}: WAV 읽기 실패 ({e})")


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise InputError(f"{Path(path).name}: UTF-8이 아닌 텍스트 파일입니다")


def _wavs(folder, depth):
    """folder의 .wav 경로를 (이름 정렬) 반환. depth=1이면 하위 한 단계 포함."""
    out = []
    subdirs = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".wav"):
                    out.append(Path(e.path))
                elif e.is_dir() and depth > 0:
                    subdirs.append(Path(e.path))
    except OSError:
        return out
    for sd in sorted(subdirs):
        out.extend(_wavs(sd, depth - 1))
    return sorted(out)


def _txts_by_stem(folder, depth):
    """txt_folder 안(하위 한 단계까지)의 .txt를 이름(확장자 제외) → 경로로 색인.
    같은 이름이 여럿이면 상위 폴더의 것을 우선한다."""
    idx = {}

    def walk(d, left):
        subs = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_file() and e.name.lower().endswith(".txt"):
                        idx.setdefault(Path(e.name).stem, Path(e.path))
                    elif e.is_dir() and left > 0:
                        subs.append(e.path)
        except OSError:
            return
        for s in sorted(subs):
            walk(s, left - 1)
    walk(folder, depth)
    return idx


def scan(folder, depth=1, validate_text=False, txt_folder=None):
    """(items, warnings) 반환. 쌍이 안 맞거나 형식이 틀린 파일은 경고 후 제외.

    folder      : .wav 폴더
    txt_folder  : 정답 .txt 폴더(없으면 folder와 같은 위치). 같은 상대 경로의
                  <이름>.txt를 먼저 찾고, 없으면 txt_folder 안에서 같은 이름을 찾는다.
    """
    folder = Path(folder)
    items, warnings = [], []
    if not folder.is_dir():
        return items, [f"음성(wav) 폴더가 없습니다: {folder}"]
    separate = bool(txt_folder) and Path(txt_folder).resolve() != folder.resolve()
    tdir = Path(txt_folder) if separate else folder
    if separate and not tdir.is_dir():
        return items, [f"전사(txt) 폴더가 없습니다: {tdir}"]
    by_stem = _txts_by_stem(tdir, depth) if separate else None
    for wav in _wavs(folder, depth):
        rel = wav.relative_to(folder).with_suffix("").as_posix()
        if separate:
            txt = tdir / (rel + ".txt")
            if not txt.exists():
                txt = by_stem.get(wav.stem, txt)
        else:
            txt = wav.with_suffix(".txt")
        if not txt.exists():
            warnings.append(f"{rel}: 정답 TXT 없음, 제외")
            continue
        try:
            dur = check_wav(wav)
            if validate_text:
                read_text(txt)
        except InputError as e:
            warnings.append(f"{e}, 제외")
            continue
        items.append(InputItem(rel, wav, txt, speaker_of(rel), dur))
    return items, warnings
