#!/usr/bin/env python3
"""remind —— Agent 免审提醒客户端（以 piagent 身份运行，不需要 sudo）。

用法:
  remind add (--at "2026-09-22T19:00" | --in 30m | --at 19:00) --text "…"
             [--kind notify|agent] [--repeat daily|weekly|every:7d]
             [--until T] [--session S] [--id ID] [--umo U]
  remind list [--all]
  remind cancel <id前缀>
  remind snooze <id前缀> (--in 10m | --at T)

原理: 本脚本只做校验 + 把请求 JSON 原子写入 requests/ 队列（先写 .tmp 再 mv，
     mv 成功即持久化，reminderd 在不在线都不丢），由 reminderd 消费执行。
     查看队列状态读只读投影 upcoming.json。本脚本不碰 store、不碰代码。

  - notify（默认）: 到点把 text 推到 QQ。
  - agent（--kind agent）: 到点把 text 作为指示自动发起一轮你会话，
    结果发 QQ。text 写清楚"做什么、做完怎么汇报"。
  - 请求非法会被挪到 rejected/<原名>（内含 error 字段）；
    正常处理几秒内可 list 看到。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STAGING = Path(os.environ.get("REMINDER_STAGING",
                              "/srv/agent-staging/reminders"))
REQ_DIR = STAGING / "requests"
PROJECTION = STAGING / "upcoming.json"
UMO_RE = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DUR_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
WD = "一二三四五六日"


def parse_dur(s: str) -> timedelta:
    m = DUR_RE.match(str(s).strip().lower())
    if not m or not any(m.groups()):
        raise ValueError(f"时长格式不对: {s!r}（如 30m / 2h / 1d2h30m）")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    if d == h == mi == 0:
        raise ValueError("时长不能为 0")
    return timedelta(days=d, hours=h, minutes=mi)


def parse_time(s) -> datetime:
    s = str(s).strip().replace(" ", "T")
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        now = datetime.now()
        t = now.replace(hour=int(m[1]), minute=int(m[2]), second=0,
                        microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t
    raise ValueError(f"时间格式不对: {s!r}（YYYY-MM-DDTHH:MM 或 HH:MM）")


def submit(req: dict) -> None:
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{req['action']}-{int(time.time())}-{secrets.token_hex(3)}.json"
    req["submitted_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = REQ_DIR / ("." + name + ".tmp")
    final = REQ_DIR / name
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    print(f"请求已入队: {final.name}")


def cmd_add(args) -> int:
    if not args.text or not args.text.strip():
        raise ValueError("--text 不能为空")
    text = args.text.strip()
    if len(text) > 2000:
        raise ValueError("text 超过 2000 字")
    kind = args.kind
    if kind not in ("notify", "agent"):
        raise ValueError("kind 只能是 notify/agent")
    if args.repeat:
        if args.repeat == "daily" or args.repeat == "weekly" or \
                re.match(r"^every:\d+[mhdw]$", args.repeat):
            pass
        else:
            raise ValueError("repeat 只支持 daily / weekly / every:<N><m|h|d|w>")
    if args.id and not ID_RE.match(args.id):
        raise ValueError(f"id 含非法字符（限字母数字._-）: {args.id!r}")
    if args.umo and not UMO_RE.match(args.umo):
        raise ValueError(f"umo 含非法字符: {args.umo!r}")
    if args.at:
        at = parse_time(args.at)
        at_raw = None
    elif args.in_:
        at = datetime.now() + parse_dur(args.in_)
        at_raw = None
    else:
        raise ValueError("需要 --at <时间> 或 --in <时长>")
    req = {"action": "add", "text": text, "kind": kind,
           "repeat": args.repeat or None, "until": args.until,
           "umo": args.umo, "session": args.session, "id": args.id,
           "req_by": "agent"}
    if args.at:
        req["at"] = args.at
    else:
        req["in"] = args.in_
    submit(req)
    wd = f"周{WD[at.weekday()]}"
    what = "Agent 任务" if kind == "agent" else "提醒"
    print(f"✅ {what}已受理: 将于 {at:%Y-%m-%d %H:%M}（{wd}）触发"
          + (f"，重复 {args.repeat}" if args.repeat else ""))
    if kind == "agent":
        print("   到点会自动发起一轮你的会话执行该指示，结果发 QQ；"
              "指示要写清楚做什么、怎么汇报。")
    print("   几秒后可 remind list 确认。")
    return 0


def cmd_list(args) -> int:
    try:
        proj = json.loads(PROJECTION.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("暂无投影（reminderd 未运行或还没写过）")
        return 1
    except Exception as e:
        print(f"投影读取失败: {e}")
        return 1
    hb = proj.get("heartbeat", "?")
    print(f"reminderd 心跳: {hb}　待触发 {proj.get('count', '?')} 条")
    rows = proj.get("upcoming", [])
    for r in rows:
        at = str(r["at"]).replace("T", " ")
        kind = "任务" if r.get("kind") == "agent" else "提醒"
        rep = f" 重复:{r['repeat']}" if r.get("repeat") else ""
        print(f"  {at} [{kind}]{rep} {r['id']}  {r.get('text_head','')}")
    if not rows:
        print("  （没有待触发的提醒）")
    if args.all:
        print("--- 最近记录 ---")
        for h in proj.get("recent", []):
            print(f"  {h['fired_at']} [{h['result']}] {h['id']}  {h['text_head']}")
    return 0


def cmd_cancel(args) -> int:
    if not args.id:
        raise ValueError("用法: remind cancel <id前缀>")
    submit({"action": "cancel", "id": args.id, "req_by": "agent"})
    print(f"取消请求已提交（{args.id}），几秒后 list 确认。")
    return 0


def cmd_snooze(args) -> int:
    if not args.id:
        raise ValueError("用法: remind snooze <id前缀> --in 10m | --at T")
    req = {"action": "snooze", "id": args.id, "req_by": "agent"}
    if args.in_:
        req["in"] = args.in_
    elif args.at:
        req["at"] = args.at
    else:
        raise ValueError("snooze 需要 --in <时长> 或 --at <时间>")
    submit(req)
    print(f"推迟请求已提交（{args.id}），几秒后 list 确认。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 免审提醒客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="加提醒/定时任务")
    p.add_argument("--at", help="触发时间 YYYY-MM-DDTHH:MM 或 HH:MM")
    p.add_argument("--in", dest="in_", help="多久后，如 30m / 2h / 1d2h")
    p.add_argument("--text", required=True, help="正文/指示")
    p.add_argument("--kind", choices=("notify", "agent"), default="notify")
    p.add_argument("--repeat", help="daily | weekly | every:7d")
    p.add_argument("--until", help="重复截止时间")
    p.add_argument("--session", help="agent 型所用 pi 会话（默认 scheduled）")
    p.add_argument("--id", help="指定 id")
    p.add_argument("--umo", help="QQ 会话（一般不用填）")

    p = sub.add_parser("list", help="看待触发")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("cancel", help="取消")
    p.add_argument("id")

    p = sub.add_parser("snooze", help="推迟")
    p.add_argument("id")
    p.add_argument("--in", dest="in_")
    p.add_argument("--at")

    args = ap.parse_args()
    return {"add": cmd_add, "list": cmd_list,
            "cancel": cmd_cancel, "snooze": cmd_snooze}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)
