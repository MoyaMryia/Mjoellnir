/**
 * /goal — 长程目标原语（Lab2）。
 *
 * 切片 3：自驱循环（turn_end 续跑 + before_agent_start 目标感知）+ create_goal/update_goal。
 * 设计见 docs/lab2-goal.md。
 *
 * 轻量契约：工厂阶段只注册；tick 级 turn_end 在无 active goal 时 O(1) 返回。
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { awareness, budgetLimit, continuation } from "./prompts.ts";
import { formatChecks, runChecks } from "./oracle.ts";
import { runAudit } from "./auditor.ts";
import { runAssess } from "./assessor.ts";
import { runSwarm } from "./swarm.ts";
import {
  activeGoalForSession,
  budgetExhausted,
  clampPlan,
  type Check,
  type Goal,
  type GoalStatus,
  loadGoal,
  newGoal,
  openGoalForSession,
  saveGoal,
  turnsRemaining,
} from "./state.ts";

const CheckInput = Type.Object({
  name: Type.String(),
  cmd: Type.String(),
  expect: Type.Optional(
    Type.Object({
      exit: Type.Optional(Type.Number()),
      metric: Type.Optional(Type.String()),
      op: Type.Optional(Type.Union([Type.Literal(">="), Type.Literal("<="), Type.Literal("==")])),
      target: Type.Optional(Type.Number()),
      stdoutMatches: Type.Optional(Type.String()),
    }),
  ),
  timeoutSec: Type.Optional(Type.Number()),
});

/** 把松散的 checks 入参归一成强类型 Check；非法项跳过。 */
function normalizeChecks(raw: unknown): Check[] {
  if (!Array.isArray(raw)) return [];
  const out: Check[] = [];
  for (const r of raw) {
    if (!r || typeof r !== "object") continue;
    const o = r as { name?: unknown; cmd?: unknown; expect?: Record<string, unknown>; timeoutSec?: unknown };
    if (typeof o.name !== "string" || typeof o.cmd !== "string") continue;
    const e = (o.expect ?? {}) as Record<string, unknown>;
    let expect: Check["expect"];
    if (e.metric !== undefined) {
      expect = { metric: String(e.metric), op: (e.op as ">=" | "<=" | "==") ?? ">=", target: Number(e.target ?? 0) };
    } else if (e.stdoutMatches !== undefined) {
      expect = { stdoutMatches: String(e.stdoutMatches) };
    } else {
      expect = { exit: Number(e.exit ?? 0) };
    }
    out.push({ name: o.name, cmd: o.cmd, expect, timeoutSec: typeof o.timeoutSec === "number" ? o.timeoutSec : undefined });
  }
  return out;
}

// 子 agent 进程会加载同一份用户扩展；goal 只属于主会话。
const IS_SUBAGENT = process.env.PI_SUBAGENT === "1";

function report(ctx: ExtensionContext, text: string, type: "info" | "error" = "info"): void {
  if (ctx.hasUI) ctx.ui.notify(text, type);
  else console.error(text);
}

function describe(goal: Goal): string {
  const lines = [
    `goal ${goal.id} [${goal.status}]`,
    `objective: ${goal.objective}`,
  ];
  if (goal.constraints.length) lines.push(`constraints: ${goal.constraints.join(" | ")}`);
  if (goal.checks.length) lines.push(`checks: ${goal.checks.map((c) => c.name).join(", ")}`);
  if (goal.plan)
    lines.push(
      `plan: ${goal.plan.mode}（branches ${goal.plan.branches}, depth ${goal.plan.depth}）${goal.plan.rationale ? ` — ${goal.plan.rationale}` : ""}`,
    );
  if (goal.tree)
    lines.push(
      `tree: round ${goal.tree.round}, best ${goal.tree.best ?? "-"}${goal.tree.bestScore !== undefined ? ` (${goal.tree.bestScore}分)` : ""}${goal.tree.bestPath ? ` → ${goal.tree.bestPath}` : ""}`,
    );
  lines.push(`budget: turns ${goal.spent.turns}/${goal.budget.turns}（剩 ${turnsRemaining(goal)}）`);
  if (goal.audit) lines.push(`audit: ${goal.audit.done ? "done" : "not done"}`);
  return lines.join("\n");
}

