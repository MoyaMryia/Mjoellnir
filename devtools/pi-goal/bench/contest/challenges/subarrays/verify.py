#!/usr/bin/env python3
"""验证 subarrays 题：小规模暴力对拍 + 大数据计时，对参考解（双单调队列）。"""
import collections
import os
import random
import subprocess
import sys
import time


def brute(a, k):
    c = 0
    n = len(a)
    for i in range(n):
        mx = mn = a[i]
        for j in range(i, n):
            if a[j] > mx:
                mx = a[j]
            if a[j] < mn:
                mn = a[j]
            if mx - mn <= k:
                c += 1
            else:
                # 非单调，不能 break
                pass
    return c


def ref(a, k):
    n = len(a)
    mx = collections.deque()
    mn = collections.deque()
    l = 0
    ans = 0
    for r in range(n):
        while mx and a[mx[-1]] <= a[r]:
            mx.pop()
        mx.append(r)
        while mn and a[mn[-1]] >= a[r]:
            mn.pop()
        mn.append(r)
        while a[mx[0]] - a[mn[0]] > k:
            l += 1
            if mx[0] < l:
                mx.popleft()
            if mn[0] < l:
                mn.popleft()
        ans += r - l + 1
    return ans


def run(sol, work, inp, tl):
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, sol], input=inp, capture_output=True, text=True, timeout=tl, cwd=work)
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
    for t in range(6):  # 小规模对拍（含大量重复值/极端 K）
        n = rnd.randint(1, 40)
        a = [rnd.randint(0, rnd.choice([3, 10, 100])) for _ in range(n)]
        k = rnd.choice([0, 1, 5, rnd.randint(0, 100)])
        inp = f"{n} {k}\n{' '.join(map(str, a))}\n"
        p, _ = run(sol, work, inp, 15)
        if p is None or p.returncode != 0:
            print(f"FAIL: 小测试{t} 运行失败 {'' if p is None else p.stderr[:200]}")
            return 1
        exp = brute(a, k)
        if p.stdout.strip() != str(exp):
            print(f"FAIL: 小测试{t} 答案错（得到 {p.stdout.strip()!r}，期望 {exp}）")
            return 1

    n = 200000  # 大数据：计时
    a = [rnd.randint(0, 10 ** 9) for _ in range(n)]
    k = rnd.randint(0, 5 * 10 ** 6)
    inp = f"{n} {k}\n{' '.join(map(str, a))}\n"
    p, el = run(sol, work, inp, 12)
    if p is None:
        print("FAIL: 大数据超时（>12s）")
        return 1
    if p.returncode != 0:
        print(f"FAIL: 大数据运行出错 {p.stderr[:200]}")
        return 1
    if p.stdout.strip() != str(ref(a, k)):
        print(f"FAIL: 大数据答案错（得到 {p.stdout.strip()!r}，期望 {ref(a, k)}）")
        return 1
    print(f"PASS: 6 组对拍 + N={n} 用时 {el:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
