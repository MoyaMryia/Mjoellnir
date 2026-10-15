/**
 * Swarm：文本候选的并行搜索 + 多轮 refine（切片 8A + refine）。
 *
 * 每轮：沿计划角度并行起 N 个独立 solver → 独立 evaluator 打分 → 取本轮最优。
 * 轮间门控（Lab2 的“没改善就保留之前更好的版本”）：
 *   - 本轮最优 > 历史最优 → 保留（覆盖 best.md）
 *   - 否则 → 回退，保留上一版，并停止（不浪费预算继续跑）
 * 最多跑 plan.depth 轮（硬性 <= MAX_DEPTH）。
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { runEvaluate } from "./evaluator.ts";
import { runJudge } from "./judge.ts";
import { resolveRole, searchExtensionPath } from "./roleconfig.ts";
import { bestAnswerPath, branchAnswerPath, MAX_DEPTH, type Branch, type Goal, type GoalTree, type TreeHistoryEntry } from "./state.ts";

const SOLVER_SYSTEM = `You are one independent solver working on a single assigned angle of a task.
Commit to your assigned angle; do not hedge back toward the obvious answer. Produce a complete, self-contained candidate answer in Markdown.
You CAN search the web with the web_search tool and read pages with web_fetch. Use them to ground concrete claims; cite URLs inline for any external fact, and mark anything you could not verify as \u3010推测/未核实\u3011.
Be concrete and specific: state claims precisely and give concrete examples, numbers, or reasoning where relevant.
If a CURRENT BEST CANDIDATE is provided, your job is to MATERIALLY IMPROVE it (fix the specific gaps named in the critique), not to restate or paraphrase it.
Do not ask questions. Do not mention that other solvers exist. Output only the candidate answer.`;

const SOLVER_TIMEOUT_MS = 300_000;

function buildSolverTask(goal: Goal, angle: string, refineBase?: string, critique?: string): string {
  const parts = [
    "Solve the objective from exactly ONE assigned angle. Output your full candidate answer in Markdown.",
    `OBJECTIVE: ${goal.objective}`,
  ];
  if (goal.constraints.length) parts.push(`CONSTRAINTS:\n${goal.constraints.map((c) => `- ${c}`).join("\n")}`);
  if (goal.checks.length) {
    parts.push(`ACCEPTANCE CHECKS (your answer should help satisfy these):\n${goal.checks.map((c) => `- ${c.name}: ${c.cmd}`).join("\n")}`);
  }
  parts.push(`YOUR ANGLE (commit to it): ${angle}`);
  if (refineBase) {
    parts.push(
      "CURRENT BEST CANDIDATE — improve it, do not restate it:",
      "<<<CURRENT>>>",
      refineBase.slice(0, 12000),
      "<<<END>>>",
    );
    if (critique) parts.push(`INDEPENDENT JUDGE'S CRITIQUE OF IT:\n${critique.slice(0, 4000)}`);
    parts.push("Produce a materially better version that fixes the specific gaps above. Keep what already works; change what is weak.");
  }
  return parts.join("\n\n");
}

function readText(p: string): string | undefined {
  try {
    return fs.readFileSync(p, "utf-8");
  } catch {
    return undefined;
  }
}

export async function runSwarm(opts: {
  cwd: string;
  goal: Goal;
  signal?: AbortSignal;
  onProgress?: (text: string) => void;
}): Promise<GoalTree> {
  const { cwd, goal } = opts;
  const plan = goal.plan;
  if (!plan || plan.mode !== "tree") throw new Error("runSwarm: goal.plan.mode !== tree");

  const maxRounds = Math.max(1, Math.min(MAX_DEPTH, plan.depth || 1));
  const rc = resolveRole("solver");
  const exts = [searchExtensionPath()].filter((p): p is string => !!p);
  const emit = (t: string) => {
    try {
      opts.onProgress?.(t);
    } catch {
      /* ignore */
    }
  };

  const tree: GoalTree = goal.tree
    ? { ...goal.tree, branches: [...goal.tree.branches], history: [...(goal.tree.history ?? [])] }
    : { round: 0, branches: [], history: [] };
  const history = tree.history as TreeHistoryEntry[];
  const bestAbs = path.join(cwd, bestAnswerPath(cwd, goal.id));

  const n = Math.max(1, Math.min(plan.branches, plan.angles.length || plan.branches));
  const angles = plan.angles.length ? plan.angles.slice(0, n) : Array.from({ length: n }, (_, i) => `独立方案 ${i + 1}`);

  for (let r = tree.round + 1; r <= maxRounds; r++) {
    const refineBase = r > 1 ? readText(bestAbs) : undefined;
    const isRefine = !!refineBase;
    emit(`${isRefine ? "改进" : "展开搜索"}：round ${r}/${maxRounds}，${n} 个独立 solver 并行…`);

    const results = await Promise.all(
      angles.map(async (angle, i) => {
        const id = `r${r}b${i + 1}`;
        const abs = branchAnswerPath(cwd, goal.id, id);
        const rj = await runJudge({
          cwd,
          systemPrompt: SOLVER_SYSTEM,
          task: buildSolverTask(goal, angle, isRefine ? refineBase : undefined, isRefine ? tree.judge : undefined),
          tools: "read,grep,find,ls,web_search,web_fetch",
          model: rc.model,
          thinking: rc.thinking,
          extensions: exts,
          timeoutMs: SOLVER_TIMEOUT_MS,
          signal: opts.signal,
        });
        return { id, angle, abs, text: rj.text, error: rj.error };
      }),
    );

    const branches: Branch[] = [];
    for (const res of results) {
      const text = res.text.trim();
      let status: Branch["status"] = "done";
      if (!text) status = "failed";
      else {
        try {
          fs.mkdirSync(path.dirname(res.abs), { recursive: true });
          fs.writeFileSync(res.abs, text + "\n", "utf-8");
        } catch {
          status = "failed";
        }
      }
      branches.push({
        id: res.id,
        angle: res.angle,
        round: r,
        status,
        artifact: path.relative(cwd, res.abs),
        chars: text.length,
        ts: Date.now(),
      });
      emit(`  · ${res.id} ${status === "done" ? `完成（${text.length} 字）` : `失败${res.error ? `：${res.error}` : ""}`}`);
    }
    tree.branches.push(...branches);

    const ok = branches.filter((b) => b.status === "done");
    if (!ok.length) {
      tree.round = r;
      tree.judge = "本轮所有候选都失败。";
      history.push({ round: r, id: "-", score: 0, kept: false, reason: "本轮全部失败", ts: Date.now() });
      break;
    }

    emit(`评估 ${ok.length} 个候选（独立 evaluator）…`);
    const ev = await runEvaluate({
      cwd,
      goal,
      candidates: ok.map((b) => ({ id: b.id, angle: b.angle, path: b.artifact })),
      signal: opts.signal,
    });
    for (const b of branches) {
      const s = ev.scores.find((x) => x.id === b.id);
      if (s) b.score = s.score;
    }
    const roundBest = [...ok].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];
    const prevScore = tree.bestScore;
    const improved = prevScore === undefined || (roundBest.score ?? 0) > prevScore;

    if (improved) {
      tree.best = roundBest.id;
      tree.bestScore = roundBest.score;
      tree.bestRound = r;
      try {
        fs.mkdirSync(path.dirname(bestAbs), { recursive: true });
        fs.copyFileSync(path.join(cwd, roundBest.artifact), bestAbs);
        tree.bestPath = path.relative(cwd, bestAbs);
      } catch {
        /* ignore */
      }
      history.push({
        round: r,
        id: roundBest.id,
        score: roundBest.score ?? 0,
        kept: true,
        reason: prevScore === undefined ? "首轮，采纳" : `提升 ${prevScore}→${roundBest.score}`,
        ts: Date.now(),
      });
      emit(`round ${r} 最优 ${roundBest.id}（${roundBest.score} 分）→ 保留${prevScore !== undefined ? `（${prevScore}→${roundBest.score}）` : ""}`);
    } else {
      history.push({
        round: r,
        id: roundBest.id,
        score: roundBest.score ?? 0,
        kept: false,
        reason: `未提升（${roundBest.score} <= ${prevScore}），保留上一版 ${tree.best}`,
        ts: Date.now(),
      });
      emit(`round ${r} 最优 ${roundBest.id}（${roundBest.score} 分）没提升 → 回退，保留 ${tree.best}（${prevScore} 分）`);
    }

    tree.round = r;
    tree.judge = ev.judge || ev.error || "";
    if (!improved) break; // 没改善就停，不浪费预算
  }

  return tree;
}
