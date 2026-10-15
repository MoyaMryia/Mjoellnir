#!/usr/bin/env python3
"""生成一道多层编码的 CTF 解密题。flag 每次随机，全网搜不到。"""
import base64, codecs, os, secrets, sys


def main():
    work, secret = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    os.makedirs(secret, exist_ok=True)
    flag = "flag{" + secrets.token_hex(8) + "}"
    s = codecs.encode(flag, "rot13")           # 1. ROT13
    s = s.encode().hex()                        # 2. hex
    s = base64.b64encode(s.encode()).decode()   # 3. base64
    s = s[::-1]                                 # 4. 反转
    with open(os.path.join(work, "cipher.txt"), "w") as f:
        f.write(s + "\n")
    with open(os.path.join(secret, "answer.txt"), "w") as f:
        f.write(flag + "\n")


if __name__ == "__main__":
    main()
