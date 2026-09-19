#!/usr/bin/env python3
"""pi 网关 v2：AstrBot 透穿插件 -> 常驻 pi RPC 进程（每会话一个）。

用法:
  pi_gateway.py [--host 0.0.0.0] [--port 8787] [--cwd DIR]
                [--model provider/modelId] [--timeout 300] [--idle 1800]
                [--token T] [--pi PATH] [--builtin-tools]

端点:
  POST /chat   {"session_id","user_id","text"} -> {"reply": "..."}
  GET  /health -> {"ok": true, "model": "...", "sessions": N}

QQ 里可用的命令（网关翻译成 RPC 命令）:
  /new                    新会话
/model [provider/model] 查看/切换模型（切换后记住）
  /status                 会话状态
  /compact [说明]         压缩上下文
  /abort                  中断当前任务
  /name <名字>            命名会话
  /help                   帮助 + pi 自定义命令清单
  其余 /xxx 若是 pi 的自定义命令（extension/prompt/skill），原样转发给 pi。

实现: 每个 session_id 一个常驻 `pi --mode rpc --session-id <sid>` 进程；
同会话串行、不同会话并行；空闲 --idle 秒回收（会话已落盘，下次自动续）。
pi 内置工具默认禁用（-nbt，避免 QQ 远控 shell），加 --builtin-tools 开启。

UNIX 契约: HTTP JSON；日志走 stderr。
退出码: 0 正常退出 / 2 用法错误。
"""
import argparse
import atexit
import collections
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PI = shutil.which("pi") or os.path.expanduser(
    "~/.nvm/versions/node/v24.19.0/bin/pi")

# pi 的 session id 只收 [A-Za-z0-9._-] 且首尾为字母数字；UMO 带冒号，非法即哈希。
SAFE_SID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

HELP_TEXT = """命令：
/new 新会话（/reset、/重置 同义）
/reload 重启 pi 进程（加载新写的扩展/工具）
/model [provider/model] 查看/切换模型
/status 会话状态
/compact [说明] 压缩上下文
/abort 中断当前任务
/name <名字> 命名会话
/browser grants 查看浏览器交互授权
/browser revoke <域名> 撤销某域名授权
/browser watch on|off 网址变动自动截图推送
/remind 查看待触发提醒（/remind list）
/remind add <HH:MM|时长|完整时间> <内容>  加提醒
/remind del <id前缀> 取消提醒
/help 本帮助
其它 /xxx 为 pi 自定义命令（若有）"""

BROWSER_PY = "/home/moyamryia/agent-tools/tools/browser.py"
REMIND_PY = "/home/moyamryia/assistant/reminder.py"
VENV_PY = "/home/moyamryia/agent-tools/venv/bin/python"


def log(*parts):
    print("[gw]", *parts, file=sys.stderr, flush=True)


def safe_sid(sid):
    if SAFE_SID.match(sid):
        return sid
    return "qq-" + hashlib.sha1(sid.encode("utf-8")).hexdigest()[:16]


def split_model(text):
    if "/" in text:
        provider, model_id = text.split("/", 1)
        return provider.strip(), model_id.strip()
    return "", text.strip()


APPROVALS_DB = "/home/moyamryia/agent-tools/state/approvals.db"
APPROVE_CLI = "/home/moyamryia/agent-tools/tools/approve.py"
APPROVE_EXEC = "/home/moyamryia/agent-tools/tools/approve_exec.py"
APPROVAL_RE = re.compile(r'\{\s*"status"\s*:\s*"approval_required".*?\}', re.S)
PUSH_SPOOL = ("/home/moyamryia/astrbot/data/plugin_data/"
              "astrbot_plugin_passthrough/push_spool")
MODEL_CONF = "/home/moyamryia/assistant/model.conf"
TOOL_LABELS = {
    "nju/mail.py": "处理邮件",
    "nju/kb_query.py": "查课表",
    "nju/lib_search.py": "查图书馆",
    "nju/repair.py": "处理报修",
    "kb_search.py": "查资料库",
    "kb_write.py": "写资料库",
    "find_tools.py": "找工具",
}


def progress_info(cmd: str):
    """认出 agent-tools 特权调用，返回 (人读标签, 脚本路径)；非特权返回 (None, None)。"""
    m = re.search(r"agent-tools-privileged\s+tools/(\S+?)(?:\s|$)", cmd or "")
    if not m:
        return None, None
    script = m.group(1)
    return TOOL_LABELS.get(script, "执行工具"), f"tools/{script}"


