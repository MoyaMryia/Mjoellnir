# Lab2 收尾小结：在 pi 上实现「算力换智力」

> 一句话：做了一套可用的 `/goal` 长程目标原语（计划 → 搜索树 → 择优/回退 → 独立核验），
> 并用客观竞赛题做了评测；**核心发现是"换模型 > 写 harness"，脚手架真正不可替代的地方是核验，而非探索。**

---

## 1. 交付物

| 东西 | 位置 | 说明 |
|---|---|---|
| `/goal` 扩展 | `devtools/pi-goal/`（软链 `~/.pi/agent/extensions/goal`） | 长程目标 + 独立裁判 + 搜索树 |
| `web_search` / `web_fetch` | `devtools/pi-search/`（软链 `~/.pi/agent/extensions/search`） | 真浏览器（Bing/Sogou）+ Firefox session |
| 冷启动基准 | `devtools/pi-goal/bench/startup.py` | 硬指标：交互首屏 < 300ms |
| 客观评测 harness | `devtools/pi-goal/bench/contest/` | 竞赛/CTF 题，pass/fail |
| 设计文档 | `docs/lab2-goal.md` | 逐切片设计 |

## 2. 架构

**一个裁判原语，三个时点复用**（`judge.ts::runJudge` → 全新上下文、只读的 pi 子进程）：

```
assessor（pre-flight）   独立评估任务、批思考预算、出 direct/tree 计划（硬性封顶）
evaluator（mid-flight）  给 swarm 候选打分、择优
auditor（post-flight）   对抗式验收（"证明它没做完"；证据不足即未完成）
```

**goal 生命周期**：`objective + constraints + checks(oracle) + budget` → 自驱循环（`turn_end` 续跑）→ 完成 = **checks 通过 ∧ 独立审计确认**。

**tree 流程**：`assessor` 判 tree → `search_candidates` 沿角度并行起 N 个独立 solver → evaluator 打分 → **多轮 refine**（把 best + 评语喂回）→ **没改善就回退保留上一版** → 主 agent 基于 best 深化。

**轻量契约**：扩展工厂只注册；无 active goal 时 `turn_end` O(1) 返回；子进程只在完成/tree 时才起。冷启动实测 ~200–230ms（PASS）。

## 3. 关键设计决策

- **`/goal` 而非 `/effort`**：outcome（目标+预算+验收+停止条件）比过程旋钮更适合已经自适应的 agent。
- **effort 由 assessor 推断，不是输入档位**：让独立裁判回答"这题该花多少算力、要不要展开树"。
- **验证腿是自研增量**：Codex 的 `ext/goal` 只有 objective+budget+自审，没有可执行验收；我们加了 `checks`(oracle) + 独立 auditor。
- **`checks` = shell 命令 + 极薄 expect**，不做 DSL。
- **角色模型复用 `/setagent`**：四次角色（assessor/evaluator/auditor/solver）读 `~/.pi/agent/agents/<role>.md` 的 `model`/`thinking`；`/goal roles` 查看。

## 4. 实验与发现

**A. 端到端真实案例**：Personal Agent 调研报告 → 5/20 轮 `satisfied`；assessor 判 tree，3 候选 88/83/74，选 best，主 agent 用 `web_search` 补真实来源，7/7 checks + 独立审计通过。

**B. 客观评测 harness**（`bench/contest/`）：自出题、**答案每次随机**、**关搜索**、**外部验证**（关键：`/goal` 的 checks 存在 `.pi/goals/*.json`，agent 能读，所以答案绝不能写进 checks）。

**C. 结果**：

| base 模型 | crypto | inversion | stego | subarrays |
|---|---|---|---|---|
| 强（deepseek-flash） | ✅ | ✅ | ✅ | ✅ |
| 弱（north-mini-code:free） | ✅ | ✅ | ❌ | ❌ |

→ **强模型全部一次做对** ⇒ 多探索无收益；**弱模型才在 stego/subarrays 上出现失败边缘** ⇒ effort 只在这一带可能有价值。

**D. 核心结论**：

> 在可一次求解的任务上，"算力换智力"的收益**远低于换个更好的模型**；脚手架真正不可替代的是**核验**（`checks` + 审计），不是**探索**（swarm/tree）。
> 这就是 bitter lesson 在小尺度上的重演；也是"agent 科研难量化"的实证——难点不在逻辑，而在**够不着合适的对照组**（免费额度 50/天，付费又觉得为"弱模型实验"不值）。

## 5. 未做

- **代码类 tree（worktree 隔离 + 每候选跑 checks）**——最重的一块，已决定不做。
- **§2.2 上下文工程**（向量库/知识库检索）——lab2 自选，本场景不缺（有文件系统 + `web_search`）。
- **§2.3 完整 direct/low/high 三方对照 + token 记账**——harness 就绪，受额度/模型限制未跑完。
- 多 auditor 投票、子目标、frontier 跨轮持久化。

## 6. 怎么用

```
/goal <objective> [--checks <json>]    新建并当场开跑
/goal show|plan|tree|roles|pause|resume|clear|checks|budget
```
工具（模型侧）：`get_goal` / `create_goal` / `update_goal` / `run_checks` / `search_candidates`
角色模型：`/setagent auditor deepseek/deepseek-flash:high`
评测：`cd devtools/pi-goal/bench/contest && python3 run.py <challenge> --mode direct|tree [--model ...]`

## 7. 最终判断

lab2 §2.1（算力换智力循环：effort/预算、搜索树、评估择优、保留 best、没改善回退、停止条件）**已实现并跑通**；核验腿（oracle+审计）是超出参考实现的增量。§2.2 未做（自选），§2.3 做了客观 harness 并得到比"打分对比"更硬的结论。
