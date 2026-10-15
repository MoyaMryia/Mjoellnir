/**
 * /goal 状态：goal 本体的读写。
 *
 * goal 存项目级文件 <cwd>/.pi/goals/<id>.json（可读 / 可改 / 可 commit）。
 * 一个 session 的 active goal 通过扫描 goals 目录（sessionId + status）确定。
 */

import * as fs from "node:fs";
import * as path from "node:path";

export type GoalStatus = "active" | "paused" | "blocked" | "satisfied" | "exhausted";

/** 极薄断言（不是 DSL）：shell 命令 + expect。 */
export type Check = {
  name: string;
  /** shell 命令，cwd = 项目根 */
  cmd: string;
  expect:
    | { exit: number }
    /** 命令 stdout 打印 `KEY=VALUE` 行；断言该 KEY 的数值 */
    | { metric: string; op: ">=" | "<=" | "=="; target: number }
    | { stdoutMatches: string };
  timeoutSec?: number;
};

export interface GoalAudit {
  done: boolean;
  missing: string[];
  evidence: string[];
  judge: string;
  ts: number;
}

/** 评估方式：有可执行 checks 就用 checks，否则用 rubric 评审。 */
export type EvaluatorSpec = { kind: "checks" } | { kind: "rubric"; rubric: string };

/** assessor（独立裁判）事前拟定的计划：走 direct 还是 tree、开几个方向、怎么评估。 */
export interface GoalPlan {
  mode: "direct" | "tree";
  rationale: string;
  /** 并行候选数（硬性 <= MAX_BRANCHES） */
  branches: number;
  /** 迭代轮数（硬性 <= MAX_DEPTH） */
  depth: number;
  angles: string[];
  evaluator: EvaluatorSpec;
  ts: number;
}

/** “只允许开几个”的硬性封顶。assessor 说什么都不能越界。 */
export const MAX_BRANCHES = 4;
export const MAX_DEPTH = 2;

/** 把 assessor 的松散输出 clamp 成合法计划；非法/缺失一律退化为 direct。 */
export function clampPlan(raw: Partial<GoalPlan> | undefined, opts: { hasChecks: boolean }): GoalPlan {
  const mode = raw?.mode === "tree" ? "tree" : "direct";
  const evaluator: EvaluatorSpec = opts.hasChecks
    ? { kind: "checks" }
    : { kind: "rubric", rubric: String((raw?.evaluator as { rubric?: unknown } | undefined)?.rubric ?? "").slice(0, 4000) };
  const angles = Array.isArray(raw?.angles)
    ? raw!.angles.map(String).map((s) => s.trim()).filter(Boolean)
    : [];
  const rationale = String(raw?.rationale ?? "");
  if (mode === "direct") {
    return { mode, rationale, branches: 1, depth: 1, angles: angles.slice(0, 1), evaluator, ts: Date.now() };
  }
  const branches = Math.min(MAX_BRANCHES, Math.max(1, Math.floor(Number(raw?.branches) || 2)));
  const depth = Math.min(MAX_DEPTH, Math.max(1, Math.floor(Number(raw?.depth) || 1)));
  return { mode, rationale, branches, depth, angles: angles.slice(0, branches), evaluator, ts: Date.now() };
}

export interface Goal {
  id: string;
  sessionId: string;
  objective: string;
  constraints: string[];
  checks: Check[];
  budget: { turns: number; tokens?: number; minutes?: number };
  spent: { turns: number; tokens: number; minutes: number };
  status: GoalStatus;
  origin: "user" | "automatic";
  createdAt: number;
  updatedAt: number;
  progress: { lastTreeHash?: string; turnsSinceChange: number };
  plan?: GoalPlan;
  audit?: GoalAudit;
  log: Array<Record<string, unknown>>;
}

export const DEFAULT_TURNS = 20;
const GOALS_SUBDIR = path.join(".pi", "goals");

export function goalsDir(cwd: string): string {
  return path.join(cwd, GOALS_SUBDIR);
}

export function goalPath(cwd: string, id: string): string {
  return path.join(goalsDir(cwd), `${id}.json`);
}

export function newGoalId(): string {
  const t = Date.now().toString(36);
  const r = Math.random().toString(36).slice(2, 6);
  return `g-${t}-${r}`;
}

export function saveGoal(cwd: string, goal: Goal): void {
  goal.updatedAt = Date.now();
  const dir = goalsDir(cwd);
  fs.mkdirSync(dir, { recursive: true });
  const file = goalPath(cwd, goal.id);
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(goal, null, 2) + "\n", "utf-8");
  fs.renameSync(tmp, file);
}

export function loadGoal(cwd: string, id: string): Goal | undefined {
  try {
    const raw = fs.readFileSync(goalPath(cwd, id), "utf-8");
    return JSON.parse(raw) as Goal;
  } catch {
    return undefined;
  }
}

export function listGoals(cwd: string): Goal[] {
  let files: string[];
  try {
    files = fs.readdirSync(goalsDir(cwd));
  } catch {
    return [];
  }
  const out: Goal[] = [];
  for (const f of files) {
    if (!f.endsWith(".json")) continue;
    const g = loadGoal(cwd, f.slice(0, -5));
    if (g) out.push(g);
  }
  return out.sort((a, b) => b.createdAt - a.createdAt);
}

/** 本 session 尚未结束的 goal（active / paused / blocked）。 */
export function openGoalForSession(cwd: string, sessionId: string): Goal | undefined {
  return listGoals(cwd).find(
    (g) => g.sessionId === sessionId && (g.status === "active" || g.status === "paused" || g.status === "blocked"),
  );
}

export function activeGoalForSession(cwd: string, sessionId: string): Goal | undefined {
  return listGoals(cwd).find((g) => g.sessionId === sessionId && g.status === "active");
}

export function newGoal(opts: {
  sessionId: string;
  objective: string;
  constraints?: string[];
  checks?: Check[];
  budget?: { turns?: number; tokens?: number; minutes?: number };
  origin?: "user" | "automatic";
}): Goal {
  const now = Date.now();
  return {
    id: newGoalId(),
    sessionId: opts.sessionId,
    objective: opts.objective,
    constraints: opts.constraints ?? [],
    checks: opts.checks ?? [],
    budget: { turns: opts.budget?.turns ?? DEFAULT_TURNS, tokens: opts.budget?.tokens, minutes: opts.budget?.minutes },
    spent: { turns: 0, tokens: 0, minutes: 0 },
    status: "active",
    origin: opts.origin ?? "user",
    createdAt: now,
    updatedAt: now,
    progress: { turnsSinceChange: 0 },
    log: [],
  };
}

export function turnsRemaining(goal: Goal): number {
  return Math.max(0, goal.budget.turns - goal.spent.turns);
}

export function budgetExhausted(goal: Goal): boolean {
  if (goal.spent.turns >= goal.budget.turns) return true;
  if (goal.budget.minutes !== undefined) {
    const mins = (Date.now() - goal.createdAt) / 60000;
    if (mins >= goal.budget.minutes) return true;
  }
  return false;
}
