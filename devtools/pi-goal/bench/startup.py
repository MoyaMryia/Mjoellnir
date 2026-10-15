#!/usr/bin/env python3
"""pi 冷启动基准。

守护"轻"契约：交互首屏必须快，扩展不得拖重启动。
    python3 bench/startup.py            # 用默认阈值
    PI_STARTUP_BUDGET_MS=300 ...        # 覆盖阈值

退出码非 0 表示超预算。
"""

import os
import pty
import select
import signal
import statistics
import subprocess
import sys
import time

BUDGET_MS = float(os.environ.get("PI_STARTUP_BUDGET_MS", "300"))
RUNS = int(os.environ.get("PI_STARTUP_RUNS", "5"))


def _time_cmd(args: list[str]) -> float:
    t0 = time.perf_counter()
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return (time.perf_counter() - t0) * 1000.0


def measure_version() -> float:
    return statistics.median(_time_cmd(["pi", "--version"]) for _ in range(RUNS))


def measure_help() -> float:
    # --help 会注册扩展/命令，接近真实启动路径。
    return statistics.median(_time_cmd(["pi", "--help"]) for _ in range(RUNS))


def measure_first_screen(timeout: float = 8.0) -> float:
    """交互启动到首屏第一字节（pty）。"""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        try:
            os.execvp("pi", ["pi"])
        except Exception:
            os._exit(127)
    start = time.perf_counter()
    first = None
    try:
        while time.perf_counter() - start < timeout:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(fd, 8192)
                except OSError:
                    break
                if data and first is None:
                    first = (time.perf_counter() - start) * 1000.0
                    break
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
    if first is None:
        raise RuntimeError("交互启动在超时内没有输出")
    return first


def main() -> int:
    version = measure_version()
    help_ms = measure_help()
    first = measure_first_screen()
    ext_ms = help_ms - version

    print(f"pi --version      : {version:6.1f} ms")
    print(f"pi --help         : {help_ms:6.1f} ms  (含扩展注册)")
    print(f"扩展注册增量      : {ext_ms:6.1f} ms")
    print(f"交互首屏          : {first:6.1f} ms")

    ok = first <= BUDGET_MS
    print(f"\n预算 交互首屏 <= {BUDGET_MS:.0f} ms : {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
