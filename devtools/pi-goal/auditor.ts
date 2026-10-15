/**
 * 独立完成审计（post-flight 裁判）。
 *
 * 用 judge.ts 的通用原语起一个全新上下文的 pi 子进程（含 bash，可跑 checks），对抗式核验 goal 是否真的达成。
 * 独立性来自：新进程 + 新上下文（看不到 worker 的推理/自述）+ 只读。
 * 模型默认继承；可用 PI_GOAL_AUDITOR_MODEL 覆盖（需要换盲点时）。
 */

import { parseJsonObject, runJudge } from "./judge.ts";
import type { Goal } from "./state.ts";

export interface AuditResult {
  done: boolean;
  missing: string[];
  evidence: string[];
  judge: string;
  raw: string;
}

const AUDIT_TIMEOUT_MS = 240_000;

const SYSTEM_PROMPT = `You are an independent completion auditor for a coding agent's goal.
You did NOT perform the work, and you must not assume it is correct.

Rules:
- Only the current repository state, command output, tests, and artifacts are authoritative. Any prior narrative is unverified.
- Keep the original scope. Never redefine success as the part that happens to be done.
- Derive concrete requirements from the objective and constraints. For each, find authoritative evidence.
- Actively try to falsify: find the strongest reason the goal is NOT done.
- Missing, weak, or indirect evidence means NOT done.
- You may run read-only commands and the provided checks. Never modify files.

Output exactly one JSON object, and nothing after it:
{"done": <boolean>, "missing": [<string>...], "evidence": [<string>...], "judge": "<one paragraph>"}`;

function buildTask(goal: Goal): string {
  const parts = [
    "Independently audit whether this goal is complete, then output the JSON verdict.",
    `OBJECTIVE: ${goal.objective}`,
  ];
  if (goal.constraints.length) parts.push(`CONSTRAINTS:\n${goal.constraints.map((c) => `- ${c}`).join("\n")}`);
  if (goal.checks.length) {
    parts.push(`PROVIDED CHECKS (you may run them):\n${goal.checks.map((c) => `- ${c.name}: ${c.cmd}`).join("\n")}`);
  }
  return parts.join("\n\n");
}

function parseVerdict(raw: string): AuditResult {
  const o = parseJsonObject<Partial<AuditResult>>(raw);
  if (!o) return { done: false, missing: ["auditor 未产出可解析的裁决"], evidence: [], judge: "", raw };
  return {
    done: Boolean(o.done),
    missing: Array.isArray(o.missing) ? o.missing.map(String) : [],
    evidence: Array.isArray(o.evidence) ? o.evidence.map(String) : [],
    judge: typeof o.judge === "string" ? o.judge : "",
    raw,
  };
}

export async function runAudit(opts: { cwd: string; goal: Goal; signal?: AbortSignal }): Promise<AuditResult> {
  const r = await runJudge({
    cwd: opts.cwd,
    systemPrompt: SYSTEM_PROMPT,
    task: buildTask(opts.goal),
    tools: "read,grep,find,ls,bash",
    model: process.env.PI_GOAL_AUDITOR_MODEL,
    timeoutMs: AUDIT_TIMEOUT_MS,
    signal: opts.signal,
  });
  if (r.error && !r.text.trim()) {
    return { done: false, missing: [r.error], evidence: [], judge: "", raw: r.raw };
  }
  return parseVerdict(r.text);
}
