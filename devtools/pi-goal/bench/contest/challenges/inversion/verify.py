#!/usr/bin/env python3
"""验证 inversion 题：现场生成随机输入（防打表）+ 大输入计时，与参考解对比。"""
import os
import random
import subprocess
import sys
import time


def inv_count(a):
    def ms(x):
        if len(x) <= 1:
            return x, 0
        m = len(x) // 2
        left, cl = ms(x[:m])
        right, cr = ms(x[m:])
        out = []
        i = j = 0
        c = cl + cr
        while i < len(left) and j < len(right):
            if left[i] <= right[j]:
                out.append(left[i])
                i += 1
            else:
                out.append(right[j])
                j += 1
                c += len(left) - i
        out += left[i:]
        out += right[j:]
        return out, c

    return ms(a)[1]


def run(sol, work, inp, tl):
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, sol], input=inp, capture_output=True, text=True, timeout=tl, cwd=work
        )
    except subprocess.TimeoutExpired:
        return None, tl
    return p, time.time() - t0


def main():
    work, _secret = sys.argv[1], sys.argv[2]
    sol = os.path.join(work, "solution.py")
    if not os.path.exists(sol):
        print("FAIL: solution.py 不存在")
        return 1

    rnd = random.Random()
    for t in range(5):  # 小规模随机测试，防打表
        n = rnd.randint(1, 60)
        a = [rnd.randint(0, 20) for _ in range(n)]
        inp = f"{n}\n{' '.join(map(str, a))}\n"
        p, _ = run(sol, work, inp, 15)
        if p is None or p.returncode != 0:
            print(f"FAIL: 测试{t} 运行失败 {'' if p is None else p.stderr[:200]}")
            return 1
        exp = inv_count(a)
        if p.stdout.strip() != str(exp):
            print(f"FAIL: 测试{t} 答案错（得到 {p.stdout.strip()!r}，期望 {exp}）")
            return 1

    n = 200000  # 大输入：计时
    a = [rnd.randint(0, 10 ** 9) for _ in range(n)]
    inp = f"{n}\n{' '.join(map(str, a))}\n"
    p, el = run(sol, work, inp, 12)
    if p is None:
        print("FAIL: 大输入超时（>12s）")
        return 1
    if p.returncode != 0:
        print(f"FAIL: 大输入运行出错 {p.stderr[:200]}")
        return 1
    if p.stdout.strip() != str(inv_count(a)):
        print("FAIL: 大输入答案错")
        return 1
    print(f"PASS: 5 随机测试 + N={n} 用时 {el:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
