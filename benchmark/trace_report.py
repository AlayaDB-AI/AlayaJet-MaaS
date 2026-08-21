#!/usr/bin/env python3
"""把一次 trace 回放的 raw_result.json 汇总成 Markdown 报告。

输入：
  --result       trace_client.py 写出的 raw_result.json（必需）
  --trace        回放用的 trace JSONL（可选；提供后能算到达节奏分布）
  --server-log   SGLang 服务端日志（可选；提供后能提取峰值吞吐/队列）
  --model        被测模型名（可选；缺省尝试从 result 里读）
  --note         附加说明，可多次传

输出：report.md（默认与 raw_result.json 同目录）。只依赖标准库，
Windows 本机与远端执行机都能跑。
"""
import argparse
import json
import os
import re
import statistics
from datetime import datetime, timezone


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def fmt_ms(x):
    return "-" if x is None else "{:.1f}".format(x)


def load_trace_ts(path, limit):
    ts = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                v = obj.get("ts")
                if isinstance(v, (int, float)):
                    ts.append(float(v))
                if limit and len(ts) >= limit:
                    break
    except OSError:
        return None
    return ts


def arrival_summary(ts):
    if not ts:
        return None
    t_min, t_max = min(ts), max(ts)
    span = t_max - t_min
    n = len(ts)
    ordered = sorted(ts)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    max_gap = max(gaps) if gaps else 0
    buckets = {}
    for t in ordered:
        buckets[int((t - t_min) / 60)] = buckets.get(int((t - t_min) / 60), 0) + 1
    peak = max(buckets.values()) if buckets else 0
    hist = []
    for m in range(min(buckets), max(buckets) + 1):
        cnt = buckets.get(m, 0)
        bar = "" if cnt == 0 else "#" * max(1, round(cnt / peak * 24))
        hist.append((m, cnt, bar))
    return {
        "count": n,
        "span_s": span,
        "avg_rate": n / span if span > 0 else 0,
        "max_gap_s": max_gap,
        "peak_min": peak,
        "hist": hist,
    }


