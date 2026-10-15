/**
 * 独立完成审计。
 *
 * 起一个全新上下文的 pi 子进程（只读工具），对抗式核验 goal 是否真的达成。
 * 独立性来自：新进程 + 新上下文（看不到 worker 的推理/自述）+ 只读。
 * 模型默认继承；可用 PI_GOAL_AUDITOR_MODEL 覆盖（需要换盲点时）。
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { Goal } from "./state.ts";

export interface AuditResult {
  done: boolean;
  missing: string[];
  evidence: string[];
  judge: string;
  raw: string;
  error?: string;
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

function getPiInvocation(args: string[]): { command: string; args: string[] } {
  const currentScript = process.argv[1];
  const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
  if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
  }
  return { command: "pi", args };
}

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
  const text = raw.trim();
  const candidates: string[] = [];
  const fence = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
  if (fence) candidates.push(fence[1]);
  const first = text.indexOf("{");
  const last = text.lastIndexOf("}");
  if (first >= 0 && last > first) candidates.push(text.slice(first, last + 1));
  for (const c of candidates) {
    try {
      const o = JSON.parse(c) as Partial<AuditResult>;
      return {
        done: Boolean(o.done),
        missing: Array.isArray(o.missing) ? o.missing.map(String) : [],
        evidence: Array.isArray(o.evidence) ? o.evidence.map(String) : [],
        judge: typeof o.judge === "string" ? o.judge : "",
        raw,
      };
    } catch {
      /* try next */
    }
  }
  return { done: false, missing: ["auditor 未产出可解析的裁决"], evidence: [], judge: "", raw };
}

export function runAudit(opts: { cwd: string; goal: Goal; signal?: AbortSignal }): Promise<AuditResult> {
  const { cwd, goal } = opts;
  let tmpDir = "";
  let promptFile = "";
  try {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-goal-audit-"));
    promptFile = path.join(tmpDir, "system.md");
    fs.writeFileSync(promptFile, SYSTEM_PROMPT, { encoding: "utf-8", mode: 0o600 });
  } catch (e) {
    return Promise.resolve({ done: false, missing: [`无法准备审计提示: ${String(e)}`], evidence: [], judge: "", raw: "" });
  }

  const model = process.env.PI_GOAL_AUDITOR_MODEL;
  const args = ["--mode", "json", "-p", "--no-session", "--no-extensions", "--tools", "read,grep,find,ls,bash"];
  if (model) args.push("--model", model);
  args.push("--append-system-prompt", promptFile, "--", `Task: ${buildTask(goal)}`);

  const inv = getPiInvocation(args);
  const child = spawn(inv.command, inv.args, {
    cwd,
    env: { ...process.env, PI_SUBAGENT: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });

  return new Promise<AuditResult>((resolve) => {
    const texts: string[] = [];
    let buf = "";
    let stderr = "";
    let settled = false;

    const finish = (result: AuditResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
      resolve(result);
    };

    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
      finish({ done: false, missing: [`审计超时（${AUDIT_TIMEOUT_MS / 1000}s）`], evidence: [], judge: "", raw: texts.join("\n") });
    }, AUDIT_TIMEOUT_MS);

    if (opts.signal) {
      opts.signal.addEventListener("abort", () => {
        try {
          child.kill("SIGKILL");
        } catch {
          /* ignore */
        }
        finish({ done: false, missing: ["审计被中断"], evidence: [], judge: "", raw: "" });
      }, { once: true });
    }

    child.stdout.setEncoding("utf-8");
    child.stdout.on("data", (data: string) => {
      buf += data;
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev: { type?: string; message?: { role?: string; content?: unknown } };
        try {
          ev = JSON.parse(line);
        } catch {
          continue;
        }
        if (ev.type === "message_end" && ev.message?.role === "assistant") {
          const content = ev.message.content;
          if (typeof content === "string") texts.push(content);
          else if (Array.isArray(content)) {
            for (const part of content) {
              if (part && typeof part === "object" && (part as { type?: string }).type === "text") {
                texts.push(String((part as { text?: string }).text ?? ""));
              }
            }
          }
        }
      }
    });
    child.stderr.setEncoding("utf-8");
    child.stderr.on("data", (d: string) => {
      stderr += d;
    });
    child.on("error", (e) => {
      finish({ done: false, missing: [`审计进程启动失败: ${String(e)}`], evidence: [], judge: "", raw: "" });
    });
    child.on("close", (code) => {
      const raw = texts.join("\n");
      if (!raw.trim()) {
        finish({ done: false, missing: [`审计无输出（exit=${code}）${stderr.slice(-400)}`], evidence: [], judge: "", raw });
        return;
      }
      finish(parseVerdict(raw));
    });
  });
}
