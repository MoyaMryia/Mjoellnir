#!/usr/bin/env python3
"""reminderd —— QQ 提醒 / 定时任务（常驻 daemon + 所有者 CLI）。

两类提醒:
  notify  到点把 text 推到 QQ（push_spool -> AstrBot 插件）
  agent   到点把 text 作为指示 POST 给网关 /chat，让 pi 自主干活，结果推回 QQ
  两者都支持 repeat: daily | weekly | every:<Nh|Nm|Nd|Nw>（可加 until 截止）

可靠性约定:
  - 所有待办持久在 store；daemon 崩溃/机器重启后扫描 at<=now 的条目:
    迟到 <= max_late_min(默认24h) 补推并标注迟到；再长则推一条"错过"汇总——
    任何情况不静默丢弃。
  - notify: push_spool 写成功才在 store 记已发（至少一次投递）。
  - agent: 网关 HTTP 200 受理即算投递；连接失败退避重试，读超时视为已在跑
    （重跑可能造成重复副作用，宁可丢结果通知）。
  - id 幂等：同 id 仍待触发时 add 拒绝；已触发/已取消（历史里）的同 id 可重加。
    cancel 对重复任务=整条删除，不是跳过一次。

Agent 入口（免审核、不经 sudo）: /srv/agent-staging/reminders/bin/remind
  它只把请求 JSON 原子写进 requests/ 队列；daemon 消费，非法请求挪 rejected/ 附原因。
  Agent 视图: /srv/agent-staging/reminders/upcoming.json（只读投影）。

用法:
  reminder.py daemon
  reminder.py add (--at T | --in DUR) --text T [--kind notify|agent] [--repeat R]
                  [--until T] [--umo U] [--session S] [--id ID]
  reminder.py list [--all]        show <id前缀>
  reminder.py cancel <id前缀>     snooze <id前缀> (--in DUR | --at T)
  reminder.py sync-class [--dry-run]
  reminder.py import-old <旧reminders.json>

时间格式: "YYYY-MM-DDTHH:MM" / "YYYY-MM-DD HH:MM" / "HH:MM"(今天,已过则明天)；
时长: "30m" "2h" "1d2h30m"。均为本地时间。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("REMINDER_HOME", "/home/moyamryia/assistant"))
STAGING = Path(os.environ.get("REMINDER_STAGING",
                              "/srv/agent-staging/reminders"))
STORE = HOME / "state" / "reminder_store.json"
LOCK = HOME / "state" / "reminder.lock"
SPOOL = Path("/home/moyamryia/astrbot/data/plugin_data/"
             "astrbot_plugin_passthrough/push_spool")
REQ_DIR = STAGING / "requests"
CLAIMED_DIR = REQ_DIR / ".claimed"
REJECTED_DIR = STAGING / "rejected"
PROJECTION = STAGING / "upcoming.json"
CLASS_CONF = HOME / "class_periods.json"
CLASS_SNAPSHOT = HOME / "state" / "class_timetable.json"

DEFAULT_UMO = "default:GroupMessage:455737212"
MAX_LATE_MIN = 1440          # 迟到补推上限，超过记 missed 并推汇总
HISTORY_CAP = 300
TEXT_CAP = 2000
LOOP_MAX_SLEEP = 15          # 醒来周期上限；到点精度 ±15s
CLASS_DAILY_AT = dtime(4, 5)
CLASS_RETRY_MIN = 45         # 课程物化失败后的最小重试间隔
AGENT_CONNECT_TIMEOUT = 5
AGENT_READ_TIMEOUT = 300
AGENT_RETRY_BACKOFF = [5, 10, 20, 40, 60]
UMO_RE = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DUR_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
ENTRY_RE = re.compile(r"周([一二三四五六日天])\s+(\d+)-(\d+)节\s+([\d]+-[\d]+)周\s+(\S+)")
DAY_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_stop = threading.Event()


def log(*parts) -> None:
    print("[remind]", *parts, file=sys.stderr, flush=True)


def human_delta(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 0:
        return "0分钟"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    out = []
    if d:
        out.append(f"{d}天")
    if h:
        out.append(f"{h}小时")
    if m or not out:
        out.append(f"{m}分钟")
    return "".join(out)


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


def gen_id(at: datetime) -> str:
    return f"r-{at:%Y%m%d-%H%M}-{secrets.token_hex(2)}"


# ---------- store ----------

def default_store() -> dict:
    return {
        "version": 1,
        "meta": {"updated_at": None, "seq": 0},
        "config": {"default_umo": DEFAULT_UMO, "max_late_min": MAX_LATE_MIN,
                   "gateway_url": "http://127.0.0.1:8787"},
        "active": [],
        "history": [],
        "class_sync": {"last_run": None, "last_ok": None, "last_error": None},
    }


def load_store() -> dict:
    try:
        st = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_store()
    except Exception as e:
        log(f"store 读取失败（{type(e).__name__}: {e}），按空库处理")
        return default_store()
    base = default_store()
    for k, v in base.items():
        st.setdefault(k, v)
    return st


class store_lock:
    """flock 串行化所有 store 变更（daemon 与 CLI 之间）。"""

    def __enter__(self):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def save_store(st: dict) -> None:
    st["meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    st["meta"]["seq"] = st["meta"].get("seq", 0) + 1
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE)


def settle(st: dict, entry: dict, result: str, fired_at: datetime) -> dict:
    """一条提醒触发后的归宿：重复则推进下一次，否则入 history。"""
    at = parse_time(entry["at"])
    nxt = next_occurrence(at, entry.get("repeat")) if entry.get("repeat") else None
    st["history"].append({
        "id": entry["id"], "kind": entry.get("kind") or "notify",
        "at": entry["at"], "fired_at": fired_at.isoformat(timespec="seconds"),
        "result": result,
        "text_head": (entry.get("text") or "")[:60],
    })
    del st["history"][:-HISTORY_CAP]
    if nxt and (not entry.get("until") or nxt <= parse_time(entry["until"])):
        entry["at"] = nxt.strftime("%Y-%m-%dT%H:%M")
        entry["fired_count"] = entry.get("fired_count", 0) + 1
        return entry
    st["active"] = [a for a in st["active"] if a["id"] != entry["id"]]
    return None


def next_occurrence(at: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return at + timedelta(days=1)
    if repeat == "weekly":
        return at + timedelta(days=7)
    if str(repeat).startswith("every:"):
        return at + parse_dur(repeat.split(":", 1)[1])
    raise ValueError(f"不认识的 repeat: {repeat!r}")


# ---------- 投递 ----------

def spool_push(text: str, umo: str) -> None:
    SPOOL.mkdir(parents=True, exist_ok=True)
    name = f"rem-{int(time.time())}-{secrets.token_hex(3)}.json"
    tmp = SPOOL / (name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text, "umo": umo}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SPOOL / name)


def gateway_token() -> str:
    names = ("PI_GATEWAY_TOKEN", "GATEWAY_TOKEN")
    for n in names:                       # systemd EnvironmentFile 已注入环境
        v = os.environ.get(n, "")
        if v:
            return v
    try:                                  # 手动跑 daemon 时的兜底
        for line in Path("/etc/pi-gateway.env") \
                .read_text(encoding="utf-8").splitlines():
            line = line.strip()
            for n in names:
                if line.startswith(n + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def gateway_chat(gateway_url: str, session: str, text: str, umo: str,
                 read_timeout: int) -> tuple[str, str]:
    """POST /chat。返回 (status, reply_or_error)：status in ok/timeout/error。"""
    body = json.dumps({"session_id": session, "user_id": "reminderd",
                       "text": text, "umo": umo}).encode("utf-8")
    req = urllib.request.Request(
        gateway_url.rstrip("/") + "/chat", data=body, method="POST",
        headers={"Authorization": "Bearer " + gateway_token(),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        return "ok", str(data.get("reply") or "(Agent 无回复)")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError) or \
                isinstance(e, TimeoutError) or "timed out" in str(e.reason):
            return "timeout", str(e)
        return "error", str(e)
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"


def agent_task_worker(gateway_url: str, entry: dict, late: timedelta) -> None:
    rid = entry["id"]
    umo = entry.get("umo") or DEFAULT_UMO
    instruction = entry["text"]
    if late > timedelta(minutes=2):
        instruction += (f"\n\n（系统注：本任务原定 "
                        f"{entry['at']} 触发，迟到 {human_delta(late)}，"
                        f"机器当时可能不在线；按当前时刻酌情处理。）")
    status, reply, tried = "error", "", 0
    for i, wait in enumerate([0] + AGENT_RETRY_BACKOFF):
        if wait:
            if _stop.wait(wait):
                return
        status, reply = gateway_chat(gateway_url, entry.get("session") or "scheduled",
                                     f"【定时任务 {rid}】\n{instruction}", umo,
                                     AGENT_READ_TIMEOUT)
        tried = i + 1
        if status != "error":
            break
    if status == "ok":
        log(f"agent 任务 {rid} 完成（尝试 {tried} 次）")
        try:
            spool_push(f"🤖 定时任务·结果（{rid}）\n{reply[:1500]}", umo)
        except Exception as e:
            log(f"结果推送失败 {rid}: {e}")
    elif status == "timeout":
        log(f"agent 任务 {rid} 读超时（{AGENT_READ_TIMEOUT}s），视为仍在跑，不重试")
    else:
        log(f"agent 任务 {rid} 触发失败：{reply}")
        try:
            with store_lock():
                st = load_store()
                st["history"].append({
                    "id": rid, "kind": "agent", "at": entry["at"],
                    "fired_at": datetime.now().isoformat(timespec="seconds"),
                    "result": "failed", "text_head": entry["text"][:60]})
                del st["history"][:-HISTORY_CAP]
                save_store(st)
            spool_push(f"⚠️ 定时任务 {rid} 触发失败（网关不可达，已重试 "
                       f"{tried} 次）：{reply[:200]}\n任务内容：{entry['text'][:100]}",
                       umo)
        except Exception as e:
            log(f"失败通知推送也失败 {rid}: {e}")


# ---------- 到点触发 ----------

def fire_due() -> None:
    now = datetime.now()
    with store_lock():
        st = load_store()
        cfg = st["config"]
        max_late = timedelta(minutes=cfg.get("max_late_min", MAX_LATE_MIN))
        due = [e for e in st["active"] if parse_time(e["at"]) <= now]
        if not due:
            return
        due.sort(key=lambda e: e["at"])
        missed = []
        for e in due:
            late = now - parse_time(e["at"])
            kind = e.get("kind") or "notify"
            if late > max_late:
                settle(st, e, "missed", now)
                missed.append(e)
                continue
            if kind == "agent":
                settle(st, e, "sent", now)
                threading.Thread(
                    target=agent_task_worker,
                    args=(cfg.get("gateway_url", "http://127.0.0.1:8787"),
                          dict(e), late), daemon=True).start()
                log(f"agent 任务 {e['id']} 已交网关（迟到 {human_delta(late)}）")
            else:
                text = e["text"]
                if late > timedelta(minutes=2):
                    text += f"\n（迟到 {human_delta(late)}，机器当时可能不在线）"
                try:
                    spool_push(text, e.get("umo") or cfg["default_umo"])
                except Exception as ex:
                    log(f"推送失败 {e['id']}（下轮重试）: {ex}")
                    continue
                settle(st, e, "late" if late > timedelta(minutes=2) else "sent",
                       now)
                log(f"已推提醒 {e['id']}（迟到 {human_delta(late)}）")
        if missed:
            lines = [f"⚠️ 有 {len(missed)} 条提醒错过太久（超补推时限"
                     f"{int(max_late.total_seconds() // 3600)}h），未推送："]
            for e in missed:
                lines.append(f"· {e['id']} {e['at']} {e['text'][:40]}")
            try:
                spool_push("\n".join(lines), cfg["default_umo"])
            except Exception as ex:
                log(f"错过汇总推送失败: {ex}")
        save_store(st)


# ---------- Agent 请求队列 ----------

def reject_request(name: str, req: dict, err: str) -> None:
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    req = dict(req or {})
    req["error"] = err
    try:
        (REJECTED_DIR / name).write_text(
            json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"写 rejected 失败: {e}")
    # 上限 50 个，超了删最旧
    olds = sorted(REJECTED_DIR.glob("*.json"))
    for p in olds[:-50]:
        p.unlink(missing_ok=True)
    log(f"请求被拒 {name}: {err}")


def validate_entry_text(req: dict) -> str:
    text = str(req.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > TEXT_CAP:
        raise ValueError(f"text 超过 {TEXT_CAP} 字")
    return text


def apply_add(req: dict, created_by: str) -> str:
    text = validate_entry_text(req)
    if "at" in req and req.get("at"):
        at = parse_time(req["at"])
    elif req.get("in"):
        at = datetime.now() + parse_dur(req["in"])
    else:
        raise ValueError("需要 --at <时间> 或 --in <时长>")
    kind = req.get("kind") or "notify"
    if kind not in ("notify", "agent"):
        raise ValueError(f"kind 只能是 notify/agent，收到 {kind!r}")
    repeat = req.get("repeat") or None
    if repeat:
        next_occurrence(at, repeat)  # 校验格式
    until = parse_time(req["until"]) if req.get("until") else None
    if until and until <= at:
        raise ValueError("until 必须晚于首次触发时间")
    umo = (req.get("umo") or None)
    if umo and not UMO_RE.match(umo):
        raise ValueError(f"umo 含非法字符: {umo!r}")
    session = (req.get("session") or "scheduled").strip()
    if not ID_RE.match(session):
        raise ValueError(f"session 名含非法字符: {session!r}")
    rid = str(req.get("id") or "") or gen_id(at)
    if not ID_RE.match(rid):
        raise ValueError(f"id 含非法字符: {rid!r}")
    entry = {"id": rid, "kind": kind, "at": at.strftime("%Y-%m-%dT%H:%M"),
             "text": text, "umo": umo, "repeat": repeat,
             "until": until.strftime("%Y-%m-%dT%H:%M") if until else None,
             "session": session if kind == "agent" else None,
             "created_by": created_by,
             "created_at": datetime.now().isoformat(timespec="seconds"),
             "fired_count": 0}
    with store_lock():
        st = load_store()
        clash = [a["id"] for a in st["active"] if a["id"] == rid]
        if clash:
            raise ValueError(f"id {rid!r} 仍待触发，先 cancel 它或换一个 id")
        st["active"].append(entry)
        save_store(st)
    return (f"{rid} 于 {at:%Y-%m-%d %H:%M} 触发"
            + (f"，重复:{repeat}" if repeat else ""))


def apply_cancel(prefix: str) -> str:
    prefix = str(prefix).strip()
    with store_lock():
        st = load_store()
        hits = [e for e in st["active"] if e["id"].startswith(prefix)]
        if not hits:
            raise ValueError(f"没有匹配 {prefix!r} 的待触发提醒")
        if len(hits) > 1:
            raise ValueError("前缀匹配到多条: " + " ".join(e["id"] for e in hits)
                             + "；请用更长前缀")
        e = hits[0]
        st["history"].append({
            "id": e["id"], "kind": e.get("kind") or "notify",
            "at": e["at"], "fired_at": datetime.now().isoformat(timespec="seconds"),
            "result": "cancelled", "text_head": (e.get("text") or "")[:60],
        })
        del st["history"][:-HISTORY_CAP]
        st["active"] = [a for a in st["active"] if a["id"] != e["id"]]
        save_store(st)
    return f"已取消并移除 {e['id']}（原定 {e['at']}）"


def apply_snooze(prefix: str, req: dict) -> str:
    prefix = str(prefix).strip()
    if req.get("in"):
        new_at = datetime.now() + parse_dur(req["in"])
    elif req.get("at"):
        new_at = parse_time(req["at"])
    else:
        raise ValueError("snooze 需要 --in <时长> 或 --at <时间>")
    with store_lock():
        st = load_store()
        hits = [e for e in st["active"] if e["id"].startswith(prefix)]
        if not hits:
            raise ValueError(f"没有匹配 {prefix!r} 的待触发提醒")
        if len(hits) > 1:
            raise ValueError("前缀匹配到多条: " + " ".join(e["id"] for e in hits))
        e = hits[0]
        e["at"] = new_at.strftime("%Y-%m-%dT%H:%M")
        save_store(st)
    return f"{e['id']} 推迟到 {new_at:%Y-%m-%d %H:%M}"


def process_requests() -> None:
    CLAIMED_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(CLAIMED_DIR.glob("*.json")):   # 上次崩溃残留，先补处理
        handle_request_file(p)
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(REQ_DIR.glob("*.json")):
        claimed = CLAIMED_DIR / p.name
        try:
            os.rename(p, claimed)                    # 先认领，崩溃不丢不重
        except OSError:
            continue
        handle_request_file(claimed)


def handle_request_file(path: Path) -> None:
    name = path.name
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(req, dict):
            raise ValueError("请求必须是 JSON 对象")
        action = str(req.get("action") or "").strip()
        created_by = str(req.get("req_by") or "agent")
        if action == "add":
            msg = apply_add(req, created_by)
        elif action == "cancel":
            msg = apply_cancel(req.get("id"))
        elif action == "snooze":
            msg = apply_snooze(req.get("id"), req)
        elif action == "sync-class":
            n = sync_class()
            msg = f"课程提醒已物化（{n} 条待触发）"
        else:
            raise ValueError(f"未知 action: {action!r}")
        path.unlink(missing_ok=True)
        log(f"请求已执行 {name}: {msg}")
    except Exception as e:
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            req = {"_raw": "<不可解析>"}
        reject_request(name, req if isinstance(req, dict) else {"_raw": req},
                       f"{type(e).__name__}: {e}")
        path.unlink(missing_ok=True)


# ---------- 课程提醒物化 ----------

def load_class_conf() -> dict:
    conf = json.loads(CLASS_CONF.read_text(encoding="utf-8"))
    for k in ("period_start", "week_anchor", "week_anchor_n"):
        if k not in conf:
            raise ValueError(f"class_periods.json 缺 {k}")
    return conf


def current_week(day: date, anchor: date, anchor_n: int) -> int:
    return max(1, anchor_n + (day - anchor).days // 7)


def classes_on(day: date, data: dict, conf: dict) -> list[dict]:
    week = current_week(day, date.fromisoformat(conf["week_anchor"]),
                        int(conf["week_anchor_n"]))
    wd = day.weekday()
    out = []
    lead = int(conf.get("lead_min", 20))
    for c in data.get("courses", []):
        name = (c.get("kcm") or "?").strip()
        for part in str(c.get("schedule", "")).split(","):
            m = ENTRY_RE.match(part.strip())
            if not m:
                continue
            dc, sp, ep, weeks, _ = m.groups()
            lo, hi = (int(x) for x in weeks.split("-"))
            if DAY_CHARS[dc] != wd or not (lo <= week <= hi):
                continue
            sp, ep = int(sp), int(ep)
            if str(sp) not in conf["period_start"]:
                continue
            hh, mm = map(int, conf["period_start"][str(sp)].split(":"))
            start = datetime.combine(day, dtime(hh, mm))
            out.append({"course": name, "teachers": c.get("teachers", ""),
                        "room": c.get("room_name") or c.get("room") or "",
                        "sp": sp, "ep": ep, "start": start,
                        "remind_at": start - timedelta(minutes=lead)})
    return sorted(out, key=lambda x: x["start"])


def class_id(day: date, course: str, sp: int) -> str:
    h = __import__("hashlib").md5(f"{course}|{sp}".encode("utf-8")).hexdigest()[:8]
    return f"class-{day:%Y%m%d}-{h}"


def sync_class(dry_run: bool = False) -> int:
    conf = load_class_conf()
    source = "网络"
    data = None
    venv_py = conf.get("venv_python", "/home/moyamryia/agent-tools/venv/bin/python")
    kb_query = conf.get("kb_query",
                        "/home/moyamryia/agent-tools/tools/nju/kb_query.py")
    try:
        p = subprocess.run([venv_py, kb_query], capture_output=True, text=True,
                           timeout=90)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or "").strip()[:200] or
                               f"kb_query 退出码 {p.returncode}")
        data = json.loads(p.stdout)
        CLASS_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        CLASS_SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False),
                                  encoding="utf-8")
    except Exception as e:
        try:
            data = json.loads(CLASS_SNAPSHOT.read_text(encoding="utf-8"))
            source = f"快照（kb_query 失败: {e}）"
        except Exception:
            log(f"课程物化失败：kb_query 不可用且无快照: {e}")
            with store_lock():
                st = load_store()
                st["class_sync"].update({
                    "last_run": datetime.now().isoformat(timespec="seconds"),
                    "last_error": str(e)[:300]})
                save_store(st)
            return -1

    today = date.today()
    horizon = int(conf.get("horizon_days", 7))
    desired: dict[str, dict] = {}
    for i in range(horizon):
        for it in classes_on(today + timedelta(days=i), data, conf):
            rid = class_id(it["start"].date(), it["course"], it["sp"])
            desired[rid] = {
                "id": rid, "kind": "notify",
                "at": it["remind_at"].strftime("%Y-%m-%dT%H:%M"),
                "text": (f"⏰ 上课提醒：距「{it['course']}」还有 "
                         f"{conf.get('lead_min', 20)} 分钟\n"
                         f"时间：{it['start']:%H:%M}（第{it['sp']}–{it['ep']}节）\n"
                         f"地点：{it['room']}\n教师：{it['teachers']}"),
                "umo": conf.get("umo") or DEFAULT_UMO,
                "repeat": None, "until": None, "session": None,
                "created_by": "system:class",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "fired_count": 0}

    with store_lock():
        st = load_store()
        if dry_run:
            keep = [e for e in st["active"] if not e["id"].startswith("class-")]
            fired = {h["id"] for h in st["history"]}
            fresh = [e for rid, e in sorted(desired.items())
                     if rid not in {x["id"] for x in keep} and rid not in fired]
            log(f"[dry-run] 课表来源 {source}；将新增 {len(fresh)} 条：")
            for e in fresh:
                log(f"  {e['id']} {e['at']} {e['text'].splitlines()[0]}")
            return len(fresh)
        fired_keys = {(h["id"], h["at"]) for h in st["history"]}
        old_class = [e for e in st["active"] if e["id"].startswith("class-")]
        keep = [e for e in st["active"] if not e["id"].startswith("class-")]
        n_new = n_kept = 0
        for rid, e in sorted(desired.items()):
            if rid in {x["id"] for x in old_class}:
                keep.append(e)      # 原样保留（时间/内容未变）
                n_kept += 1
            elif (rid, e["at"]) in fired_keys:
                continue            # 已提醒过，不再补
            else:
                keep.append(e)
                n_new += 1
        dropped = len(old_class) - n_kept
        st["active"] = keep
        st["class_sync"].update({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "last_ok": datetime.now().isoformat(timespec="seconds"),
            "last_error": None})
        save_store(st)
    log(f"课程物化完成（来源 {source}）：新增 {n_new} 保留 {n_kept} "
        f"移除 {dropped}，共 {len(desired)} 条/未来{horizon}天")
    return n_new


def maybe_sync_class() -> None:
    with store_lock():
        cs = load_store()["class_sync"]
    last_run = (datetime.fromisoformat(cs["last_run"])
                if cs.get("last_run") else None)
    last_ok = (datetime.fromisoformat(cs["last_ok"])
               if cs.get("last_ok") else None)
    now = datetime.now()
    anchor = datetime.combine(now.date(), CLASS_DAILY_AT)
    if last_ok and last_ok >= anchor:
        return
    if now < anchor:
        return
    if last_run and (now - last_run) < timedelta(minutes=CLASS_RETRY_MIN):
        return
    try:
        sync_class()
    except Exception as e:
        log(f"课程物化异常: {e}")


# ---------- 投影 ----------

def write_projection() -> None:
    st = load_store()
    now = datetime.now()
    upcoming = sorted(
        ({"id": e["id"], "kind": e.get("kind") or "notify", "at": e["at"],
          "repeat": e.get("repeat"),
          "session": e.get("session"),
          "text_head": (e.get("text") or "").splitlines()[0][:80]
          if e.get("text") else ""}
         for e in st["active"]
         if parse_time(e["at"]) <= now + timedelta(days=30)),
        key=lambda x: x["at"])[:100]
    recent = st["history"][-12:][::-1]
    proj = {
        "updated_at": now.isoformat(timespec="seconds"),
        "heartbeat": now.isoformat(timespec="seconds"),
        "default_umo": st["config"]["default_umo"],
        "hint": "只读投影，由 reminderd 维护，请勿修改；加提醒用 bin/remind",
        "count": len(st["active"]),
        "upcoming": upcoming,
        "recent": recent,
    }
    text = json.dumps(proj, ensure_ascii=False, indent=1)
    try:
        if PROJECTION.exists() and \
                PROJECTION.read_text(encoding="utf-8") == text:
            return
        PROJECTION.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROJECTION.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, PROJECTION)
        os.chmod(PROJECTION, 0o440)
    except Exception as e:
        log(f"投影写入失败: {e}")


# ---------- daemon ----------

def daemon() -> int:
    def on_term(signum, frame):
        _stop.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log(f"reminderd 启动 store={STORE} requests={REQ_DIR}")
    while not _stop.is_set():
        try:
            process_requests()
            fire_due()
            maybe_sync_class()
            write_projection()
        except Exception as e:
            log(f"循环异常（继续）: {type(e).__name__}: {e}")
            time.sleep(3)
            continue
        with store_lock():
            st = load_store()
        nxt = min((parse_time(e["at"]) for e in st["active"]), default=None)
        if nxt is None:
            sleep_s = LOOP_MAX_SLEEP
        else:
            sleep_s = min(LOOP_MAX_SLEEP,
                          max(2.0, (nxt - datetime.now()).total_seconds()))
        _stop.wait(sleep_s)
    log("reminderd 退出")
    return 0


# ---------- 所有者 CLI ----------

def cmd_add(args) -> int:
    req = {"text": args.text, "kind": args.kind, "repeat": args.repeat,
           "until": args.until, "umo": args.umo, "session": args.session,
           "id": args.id}
    if args.at:
        req["at"] = args.at
    else:
        req["in"] = args.in_
    print("已登记：" + apply_add(req, "owner"))
    return 0


def cmd_list(args) -> int:
    st = load_store()
    now = datetime.now()
    rows = sorted(st["active"], key=lambda e: e["at"])
    print(f"现在 {now:%Y-%m-%d %H:%M}　待触发 {len(rows)} 条")
    for e in rows:
        kind = "任务" if (e.get("kind") == "agent") else "提醒"
        rep = f" 重复:{e['repeat']}" if e.get("repeat") else ""
        first = (e["text"] or "").splitlines()[0][:50]
        print(f"  {e['at'].replace('T',' ')} [{kind}]{rep} {e['id']}  {first}")
    if not rows:
        print("  （没有待触发的提醒）")
    if args.all:
        print(f"--- 最近记录（{min(len(st['history']), 20)} 条）---")
        for h in st["history"][-20:][::-1]:
            print(f"  {h['fired_at']} [{h['result']}] {h['id']}  {h['text_head']}")
    return 0


def _match_one(prefix: str) -> dict:
    st = load_store()
    hits = [e for e in st["active"] if e["id"].startswith(prefix)]
    if not hits:
        raise SystemExit(f"没有匹配 {prefix!r} 的待触发提醒")
    if len(hits) > 1:
        raise SystemExit("前缀匹配到多条: " + " ".join(e["id"] for e in hits))
    return hits[0]


def cmd_show(args) -> int:
    e = _match_one(args.id)
    print(f"id:      {e['id']}")
    print(f"类型:    {e.get('kind') or 'notify'}"
          + (f"  session={e['session']}" if e.get("session") else ""))
    print(f"触发:    {e['at']}" + (f"  重复:{e['repeat']}" if e.get("repeat") else ""))
    print(f"创建:    {e.get('created_by')} @ {e.get('created_at')}")
    print("---")
    print(e["text"])
    return 0


def cmd_cancel(args) -> int:
    print(apply_cancel(args.id))
    return 0


def cmd_snooze(args) -> int:
    req = {"at": args.at} if args.at else {"in": args.in_}
    print(apply_snooze(args.id, req))
    return 0


def cmd_sync_class(args) -> int:
    n = sync_class(dry_run=args.dry_run)
    return 0 if n >= 0 else 1


def cmd_import_old(args) -> int:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    items = data.get("reminders", [])
    n = 0
    with store_lock():
        st = load_store()
        have = {e["id"] for e in st["active"]} | {h["id"] for h in st["history"]}
        for r in items:
            rid = str(r.get("id") or "")
            if not rid or rid in have:
                continue
            at = parse_time(r["at"])
            st["active"].append({
                "id": rid, "kind": "notify",
                "at": at.strftime("%Y-%m-%dT%H:%M"),
                "text": str(r.get("text") or ""), "umo": r.get("umo"),
                "repeat": None, "until": None, "session": None,
                "created_by": "import", "created_at":
                    datetime.now().isoformat(timespec="seconds"),
                "fired_count": 0})
            have.add(rid)
            n += 1
        save_store(st)
    print(f"已导入 {n}/{len(items)} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="QQ 提醒 / 定时任务")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="常驻服务")

    p = sub.add_parser("add", help="添加提醒")
    p.add_argument("--at", help="触发时间 YYYY-MM-DDTHH:MM 或 HH:MM")
    p.add_argument("--in", dest="in_", help="多久后触发，如 30m / 2h / 1d2h")
    p.add_argument("--text", required=True, help="正文（notify=推送内容；agent=指示）")
    p.add_argument("--kind", choices=("notify", "agent"), default="notify")
    p.add_argument("--repeat", help="daily | weekly | every:7d")
    p.add_argument("--until", help="重复截止时间")
    p.add_argument("--umo", help="QQ 会话（默认主人群）")
    p.add_argument("--session", help="agent 型所用 pi 会话（默认 scheduled）")
    p.add_argument("--id", help="指定 id（默认自动生成）")

    p = sub.add_parser("list", help="列出")
    p.add_argument("--all", action="store_true", help="含最近触发记录")

    p = sub.add_parser("show", help="查看完整内容")
    p.add_argument("id", help="id 前缀")

    p = sub.add_parser("cancel", help="取消")
    p.add_argument("id", help="id 前缀")

    p = sub.add_parser("snooze", help="推迟")
    p.add_argument("id", help="id 前缀")
    p.add_argument("--in", dest="in_", help="推迟多久")
    p.add_argument("--at", help="推迟到")

    p = sub.add_parser("sync-class", help="物化课程提醒")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("import-old", help="迁移旧 reminders.json")
    p.add_argument("file")

    args = ap.parse_args()
    if args.cmd == "daemon":
        return daemon()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "snooze":
        return cmd_snooze(args)
    if args.cmd == "sync-class":
        return cmd_sync_class(args)
    if args.cmd == "import-old":
        return cmd_import_old(args)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        raise
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
