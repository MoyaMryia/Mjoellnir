# `/goal` 设计（Lab2 · Deeper Research）

一个长程目标原语，做在**本机 pi（coding agent）**里，作为一个**轻量扩展**。
目标：给"算力换智力"一个**可控、可停、可续、可计量、可验证**的循环。

参考并大量借用 OpenAI Codex 的 `ext/goal`（Apache-2.0，需 NOTICE 归属）。我们的增量（标 **[新]**）：
可执行验收（oracle）、独立上下文完成审计、机械进度提示、把评估与运行合并。

---

## 0. 形态与文件

```
~/.pi/agent/extensions/goal/
  index.ts      # 只做注册：3 tools + /goal 命令 + turn_end 钩子（薄）
  state.ts      # goal 读写 + appendEntry 会话指针
  oracle.ts     # 跑 checks
  auditor.ts    # 起独立审计子 agent、解析裁决
  prompts.ts    # 内嵌 continuation / budget / objective_updated 模板
~/.pi/agent/agents/auditor.md      # [新] 完成审计 agent（只读）
<project>/.pi/goals/<id>.json      # goal 本体（可读 / 可改 / 可 commit）
```

**存储（已定）**：项目级 `.pi/goals/<id>.json`。
会话 → goal 的指针放 `pi.appendEntry()`（branch-aware，不进模型上下文）。

---

## 1. 数据模型

```ts
type GoalStatus = "active" | "paused" | "blocked" | "satisfied" | "exhausted";

type Goal = {
  id: string;
  sessionId: string;
  objective: string;             // 当 data，XML 转义；超长物化到文件
  constraints: string[];         // [新] 硬约束 / 反目标，审计一并验
  checks: Check[];               // [新] oracle，可空
  budget:  { turns: number; tokens?: number; minutes?: number };   // turns 必填
  spent:   { turns: number; tokens: number; minutes: number };
  status: GoalStatus;
  origin: "user" | "automatic";
  createdAt: number; updatedAt: number;
  progress: { lastTreeHash?: string; turnsSinceChange: number };   // [新] 机械提示
  audit?: { done: boolean; missing: string[]; evidence: string[]; judge: string; ts: number };
  log: LogEntry[];               // append-only：每个 goal-turn 一条
};

// 极薄断言（不是 DSL）
type Check = {
  name: string;
  cmd: string;                   // shell，cwd = 项目根
  expect:
    | { exit: number }                                   // 默认 exit==0
    | { metric: string; op: ">="|"<="|"=="; target: number }  // 命令输出含 KEY=VALUE
    | { stdoutMatches: string };                         // 正则
  timeoutSec?: number;           // 默认 300
};
```

`metric` 约定：检查命令在 stdout 打印 `KEY=VALUE` 行，`expect.metric` 取该 KEY 的数值断言。

---

## 2. 状态机

```
create / resume(user) ─────────────────────▶ active
active ──(model complete ∧ checks 通过 ∧ auditor done)──▶ satisfied
active ──(system: turns/tokens/minutes 耗尽)────────────▶ exhausted
active ──(model: 同一阻塞连续 ≥3 goal-turn)─────────────▶ blocked
active ◀──(user pause/resume)──▶ paused / blocked
satisfied|exhausted ──(user 新目标)─────────────────────▶ 新 goal
任意 ──(user clear)─────────────────────────────────────▶ 无
```

权限纪律（抄 Codex）：

- `create_goal` 只在**显式请求**时；不从不相关任务推断。
- `update_goal` 只能 `complete / blocked / paused`；**预算与恢复归系统 / 用户**。
- **[新] `complete` 只是"提议"**：`checks 通过 ∧ auditor done` 才真的 `satisfied`。

---

## 3. 自驱循环（`turn_end`）

```
session_start → 读 appendEntry 指针 → 载入 <project>/.pi/goals/<id>.json

turn_end:
  无 goal / status ≠ active              → return（内存 O(1)）
  有待处理用户输入（deferral）            → return（不抢用户）
  budget.turns 已耗尽                     → status=exhausted，注入 budget_limit，return
  否则:
    spent.turns++
    更新 progress：treeHash = hash(git status --porcelain)
      hash 变 → turnsSinceChange = 0；不变 → tradesSinceChange++
    注入 continuation（context_edit，非伪造 user 消息）→ return { continue: true }
```

