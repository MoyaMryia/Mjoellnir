#!/usr/bin/env python3
"""验证 crypto 题：随机 flag 是否被还原到工作目录的某个文件里。"""
import os
import sys


def main():
    work, secret = sys.argv[1], sys.argv[2]
    flag = open(os.path.join(secret, "answer.txt")).read().strip()
    hit = None
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in (".pi", ".git")]
        for name in files:
            p = os.path.join(root, name)
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if flag in text:
                hit = p
                break
        if hit:
            break
    if hit:
        print(f"PASS 找到 flag 于 {os.path.relpath(hit, work)}")
        return 0
    print(f"FAIL 未找到 flag（期望 {flag[:14]}…）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
