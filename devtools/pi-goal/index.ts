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
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { awareness, budgetLimit, continuation } from "./prompts.ts";
import { formatChecks, runChecks } from "./oracle.ts";
import { runAudit } from "./auditor.ts";
import {
  activeGoalForSession,
  budgetExhausted,
  type Goal,
  type GoalStatus,
  loadGoal,
  newGoal,
  openGoalForSession,
  saveGoal,
  turnsRemaining,
} from "./state.ts";

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

const SUBCOMMANDS = new Set(["show", "pause", "resume", "clear", "checks", "budget"]);

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
    description: "长程目标：/goal <objective> 新建；/goal 查看；pause|resume|clear|checks|budget",
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
          report(ctx, `已暂停 ${g.id}。`);
          return;
        }
        if (first === "resume") {
          g.status = "active";
          saveGoal(ctx.cwd, g);
          report(ctx, `已恢复 ${g.id}（剩 ${turnsRemaining(g)} 轮）。`);
          return;
        }
        if (first === "checks") {
          report(ctx, g.checks.length ? g.checks.map((c) => `· ${c.name}: ${c.cmd}`).join("\n") : "该 goal 没有 checks。");
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
      const g = newGoal({ sessionId: sid, objective: arg });
      saveGoal(ctx.cwd, g);
      currentGoalId = g.id;
      report(ctx, `已创建 goal ${g.id}（${g.budget.turns} 轮预算）。\n${describe(g)}`);
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
      budgetTurns: Type.Optional(Type.Number({ description: "Turn budget. Omit unless explicitly requested." })),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const sid = ctx.sessionManager.getSessionId();
      const open = openGoalForSession(ctx.cwd, sid);
      if (open) {
        return { content: [{ type: "text", text: `An unfinished goal exists: ${open.id} (${open.status}). Finish or clear it first.` }], isError: true };
      }
      const g = newGoal({
        sessionId: sid,
        objective: String(params.objective),
        constraints: params.constraints ?? [],
        budget: params.budgetTurns ? { turns: Math.max(1, Math.floor(params.budgetTurns)) } : undefined,
      });
      saveGoal(ctx.cwd, g);
      currentGoalId = g.id;
      return { content: [{ type: "text", text: `Goal created: ${g.id} (${g.budget.turns} turns).` }] };
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
    name: "update_goal",
    label: "Update Goal",
    promptSnippet: "Propose completing/pausing/blocking the current goal.",
    description:
      'Update the current goal status. `complete`: propose the objective is achieved (the system independently verifies). `blocked`: only after the same blocker repeated for >=3 consecutive goal turns. `paused`: only at the user\'s explicit request.',
    parameters: Type.Object({
      status: Type.Union([Type.Literal("complete"), Type.Literal("blocked"), Type.Literal("paused")]),
      reason: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const g = resolve(ctx);
      if (!g) return { content: [{ type: "text", text: "No active goal." }], isError: true };
      const status = params.status as "complete" | "blocked" | "paused";

      if (status === "complete") {
        // 1) oracle：checks 是硬门槛
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
