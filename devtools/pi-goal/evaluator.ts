/**
 * Evaluator（mid-flight 裁判）：给候选打分、择优。
 *
 * 复用 judge.ts 的通用原语；只读工具，让裁判自己把候选文件读进来，再输出结构化评分。
 * 没有稳定 evaluator 的 swarm = 贵的随机，所以这一步是 tree 的前置条件。
 */

import { parseJsonObject, runJudge } from "./judge.ts";
import { resolveRole, searchExtensionPath } from "./roleconfig.ts";
import type { Goal } from "./state.ts";

export interface CandidateRef {
  id: string;
  angle: string;
  /** 相对 cwd 的路径 */
  path: string;
}

export interface EvalResult {
  scores: Array<{ id: string; score: number; why: string }>;
  best: string;
  judge: string;
  raw: string;
  error?: string;
}

const EVAL_SYSTEM = `You are an independent evaluator comparing candidate answers to a task.
You did not write any of them. Judge only from the candidates themselves and the task statement.

Rules:
- Score each candidate 0-100 on how well it would satisfy the objective (and the rubric, if given).
- Be adversarial: prefer concrete, checkable substance over confident prose. Penalize hand-waving, unverifiable claims, and scope drift.
- Do not reward length. A shorter answer that is correct and specific beats a longer vague one.
- Read each candidate file before scoring. Base "why" on specific evidence from it.
- Pick exactly one best candidate: the highest score; break ties by which is easier to verify.

Output exactly one JSON object and nothing after it:
{"scores":[{"id":"<branch id>","score":<0-100>,"why":"<one sentence>"}],"best":"<branch id>","judge":"<one paragraph comparing them>"}`;

function buildTask(goal: Goal, candidates: CandidateRef[]): string {
  const parts = [
    "Compare the candidate answers, score each, and pick the best.",
    `OBJECTIVE: ${goal.objective}`,
  ];
  if (goal.constraints.length) parts.push(`CONSTRAINTS:\n${goal.constraints.map((c) => `- ${c}`).join("\n")}`);
  if (goal.plan?.evaluator.kind === "rubric" && goal.plan.evaluator.rubric) {
    parts.push(`RUBRIC:\n${goal.plan.evaluator.rubric}`);
  } else if (goal.checks.length) {
    parts.push(`ACCEPTANCE CHECKS (the final answer must satisfy these):\n${goal.checks.map((c) => `- ${c.name}: ${c.cmd}`).join("\n")}`);
  }
  parts.push(
    `CANDIDATES (read each file with the read tool):\n${candidates
      .map((c) => `- id=${c.id} angle="${c.angle}": ${c.path}`)
      .join("\n")}`,
  );
  return parts.join("\n\n");
}

export async function runEvaluate(opts: {
  cwd: string;
  goal: Goal;
  candidates: CandidateRef[];
  signal?: AbortSignal;
}): Promise<EvalResult> {
  const r = await runJudge({
    cwd: opts.cwd,
    systemPrompt: EVAL_SYSTEM,
    task: buildTask(opts.goal, opts.candidates),
    tools: "read,grep,find,ls,web_search,web_fetch",
    model: resolveRole("evaluator").model,
    thinking: resolveRole("evaluator").thinking,
    extensions: [searchExtensionPath()].filter((p): p is string => !!p),
    timeoutMs: 180_000,
    signal: opts.signal,
  });
  const parsed = parseJsonObject<Partial<EvalResult>>(r.text);
  if (!parsed || !Array.isArray(parsed.scores)) {
    return { scores: [], best: "", judge: "", raw: r.text, error: r.error ?? "evaluator 未产出可解析的评分" };
  }
  const scores = parsed.scores
    .filter((s) => s && typeof s === "object")
    .map((s) => ({
      id: String((s as { id?: unknown }).id ?? ""),
      score: Math.max(0, Math.min(100, Number((s as { score?: unknown }).score) || 0)),
      why: String((s as { why?: unknown }).why ?? ""),
    }))
    .filter((s) => s.id);
  const best = typeof parsed.best === "string" && parsed.best ? parsed.best : [...scores].sort((a, b) => b.score - a.score)[0]?.id ?? "";
  return { scores, best, judge: typeof parsed.judge === "string" ? parsed.judge : "", raw: r.text };
}
