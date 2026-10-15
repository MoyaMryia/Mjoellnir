#!/usr/bin/env python3
"""
竞赛/CTF 客观评测 harness。

用法：
  python3 run.py crypto [--mode auto|direct|tree] [--budget 12] [--model ...] [--timeout 900]

做的事：
  1. 建 run 目录：work/（agent 的 cwd）+ secret/（外部，agent 不该看）
  2. gen.py 生成一次性题目实例（答案随机）
  3. 预置一个 active goal（checks 留空，避免把答案泄露给 agent）
  4. headless 跑 pi + goal 扩展（PI_GOAL_NO_SEARCH=1 关搜索），驱动自驱循环
  5. verify.py 在外部做客观判定（pass/fail）
  6. 打印 PASS/FAIL + turns + 秒数，并追加到 results.tsv
"""

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
GOAL_EXT = os.path.abspath(os.path.join(ROOT, "..", "..", "index.ts"))


def goal_turns(work):
    try:
        return json.load(open(os.path.join(work, ".pi", "goals", "g-bench.json")))["spent"]["turns"]
    except Exception:
        return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("challenge")
    ap.add_argument("--mode", choices=["auto", "direct", "tree"], default="auto")
    ap.add_argument("--budget", type=int, default=12)
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    cdir = os.path.join(ROOT, "challenges", args.challenge)
    if not os.path.isdir(cdir):
        sys.exit(f"未知题目：{args.challenge}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    run = os.path.join(ROOT, "runs", f"{args.challenge}-{args.mode}-{ts}")
    work, secret = os.path.join(run, "work"), os.path.join(run, "secret")
    os.makedirs(work)
    os.makedirs(secret)

    subprocess.run([sys.executable, os.path.join(cdir, "gen.py"), work, secret], check=True)
    objective = open(os.path.join(cdir, "prompt.md")).read().strip()

    sid = "bench-" + ts
    os.makedirs(os.path.join(work, ".pi", "goals"), exist_ok=True)
    now = int(time.time() * 1000)
    goal = {
        "id": "g-bench",
        "sessionId": sid,
        "objective": objective,
        "constraints": [],
        "checks": [],  # 留空：答案不写进 goal 文件
        "budget": {"turns": args.budget},
        "spent": {"turns": 0, "tokens": 0, "minutes": 0},
        "status": "active",
        "origin": "user",
        "createdAt": now,
        "updatedAt": now,
        "progress": {"turnsSinceChange": 0},
        "log": [],
    }
    with open(os.path.join(work, ".pi", "goals", "g-bench.json"), "w") as f:
        json.dump(goal, f, ensure_ascii=False, indent=2)

    env = dict(os.environ)
    env["PI_GOAL_NO_SEARCH"] = "1"  # 关搜索
    if args.mode != "auto":
        env["PI_GOAL_FORCE_MODE"] = args.mode

    cmd = ["pi", "--no-extensions", "-e", GOAL_EXT, "--session-id", sid, "-p", "继续推进当前的 thread goal。"]
    if args.model:
        cmd += ["--model", args.model]

    t0 = time.time()
    rc = None
    with open(os.path.join(run, "agent.log"), "w") as log:
        try:
            p = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = -9
    elapsed = time.time() - t0

    v = subprocess.run([sys.executable, os.path.join(cdir, "verify.py"), work, secret], capture_output=True, text=True)
    ok = v.returncode == 0
    line = f"{'PASS' if ok else 'FAIL'}\t{args.challenge}\t{args.mode}\tturns={goal_turns(work)}\t{elapsed:.0f}s\texit={rc}"
    print("\n================ RESULT ================")
    print(line)
    print(v.stdout.strip()[:400])
    print(f"run dir: {run}")
    with open(os.path.join(ROOT, "results.tsv"), "a") as f:
        f.write(line.replace("\t", "\t") + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
