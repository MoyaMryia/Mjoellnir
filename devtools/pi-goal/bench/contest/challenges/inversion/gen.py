#!/usr/bin/env python3
"""生成逆序对题的大输入（答案由 verify 现场用参考解算，不落盘）。"""
import os
import random
import sys


def main():
    work, _secret = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    n = 200000
    rnd = random.Random(20260101)
    arr = [rnd.randint(0, 10 ** 9) for _ in range(n)]
    with open(os.path.join(work, "input.txt"), "w") as f:
        f.write(f"{n}\n{' '.join(map(str, arr))}\n")
    with open(os.path.join(work, "problem.md"), "w") as f:
        f.write(
            "# 逆序对\n\n"
            "给定 N 个整数 a[1..N]，求满足 i<j 且 a[i]>a[j] 的数对数量。\n\n"
            "输入：第一行 N；第二行 N 个整数。输出：一个整数。\n\n"
            f"本目录 `input.txt` 的 N={n}。朴素 O(N^2) 会超时。\n"
        )


if __name__ == "__main__":
    main()
