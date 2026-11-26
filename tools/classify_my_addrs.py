"""把本机每个地址分类：私网 / 公网 / 回环 / 链路本地 / 共享(CGNAT)。

很多人分不清"我的地址"到底指哪个，因为一台机器上同时存在五六个地址，
而它们的可见范围完全不同。这个脚本把每个地址的"谁能访问"直接标出来。

用法：python tools/classify_my_addrs.py
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys

# RFC1918 私网段 + 其他特殊段
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
LINKLOCAL = ipaddress.ip_network("169.254.0.0/16")
CGNAT = ipaddress.ip_network("100.64.0.0/10")   # 运营商级 NAT，Tailscale 也用这段
DOCKER = [ipaddress.ip_network(n) for n in ("172.17.0.0/16", "172.18.0.0/16", "172.19.0.0/16")]


def ps(cmd: str) -> str:
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout or ""


def collect() -> list[dict]:
    out = ps("Get-NetIPAddress | Select-Object IPAddress,PrefixLength,InterfaceAlias,AddressFamily | "
             "ConvertTo-Json -Compress")
    try:
        rows = json.loads(out) if out.strip() else []
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    res = []
    for r in rows:
        addr = str(r.get("IPAddress", ""))
        if not addr or addr.startswith("fe80"):
            continue
        try:
            ip = ipaddress.ip_address(addr)
        except Exception:
            continue
        res.append({"ip": addr, "ver": 4 if ip.version == 4 else 6,
                    "iface": str(r.get("InterfaceAlias", "")), "obj": ip})
    return res


def classify(ip) -> tuple[str, str, str]:
    """返回 (类别, 谁能访问, 说明)"""
    if ip.version == 6:
        if ip.is_loopback:
            return "回环 v6", "只有本机", "::1 等价于 127.0.0.1"
        if ip.is_link_local:
            return "链路本地 v6", "同一段链路", "fe80:: 只在本地链路上有效"
        return "IPv6", "视前缀而定", "取决于运营商分配的前缀"

    if ip in LOOPBACK:
        return "回环", "只有本机", "假网卡 lo，数据包不出机器"
    if ip in LINKLOCAL:
        return "链路本地", "几乎无人", "通常表示没拿到 DHCP 地址（自分配）"
    if ip in CGNAT:
        return "共享地址(CGNAT)", "同一虚拟网络内", "运营商级 NAT 段；Tailscale 也用这段"
    for d in DOCKER:
        if ip in d:
            return "Docker 虚拟网桥", "容器之间", "docker/compose 自动创建"
    if ip in PRIVATE[0] or any(ip in n for n in PRIVATE):
        return "私网(RFC1918)", "同一子网内的设备", "全世界重复使用，出网要经 NAT"
    if ip.is_global:
        return "公网", "整个互联网", "全球唯一，由运营商分配"
    return "其他", "不确定", ""


def main() -> int:
    rows = collect()
    if not rows:
        print("没读到网卡信息")
        return 1

    print("=" * 96)
    print("本机每个地址分别是什么、谁能访问")
    print("=" * 96)
    print(f"{'地址':<26}{'网卡':<26}{'类别':<18}{'谁能访问':<22}")
    print("-" * 96)
    for r in rows:
        kind, who, _ = classify(r["obj"])
        print(f"{r['ip']:<26}{r['iface'][:24]:<26}{kind:<18}{who:<22}")

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    private_like = [r for r in rows if classify(r["obj"])[0] in
                    ("私网(RFC1918)", "回环", "链路本地", "共享地址(CGNAT)", "Docker 虚拟网桥")]
    print(f"本机共 {len(rows)} 个地址，其中 {len(private_like)} 个**不是公网地址**：")
    for r in private_like:
        print(f"  · {r['ip']:<20} {classify(r['obj'])[0]}")
    print()
    print("含义：这些地址在互联网上不存在，别人不可能直接用它们访问你的服务。")
    print("      要让外面的人访问，必须走公网地址 + 端口映射（或用虚拟局域网）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
