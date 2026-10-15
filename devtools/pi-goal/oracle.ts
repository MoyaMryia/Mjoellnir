/**
 * Oracle：跑 goal 的 checks（shell + 极薄 expect）。
 * 只在 complete 提议或 run_checks 时执行。
 */

import { execFile } from "node:child_process";
import type { Check } from "./state.ts";

export interface CheckResult {
  name: string;
  ok: boolean;
  detail: string;
  ms: number;
}

interface RunOut {
  code: number;
  out: string;
  err: string;
  timedOut: boolean;
}

function runShell(cmd: string, cwd: string, timeoutMs: number, signal?: AbortSignal): Promise<RunOut> {
  return new Promise((resolve) => {
    const cp = execFile(
      "/bin/sh",
      ["-c", cmd],
      { cwd, timeout: timeoutMs, maxBuffer: 8 << 20 },
      (e, out, err) => {
        let code = 0;
        let timedOut = false;
        if (e) {
          const anyE = e as { code?: unknown; killed?: boolean };
          code = typeof anyE.code === "number" ? anyE.code : 1;
          if (anyE.killed === true) timedOut = true;
        }
        resolve({ code, out: out || "", err: err || "", timedOut });
      },
    );
    if (signal) {
      signal.addEventListener("abort", () => {
        try {
          cp.kill("SIGKILL");
        } catch {
          /* ignore */
        }
      }, { once: true });
    }
  });
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function evalExpect(expect: Check["expect"], r: RunOut): { ok: boolean; detail: string } {
  if (r.timedOut) return { ok: false, detail: "timed out" };
  if ("exit" in expect) return { ok: r.code === expect.exit, detail: `exit=${r.code} (期望 ${expect.exit})` };
  if ("stdoutMatches" in expect) {
    let re: RegExp;
    try {
      re = new RegExp(expect.stdoutMatches);
    } catch {
      return { ok: false, detail: "非法正则" };
    }
    return { ok: re.test(r.out), detail: re.test(r.out) ? "stdout 匹配" : "stdout 不匹配" };
  }
  const key = expect.metric;
  const m = new RegExp(`^\\s*${escapeRe(key)}\\s*=\\s*(-?\\d+(?:\\.\\d+)?)`, "m").exec(r.out);
  if (!m) return { ok: false, detail: `stdout 里找不到 ${key}=<number>` };
  const v = Number(m[1]);
  const ok = expect.op === ">=" ? v >= expect.target : expect.op === "<=" ? v <= expect.target : v === expect.target;
  return { ok, detail: `${key}=${v} ${expect.op} ${expect.target} -> ${ok}` };
}

export async function runChecks(cwd: string, checks: Check[], signal?: AbortSignal): Promise<CheckResult[]> {
  const out: CheckResult[] = [];
  for (const c of checks) {
    const t0 = Date.now();
    const r = await runShell(c.cmd, cwd, (c.timeoutSec ?? 300) * 1000, signal);
    const ev = evalExpect(c.expect, r);
    const tail = `${r.out}${r.err ? `\n${r.err}` : ""}`.trim().slice(-800);
    out.push({
      name: c.name,
      ok: ev.ok,
      detail: ev.ok ? ev.detail : `${ev.detail}\n${tail}`,
      ms: Date.now() - t0,
    });
  }
  return out;
}

export function formatChecks(results: CheckResult[]): string {
  if (!results.length) return "(没有 checks)";
  return results.map((r) => `${r.ok ? "PASS" : "FAIL"} ${r.name} (${r.ms}ms): ${r.detail}`).join("\n");
}
