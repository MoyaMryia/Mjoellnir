#!/usr/bin/env python3
"""生成「max-min<=K 子数组计数」题的输入（N=2e5）。"""
import os
import random
import sys


def main():
    work, _secret = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    n = 200000
    rnd = random.Random(777)
    arr = [rnd.randint(0, 10 ** 9) for _ in range(n)]
    k = rnd.randint(0, 5 * 10 ** 6)
    with open(os.path.join(work, "input.txt"), "w") as f:
        f.write(f"{n} {k}\n{' '.join(map(str, arr))}\n")
    with open(os.path.join(work, "problem.md"), "w") as f:
        f.write(
            "# 子数组计数\n\n"
            "给定 N 个整数 a[1..N] 和整数 K，求满足 `max(a[l..r]) - min(a[l..r]) <= K` 的子数组数量。\n\n"
            "输入：第一行 `N K`；第二行 N 个整数。输出：一个整数（用 64 位整数）。\n\n"
            f"本目录 `input.txt` 的 N={n}。朴素 O(N^2) 会超时。\n"
        )


if __name__ == "__main__":
    main()