def tool_args_summary(tool, args):
    if not isinstance(args, dict):
        return str(args)[:200]
    if tool == "bash":
        return re.sub(r"\s+", " ", str(args.get("command") or ""))[:200]
    for k in ("path", "file_path", "filePath", "pattern", "query", "url"):
        if args.get(k):
            return str(args[k])[:200]
    return json.dumps(args, ensure_ascii=False)[:200]


def tool_result_summary(result):
    text = ""
    try:
        content = (result or {}).get("content") or []
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(parts).strip()
    except Exception:
        text = str(result)
    return re.sub(r"\s+", " ", text)[:300]


def push_spool(text: str, umo: str) -> None:
    os.makedirs(PUSH_SPOOL, exist_ok=True)
    name = f"gw-{int(time.time())}-{secrets.token_hex(3)}.json"
    tmp = os.path.join(PUSH_SPOOL, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text, "umo": umo}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(PUSH_SPOOL, name))
APPROVE_RE = re.compile(r"^(y|yes|ok|批准|同意|好|是|确认|approve)(?:\s+(.*))?$", re.I)
DENY_RE = re.compile(r"^(n|no|拒绝|取消|不行|不要|否|deny|reject)(?:\s+(.*))?$", re.I)


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def find_approval_id(events):
    for e in events:
        for s in _iter_strings(e):
            m = APPROVAL_RE.search(s)
            if m:
                try:
                    return (json.loads(m.group(0)) or {}).get("id")
                except ValueError:
                    pass
    return None


def lookup_pending(aid):
    if not aid:
        return None
    try:
        con = sqlite3.connect(APPROVALS_DB, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, tool, summary, expires, status FROM approvals "
                          "WHERE id=?", (aid,)).fetchone()
        con.close()
        if row and row["status"] == "pending" and row["expires"] > time.time():
            return dict(row)
    except Exception as e:
        log(f"查批准库失败: {e}")
    return None


def extract_reply(events):
    text = ""
    err = ""
    for e in events:
        if e.get("type") != "message_end":
            continue
        m = e.get("message") or {}
        if m.get("role") != "assistant":
            continue
        em = m.get("errorMessage") or m.get("error")
        if em:
            err = str(em)
        c = m.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            buf = "".join(p.get("text", "") for p in c
                          if isinstance(p, dict) and p.get("type") == "text")
            if buf.strip():
                text = buf
    return text.strip(), err.strip()


class PiDead(Exception):
    pass


class PiTimeout(Exception):
    pass


