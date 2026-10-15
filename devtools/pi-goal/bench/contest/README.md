# contest — 客观评测 harness

用**竞赛/CTF 题**给 `/goal` 打一个**不靠 LLM 打分**的可信数字：pass/fail。

## 设计要点（为什么这样做）

1. **答案每次随机**（`gen.py` 现场生成 flag / 输入）→ 全网搜不到，`web_search` 作弊无意义。
2. **搜索关掉**（`PI_GOAL_NO_SEARCH=1`）→ 双保险。
3. **验证器在工作目录外**（`secret/` + `verify.py` 由 harness 调用）→ 关键：`/goal` 的 `checks` 存在 `.pi/goals/*.json`，**agent 能读到**，所以答案绝不能写进 checks（本 harness 里 `checks` 留空）。
4. **外部客观判定**：`verify.py` 用参考解 / 随机测试对比，打表也过不了（inversion 会现场重生成随机输入）。
5. **headless 驱动**：预置一个 active goal（`--session-id` 对齐），`pi -p` 跑自驱循环；harness 数 turns、计时。

## 用法

```bash
python3 run.py <challenge> [--mode auto|direct|tree] [--budget 12] [--model ...] [--timeout 900]
```

产物：`runs/<challenge>-<mode>-<ts>/`（`work/` 是 agent 的 cwd，`secret/` 外部，`agent.log`），以及汇总 `results.tsv`。

## 现有题目

| 题目 | 类型 | 判定 |
|---|---|---|
| `crypto` | CTF 解密（多层编码，flag 随机） | 工作目录里是否出现正确 flag |
| `inversion` | 算法/性能（N=2×10^5 逆序对，朴素 O(N²) 超时） | 5 组随机测试 + 大数据计时，对参考解 |

## 首批战绩（2026-10-15）

```
PASS  crypto     direct  turns=5  26s
PASS  inversion  direct  turns=3  25s
PASS  crypto     tree    turns=5  24s
PASS  inversion  tree    turns=6  32s
```

**结论**：这两题 direct 已经全过，tree 没买到任何提升、还多花轮次——正是 Lab2 §2.3 警告的"多花 token 效果不如直接输出"。要有信号，必须上 **direct 会挂的题**。

## 已知限制

- tree/swarm 是为**文本候选**设计的；对"必须真的跑代码"的题目偏别扭（这正是 worktree 隔离要补的洞）。
- 沙箱只做了"关 search + 随机答案"，没做 OS 级隔离（`bwrap`）；agent 理论上能读 `secret/`（但它没动机，checks 也不指向那里）。
