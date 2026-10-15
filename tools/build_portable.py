"""폐쇄망 현장 PC용 휴대용 패키지(zip) 생성.

사용: python tools/build_portable.py
결과: dist/CER_App_portable.zip
  - 내장 Python(설치·관리자 권한 불필요) + websocket-client + app + 오프라인 매뉴얼
  - API 키·시험 데이터·개인 설정(config.local.json)은 포함하지 않는다.
"""
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "portable"
CACHE = ROOT / "build" / "cache"
DIST = ROOT / "dist"
NAME = "CER_App"
VER = "%d.%d.%d" % sys.version_info[:3]
EMBED_URL = f"https://www.python.org/ftp/python/{VER}/python-{VER}-embed-amd64.zip"

START_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CER App
echo CER/WER 시험 프로그램을 시작합니다. 잠시 후 브라우저가 열립니다.
echo 이 창을 닫으면 프로그램이 종료됩니다. (주소: http://127.0.0.1:8765)
echo.
python\python.exe -m app.main %*
if errorlevel 1 (
  echo.
  echo [오류] 프로그램이 종료되었습니다. 위 메시지를 확인하세요.
  pause
)
"""

README = """CER/WER 시험 프로그램 (폐쇄망용 휴대용 버전)
================================================

설치
  1) CER_App_portable.zip 을 원하는 폴더에 풉니다. (설치·관리자 권한 불필요)
  2) start.bat 을 더블클릭합니다. 브라우저(Edge/Chrome)가 자동으로 열립니다.
     열리지 않으면 주소창에 http://127.0.0.1:8765 를 입력합니다.
  3) '① 설정·실행' 탭에서 서버 주소, 포트, API 키를 입력하고 [연결 확인]을 누릅니다.
     (한 번 저장하면 이 PC에 config.local.json 으로 남습니다)

폴더
  start.bat        실행 파일
  python\\          내장 Python (수정하지 마세요)
  app\\             프로그램
  lib\\             필요한 라이브러리(websocket-client)
  data\\input       시험용 .wav / .txt 를 넣는 폴더 (다른 폴더를 지정해도 됩니다)
  data\\results     시험 결과가 저장되는 폴더
  사용설명서.html   자세한 사용법 (브라우저로 열기)

주의
  - API 키가 저장된 config.local.json 은 다른 사람에게 복사하지 마세요.
  - 인터넷 연결이 필요 없습니다.
  - 종료: start.bat 창을 닫거나 Ctrl+C
"""


def download_embed():
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"python-{VER}-embed-amd64.zip"
    if not p.exists():
        print("다운로드:", EMBED_URL)
        urllib.request.urlretrieve(EMBED_URL, p)
    return p


def find_websocket():
    import websocket
    return Path(websocket.__file__).resolve().parent


def build():
    if BUILD.exists():
        shutil.rmtree(BUILD)
    pkg = BUILD / NAME
    (pkg / "python").mkdir(parents=True)

    # 1) 내장 Python
    with zipfile.ZipFile(download_embed()) as z:
        z.extractall(pkg / "python")
    pth = next((pkg / "python").glob("python*._pth"))
    pth.write_text("\n".join([pth.stem.split("._pth")[0] + ".zip", ".", "..", "..\\lib"]) + "\n",
                   encoding="utf-8")

    # 2) 라이브러리 (순수 Python)
    shutil.copytree(find_websocket(), pkg / "lib" / "websocket",
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))

    # 3) 프로그램
    shutil.copytree(ROOT / "app", pkg / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 4) 데이터 폴더, 시작 파일, 안내
    for d in ("data/input", "data/results"):
        (pkg / d).mkdir(parents=True)
    (pkg / "data" / "input" / "README.md").write_text(
        "# 시험 입력\n\n시험용 WAV와 정답 전사 TXT를 같은 이름의 쌍으로 넣습니다. 예: a001.wav / a001.txt\n",
        encoding="utf-8")
    (pkg / "start.bat").write_text(START_BAT.replace("\n", "\r\n"), encoding="utf-8")
    (pkg / "README.txt").write_text(README.replace("\n", "\r\n"), encoding="utf-8-sig")

    # 5) 오프라인 매뉴얼 (외부 글꼴 링크 제거)
    body = (ROOT / "docs" / "05_release" / "manual_body.html").read_text(encoding="utf-8")
    body = "\n".join(l for l in body.splitlines() if "fonts.googleapis.com" not in l)
    (pkg / "사용설명서.html").write_text(
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n' + body + "\n</html>\n",
        encoding="utf-8")

    # 6) zip
    DIST.mkdir(exist_ok=True)
    out = DIST / "CER_App_portable.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(BUILD).as_posix())
    print("생성:", out, f"{out.stat().st_size / 1024 / 1024:.1f} MB")
    return out


if __name__ == "__main__":
    build()
