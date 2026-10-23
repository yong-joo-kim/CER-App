"""Claude Code 세션 기록(jsonl)을 docs/대화기록_CER_App.html 로 내보낸다. (markdown 패키지 필요)"""
import glob
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.expanduser("~/.claude/projects/D--2--Team-Source-CER-App/*.jsonl")
OUT = os.path.join(ROOT, "docs", "대화기록_CER_App.html")
KST = timezone(timedelta(hours=9))
KEY = re.compile(r"sk-[A-Za-z0-9_\-]{10,}")
SYS = re.compile(r"<system-reminder>.*?</system-reminder>|<command-[a-z-]+>.*?</command-[a-z-]+>"
                 r"|<local-command-[a-z]+>.*?</local-command-[a-z]+>", re.S)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)


def user_text(c):
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def collect():
    msgs, seen = [], set()
    for f in glob.glob(SRC):
        for line in open(f, encoding="utf-8"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t, u = o.get("type"), o.get("uuid")
            if o.get("isSidechain") or o.get("isMeta"):
                continue
            if t == "attachment":
                a = o.get("attachment", {})
                if a.get("type") == "queued_command" and a.get("origin", {}).get("kind") == "human":
                    key = a.get("source_uuid") or a.get("delivery_id")
                    if key in seen:
                        continue
                    seen.add(key)
                    msgs.append(("user", ts(a["timestamp"]), user_text(a.get("prompt", ""))))
                continue
            if t not in ("user", "assistant") or u in seen:
                continue
            seen.add(u)
            c = o.get("message", {}).get("content")
            if t == "assistant":
                txt = "\n\n".join(b.get("text", "") for b in c if b.get("type") == "text")
            else:
                if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
                    continue
                txt = user_text(c)
            txt = SYS.sub("", txt).strip()
            if txt and "timestamp" in o:
                msgs.append((t, ts(o["timestamp"]), txt))
    msgs.sort(key=lambda m: m[1])
    # 같은 사용자 입력이 직접·대기열 양쪽으로 기록된 경우 제거
    out = []
    for m in msgs:
        if out and m[0] == "user" and out[-1][0] == "user" and out[-1][2] == m[2]:
            continue
        out.append(m)
    return out


def main():
    msgs = collect()
    old = open(OUT, encoding="utf-8").read()
    head = old[:old.index('<p class="sub">')]
    parts = [head, '<p class="sub">Claude Code와 나눈 대화(사용자 입력 / Claude 응답) · 총 %d개 메시지 · '
             '표와 코드는 원문 형식을 유지했습니다 · 도구 실행 로그와 시스템 알림은 제외했고 API 키 값은 가렸습니다</p>\n' % len(msgs)]
    for i, (role, when, txt) in enumerate(msgs, 1):
        txt = KEY.sub("sk-****", txt)
        who = "나" if role == "user" else "Claude"
        if role == "user":
            body = '<p class="u">%s</p>' % html.escape(txt).replace("\n", "<br />")
        else:
            body = markdown.markdown(txt, extensions=["tables", "fenced_code", "nl2br"])
            body = body.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")
        parts.append('<article class="m %s"><header><b>%s</b><span>%s</span><i>#%d</i></header><div class="b">%s</div></article>'
                     % (role, who, when.strftime("%Y-%m-%d %H:%M"), i, body))
    parts.append("\n</div></body></html>\n")
    open(OUT, "w", encoding="utf-8").write("".join(parts))
    print(len(msgs), "messages ->", OUT)


if __name__ == "__main__":
    sys.exit(main())