/** 机械进度提示：worktree + HEAD 的哈希。失败（非 git）返回 undefined。 */
function treeHash(cwd: string): string | undefined {
  try {
    const status = execFileSync("git", ["status", "--porcelain=v1", "-z"], {
      cwd,
      timeout: 3000,
      maxBuffer: 4 << 20,
    });
    let head = "";
    try {
      head = execFileSync("git", ["rev-parse", "HEAD"], { cwd, timeout: 2000 }).toString().trim();
    } catch {
      /* 无 commit 也可 */
    }
    return createHash("sha1").update(head).update("\0").update(status).digest("hex");
  } catch {
    return undefined;
  }
}

/** 边界注入条目（display:false，不进 UI）。 */
function boundaryEntry(customType: string, content: string) {
  return { type: "custom_message" as const, customType, content, display: false };
}

/** 事前独立评估：填 goal.plan。失败不阻断建目标，退化为 direct。 */
async function assessGoal(ctx: ExtensionContext, goal: Goal): Promise<void> {
  report(ctx, "正在独立评估任务、拟定计划（新上下文，约 5–15s）…");
  try {
    goal.plan = await runAssess({ cwd: ctx.cwd, goal });
  } catch {
    goal.plan = clampPlan({ mode: "direct" }, { hasChecks: goal.checks.length > 0 });
  }
  saveGoal(ctx.cwd, goal);
}

const SUBCOMMANDS = new Set(["show", "pause", "resume", "clear", "checks", "budget", "plan", "tree"]);

