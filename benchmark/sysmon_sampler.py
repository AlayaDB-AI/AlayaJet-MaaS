#!/usr/bin/env python3
"""远端主机资源采样器（framework.md §3.2 CPU/内存/网络/存储吞吐）。

从 /proc 读取，1 秒粒度输出 CSV 行到 stdout：
  epoch,cpu_pct,mem_used_pct,net_rx_kbps,net_tx_kbps,disk_read_kbps,disk_write_kbps

只依赖标准库，用远端系统 python3 运行：
  nohup python3 sysmon_sampler.py > sysmon.log 2>&1 &
"""
import re
import time

WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+)$")


def cpu_times():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals), idle


def mem_used_pct():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.strip().split()[0])
    total = info["MemTotal"]
    return 100.0 * (total - info.get("MemAvailable", info.get("MemFree", 0))) / total


def net_bytes():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":")
            if iface.strip() == "lo":
                continue
            fields = rest.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def disk_sectors():
    r = w = 0
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if len(p) < 14 or not WHOLE_DISK.match(p[2]):
                continue
            r += int(p[5])   # sectors read
            w += int(p[9])   # sectors written
    return r * 512, w * 512  # 字节


def main():
    prev = (time.time(), *cpu_times(), *net_bytes(), *disk_sectors())
    while True:
        time.sleep(1.0)
        now = time.time()
        total, idle = cpu_times()
        rx, tx = net_bytes()
        dr, dw = disk_sectors()
        dt = now - prev[0]
        d_total = total - prev[1]
        d_idle = idle - prev[2]
        cpu_pct = 100.0 * (1 - d_idle / d_total) if d_total > 0 else 0.0
        print(f"{now:.0f},{cpu_pct:.1f},{mem_used_pct():.1f},"
              f"{(rx - prev[3]) / dt / 1024:.1f},{(tx - prev[4]) / dt / 1024:.1f},"
              f"{(dr - prev[5]) / dt / 1024:.1f},{(dw - prev[6]) / dt / 1024:.1f}",
              flush=True)
        prev = (now, total, idle, rx, tx, dr, dw)


if __name__ == "__main__":
    main()
