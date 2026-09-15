#!/usr/bin/env python3
"""每日维护：让助手过一遍个人库的四个区，做增量追加，并把总结推回 QQ。

用法: kb_maintenance.py [--session maintenance] [--umo UMO] [--timeout 900]

以 moyamryia 身份运行（systemd）。token 从 /etc/pi-gateway.env 读取（sudo cat）。
总结写进插件推送队列（spool），由 AstrBot 发到 QQ；不带 umo 给网关，所以
工具进度不会刷屏，只推最终总结。
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time

GATEWAY = "http://127.0.0.1:8787/chat"
SPOOL = ("/home/moyamryia/astrbot/data/plugin_data/"
         "astrbot_plugin_passthrough/push_spool")
DEFAULT_UMO = "default:GroupMessage:455737212"
PROMPT = """[每日维护] 过一遍个人资料库的 profile.md、info/、projects/、notes/，再 list 一下最近邮件。

做两件事：
1) 把最近邮件里值得留存的信息（待办、截止日期、重要往来）追加到 notes/<当月>.md
   （kb_write append），每条带来源（发件人 + 主题）。
2) 若发现 profile/info/projects 里明显缺了今天出现的事实，在总结里列出建议，不要直接改。

只允许新增/追加；不要 edit/delete。最后用 3-5 行总结你做了什么。"""


def token() -> str:
    out = subprocess.check_output(["sudo", "cat", "/etc/pi-gateway.env"], text=True)
    for line in out.splitlines():
        if line.startswith("PI_GATEWAY_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 PI_GATEWAY_TOKEN")


def main() -> int:
    ap = argparse.ArgumentParser(description="个人库每日维护（助手跑一遍）")
    ap.add_argument("--session", default="maintenance")
    ap.add_argument("--umo", default=DEFAULT_UMO)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    payload = json.dumps({"session_id": args.session, "text": PROMPT},
                         ensure_ascii=False)
    req = subprocess.run(
        ["curl", "-s", "-m", str(args.timeout), "-X", "POST",
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {token()}",
         "-d", payload, GATEWAY],
        capture_output=True, text=True)
    try:
        reply = json.loads(req.stdout).get("reply", "")
    except ValueError:
        reply = req.stdout
    if not reply:
        print(f"error: 空回复 rc={req.returncode} {req.stderr[:200]}", file=sys.stderr)
        return 1

    os.makedirs(SPOOL, exist_ok=True)
    name = f"maint-{int(time.time())}-{secrets.token_hex(3)}.json"
    tmp = os.path.join(SPOOL, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": "🌙 每日维护\n" + reply[:1500], "umo": args.umo},
                  f, ensure_ascii=False)
    os.replace(tmp, os.path.join(SPOOL, name))
    print(reply[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
