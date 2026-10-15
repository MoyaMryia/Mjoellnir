/**
 * /goal 各角色的模型配置。
 *
 * 复用 subagent 的 agent 文件约定：读 `~/.pi/agent/agents/<role>.md` 的 frontmatter
 * `model` / `thinking`。这样 `/setagent auditor deepseek/xxx:low` 就能直接配置 goal 角色，
 * `/agents` 也能看到。
 *
 * 优先级：env `PI_GOAL_<ROLE>_MODEL` / `_THINKING` > agent 文件 > 继承（不传）。
 */

import * as fs from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";

export type GoalRole = "assessor" | "evaluator" | "auditor" | "solver";

export interface RoleConfig {
  model?: string;
  thinking?: string;
}

function parseFrontmatterField(text: string, key: string): string | undefined {
  const fm = /^(?:\uFEFF)?---[^\S\r\n]*\r?\n([\s\S]*?)\r?\n---/.exec(text);
  if (!fm) return undefined;
  const m = new RegExp(`^[^\\S\\r\\n]*${key}[^\\S\\r\\n]*:[^\\S\\r\\n]*(.+?)[^\\S\\r\\n]*$`, "m").exec(fm[1]);
  if (!m) return undefined;
  let v = m[1].trim();
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
  return v || undefined;
}

export function agentFileFor(role: GoalRole): string {
  return path.join(homedir(), ".pi", "agent", "agents", `${role}.md`);
}

export function resolveRole(role: GoalRole): RoleConfig {
  let model: string | undefined;
  let thinking: string | undefined;
  try {
    const text = fs.readFileSync(agentFileFor(role), "utf-8");
    model = parseFrontmatterField(text, "model");
    thinking = parseFrontmatterField(text, "thinking");
  } catch {
    /* 没有该角色的 agent 文件就继承 */
  }
  const envModel = process.env[`PI_GOAL_${role.toUpperCase()}_MODEL`];
  const envThinking = process.env[`PI_GOAL_${role.toUpperCase()}_THINKING`];
  if (envModel) model = envModel;
  if (envThinking) thinking = envThinking;
  return { model, thinking };
}

/** 搜索扩展的绝对路径（供 child 用显式 -e 加载）；找不到返回 undefined。 */
export function searchExtensionPath(): string | undefined {
  if (process.env.PI_GOAL_NO_SEARCH === "1") return undefined; // 评测用：断搜索
  const candidates: string[] = [];
  try {
    candidates.push(new URL("../pi-search/index.ts", import.meta.url).pathname);
  } catch {
    /* ignore */
  }
  candidates.push(path.join(homedir(), ".pi", "agent", "extensions", "search", "index.ts"));
  return candidates.find((p) => {
    try {
      return fs.existsSync(p);
    } catch {
      return false;
    }
  });
}