class PiRpc:
    """一个常驻 pi --mode rpc 子进程，JSON 行协议。"""

    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self, sid, argv, cwd):
        self.sid = sid
        self.argv = argv
        self.cwd = cwd
        self.dead = False
        self.settled = False
        self.responses = {}
        self.resp_cv = threading.Condition()
        self.events = []
        self.event_cv = threading.Condition()
        self.stderr_tail = collections.deque(maxlen=40)
        self.last_used = time.time()
        self.proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    @classmethod
    def _next_id(cls):
        with cls._seq_lock:
            cls._seq += 1
            return f"gw{cls._seq}"

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log(f"sid={self.sid} 非 JSON 输出: {line[:120]}")
                    continue
                if msg.get("type") == "response":
                    with self.resp_cv:
                        self.responses[msg.get("id")] = msg
                        self.resp_cv.notify_all()
                else:
                    with self.event_cv:
                        self.events.append(msg)
                        if msg.get("type") == "agent_settled":
                            self.settled = True
                        self.event_cv.notify_all()
        except Exception as e:
            log(f"sid={self.sid} stdout 异常: {e}")
        finally:
            self.dead = True
            with self.resp_cv:
                self.resp_cv.notify_all()
            with self.event_cv:
                self.settled = True
                self.event_cv.notify_all()

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail.append(line.rstrip())
        except Exception:
            pass

    def alive(self):
        return not self.dead and self.proc.poll() is None

    def request(self, obj, timeout):
        if not self.alive():
            raise PiDead("进程已退出: " + ("; ".join(list(self.stderr_tail)[-3:]) or "无 stderr"))
        rid = self._next_id()
        payload = dict(obj)
        payload["id"] = rid
        with self.resp_cv:
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                raise PiDead(f"写入失败: {e}")
            deadline = time.time() + timeout
            while rid not in self.responses:
                if not self.alive():
                    raise PiDead("等待响应时进程退出")
                remain = deadline - time.time()
                if remain <= 0:
                    raise PiTimeout(f"{obj.get('type')} 超时 {timeout}s")
                self.resp_cv.wait(min(remain, 0.5))
            self.last_used = time.time()
            return self.responses.pop(rid)

    def prompt(self, text, timeout, progress=None):
        with self.event_cv:
            self.events = []
            self.settled = False
        ack = self.request({"type": "prompt", "message": text}, 30)
        if not ack.get("success"):
            return "（pi 拒绝了请求）" + str(ack.get("error") or ""), None
        deadline = time.time() + timeout
        timed_out = False
        seen = 0
        labels = {}

        def emit(ev):
            t = ev.get("type")
            tid = ev.get("toolCallId") or ""
            tool = ev.get("toolName") or "?"
            if t == "tool_execution_start":
                if tool == "bash":
                    cmd = str((ev.get("args") or {}).get("command") or "")
                    label, script = progress_info(cmd)
                    if label:
                        labels[tid] = label
                        progress(f"🔧 {label}（{script}）")
                    else:
                        flat = re.sub(r"\s+", " ", cmd)[:200]
                        progress(f"🔧 bash: {flat}")
                else:
                    progress(f"🔧 {tool}: {tool_args_summary(tool, ev.get('args'))}")
            else:
                name = labels.get(tid) or tool
                tag = "❌" if ev.get("isError") else "↩️"
                progress(f"{tag} {name} 返回: {tool_result_summary(ev.get('result'))}")

        with self.event_cv:
            while not self.settled:
                if not self.alive():
                    break
                remain = deadline - time.time()
                if remain <= 0:
                    timed_out = True
                    break
                if progress and len(self.events) > seen:
                    for ev in self.events[seen:]:
                        if ev.get("type") in ("tool_execution_start",
                                              "tool_execution_end"):
                            emit(ev)
                    seen = len(self.events)
                self.event_cv.wait(min(remain, 0.5))
            events = list(self.events)
        self.last_used = time.time()
        if timed_out:
            try:
                self.request({"type": "abort"}, 10)
            except Exception:
                pass
            return f"（任务超过 {timeout}s，已请求中断；可发 /status 查看）", None
        if not self.alive():
            err = " | ".join(list(self.stderr_tail)[-2:])
            return "（pi 进程退出）" + (f" {err}" if err else ""), None
        aid = find_approval_id(events)
        reply, err = extract_reply(events)
        if reply:
            return reply, aid
        if err:
            return "（模型调用失败）" + err[:300], aid
        stderr = " | ".join(list(self.stderr_tail)[-2:])
        return "（pi 没有输出）" + (f" {stderr}" if stderr else ""), aid

    def refresh_sid(self):
        try:
            st = self.request({"type": "get_state"}, 15).get("data") or {}
            if st.get("sessionId"):
                self.sid = st["sessionId"]
        except Exception:
            pass

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class Session:
    def __init__(self, sid, gw):
        self.sid = sid
        self.gw = gw
        self.lock = threading.Lock()
        self.rpc = None
        self.last_pi_sid = None
        self.pending_reload = 0.0
        self.pending_approval = None
        self.umo = ""
        self.last_progress = 0.0
        self.last_label = ""

    def _do_reload(self):
        if self.rpc:
            self.rpc.close()
        self.rpc = None
        self.last_pi_sid = None
        return "已重载：下条消息会重启 pi 进程，加载新扩展/工具。"

    def ensure(self):
        if self.rpc is None or not self.rpc.alive():
            if self.rpc is not None:
                self.rpc.close()
            pi_sid = self.last_pi_sid or self.gw.safe_pi_sid(self.sid)
            argv = self.gw.pi_argv(pi_sid)
            log(f"启动 pi RPC: session={self.sid} pi-sid={pi_sid}")
            self.rpc = PiRpc(pi_sid, argv, self.gw.cwd)
        return self.rpc

    def _exec_approval(self, aid):
        log(f"执行批准 {aid}")
        try:
            p = subprocess.run([APPROVE_EXEC, aid], capture_output=True,
                               text=True, timeout=200)
        except subprocess.TimeoutExpired:
            return "已批准，但执行超时。"
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        ok = p.returncode == 0
        try:
            rpc = self.ensure()
            note = (f"[系统] 用户已批准并执行了你之前被拦的动作（{aid}）。\n"
                    f"执行结果（{'成功' if ok else '失败 exit=' + str(p.returncode)}）：\n"
                    f"{(out or err)[-1200:]}\n"
                    f"请用一两句话向我汇报结果，不要重复执行。")
            return self._run_prompt(rpc, note)
        except Exception as e:
            log(f"结果回流失败: {e}")
            body = out or err or "(无输出)"
            head = "已批准并执行" if ok else f"已批准，但执行失败（exit={p.returncode}）"
            return f"{head}。\n{body[:800]}"

    @staticmethod
    def _deny_reason(m, aid):
        rest = (m.group(2) or "").strip()
        if not rest:
            return ""
        parts = rest.split(None, 1)
        if parts[0] == aid:
            rest = parts[1].strip() if len(parts) > 1 else ""
        return rest

    def _notify_progress(self, msg):
        if not self.umo or not self.gw.progress:
            return
        log(f"progress -> {msg[:120]}")
        try:
            push_spool(msg, self.umo)
        except Exception as e:
            log(f"进度推送失败: {e}")

    def _run_prompt(self, rpc, text):
        reply, aid = rpc.prompt(text, self.gw.timeout,
                                progress=self._notify_progress)
        if aid:
            rec = lookup_pending(aid)
            if rec:
                self.pending_approval = {
                    "id": aid, "expires": rec["expires"],
                    "tool": rec["tool"], "summary": rec["summary"]}
                reply = (reply or "").rstrip() + (
                    f"\n\n⏳ 需要批准：{rec['summary']}\n"
                    f"回复「批准」执行，或「拒绝 <理由>」取消。")
        return reply

    def handle(self, text, umo=""):
        with self.lock:
            if umo:
                self.umo = umo
            t = text.strip()
            if self.pending_reload:
                if time.time() > self.pending_reload:
                    self.pending_reload = 0.0
                elif t.lower() in ("y", "yes", "ok", "确认", "好", "是"):
                    self.pending_reload = 0.0
                    return self._do_reload()
                elif t.lower() in ("n", "no", "取消", "不"):
                    self.pending_reload = 0.0
                    return "已取消重载。"
                else:
                    self.pending_reload = 0.0
            if self.pending_approval:
                pa = self.pending_approval
                if time.time() > pa["expires"]:
                    self.pending_approval = None
                else:
                    aid = pa["id"]
                    m_ok = APPROVE_RE.match(t)
                    m_no = DENY_RE.match(t)
                    if m_ok:
                        self.pending_approval = None
                        return self._exec_approval(aid)
                    if m_no:
                        self.pending_approval = None
                        reason = self._deny_reason(m_no, aid)
                        try:
                            subprocess.run([APPROVE_CLI, "no", aid],
                                           capture_output=True, timeout=20)
                        except Exception as e:
                            log(f"写入拒绝失败: {e}")
                        if not reason:
                            return "已拒绝，动作未执行。"
                        rpc = self.ensure()
                        note = (f"[系统] 用户拒绝了你刚才的动作（{aid}，"
                                f"{pa.get('tool', '')}）。理由：{reason}。"
                                f"不要重试原动作；根据理由调整后重新准备，"
                                f"并再次征求用户确认。")
                        return "已拒绝，理由已转达。\n" + self._run_prompt(rpc, note)
            if t == "/reload":
                self.pending_reload = time.time() + 120
                return ("确认重载？会重启 pi 进程并加载新写的扩展/工具。"
                        "回复 y 确认，n 取消。")
            rpc = self.ensure()
            try:
                if text.startswith("/"):
                    return self.gw.handle_command(self, text)
                return self._run_prompt(rpc, text)
            except (PiDead, PiTimeout) as e:
                rpc.close()
                self.rpc = None
                return f"（后端异常：{e}）"
            finally:
                if self.rpc is not None:
                    self.rpc.last_used = time.time()


