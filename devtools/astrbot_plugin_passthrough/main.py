"""透穿插件：QQ 消息 -> 外部后端(HTTP) -> 回复发回聊天。

AstrBot 在这里只做传输层：命中时调用 should_call_llm(False)，不走自带 LLM。
触发范围由 session_whitelist 控制（留空=所有会话）：
群聊填群号，私聊填用户 ID（session_id = get_group_id() or get_sender_id()）。
群聊不要求 @/唤醒词，白名单里的群每条消息都转发。

后端契约（POST backend_url，JSON）:
    请求 {"session_id": "...", "user_id": "...", "text": "..."}
    响应 {"reply": "..."} 或 {"text": "..."} 或纯文本
后端会话粒度: 私聊按会话；群聊按 (群, 发言人) 分，避免多人上下文串台。
"""
import asyncio
import json
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_passthrough"
DATA_DIR = Path("data/plugin_data") / PLUGIN_NAME


@register(PLUGIN_NAME, "moyamryia",
          "透穿：把消息转发给外部后端并把回复发回聊天（不走自带 LLM，按会话白名单触发）",
          "0.5.1")
class PassthroughPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session_whitelist = [
            str(sid).strip()
            for sid in (config.get("session_whitelist") or [])
            if str(sid).strip()
        ]
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, str] = {}
        self.push_enabled = bool(config.get("push_enabled", True))
        self.push_target = str(config.get("push_target", "") or "").strip()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._sessions_file = DATA_DIR / "sessions.json"
        self._load_sessions()
        logger.info("[passthrough] 初始化完成 backend=%r whitelist=%s push=%s",
                    str(self.config.get("backend_url", "") or ""),
                    self.session_whitelist or "(全部会话)",
                    self.push_target or "(未配置)")

    # ---------- 主动推送 ----------

    async def initialize(self) -> None:
        if self.push_enabled:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info("[passthrough] 推送循环已启动 target=%r", self.push_target)

    async def terminate(self) -> None:
        task = getattr(self, "_push_task", None)
        if task:
            task.cancel()

    async def _push_loop(self) -> None:
        spool = DATA_DIR / "push_spool"
        spool.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                for f in sorted(spool.glob("*.json")):
                    item = {}
                    try:
                        item = json.loads(f.read_text("utf-8"))
                    except Exception as e:
                        logger.warning("[passthrough] 推送文件损坏 %s: %s", f.name, e)
                    target = str(item.get("umo") or self.push_target or "").strip()
                    text = str(item.get("text") or "")
                    if not (target and text):
                        logger.warning("[passthrough] 推送目标为空，丢弃 %s", f.name)
                        f.unlink(missing_ok=True)
                        continue
                    try:
                        found = await self.context.send_message(
                            target, MessageChain().message(text))
                    except Exception as e:
                        found = False
                        logger.warning("[passthrough] 推送异常 %s: %s", target, e)
                    if found:
                        f.unlink(missing_ok=True)
                    else:
                        logger.warning("[passthrough] 推送失败(未找到平台?) %s", target)
                        try:
                            f.rename(f.with_suffix(".err"))
                        except OSError:
                            f.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("[passthrough] 推送循环异常: %s", e)
            await asyncio.sleep(2)

    # ---------- 会话映射 ----------

    def _load_sessions(self) -> None:
        try:
            if self._sessions_file.exists():
                self._sessions = json.loads(self._sessions_file.read_text("utf-8"))
        except Exception as e:
            logger.warning("[passthrough] 读取会话映射失败: %s", e)

    def _save_sessions(self) -> None:
        try:
            self._sessions_file.write_text(
                json.dumps(self._sessions, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            logger.warning("[passthrough] 写会话映射失败: %s", e)

    def _session_key(self, event: AstrMessageEvent, sender: str) -> str:
        umo = event.unified_msg_origin
        return umo if event.is_private_chat() else f"{umo}:{sender}"

    def _session_id(self, event: AstrMessageEvent, sender: str) -> str:
        key = self._session_key(event, sender)
        sid = self._sessions.get(key)
        if not sid:
            sid = f"qq-{key}"
            self._sessions[key] = sid
            self._save_sessions()
        return sid

    # ---------- 白名单 ----------

    def _is_session_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.session_whitelist:
            return True
        session_id = event.get_group_id() or event.get_sender_id()
        return str(session_id) in self.session_whitelist

    # ---------- 事件入口 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        if not self._is_session_allowed(event):
            return

        # 命中：不要让 AstrBot 自带 LLM 也来插一脚
        event.should_call_llm(False)
        sender = event.get_sender_id()

        sid = self._session_id(event, sender)
        lock = self._locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            await event.send(event.plain_result("上一条还在处理中，稍等。"))
            return

        async with lock:
            ack = str(self.config.get("ack_text", "") or "").strip()
            if ack:
                await event.send(event.plain_result(ack))
            try:
                await event.send_typing()
            except Exception:
                pass
            reply = await self._call_backend(sid, sender, text,
                                             event.unified_msg_origin)
            if reply:
                await event.send(event.plain_result(reply))

    # ---------- 后端调用 ----------

    async def _call_backend(self, sid: str, user_id: str, text: str,
                            umo: str = "") -> str:
        url = str(self.config.get("backend_url", "") or "").strip()
        if not url:
            return "后端未配置（backend_url 为空）。"
        timeout = int(self.config.get("timeout_seconds", 180) or 180)
        headers = {"Content-Type": "application/json"}
        token = str(self.config.get("api_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {"session_id": sid, "user_id": user_id, "text": text,
                   "umo": umo}
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as sess:
                async with sess.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("[passthrough] 后端 HTTP %s: %.200s", resp.status, body)
                        return f"后端错误（HTTP {resp.status}）"
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        return body
                    if isinstance(data, dict):
                        return str(data.get("reply") or data.get("text") or "")
                    return str(data)
        except asyncio.TimeoutError:
            return f"后端超时（{timeout}s）。"
        except Exception as e:
            logger.warning("[passthrough] 调用后端失败: %s", e)
            return f"后端不可用：{e}"
