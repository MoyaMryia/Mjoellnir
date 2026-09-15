# Mjoellnir · 会成长的个人助手

一个**常驻远程机器、通过 QQ 对话**的个人助手（南京大学 GSE Lab1 的实现）。

你用 QQ 发一句话，它替你在后台跑脚本、写草稿、查课表、办事务，把过程与结果推回 QQ；需要你拍板的危险动作，它会先问过你再动手。

> 名字取自北欧神话里的雷神之锤 Mjölnir——工具是手的延伸，握锤的始终是你。

## 它长什么样

```
QQ（手机 / 桌面）
  │  发消息
  ▼
NapCat（QQ 协议端） ──反向 WS──▶ AstrBot
                                 │  astrbot_plugin_passthrough（纯透传，不走自带 LLM）
                                 ▼
                          pi 网关  devtools/pi_gateway.py
                                 │  每个会话一个常驻 `pi --mode rpc` 进程
                                 ▼
                          pi 智能体（低权限账户运行）
                                 │  调工具
                                 ▼
                    agent-tools 工具池（危险动作经审批闸门；特权调用经 sudo 包装器）
```

设计取舍：

- **AstrBot 只做传输层**。插件把 QQ 消息透传给网关、把回复发回聊天；不经过 AstrBot 的 LLM/Agent。这样"谁在说话、说了什么"完全可控。
- **决策全在 pi**。网关为每个会话维护一个常驻 `pi --mode rpc` 子进程，同会话串行、不同会话并行，空闲回收、会话落盘续接。
- **权限切分**。网关、工具池、密钥、批准库属于所有者账户；pi 以独立低权限账户运行，读不到密钥、改不了工具、动不了批准库。
- **危险动作要人批准**。发邮件、提交表单、修改资料等会被"审批闸门"拦下并在 QQ 里问你：回「批准」才执行，回「拒绝 <理由>」它会按理由改。审批是内容哈希绑定、单次消费、有过期时间的。

## 目录结构

```
.
├── lab1.md                                 # 实验说明（"会成长的个人助手"）
├── devtools/
│   ├── pi_gateway.py                       # 网关：会话管理 / 命令 / 审批回环 / 进度推送
│   ├── mock_llm.py                         # 本地假后端，用于离线联调插件与网关
│   ├── mock_turns.example.json             # 假后端的脚本化回复样例
│   ├── assistant/
│   │   ├── AGENTS.md                       # 助手的行为约定（说话风格 / 硬规矩 / 工具发现）
│   │   ├── mail_poll.py                    # 邮箱轮询：拉新邮件 → 过滤 → 推送
│   │   ├── mail_filter.json                # 邮件过滤规则（keep/skip 白黑名单）
│   │   ├── kb_maintenance.py               # 每日维护：过一遍资料库与近期邮件，写增量笔记
│   │   └── browser_watch.py                # 浏览器网址监测：跳转即截图推 QQ
│   └── astrbot_plugin_passthrough/         # AstrBot 透穿插件（v0.6）
│       ├── main.py                         # 消息透传 + 主动推送（文本/图片，失败重试）
│       ├── _conf_schema.json               # 插件配置项（后端地址/token/白名单/推送目标）
│       ├── metadata.yaml
│       └── README.md                       # 插件自身的说明
└── .gitignore
```

> 工具池（邮箱、报修、课表、馆藏、资料库、浏览器、课程自动学习等）在另一个仓库
> **agent-tools** 中，本仓库只放"助手本体"（网关、插件、约定与常驻脚本）。

## 组件

### `devtools/pi_gateway.py` — 网关

AstrBot 插件与 pi 之间的中间层，HTTP JSON。

- `POST /chat`：`{"session_id","user_id","text","umo"}` → `{"reply": "..."}`
- `GET /health`：健康检查
- 每个 `session_id` 一个常驻 `pi --mode rpc --session-id <sid>`，会话状态落盘、空闲回收、下次自动续接
- 把 QQ 命令翻译成 pi RPC：
  - `/new`（`/reset`、`/重置`）新会话
  - `/model [provider/model]` 查看/切换模型，切换后记住
  - `/status` 会话状态、`/compact` 压缩上下文、`/abort` 中断
  - `/name` 命名会话、`/help` 帮助 + pi 自定义命令清单
  - `/reload` 重启 pi 进程（加载新写的扩展，二次确认）
  - `/browser grants|revoke|watch` 查看/撤销浏览器交互授权、开关网址监测
- **审批回环**：工具被闸门拦下时输出 `approval_required`，网关识别后追加提示；你在 QQ 回「批准」即调用 `approve_exec` 原样重放被拦的动作，回「拒绝」则把理由转达给 pi
- **进度推送**：把工具调用/返回（以及特权脚本名）写进推送队列，由插件发到 QQ
- 需要 `Authorization: Bearer <token>`（防止 Agent 伪造确认）

### `devtools/astrbot_plugin_passthrough/` — 透穿插件

- 按会话白名单触发，命中即 `should_call_llm(False)` 抑制 AstrBot 自带 LLM
- 把消息发给网关、把回复发回原会话
- **主动推送队列**：网关/脚本往 `push_spool/` 写 JSON，插件轮询发送（支持文本 + 图片，失败自动重试）

### `devtools/assistant/` — 常驻脚本与约定

- **AGENTS.md**：助手的人设与硬规矩（产出物中性专业、工具用 `find_tools` 发现、资料库读写规则等）
- **mail_poll.py**：定时拉新邮件，按 `mail_filter.json` 过滤后推送（不重复处理）
- **kb_maintenance.py**：每日过一遍资料库与近期邮件，写带出处的增量笔记
- **browser_watch.py**：轮询当前网页 URL，跳转即截图推 QQ（浏览器被占用时自动跳过）

## 依赖

- **Pi agent**（`--mode rpc`）作为对话与工具执行的大脑
- **AstrBot** + **NapCat** 作为 QQ 接入（仅传输）
- **agent-tools** 工具池 + 特权包装器（`sudo` 白名单）+ 沙箱暂存目录
- 浏览器能力：系统 **Firefox** + 其 **WebDriver BiDi** 远端代理（有头、常驻、可 VNC 围观）

## 部署（概要）

在助手主机上：

1. 部署 `pi_gateway.py`，用 systemd 常驻，token 放在仅所有者可读的环境文件里
2. AstrBot 装入透穿插件并配置后端地址/token/白名单/推送目标
3. 定时任务用于邮箱轮询、资料库每日维护等
4. 浏览器栈（Xvfb / 窗口管理器 / Firefox / VNC / noVNC）各自以 systemd 常驻

具体路径与账户名因机器而异，此处用占位说明。

## 安全边界与已知限制

- pi 以低权限账户运行：**看不到密钥、改不了工具池、动不了批准库**；需要特权效果时只能经白名单脚本，且危险动作必须你批准
- 审批记录绑定动作内容（哈希）、单次有效、有过期时间
- 沙箱模式下，给工具的文件必须落在暂存目录内
- 已知限制：
  - 同一所有者账户下的脚本（例如你自己手动跑）不受闸门约束——闸门防的是"Agent 越权"，不是"所有者本人"
  - 浏览器栈重启会丢失网页登录态（CAS 会话 cookie 不持久）；需要时可自动重注入登录态

## 许可

[GPL-3.0](LICENSE)
