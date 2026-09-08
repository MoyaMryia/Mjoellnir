# astrbot_plugin_passthrough

把 QQ 消息透传给外部后端，把后端回复发回聊天。AstrBot 只做传输层，不走自带 LLM/Agent。

## 触发规则（会话白名单）

`session_whitelist` 为列表，语义与 `astrbot_plugin_video_analysis` 一致：

- **留空**：所有会话都转发。
- **群聊**：填群号；命中即转发，**不要求 @/唤醒词**。
- **私聊**：填用户 ID。

判定：`session_id = event.get_group_id() or event.get_sender_id()`。

命中后调用 `should_call_llm(False)`，抑制 AstrBot 自带 LLM；未命中的消息不干预。

## 后端契约

```
POST <backend_url>
Content-Type: application/json
Authorization: Bearer <api_token>   # token 非空时

{"session_id": "qq-<...>", "user_id": "<QQ号>", "text": "<消息原文>"}
```

响应：

```json
{"reply": "要发回聊天的文本"}
```

也接受 `{"text": "..."}` 或纯文本响应体。

## 主动推送

插件每 5 秒扫 `data/plugin_data/astrbot_plugin_passthrough/push_spool/*.json`，
把 `{"text": "..."}` 发到 `push_target`（unified_msg_origin，如
`default:FriendMessage:2185281742`）。发送成功后删除文件，失败改名 `.err`。

邮件轮询器（`~/assistant/mail_poll.py` + systemd timer）负责往 spool 里写新邮件通知。

## 命令

插件不解释任何命令，全部原样转发给后端。后端（pi 网关）支持的命令直接发即可：
`/new`、`/reset`、`/model`、`/status`、`/compact`、`/abort`、`/help`。

## 配置

见 `_conf_schema.json`：`backend_url` / `api_token` / `session_whitelist` / `ack_text` / `timeout_seconds`。
