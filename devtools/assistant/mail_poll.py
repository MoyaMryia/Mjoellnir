#!/usr/bin/env python3
"""邮件轮询 -> 写推送队列（由 AstrBot 透穿插件发到 QQ）。

用法:
  mail_poll.py [--accounts nju,outlook] [--limit 10] [--spool DIR] [--test]

对每个账号调用 mail.py fetch（UID 水位线去重）；有新邮件就写一条 JSON 到
spool 目录：<spool>/<ts>-<rand>.json  {"text": "..."}。
插件每 5 秒扫 spool，发送后删除文件。
首次拉取（水位线为 0）只建水位线、不推送，避免把历史邮件全推一遍。

UNIX 契约: stdout=人读摘要；stderr=日志。
退出码: 0 成功 / 2 用法错误 / 69 部分账号失败。
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time

PY = "/home/moyamryia/agent-tools/venv/bin/python"
MAIL = "/home/moyamryia/agent-tools/tools/nju/mail.py"
DEFAULT_SPOOL = ("/home/moyamryia/astrbot/data/plugin_data/"
                 "astrbot_plugin_passthrough/push_spool")
DEFAULT_FILTER = "/home/moyamryia/assistant/mail_filter.json"


def log(*parts):
    print("[poll]", *parts, file=sys.stderr, flush=True)


def load_rules(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"skip_from": [], "skip_subject": [], "keep_from": [],
                "keep_subject": []}
    return {
        "skip_from": [re.compile(p, re.I) for p in d.get("skip_from", [])],
        "skip_subject": [re.compile(p, re.I) for p in d.get("skip_subject", [])],
        "keep_from": [re.compile(p, re.I) for p in d.get("keep_from", [])],
        "keep_subject": [re.compile(p, re.I) for p in d.get("keep_subject", [])],
    }


def is_noise(msg: dict, rules: dict) -> bool:
    frm = str(msg.get("from") or "")
    subj = str(msg.get("subject") or "")
    if any(p.search(frm) for p in rules["keep_from"]):
        return False
    if any(p.search(subj) for p in rules["keep_subject"]):
        return False
    if any(p.search(frm) for p in rules["skip_from"]):
        return True
    if any(p.search(subj) for p in rules["skip_subject"]):
        return True
    return False


def fetch(account: str, limit: int):
    try:
        p = subprocess.run([PY, MAIL, "--account", account, "fetch",
                            "--limit", str(limit)],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log(f"{account} fetch 超时")
        return None
    if p.returncode != 0:
        log(f"{account} fetch 失败 exit={p.returncode}: {(p.stderr or '')[-200:]}")
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        log(f"{account} 输出非 JSON: {p.stdout[:120]}")
        return None


def queue(spool: str, text: str, umo: str = "") -> None:
    os.makedirs(spool, exist_ok=True)
    name = f"{int(time.time())}-{secrets.token_hex(3)}.json"
    tmp = os.path.join(spool, name + ".tmp")
    item = {"text": text}
    if umo:
        item["umo"] = umo
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(spool, name))
    log(f"queued {name} -> {umo or '(插件默认)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="邮件轮询 -> QQ 推送队列")
    ap.add_argument("--accounts", default="nju", help="逗号分隔（默认 nju）")
    ap.add_argument("--limit", type=int, default=10, help="每账号最多取几封")
    ap.add_argument("--spool", default=DEFAULT_SPOOL, help="推送队列目录")
    ap.add_argument("--umo", default="", help="推送目标（留空=用插件配置）")
    ap.add_argument("--filter", default=DEFAULT_FILTER, help="过滤规则 JSON")
    ap.add_argument("--test", action="store_true", help="只写一条测试推送")
    args = ap.parse_args()

    if args.test:
        queue(args.spool, "🔔 测试推送：邮件轮询通道正常。", args.umo)
        print("test notification queued")
        return 0

    rules = load_rules(args.filter)
    lines, failed = [], False
    for acc in [a.strip() for a in args.accounts.split(",") if a.strip()]:
        d = fetch(acc, args.limit)
        if d is None:
            failed = True
            continue
        msgs = d.get("messages") or []
        if not msgs:
            continue
        if int(d.get("watermark_before") or 0) == 0:
            log(f"{acc} 首次拉取，建立水位线 {d.get('watermark_before')} -> "
                f"{max([int(m['uid']) for m in msgs])}，不推送")
            continue
        kept, skipped = [], 0
        for m in msgs:
            if is_noise(m, rules):
                skipped += 1
                log(f"{acc} 过滤: {str(m.get('from'))[:40]} — {str(m.get('subject'))[:50]}")
            else:
                kept.append(m)
        if not kept:
            log(f"{acc}: {len(msgs)} 封全部被过滤")
            continue
        head = f"📬 {acc} 新邮件 {len(kept)} 封："
        if skipped:
            head += f"（另过滤 {skipped} 封）"
        lines.append(head)
        for m in kept:
            frm = (m.get("from") or "")[:40]
            subj = (m.get("subject") or "(无主题)")[:60]
            lines.append(f"  · {frm} — {subj}")

    if lines:
        lines.append("回复「读邮件」看详情。")
        queue(args.spool, "\n".join(lines), args.umo)
    else:
        log("no new mail")
    print("\n".join(lines) if lines else "no new mail")
    return 69 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
