/**
 * /goal — 长程目标原语（Lab2）。
 *
 * 切片 1：状态骨架 + /goal 命令 + get_goal 工具。
 * 设计见 docs/lab2-goal.md。
 *
 * 轻量契约：工厂阶段只注册；不做任何 IO。
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
  activeGoalForSession,
  budgetExhausted,
  type Goal,
  loadGoal,
  newGoal,
  openGoalForSession,
  saveGoal,
  turnsRemaining,
} from "./state.ts";

function report(ctx: ExtensionContext, text: string, type: "info" | "error" = "info"): void {
  if (ctx.hasUI) ctx.ui.notify(text, type);
  else console.error(text); // 不污染 JSON/RPC stdout
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

const SUBCOMMANDS = new Set(["show", "pause", "resume", "clear", "checks", "budget"]);

export default function (pi: ExtensionAPI) {
  // 当前 session 的 active goal（内存缓存；session_start 时重建）。
  let currentGoalId: string | undefined;

  pi.on("session_start", (_event, ctx) => {
    const g = activeGoalForSession(ctx.cwd, ctx.sessionManager.getSessionId());
    currentGoalId = g?.id;
  });

  pi.registerCommand("goal", {
    description: "长程目标：/goal <objective> 新建；/goal 查看；pause|resume|clear|checks|budget",
    handler: async (args, ctx) => {
      const arg = args.trim();
      const sid = ctx.sessionManager.getSessionId();
      const open = openGoalForSession(ctx.cwd, sid);

      const resolve = (): Goal | undefined => {
        if (currentGoalId) {
          const g = loadGoal(ctx.cwd, currentGoalId);
          if (g) return g;
        }
        if (open) {
          currentGoalId = open.id;
          return open;
        }
        return undefined;
      };

      if (!arg || arg === "show") {
        const g = resolve();
        report(ctx, g ? describe(g) : "当前没有 goal。用 /goal <objective> 新建。");
        return;
      }

      const first = arg.split(/\s+/, 1)[0];
      if (SUBCOMMANDS.has(first)) {
        const rest = arg.slice(first.length).trim();
        const g = resolve();
        if (first === "clear") {
          if (!g) {
            report(ctx, "没有可清除的 goal。");
            return;
          }
          // clear = 从会话视野移除；文件保留可查。标记为 exhausted（终止）。
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
          if (!g.checks.length) {
            report(ctx, "该 goal 没有 checks。");
            return;
          }
          report(ctx, g.checks.map((c) => `· ${c.name}: ${c.cmd}`).join("\n"));
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

      // 其余整串视为新 objective
      if (open) {
        report(
          ctx,
          `已有未结束的 goal ${open.id}（${open.status}）。先 /goal clear 或完成它。`,
          "error",
        );
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
    description: "Get the current goal for this session, including status, budget, checks and remaining turns.",
    parameters: Type.Object({}),
    async execute(_id, _params, _signal, _onUpdate, ctx) {
      const g =
        (currentGoalId && loadGoal(ctx.cwd, currentGoalId)) ||
        activeGoalForSession(ctx.cwd, ctx.sessionManager.getSessionId());
      if (!g) {
        return { content: [{ type: "text", text: "No active goal." }] };
      }
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
        audit: g.audit,
      };
      return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
    },
  });
}
