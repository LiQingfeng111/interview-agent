"""查清"我和谁在同一个局域网"。

输出四块：
  1. 我的每块网卡：地址、掩码、子网范围（这就是"辐射范围"）
  2. 默认网关（出口在哪）
  3. 已经出现在 ARP/邻居表里的设备（= 真实通信过的同网段机器）
  4. 判定：哪些邻居和我同一个子网

用法：python tools/who_is_my_lan.py
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys

SKIP_IFACE = ("Loopback", "vEthernet (Default Switch)", "Bluetooth")


def ps(cmd: str) -> str:
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout or ""


def _json_rows(cmd: str) -> list[dict]:
    out = ps(cmd)
    try:
        rows = json.loads(out) if out.strip() else []
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def my_addrs() -> list[dict]:
    rows = _json_rows("Get-NetIPAddress -AddressFamily IPv4 | "
                      "Select-Object IPAddress,PrefixLength,InterfaceAlias | ConvertTo-Json -Compress")
    res = []
    for r in rows:
        alias = str(r.get("InterfaceAlias", ""))
        if any(s in alias for s in SKIP_IFACE):
            continue
        try:
            res.append({"ip": r["IPAddress"], "prefix": int(r["PrefixLength"]), "iface": alias})
        except Exception:
            continue
    return res


def gateways() -> list[dict]:
    return _json_rows("Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                      "Select-Object NextHop,InterfaceAlias | ConvertTo-Json -Compress")


def neighbors() -> list[dict]:
    rows = _json_rows(
        "Get-NetNeighbor -AddressFamily IPv4 | "
        "Where-Object {$_.IPAddress -notlike '224.*' -and $_.IPAddress -ne '255.255.255.255'} | "
        "Select-Object IPAddress,LinkLayerAddress,State,InterfaceAlias | ConvertTo-Json -Compress")
    return [r for r in rows if r.get("IPAddress")]


def main() -> int:
    print("=" * 74)
    print("一、我这台机器的网络身份（每块网卡就是一条出口）")
    print("=" * 74)
    nets = []
    for a in my_addrs():
        try:
            net = ipaddress.ip_network(f"{a['ip']}/{a['prefix']}", strict=False)
        except Exception:
            continue
        nets.append((a, net))
        loop = "（回环：只在本机内部）" if a["ip"].startswith("127.") else ""
        print(f"  {a['iface']:<28} {a['ip']}/{a['prefix']} {loop}")
        if not a["ip"].startswith("127."):
            print(f"  {'':<28} 子网 {net}  范围 {net.network_address} ~ {net.broadcast_address}"
                  f"  可用约 {net.num_addresses - 2} 个")

    print()
    print("=" * 74)
    print("二、出口（默认网关）")
    print("=" * 74)
    for g in gateways():
        print(f"  {str(g.get('InterfaceAlias', '')):<28} → 网关 {g.get('NextHop', '')}")

    print()
    print("=" * 74)
    print("三、邻居表里出现过的设备（说明真的通过信）")
    print("=" * 74)
    nbs = neighbors()
    if not nbs:
        print("  （空：还没和同网段的设备通信过）")
    for n in nbs[:20]:
        print(f"  {str(n.get('IPAddress', '')):<16} {str(n.get('LinkLayerAddress', '')):<18} "
              f"{str(n.get('State', '')):<12} {n.get('InterfaceAlias', '')}")

    print()
    print("=" * 74)
    print("四、判定：哪些邻居和我同一个子网（= 同一个局域网）")
    print("=" * 74)
    same = 0
    for n in nbs:
        try:
            ip = ipaddress.ip_address(str(n["IPAddress"]))
        except Exception:
            continue
        for a, net in nets:
            if a["ip"].startswith("127."):
                continue
            if ip in net:
                print(f"  [同网段] {n['IPAddress']}  与 {a['ip']}/{a['prefix']} 同一子网（{net}）")
                same += 1
                break
    print()
    if same:
        print(f"结论：你此刻至少和 {same} 台设备在同一个局域网里。")
    else:
        print("结论：邻居表里没有同网段设备（可能刚断开、或网络开了终端隔离）。")
    print()
    print("提示：子网范围 = 你的局域网范围。换 WiFi / 换路由器，就不再是同一个局域网。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