### continuation 注入内容（每轮）

- objective（当 data，XML 转义）+ constraints
- **进度自汇报要求**：把上一轮分类为 `progress / verified wait / no progress`，并说出下一步动作
- 机械提示：`worktree 自上次变化以来已过 N 轮`（帮它诚实；**不自动停**）
- **剩余轮数**：`已用 X / 共 Y 轮，剩 Z 轮`；`Z ≤ 5` 时追加"优先收尾 / 聚焦最关键的未完成项"
- 从 Codex `continuation.md` 移植的行为约束：跨轮保持目标不缩水、work from evidence、completion audit、blocked audit、fidelity/反 Goodhart

**停滞处理（已定）**：不机械自动停；由模型自汇报。硬停只靠 turn 预算。

---

## 4. 完成流程（oracle + 独立审计）

模型调 `update_goal({status:"complete"})` 时，**由扩展强制执行**（模型不能跳过）：

```
1. 跑 checks（oracle.ts，child_process + timeout，cwd=项目根）
   任一失败 → 把失败作为 tool 结果打回，保持 active
2. 无 checks → 跳过
3. 起独立 auditor（subagent 机制）
     上下文：全新（看不到 worker 推理/自述）
     工具：只读
     输入：objective + constraints + checks + 当前状态证据
     输出：{ done, missing[], evidence[], judge }
     独立性：默认靠"新上下文"；模型可配置（need 换盲点时才换模型）
4. done ∧ checks 全过 → satisfied
   否则 → missing[] 打回，保持 active
```

审计要点（从 Codex `completion.md` 精神移植）：

- **对抗式**：任务是"证明它**没**完成 / 找出最强的不完成理由"。
- **逐条 requirement 找权威证据**；证据不足 / 间接 ⇒ 未完成。
- 不许把目标缩水；不许拿"没发现明显遗留"当完成。
- auditor 由扩展选，不由 worker 选（防挑软审）。

---

## 5. 工具 / 命令面

| 面 | 东西 |
|---|---|
| 模型 tool | `get_goal()`；`create_goal({objective, checks?, constraints?, budget?})`；`update_goal({status})`；`run_checks()` |
| 人 command | `/goal <objective>`；`/goal`（看）；`/goal checks`（看/跑）；`/goal pause\|resume\|clear`；`/goal budget <turns>` |

---

## 6. 预算与计量

- **turns（主）**：goal 自动续跑计数；每轮告知剩余。默认 20。
- tokens / minutes（可选）：tokens 按 goal-turn 的 session usage 增量累计；subagent 的 usage 计入。minutes 按 active 墙钟。
- 任一触顶 → `exhausted` + `budget_limit` steering。
- **扩展预算只由用户** `/goal budget`。

---

## 7. 轻量契约（守住冷启动）

baseline（本机）：交互首屏 ~0.21s；`pi --help`（含扩展注册）~0.21s。

- import 阶段：零重依赖、prompt 内嵌，**只注册**。
- `session_start`：一个小文件读。
- `turn_end`：内存 O(1)；仅状态变化时写盘。
- checks / auditor：**仅在 `complete` 提议或 `run_checks`** 时才起子进程。
- 不碰 `tool_call`。
- 硬指标：交互首屏 < 300ms，扩展注册增量 < 50ms；附 `bench/startup.sh`（pty 探测）。

---

## 8. MVP 边界

**做**：objective + shell checks + `get/create/update_goal` + `/goal` + `turn_end` 续跑 + oracle + 单 auditor（新上下文）+ turn 预算 + 项目级文件持久化 + 移植模板。

**不做（后续）**：frontier / 搜索树、worktree 隔离、多 auditor 投票、自适应预算升级、子目标、换模型审计的默认化。

---

## 9. 已定决策

1. 存储：项目级 `.pi/goals/*.json`（可 commit）。
2. 无 checks 时：**仍走独立 auditor**（不退回自审）。
3. auditor 独立性：默认**新上下文**，模型可配置（非必须换模型）。
4. 预算：**auto-turn 为主，默认 20，每轮告知剩余**。
5. 停滞：模型**自汇报**，不机械自动停；给机械提示当参考。
6. checks：**shell 命令 + 极薄 expect**，不做 DSL。
