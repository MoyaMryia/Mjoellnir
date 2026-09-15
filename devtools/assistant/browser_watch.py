#!/home/moyamryia/agent-tools/venv/bin/python
"""浏览器网址监测：轮询当前标签 URL，变动后截图并推送到 QQ（私聊）。

与 browser.py 共用 ~/.browser/control.lock：Agent 正在用浏览器时本轮跳过，
不抢会话、不阻塞 Agent。开关：~/.browser/watch_enabled 存在即启用（默认开）。

退出码：0 正常循环；2 用法错误；66 浏览器不可用（跳过本轮）。
"""
import asyncio
import base64
import fcntl
import json
import os
import sys
import time

import websockets

WS = "ws://127.0.0.1:9222/session"
LOCK = "/home/moyamryia/.browser/control.lock"
STATE = "/home/moyamryia/.browser/watch_last.json"
FLAG = "/home/moyamryia/.browser/watch_enabled"
SPOOL = ("/home/moyamryia/astrbot/data/plugin_data/"
         "astrbot_plugin_passthrough/push_spool")
STAGING = "/srv/agent-staging"
POLL = 3.0
SETTLE = 1.5
MIN_PUSH_GAP = 5.0
SKIP_PREFIX = ("about:", "chrome:", "file:")


class BiDi:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.session = None

    async def call(self, method, params=None):
        self.seq += 1
        msg = {"id": self.seq, "method": method, "params": params or {}}
        if self.session:
            msg["session"] = self.session
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            m = json.loads(raw)
            if m.get("id") == self.seq:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})


class Lock:
    def __init__(self, path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a", encoding="utf-8")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False

    def release(self):
        if self.fh:
            self.fh.close()
            self.fh = None


async def with_session(fn):
    async with websockets.connect(WS, max_size=200 * 1024 * 1024) as ws:
        b = BiDi(ws)
        r = await b.call("session.new", {"capabilities": {}})
        b.session = r["sessionId"]
        try:
            return await fn(b)
        finally:
            try:
                await b.call("session.end")
            except Exception:
                pass


async def page_info(b):
    tree = await b.call("browsingContext.getTree")
    ctx = (tree.get("contexts") or [{}])[0].get("context")
    if not ctx:
        return {}
    r = await b.call("script.evaluate", {
        "expression": "JSON.stringify({url: location.href, title: document.title})",
        "target": {"context": ctx}, "awaitPromise": False,
        "resultOwnership": "none"})
    try:
        return json.loads((r.get("result") or {}).get("value") or "{}")
    except ValueError:
        return {}


async def snapshot():
    async def fn(b):
        tree = await b.call("browsingContext.getTree")
        ctx = (tree.get("contexts") or [{}])[0].get("context")
        r = await b.call("browsingContext.captureScreenshot", {"context": ctx})
        return base64.b64decode(r["data"])
    return await with_session(fn)


def push_shot(png: bytes, text: str) -> None:
    os.makedirs(STAGING, exist_ok=True)
    os.makedirs(SPOOL, exist_ok=True)
    path = os.path.join(STAGING, f"watch-{int(time.time())}.png")
    with open(path, "wb") as f:
        f.write(png)
    name = f"watch-{int(time.time())}-{os.getpid()}.json"
    tmp = os.path.join(SPOOL, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text, "image": path}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(SPOOL, name))


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


async def tick(state: dict) -> dict:
    lock = Lock(LOCK)
    if not lock.acquire():
        return state
    try:
        info = await with_session(page_info)
    except Exception as e:
        print(f"[watch] 读取失败: {e}", file=sys.stderr, flush=True)
        return state
    finally:
        lock.release()

    url = info.get("url") or ""
    if not url or url == state.get("url"):
        return state
    title = (info.get("title") or "").strip()
    print(f"[watch] URL 变动 -> {url}", file=sys.stderr, flush=True)
    state = {**state, "url": url, "title": title, "ts": time.time()}

    if url.startswith(SKIP_PREFIX):
        save_state(state)
        return state
    now = time.time()
    if now - state.get("last_push", 0) < MIN_PUSH_GAP:
        save_state(state)
        return state

    await asyncio.sleep(SETTLE)
    lock = Lock(LOCK)
    if not lock.acquire():
        save_state(state)
        return state
    try:
        png = await snapshot()
        push_shot(png, f"🔔 浏览器跳转：{title or url}\n{url}")
        state["last_push"] = time.time()
        print("[watch] 已推送截图", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[watch] 截图/推送失败: {e}", file=sys.stderr, flush=True)
    finally:
        lock.release()
    save_state(state)
    return state


async def main() -> int:
    state = load_state()
    if not state.get("url"):
        lock = Lock(LOCK)
        if lock.acquire():
            try:
                info = await with_session(page_info)
                if info.get("url"):
                    state = {"url": info["url"],
                             "title": info.get("title") or "",
                             "ts": time.time()}
                    save_state(state)
            except Exception:
                pass
            finally:
                lock.release()
    print(f"[watch] 启动，当前记录: {state.get('url') or '(无)'}",
          file=sys.stderr, flush=True)
    while True:
        if os.path.exists(FLAG):
            try:
                state = await tick(state)
            except Exception as e:
                print(f"[watch] 循环异常: {e}", file=sys.stderr, flush=True)
        await asyncio.sleep(POLL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