def parse_server_log(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    nums = lambda pat: [float(m) for m in re.findall(pat, text)]
    ints = lambda pat: [int(m) for m in re.findall(pat, text)]
    memory_lines = re.findall(
        r".*(?:Load weight end|KV Cache is allocated|Mamba Cache is allocated|"
        r"Memory pool end|server is fired up).*", text)
    return {
        "peak_gen_tps": max(nums(r"gen throughput \(token/s\): ([\d.]+)"), default=None),
        "peak_prefill_tps": max(nums(r"input throughput \(token/s\): ([\d.]+)"), default=None),
        "peak_running": max(ints(r"#running-req: (\d+)"), default=None),
        "peak_queue": max(ints(r"#queue-req: (\d+)"), default=None),
        "memory_lines": memory_lines[-4:] if memory_lines else [],
    }


def build_report(result, trace_ts, server, args):
    out = []
    add = out.append

    add("# 真实业务 trace 回放报告")
    add("")
    add("> 生成时间：{}（UTC）".format(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    add("")

    replayed = result.get("replayed", 0)
    completed = result.get("completed", 0)
    duration = result.get("duration")
    success_rate = completed / replayed if replayed else None

    add("## 1. 概览")
    add("")
    add("| 项目 | 值 |")
    add("|---|---|")
    add("| 目标服务 | `{}` |".format(result.get("url", "-")))
    add("| 模型 | `{}` |".format(args.model or result.get("model", "-")))
    add("| 到达模型 | {} |".format(result.get("arrival_model", "-")))
    add("| 时间缩放 time_scale | {} |".format(result.get("time_scale", "-")))
    add("| trace 文件 | `{}` |".format(result.get("trace_file", "-")))
    add("| 重放 / 成功 | {} / {} |".format(replayed, completed))
    add("| 成功率 | {} |".format(
        "{:.2%}".format(success_rate) if success_rate is not None else "-"))
    add("| 总时长 | {:.1f}s（{:.1f}min）|".format(duration, duration / 60) if duration else "| 总时长 | - |")
    add("| 调度误差 | 均值 {:.1f}ms / 最大 {:.1f}ms |".format(
        result.get("schedule_lag_ms_mean") or 0,
        result.get("schedule_lag_ms_max") or 0))
    add("")

    add("## 2. 吞吐")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add("| 请求吞吐 | {:.3f} req/s |".format(result.get("request_throughput") or 0))
    add("| 输入吞吐 | {:.0f} tok/s |".format(result.get("input_throughput") or 0))
    add("| 输出吞吐 | {:.0f} tok/s |".format(result.get("output_throughput") or 0))
    add("| 总输入 token | {} |".format(result.get("total_input_tokens") or 0))
    add("| 总输出 token | {} |".format(result.get("total_output_tokens") or 0))
    add("")

    ttfts = sorted(x for x in (result.get("ttfts") or []) if isinstance(x, (int, float)) and x > 0)
    e2es = []
    for t, itl in zip(result.get("ttfts") or [], result.get("itls") or []):
        if isinstance(t, (int, float)) and t > 0 and isinstance(itl, list):
            e2es.append(t + sum(itl))
    e2es = sorted(e2es)
    itls = (result.get("itls") or [])
    tpot_per_req = sorted(
        statistics.mean(x) for x in itls if isinstance(x, list) and x)

    def row_metrics(name, values_s):
        if not values_s:
            return ["| {} | - | - | - | - | - |".format(name)]
        return ["| {} | {:.1f}ms | {:.1f}ms | {:.1f}ms | {:.1f}ms | {:.1f}ms |".format(
            name,
            statistics.mean(values_s) * 1000,
            pct(sorted(values_s), 0.5) * 1000,
            pct(sorted(values_s), 0.9) * 1000,
            pct(sorted(values_s), 0.99) * 1000,
            max(values_s) * 1000)]

    add("## 3. 客户端视角延迟（含排队时间）")
    add("")
    add("| 指标 | 均值 | P50 | P90 | P99 | 最大 |")
    add("|---|---|---|---|---|---|")
    add(row_metrics("TTFT", ttfts)[0])
    add(row_metrics("TPOT（每请求平均 ITL）", tpot_per_req)[0])
    if e2es:
        add(row_metrics("端到端 E2E", e2es)[0])
    decode_s = sorted(sum(x) for x in itls if isinstance(x, list) and x)
    if ttfts and decode_s:
        add("")
        add("E2E 中位构成 ≈ TTFT（排队+prefill）{:.1f}s + 纯解码 {:.1f}s = {:.1f}s。".format(
            pct(ttfts, 0.5), pct(decode_s, 0.5),
            pct(ttfts, 0.5) + pct(decode_s, 0.5)))
    add("")

    in_lens = [x for x in (result.get("input_lens") or []) if isinstance(x, (int, float)) and x]
    out_lens = [x for x in (result.get("output_lens") or []) if isinstance(x, (int, float))]

    def size_row(name, values):
        if not values:
            return ["| {} | - | - | - | - | - | - |".format(name)]
        s = sorted(values)
        return ["| {} | {} | {:.0f} | {:.0f} | {:.0f} | {:.0f} | {} |".format(
            name, min(s), statistics.mean(s), pct(s, 0.5), pct(s, 0.9),
            pct(s, 0.99), max(s))]

    add("## 4. 请求规模（token）")
    add("")
    add("| 指标 | 最小 | 平均 | P50 | P90 | P99 | 最大 |")
    add("|---|---|---|---|---|---|---|")
    add(size_row("输入", in_lens)[0])
    add(size_row("输出", out_lens)[0])
    add("")

    arr = arrival_summary(trace_ts)
    add("## 5. 到达节奏（来自 trace 的 ts）")
    add("")
    if arr:
        add("- 请求数 {}，窗口 {:.0f}s（{:.1f}min），平均速率 {:.1f} 个/分钟".format(
            arr["count"], arr["span_s"], arr["span_s"] / 60, arr["avg_rate"] * 60))
        add("- 每分钟峰值 {} 个，最大相邻间隔 {:.0f}s".format(arr["peak_min"], arr["max_gap_s"]))
        add("")
        add("```")
        for minute, cnt, bar in arr["hist"]:
            add("  {:>3}min {:>4} |{}".format(minute, cnt, bar))
        add("```")
    else:
        add("- 未提供 trace 文件，无法统计到达节奏。")
    add("")

    # TTFT 随到达时间的变化：突发导致的排队通常表现为“某一分钟之后 TTFT 整体抬高”
    ttft_by_min = {}
    if trace_ts and result.get("ttfts"):
        t_min = min(trace_ts)
        for t, tt in zip(trace_ts, result.get("ttfts") or []):
            if isinstance(t, (int, float)) and isinstance(tt, (int, float)) and tt > 0:
                ttft_by_min.setdefault(int((t - t_min) / 60), []).append(tt)
    add("## 6. TTFT 按到达分钟")
    add("")
    if ttft_by_min:
        add("| 到达分钟 | 请求数 | TTFT 中位(s) | TTFT P90(s) | TTFT P99(s) |")
        add("|---|---|---|---|---|")
        for minute in sorted(ttft_by_min):
            vals = sorted(ttft_by_min[minute])
            add("| {} | {} | {:.1f} | {:.1f} | {:.1f} |".format(
                minute, len(vals), pct(vals, 0.5), pct(vals, 0.9), pct(vals, 0.99)))
        add("")
        add("同一分钟内 TTFT 单调抬升 = 该分钟的请求在服务端 FIFO 排队；")
        add("后续分钟的请求继承未排空的积压。")
    else:
        add("- 无可用数据。")
    add("")

    errors = [e for e in (result.get("errors") or []) if e]
    add("## 7. 失败明细")
    add("")
    if errors:
        from collections import Counter
        for msg, cnt in Counter(errors).most_common():
            add("- {} × {}".format(msg, cnt))
    else:
        add("无失败请求。")
    add("")

    add("## 8. 服务端观测")
    add("")
    if server:
        add("| 指标 | 峰值 |")
        add("|---|---|")
        add("| decode 吞吐 | {:.1f} tok/s |".format(server["peak_gen_tps"]) if server["peak_gen_tps"] is not None else "| decode 吞吐 | - |")
        add("| prefill 吞吐 | {:.1f} tok/s |".format(server["peak_prefill_tps"]) if server["peak_prefill_tps"] is not None else "| prefill 吞吐 | - |")
        add("| 并发 running | {} |".format(server["peak_running"]) if server["peak_running"] is not None else "| 并发 running | - |")
        add("| 排队长度 | {} |".format(server["peak_queue"]) if server["peak_queue"] is not None else "| 排队长度 | - |")
        if server["memory_lines"]:
            add("")
            add("关键加载信息：")
            add("```")
            for line in server["memory_lines"]:
                add(line.strip()[:220])
            add("```")
    else:
        add("未提供服务端日志，跳过。")
    add("")

    notes = list(args.note)
    if not notes:
        notes.append("TTFT/TPOT/E2E 为客户端视角，已包含服务端排队时间；到达节奏来自 trace 原始 ts。")
    add("## 9. 备注")
    add("")
    for n in notes:
        add("- {}".format(n))
    add("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True, help="trace_client.py 的 raw_result.json")
    ap.add_argument("--trace", default=None, help="回放用的 trace JSONL")
    ap.add_argument("--server-log", default=None, help="SGLang 服务端日志")
    ap.add_argument("--model", default=None, help="被测模型名")
    ap.add_argument("--title", default=None, help="报告标题（默认自动）")
    ap.add_argument("-n", "--note", action="append", default=[], help="附加备注，可多次传")
    ap.add_argument("-o", "--output", default=None, help="输出 md 路径（默认与 result 同目录 report.md）")
    args = ap.parse_args()

    with open(args.result, encoding="utf-8") as f:
        result = json.load(f)

    limit = result.get("replayed") or result.get("trace_lines") or 0
    trace_ts = load_trace_ts(args.trace, limit) if args.trace else None
    server = parse_server_log(args.server_log) if args.server_log else None
    report = build_report(result, trace_ts, server, args)
    if args.title:
        report = report.replace("# 真实业务 trace 回放报告",
                                "# {}".format(args.title), 1)

    out_path = args.output or os.path.join(os.path.dirname(os.path.abspath(args.result)), "report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print("report written: {}".format(out_path))


if __name__ == "__main__":
    main()