class Gateway:
    def __init__(self, cwd, model, timeout, token, pi, idle, builtin_tools,
                 pi_user=None, progress=True):
        self.cwd = cwd
        self.model_str = model
        self.timeout = timeout
        self.token = token
        self.pi = pi
        self.pi_user = pi_user
        self.idle = idle
        self.builtin_tools = builtin_tools
        self.progress = progress
        self.provider, self.model_id = split_model(model)
        try:
            with open(MODEL_CONF, encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                self.provider, self.model_id = split_model(saved)
                model = saved
                log(f"模型配置来自 {MODEL_CONF}: {saved}")
        except OSError:
            pass
        self.model_str = model
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.stopping = False
        threading.Thread(target=self._reaper, daemon=True).start()

    def safe_pi_sid(self, sid):
        return safe_sid(sid)

    def persist_model(self, provider, model_id):
        self.provider, self.model_id = provider, model_id
        self.model_str = f"{provider}/{model_id}" if provider else model_id
        try:
            with open(MODEL_CONF, "w", encoding="utf-8") as f:
                f.write(self.model_str + "\n")
            log(f"模型已持久化: {self.model_str}")
        except OSError as e:
            log(f"写 {MODEL_CONF} 失败: {e}")

    def pi_argv(self, pi_sid):
        argv = [self.pi, "--mode", "rpc", "--session-id", pi_sid]
        if self.provider:
            argv += ["--provider", self.provider]
        if self.model_id:
            argv += ["--model", self.model_id]
        if not self.builtin_tools:
            argv.append("--no-builtin-tools")
        if self.pi_user:
            pi_bin = os.path.dirname(os.path.abspath(self.pi))
            argv = ["sudo", "-n", "-H", "-u", self.pi_user, "/usr/bin/env",
                    f"PATH={pi_bin}:/usr/local/bin:/usr/bin:/bin"] + argv
        return argv

    def session(self, sid):
        with self.sessions_lock:
            s = self.sessions.get(sid)
            if s is None:
                s = Session(sid, self)
                self.sessions[sid] = s
            return s

    def handle(self, sid, text, umo=""):
        return self.session(sid).handle(text, umo)

    def handle_command(self, sess, text):
        head, _, arg = text.partition(" ")
        cmd = head.lower()
        arg = arg.strip()
        rpc = sess.rpc

        def simple(obj, t=20):
            r = rpc.request(obj, t)
            if r.get("success"):
                return None
            return str(r.get("error") or r)

        if cmd in ("/help", "/?", "/commands"):
            lines = [HELP_TEXT]
            try:
                r = rpc.request({"type": "get_commands"}, 15)
                names = [c.get("name") for c in
                         (r.get("data") or {}).get("commands", []) if c.get("name")]
                if names:
                    lines.append("自定义命令: " + " ".join("/" + n for n in names))
            except Exception as e:
                lines.append(f"(自定义命令清单获取失败: {e})")
            return "\n".join(lines)

        if cmd in ("/new", "/reset", "/重置"):
            err = simple({"type": "new_session"}, 30)
            if err:
                return "开新会话失败: " + err
            rpc.refresh_sid()
            sess.last_pi_sid = rpc.sid
            return "已开新会话。"

        if cmd == "/model":
            if not arg:
                st = rpc.request({"type": "get_state"}, 15).get("data") or {}
                m = st.get("model") or {}
                out = [f"当前模型: {m.get('provider')}/{m.get('id')}"]
                try:
                    ms = rpc.request({"type": "get_available_models"}, 25) \
                             .get("data", {}).get("models", [])
                    out.append("可用: " + ", ".join(
                        f"{x['provider']}/{x['id']}" for x in ms[:24]))
                    if len(ms) > 24:
                        out.append(f"...共 {len(ms)} 个")
                except Exception as e:
                    out.append(f"(模型清单获取失败: {e})")
                return "\n".join(out)
            if "/" in arg:
                provider, model_id = arg.split("/", 1)
            else:
                ms = rpc.request({"type": "get_available_models"}, 25) \
                         .get("data", {}).get("models", [])
                hit = next((x for x in ms if x.get("id") == arg), None)
                if not hit:
                    return f"找不到模型 {arg}，发 /model 看清单。"
                provider, model_id = hit["provider"], hit["id"]
            r = rpc.request({"type": "set_model", "provider": provider,
                             "modelId": model_id}, 30)
            if r.get("success"):
                m = r.get("data") or {}
                pid_, mid = m.get("provider"), m.get("id")
                if pid_ and mid:
                    self.persist_model(pid_, mid)
                return f"已切换并记住: {pid_}/{mid}（新会话/重启后仍生效）"
            return "切换失败: " + str(r.get("error") or r)

        if cmd == "/status":
            st = rpc.request({"type": "get_state"}, 15).get("data") or {}
            m = st.get("model") or {}
            out = [f"模型: {m.get('provider')}/{m.get('id')}",
                   f"思考级别: {st.get('thinkingLevel')}",
                   f"会话: {st.get('sessionName') or st.get('sessionId')}",
                   f"消息数: {st.get('messageCount')}  忙: {st.get('isStreaming')}"]
            try:
                s = rpc.request({"type": "get_session_stats"}, 15).get("data") or {}
                out.append("统计: " + json.dumps(s, ensure_ascii=False)[:400])
            except Exception:
                pass
            return "\n".join(out)

        if cmd == "/compact":
            err = simple({"type": "compact",
                          "customInstructions": arg or None}, self.timeout)
            return "上下文已压缩。" if not err else "压缩失败: " + err

        if cmd in ("/abort", "/stop"):
            err = simple({"type": "abort"}, 15)
            return "已请求中断。" if not err else "中断失败: " + err

        if cmd == "/name":
            if not arg:
                return "用法: /name <名字>"
            err = simple({"type": "set_session_name", "name": arg}, 15)
            return "已命名。" if not err else "命名失败: " + err

        if cmd == "/browser":
            args = arg.split()
            sub = args[0].lower() if args else "grants"
            if sub in ("watch", "监测"):
                flag = "/home/moyamryia/.browser/watch_enabled"
                what = args[1].lower() if len(args) > 1 else ""
                if what in ("on", "开", "1"):
                    open(flag, "w").close()
                    return "网址监测已开启：Firefox 每次跳转都会截图推给你。"
                if what in ("off", "关", "0"):
                    try:
                        os.remove(flag)
                    except OSError:
                        pass
                    return "网址监测已关闭。"
                return ("网址监测：" + ("开启" if os.path.exists(flag) else "关闭")
                        + "。用法: /browser watch on|off")
            if sub in ("grants", "list", ""):
                cli = [VENV_PY, BROWSER_PY, "grants"]
            elif sub in ("revoke", "撤"):
                if len(args) < 2:
                    return "用法: /browser revoke <域名>"
                cli = [VENV_PY, BROWSER_PY, "grants", "--revoke", args[1]]
            else:
                return "用法: /browser grants | /browser revoke <域名>"
            try:
                p = subprocess.run(cli, capture_output=True, text=True,
                                   timeout=30)
            except Exception as e:
                return f"浏览器授权查询失败: {e}"
            if p.returncode != 0:
                return "浏览器授权查询失败: " + (p.stderr or "").strip()[:200]
            try:
                data = json.loads(p.stdout or "{}")
            except ValueError:
                return p.stdout.strip()[:300]
            if sub in ("revoke", "撤"):
                return (f"已撤销 {args[1]} 的浏览器交互授权。"
                        if data.get("ok") else f"没有 {args[1]} 的有效授权。")
            grants = data.get("grants") or []
            if not grants:
                return "当前没有浏览器交互授权。"
            lines = [f"浏览器交互授权（{len(grants)} 条）："]
            for g in grants:
                lines.append(f"· {g['subject']}（剩余 {g['left_hours']}h）")
            return "\n".join(lines)

        if cmd == "/remind":
            parts = arg.split()
            sub = parts[0].lower() if parts else "list"
            if sub in ("list", "查"):
                argv = ["list"]
            elif sub in ("del", "cancel", "删"):
                if len(parts) < 2:
                    return "用法: /remind del <id前缀>"
                argv = ["cancel", parts[1]]
            elif sub == "show":
                if len(parts) < 2:
                    return "用法: /remind show <id前缀>"
                argv = ["show", parts[1]]
            elif sub in ("add", "加"):
                if len(parts) < 3:
                    return ("用法: /remind add <HH:MM|时长|完整时间> <内容>\n"
                            "例: /remind add 18:30 取快递 / "
                            "/remind add 45m 站起来走走")
                t, text = parts[1], " ".join(parts[2:])
                argv = ["add", "--at" if ":" in t else "--in", t,
                        "--text", text]
            else:
                return ("用法: /remind [list] | add <时间> <内容> | "
                        "del <id前缀> | show <id前缀>")
            try:
                p = subprocess.run([sys.executable, REMIND_PY, *argv],
                                   capture_output=True, text=True, timeout=20)
            except Exception as e:
                return f"提醒工具执行失败: {e}"
            if p.returncode != 0:
                return ("提醒命令失败: "
                        + ((p.stderr or "").strip() or p.stdout.strip())[:300])
            return (p.stdout or "").strip()[:1500] or "(无输出)"

        name = cmd.lstrip("/")
        try:
            r = rpc.request({"type": "get_commands"}, 15)
            names = {c.get("name") for c in
                     (r.get("data") or {}).get("commands", [])}
        except Exception:
            names = set()
        if name in names:
            reply, _ = rpc.prompt(text, self.timeout)
            return reply
        return f"未知命令 {cmd}，发 /help 看清单。"

    def _reaper(self):
        while not self.stopping:
            time.sleep(30)
            now = time.time()
            victims = []
            with self.sessions_lock:
                for sid, s in list(self.sessions.items()):
                    rpc = s.rpc
                    if rpc and now - rpc.last_used > self.idle \
                            and s.lock.acquire(blocking=False):
                        if s.rpc is rpc:
                            s.last_pi_sid = rpc.sid
                            s.rpc = None
                            victims.append((sid, rpc))
                        s.lock.release()
            for sid, rpc in victims:
                log(f"回收空闲会话 {sid} (pi-sid={rpc.sid})")
                rpc.close()

    def shutdown(self):
        self.stopping = True
        with self.sessions_lock:
            for s in self.sessions.values():
                if s.rpc:
                    s.rpc.close()
                    s.rpc = None


class Handler(BaseHTTPRequestHandler):
    server_version = "pi-gateway/0.2"
    gw = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "model": self.gw.model_str,
                             "sessions": len(self.gw.sessions)})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat"):
            self._json(404, {"error": {"message": "path must be /chat"}})
            return
        if self.gw.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.gw.token}":
                self._json(401, {"error": {"message": "bad token"}})
                return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json(400, {"error": {"message": f"bad json: {e}"}})
            return
        sid = str(req.get("session_id") or "default").strip() or "default"
        text = str(req.get("text") or "").strip()
        umo = str(req.get("umo") or "").strip()
        if not text:
            self._json(400, {"error": {"message": "empty text"}})
            return
        started = time.time()
        reply = self.gw.handle(sid, text, umo)
        log(f"sid={sid} {time.time() - started:.1f}s cmd={text[:24]!r} "
            f"reply={len(reply)}B")
        self._json(200, {"reply": reply})


