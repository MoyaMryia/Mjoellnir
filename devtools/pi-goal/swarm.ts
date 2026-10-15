/**
 * Swarm（切片 8A）：文本候选的并行搜索。
 *
 * 沿 assessor 计划里的角度，并行起 N 个独立 solver（只读工具，各自输出 Markdown），
 * 写入 `.pi/goals/<id>/branches/<bid>/answer.md`；再用独立 evaluator 打分选最优，
 * 复制到 `.pi/goals/<id>/best.md`。硬性上限由 plan（clamp 过）保证。
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { runEvaluate } from "./evaluator.ts";
import { runJudge } from "./judge.ts";
import { bestAnswerPath, branchAnswerPath, type Branch, type Goal, type GoalTree } from "./state.ts";

const SOLVER_SYSTEM = `You are one independent solver working on a single assigned angle of a task.
Commit to your assigned angle; do not hedge back toward the obvious answer. Produce a complete, self-contained candidate answer in Markdown.
Be concrete and specific: state claims precisely and give concrete examples, numbers, or reasoning where relevant.
Do not ask questions. Do not mention that other solvers exist. Output only the candidate answer.`;

const SOLVER_TIMEOUT_MS = 300_000;

function buildSolverTask(goal: Goal, angle: string): string {
  const parts = [
    "Solve the objective from exactly ONE assigned angle. Output your full candidate answer in Markdown.",
    `OBJECTIVE: ${goal.objective}`,
  ];
  if (goal.constraints.length) parts.push(`CONSTRAINTS:\n${goal.constraints.map((c) => `- ${c}`).join("\n")}`);
  if (goal.checks.length) {
    parts.push(`ACCEPTANCE CHECKS (your answer should help satisfy these):\n${goal.checks.map((c) => `- ${c.name}: ${c.cmd}`).join("\n")}`);
  }
  parts.push(`YOUR ANGLE (commit to it): ${angle}`);
  return parts.join("\n\n");
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

  const round = (goal.tree?.round ?? 0) + 1;
  const n = Math.max(1, Math.min(plan.branches, plan.angles.length || plan.branches));
  const angles = plan.angles.length
    ? plan.angles.slice(0, n)
    : Array.from({ length: n }, (_, i) => `独立方案 ${i + 1}`);
  const emit = (t: string) => {
    try {
      opts.onProgress?.(t);
    } catch {
      /* ignore */
    }
  };

  emit(`展开搜索：${n} 个独立 solver 并行探索（round ${round}）…`);

  const results = await Promise.all(
    angles.map(async (angle, i) => {
      const id = `r${round}b${i + 1}`;
      const abs = branchAnswerPath(cwd, goal.id, id);
      const r = await runJudge({
        cwd,
        systemPrompt: SOLVER_SYSTEM,
        task: buildSolverTask(goal, angle),
        tools: "read,grep,find,ls",
        model: process.env.PI_GOAL_SOLVER_MODEL,
        timeoutMs: SOLVER_TIMEOUT_MS,
        signal: opts.signal,
      });
      return { id, angle, abs, text: r.text, error: r.error };
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
      round,
      status,
      artifact: path.relative(cwd, res.abs),
      chars: text.length,
      ts: Date.now(),
    });
    emit(
      `  · ${res.id} ${status === "done" ? `完成（${text.length} 字）` : `失败${res.error ? `：${res.error}` : ""}`} — ${res.angle.slice(0, 48)}…`,
    );
  }

  const ok = branches.filter((b) => b.status === "done");
  if (!ok.length) {
    return { round, branches, judge: "所有候选都失败。" };
  }

  emit(`评估 ${ok.length} 个候选（独立 evaluator，只读）…`);
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

  const bestId =
    ev.best && ok.some((b) => b.id === ev.best)
      ? ev.best
      : [...ok].sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0].id;
  const bestBranch = branches.find((b) => b.id === bestId)!;

  let bestPath: string | undefined;
  try {
    const dest = bestAnswerPath(cwd, goal.id);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(path.join(cwd, bestBranch.artifact), dest);
    bestPath = path.relative(cwd, dest);
  } catch {
    /* best 复制失败不影响继续 */
  }

  const tree: GoalTree = {
    round,
    branches,
    best: bestId,
    bestScore: bestBranch.score,
    bestPath,
    judge: ev.judge || ev.error || "",
  };
  emit(`最优候选：${bestId}（${bestBranch.score ?? "?"} 分）${bestPath ? ` → ${bestPath}` : ""}`);
  return tree;
}
