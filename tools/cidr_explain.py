"""解释 CIDR 写法：/8 /12 /16 到底在说什么，为什么一下就那么多地址。

用法：python tools/cidr_explain.py
"""
from __future__ import annotations

import ipaddress
import sys


def bits(n: ipaddress.IPv4Address) -> str:
    return ".".join(f"{b:08b}" for b in n.packed)


def main() -> int:
    print("=== 1) IP 是 32 位数字，写成 4 段十进制，每段 8 位 ===")
    for s in ("10.0.0.0", "172.16.0.0", "192.168.0.0", "10.4.163.79"):
        a = ipaddress.ip_address(s)
        print(f"  {s:<15} = {bits(a)}  = {int(a):>12,}")

    print()
    print("=== 2) /8 是'前 8 位固定'，剩下的位随便变 ===")
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "10.0.0.0/22", "10.0.0.0/24", "10.0.0.0/30"):
        net = ipaddress.ip_network(cidr, strict=False)
        free = 32 - net.prefixlen
        print(f"  {cidr:<18} 固定 {net.prefixlen:>2} 位 · 可变 {free:>2} 位 "
              f"→ 2^{free:<2} = {net.num_addresses:>13,} 个")

    print()
    print("=== 3) 前缀每少一位，容量翻一倍 ===")
    for p in (30, 28, 26, 24, 22, 20, 16, 12, 8, 1):
        print(f"  /{p:<3} → {2 ** (32 - p):>15,} 个地址")

    print()
    print("=== 4) 掩码是同一件事的另一种写法 ===")
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/22", "10.0.0.0/24"):
        net = ipaddress.ip_network(cidr)
        print(f"  /{net.prefixlen:<3} 掩码 {str(net.netmask):<16} "
              f"范围 {net.network_address} ~ {net.broadcast_address}")

    print()
    print("=== 5) 为什么 172.16/12 是 16 个 /16 ===")
    print("  第二段只能取 16~31（共 16 个值），每个 /16 有 65,536 个")
    print(f"  16 × 65,536 = {16 * 65536:,} = 2^20 ✓")

    print()
    print("=== 6) 0.42% 怎么算的 ===")
    priv = 2 ** 24 + 2 ** 20 + 2 ** 16
    print(f"  私网合计 {priv:,} ÷ IPv4 总量 {2 ** 32:,} = {priv / 2 ** 32 * 100:.4f}%")

    print()
    print("=== 7) 你的地址落在哪 ===")
    groups = [("RFC1918 私网", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]),
              ("CGNAT 共享段", ["100.64.0.0/10"])]
    for ip in ("10.4.163.79", "172.22.48.1", "172.19.0.1", "100.90.97.27", "183.24.41.212"):
        a = ipaddress.ip_address(ip)
        tag = "可全球路由的公网"
        for name, blocks in groups:
            if any(a in ipaddress.ip_network(b) for b in blocks):
                tag = name
                break
        print(f"  {ip:<16} → {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
