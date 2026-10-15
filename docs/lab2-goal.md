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

---

## 10. 实现进度

代码在 `devtools/pi-goal/`，软链 `~/.pi/agent/extensions/goal`。

| 切片 | 内容 | 状态 |
|---|---|---|
| 1 | 骨架 + 状态/文件持久化 + `/goal` + `get_goal` | ✅ `522ba58` |
| 3 | 自驱循环（`turn_end` 续跑 + `before_agent_start` 感知）+ turn 预算 + `create_goal`/`update_goal` | ✅ `6315827` |
| 4 | oracle：`oracle.ts`（shell checks + expect）+ `run_checks` 工具 | ✅ `a697997` |
| 5 | 独立审计：`auditor.ts`（fresh-context 只读子进程）；`complete` 需 `checks ∧ audit` | ✅ `a697997` |
| 6 | 冷启动基准 `bench/startup.py`（硬指标：交互首屏 < 300ms） | ✅ |
| 7 | 独立裁判原语 `judge.ts` + assessor 出计划（`direct`/`tree` + 硬性封顶） | ✅ |
| 8A | `tree` 自动执行：`swarm.ts` 并行 solver + `evaluator.ts` 择优 → `best.md`；`search_candidates` 工具 | ✅ |

工具面（模型）：`get_goal`（带 plan/tree）/ `create_goal`（支持 `checks`）/ `update_goal` / `run_checks` / `search_candidates`。
命令面（人）：`/goal <objective> [--checks <json>]`、`/goal checks [set <json>]`、`/goal plan`、`/goal tree`、`pause|resume|clear|budget`。

---

## 11. 独立裁判 + 计划（切片 7）

把 auditor 的“独立能力”抽成一个通用原语，一个原语三个角色（对应三个时点）：

| 时点 | 角色 | 文件 | 干什么 |
|---|---|---|---|
| pre-flight | **assessor** | `assessor.ts` | 事前独立评估：这题该花多少算力、走 `direct` 还是 `tree`、开几个方向、怎么评估 |
| mid-flight | evaluator（切片 8） | — | 给候选打分、择优 |
| post-flight | **auditor** | `auditor.ts` | 完成后对抗式验收 |

三者都调 `judge.ts` 的 `runJudge()`：起一个全新上下文、只读的 pi 子进程，收回 assistant 文本并 `parseJsonObject`。独立性 = 新进程 + 新上下文 + 只读。

### 计划 schema

```jsonc
{
  "mode": "direct" | "tree",
  "rationale": "...",
  "branches": 1,      // 并行候选数
  "depth": 1,         // 迭代轮数
  "angles": ["..."],  // tree 时给出真正不同的方向
  "evaluator": { "kind": "checks" } | { "kind": "rubric", "rubric": "..." },
  "ts": 0
}
```

**“只允许开几个”的硬性封顶**（`state.ts`）：`MAX_BRANCHES=4`、`MAX_DEPTH=2`。
`clampPlan()` 在代码侧强制：assessor 说什么都不能越界；非法/缺失一律退化为 `direct`。
有可执行 `checks` 时，evaluator 强制为 `checks`（硬 oracle）。

建目标时自动跑一次 assessor（失败不阻断，退化为 `direct`）。`plan` 注入续跑提示：`direct` 照旧；`tree` 要求先对每个方向给出初步结论、比较后择优，并保留未选方向便于回退。

实测：开放调研任务 → `tree`（3 个真正不同的角度 + 详细 rubric）；琐碎文件任务 → `direct`（1×1，checks）。

---

## 12. Swarm：文本候选的搜索（切片 8A）

`tree` 计划的自动执行：

1. `search_candidates` 工具沿计划角度，并行起 N 个**独立、只读** solver（`runJudge`，无 bash，避免并发写），各产出一份 Markdown 候选，写入 `.pi/goals/<id>/branches/<bid>/answer.md`。
2. 独立 `evaluator.ts` 读入全部候选文件，按 objective +（rubric 或 checks）打 0–100 分，选出 best。
3. best 复制到 `.pi/goals/<id>/best.md`，返回给主 agent 深化、补证据。

硬性上限仍由 `plan`（已 clamp）保证：并行 ≤ `MAX_BRANCHES(=4)`。

实测（`node --experimental-strip-types` 直接单测 `runSwarm`）：2 个 solver 并行 26.8s 完成，evaluator 给 72/85 分并选出因果序角度的答案；pty 全流程：SQLite/DuckDB 任务 → 3 候选（80/85/89）→ 选 best → agent 接着真去跑基准深化。

---

## 13. 角色模型配置（复用 /setagent）（切片 9）

/goal 的四个角色（assessor / evaluator / auditor / solver）的 model + thinking **复用 subagent 的 agent 文件约定**：`~/.pi/agent/agents/<role>.md` 的 frontmatter `model` / `thinking`。

- 直接用现成命令改：`/setagent auditor deepseek/deepseek-flash:high`；`/agents` 里也能看到。
- `/goal roles` 查看各角色当前解析到的模型。
- 优先级：env `PI_GOAL_<ROLE>_MODEL` / `_THINKING` > agent 文件 > 继承。
- 代码：`roleconfig.ts::resolveRole()`；`judge.ts::runJudge()` 现支持 `thinking` 与 `extensions`。

**并行 solver / auditor / evaluator 现在能联网**：`runJudge` 通过显式 `-e <search扩展>` 给子进程挂上 `web_search` / `web_fetch`（`--no-extensions` 下 `-e` 仍生效），所以候选从一开始就带真实引用、审计能自己联网核验。非交互子进程用完会关掉浏览器，避免事件循环不退出。

**多轮 refine + “没改善就回退”（切片 8B，已做）**：`runSwarm` 现在一次调用跑完 `plan.depth` 轮（<= `MAX_DEPTH`）。round r>1 时把当前 best + evaluator 的评语喂回 solver 让其**改进**；每轮评估后门控：本轮最优 > 历史最优才覆盖 `best.md`，否则**回退保留上一版**并停止。`tree.history` 记录每轮 kept/rollback。实测：90→91 保留；100→100 回退。

**未做（后续）**：代码类 tree（worktree 隔离 + 每候选跑 checks）、多 auditor 投票、frontier 持久化、子目标。
