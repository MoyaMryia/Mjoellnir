/**
 * Assessor（pre-flight 裁判）：事前独立评估任务、拟定计划 —— 也就是“批思考预算”。
 *
 * 它不参与求解，只回答：这题该花多少算力、走 direct 还是 tree、开几个方向、怎么评估。
 * 代码侧对 branches / depth 做硬性 clamp（只允许开几个），assessor 说什么都不能越界。
 */

import { parseJsonObject, runJudge } from "./judge.ts";
import { resolveRole, searchExtensionPath } from "./roleconfig.ts";
import { clampPlan, type Goal, type GoalPlan, MAX_BRANCHES, MAX_DEPTH } from "./state.ts";

const SYSTEM_PROMPT = `You are an independent task assessor for a coding agent's goal.
You received no prior reasoning; judge only from the objective as given.

Decide how much compute the goal deserves and what shape the search should take, then output exactly one JSON object and nothing after it:
{"mode":"direct"|"tree","rationale":"<one or two sentences>","branches":<int>,"depth":<int>,"angles":["...","..."],"evaluator":{"kind":"rubric","rubric":"<how to judge candidate quality>"}}

Guidance:
- Prefer "direct" (branches=1, depth=1) for small, well-specified, or single-obvious-path tasks. Cheap is better when in doubt.
- Use "tree" only for open-ended tasks that have several genuinely different plausible approaches (reports, proofs, design or algorithm choices) where comparing candidates actually helps.
- branches = how many independent directions to explore; depth = how many refine-and-compare rounds.
- HARD LIMITS: branches between 1 and ${MAX_BRANCHES}; depth between 1 and ${MAX_DEPTH}. Anything larger will be clamped, so stay within.
- angles: for "tree", give exactly <branches> DISTINCT angles, each a concrete instruction that steers a solver away from the obvious first idea (e.g. "try the opposite hypothesis", "construct the smallest counterexample", "switch algorithm family").
- Be adversarial to the first idea: if the obvious first approach is likely a dead end, say so in rationale.
- Never redefine the goal; it must still satisfy the original objective.`;

function buildTask(goal: Goal): string {
  const parts = ["Assess this goal and output the JSON plan.", `OBJECTIVE: ${goal.objective}`];
  if (goal.constraints.length) parts.push(`CONSTRAINTS:\n${goal.constraints.map((c) => `- ${c}`).join("\n")}`);
  if (goal.checks.length) {
    parts.push(`EXECUTABLE CHECKS (the goal will be verified by these):\n${goal.checks.map((c) => `- ${c.name}: ${c.cmd}`).join("\n")}`);
  } else {
    parts.push("NOTE: there are no executable checks; the evaluator must be a rubric.");
  }
  return parts.join("\n\n");
}

export async function runAssess(opts: { cwd: string; goal: Goal; signal?: AbortSignal }): Promise<GoalPlan> {
  const rc = resolveRole("assessor");
  const r = await runJudge({
    cwd: opts.cwd,
    systemPrompt: SYSTEM_PROMPT,
    task: buildTask(opts.goal),
    tools: "read,grep,find,ls",
    model: rc.model,
    thinking: rc.thinking,
    timeoutMs: 120_000,
    signal: opts.signal,
  });
  const draft = parseJsonObject<Partial<GoalPlan>>(r.text);
  return clampPlan(draft, { hasChecks: opts.goal.checks.length > 0 });
}
