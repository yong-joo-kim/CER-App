"""진입점: python -m app.main [--port 8765] [--host 127.0.0.1] [--no-browser]"""
import argparse

from .web import server


def main():
    ap = argparse.ArgumentParser(description="CER/WER 시험 프로그램 (웹 화면)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    server.serve(a.host, a.port, not a.no_browser)


if __name__ == "__main__":
    main()