export default function (pi: ExtensionAPI) {
  let currentGoalId: string | undefined;
  // 下一轮是否为本扩展触发的续跑（用于避免重复注入感知消息）。
  let continuationPending = false;

  const resolve = (ctx: ExtensionContext): Goal | undefined => {
    const sid = ctx.sessionManager.getSessionId();
    if (currentGoalId) {
      const g = loadGoal(ctx.cwd, currentGoalId);
      if (g) return g;
    }
    const g = activeGoalForSession(ctx.cwd, sid) ?? openGoalForSession(ctx.cwd, sid);
    if (g) currentGoalId = g.id;
    return g;
  };

  pi.on("session_start", (_event, ctx) => {
    const sid = ctx.sessionManager.getSessionId();
    currentGoalId = activeGoalForSession(ctx.cwd, sid)?.id;
    continuationPending = false;
  });

  // 目标感知：非续跑轮，把 active goal 注入模型上下文。
  pi.on("before_agent_start", (_event, ctx) => {
    if (IS_SUBAGENT) return;
    const wasContinuation = continuationPending;
    continuationPending = false;
    if (wasContinuation) return;
    const g = activeGoalForSession(ctx.cwd, ctx.sessionManager.getSessionId());
    if (!g) return;
    currentGoalId = g.id;
    return {
      message: { customType: "goal-awareness", content: awareness(g), display: false },
    };
  });

  // 自驱循环。
  pi.on("turn_end", (_event, ctx) => {
    if (IS_SUBAGENT) return;
    const g = activeGoalForSession(ctx.cwd, ctx.sessionManager.getSessionId());
    if (!g) {
      continuationPending = false;
      return;
    }
    currentGoalId = g.id;

    g.spent.turns += 1;
    const h = treeHash(ctx.cwd);
    if (h && g.progress.lastTreeHash && h === g.progress.lastTreeHash) {
      g.progress.turnsSinceChange += 1;
    } else {
      g.progress.turnsSinceChange = 0;
    }
    if (h) g.progress.lastTreeHash = h;

    if (budgetExhausted(g)) {
      g.status = "exhausted";
      g.log.push({ ts: Date.now(), event: "exhausted", turns: g.spent.turns });
      saveGoal(ctx.cwd, g);
      continuationPending = false;
      return { entries: [boundaryEntry("goal-budget", budgetLimit(g))], continue: true };
    }

    saveGoal(ctx.cwd, g);

    if (ctx.hasPendingMessages()) {
      // 用户已经排了话，别抢。
      continuationPending = false;
      return;
    }

    continuationPending = true;
    return { entries: [boundaryEntry("goal-continuation", continuation(g))], continue: true };
  });

  pi.registerCommand("goal", {
    description: "长程目标：/goal <objective> [--checks <json>] 新建；/goal 查看；pause|resume|clear|checks [set <json>]|budget|plan|tree",
    handler: async (args, ctx) => {
      const arg = args.trim();
      const sid = ctx.sessionManager.getSessionId();
      const open = openGoalForSession(ctx.cwd, sid);

      if (!arg || arg === "show") {
        const g = resolve(ctx);
        report(ctx, g ? describe(g) : "当前没有 goal。用 /goal <objective> 新建。");
        return;
      }

      const first = arg.split(/\s+/, 1)[0];
      if (SUBCOMMANDS.has(first)) {
        const rest = arg.slice(first.length).trim();
        const g = resolve(ctx);
        if (first === "clear") {
          if (!g) {
            report(ctx, "没有可清除的 goal。");
            return;
          }
          g.status = "exhausted";
          g.log.push({ ts: Date.now(), event: "clear" });
          saveGoal(ctx.cwd, g);
          currentGoalId = undefined;
          try { ctx.abort(); } catch { /* 未在跑则忽略 */ }
          report(ctx, `已清除 goal ${g.id}（文件保留在 .pi/goals/）。`);
          return;
        }
        if (!g) {
          report(ctx, "当前没有 goal。", "error");
          return;
        }
        if (first === "pause") {
          g.status = "paused";
          saveGoal(ctx.cwd, g);
          continuationPending = false;
          try { ctx.abort(); } catch { /* 未在跑则忽略 */ }
          report(ctx, `已暂停 ${g.id}，并打断当前那轮。`);
          return;
        }
        if (first === "resume") {
          g.status = "active";
          saveGoal(ctx.cwd, g);
          report(ctx, `已恢复 ${g.id}（剩 ${turnsRemaining(g)} 轮）。`);
          // 当场续跑
          pi.sendMessage(
            { customType: "goal-kick", content: continuation(g), display: false },
            { triggerTurn: true },
          );
          return;
        }
        if (first === "checks") {
          if (rest.startsWith("set")) {
            const json = rest.slice(3).trim();
            try {
              g.checks = normalizeChecks(JSON.parse(json));
            } catch {
              report(ctx, "checks JSON 解析失败。", "error");
              return;
            }
            saveGoal(ctx.cwd, g);
            report(ctx, `已设置 ${g.checks.length} 条 checks。`);
            return;
          }
          report(ctx, g.checks.length ? g.checks.map((c) => `· ${c.name}: ${c.cmd}`).join("\n") : "该 goal 没有 checks。");
          return;
        }
        if (first === "plan") {
          report(ctx, g.plan ? JSON.stringify(g.plan, null, 2) : "该 goal 还没有 plan。");
          return;
        }
        if (first === "tree") {
          if (!g.tree) {
            report(ctx, "该 goal 还没有搜索树。");
            return;
          }
          const rows = g.tree.branches
            .map((b) => `· ${b.id} [${b.status}]${b.score !== undefined ? ` ${b.score}分` : ""} r${b.round}: ${b.angle.slice(0, 60)}`)
            .join("\n");
          report(ctx, `tree round ${g.tree.round}\n${rows}${g.tree.bestPath ? `\nbest → ${g.tree.bestPath}` : ""}`);
          return;
        }
        if (first === "budget") {
          const n = Number.parseInt(rest, 10);
          if (!Number.isFinite(n) || n <= 0) {
            report(ctx, "用法：/goal budget <turns>", "error");
            return;
          }
          g.budget.turns = n;
          saveGoal(ctx.cwd, g);
          report(ctx, `已把 turns 预算设为 ${n}（剩 ${turnsRemaining(g)}）。`);
          return;
        }
        return;
      }

      if (open) {
        report(ctx, `已有未结束的 goal ${open.id}（${open.status}）。先 /goal clear 或完成它。`, "error");
        return;
      }
      // 允许在 objective 后跟 --checks <json array>
      let objective = arg;
      let checksRaw: unknown = [];
      const cidx = arg.indexOf(" --checks ");
      if (cidx >= 0) {
        objective = arg.slice(0, cidx).trim();
        const jsonPart = arg.slice(cidx + " --checks ".length).trim();
        try {
          checksRaw = JSON.parse(jsonPart);
        } catch {
          report(ctx, "checks JSON 解析失败。", "error");
          return;
        }
      }
      const chks = normalizeChecks(checksRaw);
      const g = newGoal({ sessionId: sid, objective, checks: chks });
      saveGoal(ctx.cwd, g);
      currentGoalId = g.id;
      report(ctx, `已创建 goal ${g.id}（${g.budget.turns} 轮预算，${chks.length} 条 checks）。`);
      await assessGoal(ctx, g);
      report(ctx, describe(g));
      // 当场启动：注入隐藏的 continuation 并触发一轮，不等用户再发话。
      pi.sendMessage(
        { customType: "goal-kick", content: continuation(g), display: false },
        { triggerTurn: true },
      );
    },
  });

  pi.registerTool({
    name: "get_goal",
    label: "Get Goal",
    promptSnippet: "Get the current long-running goal (status, budget, checks, remaining turns).",
    description: "Get the current goal for this session: status, objective, constraints, checks, budget and remaining turns.",
    parameters: Type.Object({}),
    async execute(_id, _params, _signal, _onUpdate, ctx) {
      const g = resolve(ctx);
      if (!g) return { content: [{ type: "text", text: "No active goal." }] };
      const payload = {
        id: g.id,
        status: g.status,
        objective: g.objective,
        constraints: g.constraints,
        checks: g.checks,
        turnsUsed: g.spent.turns,
        turnsBudget: g.budget.turns,
        turnsRemaining: turnsRemaining(g),
        exhausted: budgetExhausted(g),
        stalledTurns: g.progress.turnsSinceChange,
        plan: g.plan,
        tree: g.tree,
        audit: g.audit,
      };
      return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
    },
  });

  pi.registerTool({
    name: "create_goal",
    label: "Create Goal",
    promptSnippet: "Start a long-running goal. Only when the user explicitly asks for a goal.",
    description:
      "Create a new active goal. Only call this when the user explicitly asks for a goal; do not infer goals from ordinary tasks. Fails if an unfinished goal already exists.",
    parameters: Type.Object({
      objective: Type.String({ description: "The concrete objective to pursue." }),
      constraints: Type.Optional(Type.Array(Type.String(), { description: "Hard constraints / anti-goals." })),
      checks: Type.Optional(Type.Array(CheckInput, { description: "Objective acceptance checks (shell command + expect)." })),
      budgetTurns: Type.Optional(Type.Number({ description: "Turn budget. Omit unless explicitly requested." })),
    }),
    async execute(_id, params, _signal, onUpdate, ctx) {
      const sid = ctx.sessionManager.getSessionId();
      const open = openGoalForSession(ctx.cwd, sid);
      if (open) {
        return { content: [{ type: "text", text: `An unfinished goal exists: ${open.id} (${open.status}). Finish or clear it first.` }], isError: true };
      }
      const g = newGoal({
        sessionId: sid,
        objective: String(params.objective),
        constraints: params.constraints ?? [],
        checks: normalizeChecks(params.checks),
        budget: params.budgetTurns ? { turns: Math.max(1, Math.floor(params.budgetTurns)) } : undefined,
      });
      saveGoal(ctx.cwd, g);
      currentGoalId = g.id;
      try {
        onUpdate?.({ content: [{ type: "text", text: "正在独立评估任务、拟定计划（新上下文，约 5–15s）…" }] });
      } catch {
        /* ignore */
      }
      await assessGoal(ctx, g);
      return { content: [{ type: "text", text: `Goal created: ${g.id} (${g.budget.turns} turns; plan=${g.plan?.mode ?? "?"}).` }] };
    },
  });

  pi.registerTool({
    name: "run_checks",
    label: "Run Checks",
    promptSnippet: "Run the current goal's checks (oracle) and return pass/fail.",
    description: "Run the current goal's checks (read-only shell commands defined by the goal) and return their results.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, ctx) {
      const g = resolve(ctx);
      if (!g) return { content: [{ type: "text", text: "No active goal." }], isError: true };
      if (!g.checks.length) return { content: [{ type: "text", text: "该 goal 没有 checks。" }] };
      const results = await runChecks(ctx.cwd, g.checks, signal);
      g.log.push({ ts: Date.now(), event: "run_checks", ok: results.every((r) => r.ok) });
      saveGoal(ctx.cwd, g);
      return { content: [{ type: "text", text: formatChecks(results) }], isError: results.some((r) => !r.ok) };
    },
  });

  pi.registerTool({
    name: "search_candidates",
    label: "Search Candidates",
    promptSnippet: "Run the planned multi-angle search (plan.mode=tree) and return the best candidate.",
    description:
      "Only for goals whose plan mode is 'tree'. Spawns independent solvers along the planned angles, scores candidates with an independent evaluator, writes the best to .pi/goals/<id>/best.md, and returns it. Call this BEFORE writing your own answer.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, onUpdate, ctx) {
      const g = resolve(ctx);
      if (!g) return { content: [{ type: "text", text: "No active goal." }], isError: true };
      if (!g.plan || g.plan.mode !== "tree") {
        return { content: [{ type: "text", text: `当前 plan.mode=${g.plan?.mode ?? "未评估"}，不需要搜索，直接求解。` }] };
      }
      const emit = (t: string) => {
        try {
          onUpdate?.({ content: [{ type: "text", text: t }] });
        } catch {
          /* ignore */
        }
      };
      const tree = await runSwarm({ cwd: ctx.cwd, goal: g, signal, onProgress: emit });
      g.tree = tree;
      g.log.push({ ts: Date.now(), event: "swarm", round: tree.round, best: tree.best });
      saveGoal(ctx.cwd, g);
      const scoreLines = tree.branches
        .map((b) => `· ${b.id}${b.score !== undefined ? ` ${b.score}分` : ""} — ${b.angle.slice(0, 60)}`)
        .join("\n");
      let bestText = "";
      if (tree.bestPath) {
        try {
          bestText = fs.readFileSync(path.join(ctx.cwd, tree.bestPath), "utf-8");
        } catch {
          /* ignore */
        }
      }
      const body = [
        `搜索完成（round ${tree.round}）。候选：`,
        scoreLines,
        tree.judge ? `\n评审：${tree.judge}` : "",
        `\n最优：${tree.best}${tree.bestScore !== undefined ? `（${tree.bestScore} 分）` : ""}${tree.bestPath ? ` → ${tree.bestPath}` : ""}`,
        bestText ? `\n--- 最优候选全文 ---\n${bestText.slice(0, 12000)}` : "",
        "\n现在基于最优候选深化、补齐证据后收尾，不要再从头另写一份。",
      ]
        .filter(Boolean)
        .join("\n");
      return { content: [{ type: "text", text: body }] };
    },
  });

  pi.registerTool({
    name: "update_goal",
    label: "Update Goal",
    promptSnippet: "Propose completing/pausing/blocking the current goal.",
    description:
      'Update the current goal status. `complete`: propose the objective is achieved (the system independently verifies). `blocked`: only after the same blocker repeated for >=3 consecutive goal turns. `paused`: only at the user\'s explicit request.',
    parameters: Type.Object({
      status: Type.Union([Type.Literal("complete"), Type.Literal("blocked"), Type.Literal("paused")]),
      reason: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      const g = resolve(ctx);
      if (!g) return { content: [{ type: "text", text: "No active goal." }], isError: true };
      const status = params.status as "complete" | "blocked" | "paused";

      const emit = (t: string) => {
        try {
          onUpdate?.({ content: [{ type: "text", text: t }] });
        } catch {
          /* ignore */
        }
      };

      if (status === "complete") {
        // 1) oracle：checks 是硬门槛
        emit("核验完成提议：正在跑 checks…");
        const results = await runChecks(ctx.cwd, g.checks, signal);
        g.log.push({ ts: Date.now(), event: "checks", ok: results.every((r) => r.ok) });
        if (results.some((r) => !r.ok)) {
          saveGoal(ctx.cwd, g);
          return {
            content: [{ type: "text", text: `完成被拒绝：checks 未通过。\n${formatChecks(results)}\n\n修正后重新提议 complete。` }],
            isError: true,
          };
        }
        // 2) 独立审计（新上下文）
        emit("checks 通过，开始独立审计（新上下文，约 30–60s，稍等）…");
        const audit = await runAudit({ cwd: ctx.cwd, goal: g, signal });
        g.audit = { done: audit.done, missing: audit.missing, evidence: audit.evidence, judge: audit.judge, ts: Date.now() };
        if (!audit.done) {
          g.log.push({ ts: Date.now(), event: "audit", done: false });
          saveGoal(ctx.cwd, g);
          const miss = audit.missing.length ? audit.missing.map((m) => `- ${m}`).join("\n") : "(未给出)";
          return {
            content: [{ type: "text", text: `完成被独立审计拒绝。缺口：\n${miss}\n\n审计意见：${audit.judge}\n\n补齐后重新提议 complete。` }],
            isError: true,
          };
        }
        g.status = "satisfied";
        g.log.push({ ts: Date.now(), event: "status:complete", checks: results.length, audit: true });
        saveGoal(ctx.cwd, g);
        continuationPending = false;
        return { content: [{ type: "text", text: `Goal ${g.id} 完成：checks 通过 ∧ 独立审计确认。\n${formatChecks(results)}` }] };
      }

      const next: GoalStatus = status === "blocked" ? "blocked" : "paused";
      g.status = next;
      g.log.push({ ts: Date.now(), event: `status:${status}`, reason: params.reason });
      saveGoal(ctx.cwd, g);
      continuationPending = false;
      return { content: [{ type: "text", text: `Goal ${g.id} → ${next}.` }] };
    },
  });
}
