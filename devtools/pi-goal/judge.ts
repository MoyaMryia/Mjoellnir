/**
 * 通用“独立裁判”子进程。
 *
 * 起一个全新上下文的 pi 子进程（默认只读工具），跑完收回 assistant 文本。
 * 三个角色共用同一原语：
 *   - assessor：事前批思考预算 / 出计划（pre-flight）
 *   - evaluator：给候选打分、择优（mid-flight）
 *   - auditor：完成后对抗式验收（post-flight）
 * 独立性来自：新进程 + 新上下文（看不到 worker 的推理/自述）+ 只读。
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface JudgeResult {
  text: string;
  raw: string;
  stderr: string;
  error?: string;
}

function getPiInvocation(args: string[]): { command: string; args: string[] } {
  const currentScript = process.argv[1];
  const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
  if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
  }
  return { command: "pi", args };
}

export function runJudge(opts: {
  cwd: string;
  systemPrompt: string;
  task: string;
  tools?: string;
  model?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
}): Promise<JudgeResult> {
  const { cwd, systemPrompt, task } = opts;
  const timeoutMs = opts.timeoutMs ?? 180_000;
  const tools = opts.tools ?? "read,grep,find,ls";

  let tmpDir = "";
  let promptFile = "";
  try {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-goal-judge-"));
    promptFile = path.join(tmpDir, "system.md");
    fs.writeFileSync(promptFile, systemPrompt, { encoding: "utf-8", mode: 0o600 });
  } catch (e) {
    return Promise.resolve({ text: "", raw: "", stderr: "", error: `无法准备裁判提示: ${String(e)}` });
  }

  const args = ["--mode", "json", "-p", "--no-session", "--no-extensions"];
  if (tools) args.push("--tools", tools);
  if (opts.model) args.push("--model", opts.model);
  args.push("--append-system-prompt", promptFile, "--", `Task: ${task}`);

  const inv = getPiInvocation(args);
  const child = spawn(inv.command, inv.args, {
    cwd,
    env: { ...process.env, PI_SUBAGENT: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });

  return new Promise<JudgeResult>((resolve) => {
    const texts: string[] = [];
    let buf = "";
    let stderr = "";
    let settled = false;

    const cleanup = () => {
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    };
    const finish = (r: JudgeResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      resolve(r);
    };

    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
      const raw = texts.join("\n");
      finish({ text: raw, raw, stderr, error: `裁判超时（${timeoutMs / 1000}s）` });
    }, timeoutMs);

    if (opts.signal) {
      opts.signal.addEventListener(
        "abort",
        () => {
          try {
            child.kill("SIGKILL");
          } catch {
            /* ignore */
          }
          const raw = texts.join("\n");
          finish({ text: raw, raw, stderr, error: "裁判被中断" });
        },
        { once: true },
      );
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
      const raw = texts.join("\n");
      finish({ text: raw, raw, stderr, error: `裁判进程启动失败: ${String(e)}` });
    });
    child.on("close", (code) => {
      const raw = texts.join("\n");
      if (!raw.trim()) {
        finish({ text: "", raw, stderr, error: `裁判无输出（exit=${code}）${stderr.slice(-400)}` });
        return;
      }
      finish({ text: raw, raw, stderr });
    });
  });
}

/** 从模型输出里抓第一个可解析的 JSON 对象（容忍 ```json 包裹和前后废话）。 */
export function parseJsonObject<T>(raw: string): T | undefined {
  const text = raw.trim();
  const candidates: string[] = [];
  const fence = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
  if (fence) candidates.push(fence[1]);
  const first = text.indexOf("{");
  const last = text.lastIndexOf("}");
  if (first >= 0 && last > first) candidates.push(text.slice(first, last + 1));
  for (const c of candidates) {
    try {
      return JSON.parse(c) as T;
    } catch {
      /* try next */
    }
  }
  return undefined;
}
