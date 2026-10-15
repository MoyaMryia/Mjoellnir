#!/usr/bin/env python3
"""隐写题：随机 flag 按位写进 24 位 BMP 像素数据的最低有效位（LSB）。"""
import os
import secrets
import struct
import sys


def main():
    work, secret = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    os.makedirs(secret, exist_ok=True)
    flag = "flag{" + secrets.token_hex(8) + "}"

    w, h = 96, 96
    row = (w * 3 + 3) & ~3  # BMP 每行 4 字节对齐
    data = bytearray(secrets.token_bytes(row * h))
    bits = "".join(f"{b:08b}" for b in flag.encode())
    for i, bit in enumerate(bits):
        data[i] = (data[i] & 0xFE) | int(bit)

    filesize = 54 + len(data)
    header = b"BM" + struct.pack("<IHHI", filesize, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    with open(os.path.join(work, "image.bmp"), "wb") as f:
        f.write(header + dib + bytes(data))
    with open(os.path.join(secret, "answer.txt"), "w") as f:
        f.write(flag + "\n")


if __name__ == "__main__":
    main()