def main():
    ap = argparse.ArgumentParser(description="pi 网关 v2（AstrBot 透穿插件 -> pi RPC）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--cwd", default=os.path.expanduser("~/Projects/GSE/Lab1/assistant"))
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash-expires-on-0910")
    ap.add_argument("--timeout", type=int, default=300, help="单次任务超时秒数")
    ap.add_argument("--idle", type=int, default=1800, help="空闲多久回收 pi 进程（秒）")
    ap.add_argument("--token", default=os.environ.get("PI_GATEWAY_TOKEN", ""))
    ap.add_argument("--pi", default=DEFAULT_PI)
    ap.add_argument("--no-builtin-tools", action="store_true",
                    help="禁用 pi 内置工具（默认启用；内置工具以 piagent 身份运行，无特权）")
    ap.add_argument("--pi-user", default="",
                    help="以该低权限账户运行 pi（如 piagent；经 sudo -n -H -u）")
    ap.add_argument("--no-progress", action="store_true",
                    help="关闭工具执行进度推送（默认开启，只推 agent-tools 特权调用）")
    args = ap.parse_args()
    if not os.path.exists(args.pi):
        print(f"error: 找不到 pi: {args.pi}", file=sys.stderr)
        sys.exit(2)
    os.makedirs(args.cwd, exist_ok=True)
    gw = Gateway(args.cwd, args.model, args.timeout, args.token, args.pi,
                 args.idle, not args.no_builtin_tools, args.pi_user or None,
                 not args.no_progress)
    Handler.gw = gw

    def _stop(signum, frame):
        log(f"收到信号 {signum}，退出")
        gw.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    atexit.register(gw.shutdown)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"listening http://{args.host}:{args.port}/chat model={args.model} "
        f"cwd={args.cwd} builtin_tools={not args.no_builtin_tools} "
        f"pi_user={args.pi_user or '(self)'} token={'yes' if args.token else 'no'} "
        f"idle={args.idle}s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
