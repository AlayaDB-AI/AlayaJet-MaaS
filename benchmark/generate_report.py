#!/usr/bin/env python3
"""从评估根目录（benchmark/runs/<eval_id>/）生成人类可读报告 report.md。

对应 framework.md §8：质量门槛判定、容量结论、资源与成本汇总。
用法: python3 generate_report.py <eval_dir>
"""
import glob
import json
import os
import sys


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def fmt(v, nd=0):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "-"


def main():
    eval_dir = sys.argv[1]
    manifest = load_json(os.path.join(eval_dir, "manifest.json"), {}) or {}
    quality = load_json(os.path.join(eval_dir, "quality", "quality.json"))
    baseline = load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "quality", "quality_baseline.json"))

    lines = []
    lines.append(f"# 模型部署评估报告：{manifest.get('model', '?')}")
    lines.append("")
    lines.append(f"- run_id: `{manifest.get('run_id') or manifest.get('eval_id', '?')}`")
    lines.append(f"- 部署配置: {manifest.get('deployment', '?')}")
    lines.append(f"- git: `{manifest.get('git_commit', '?')}`"
                 + ("（有未提交改动）" if manifest.get("git_dirty") else ""))
    lines.append(f"- 时间: {manifest.get('started_at', '?')}")
    em = manifest.get("experiment_matrix") or {}
    if em:
        prec = em.get("precision") or {}
        opt = em.get("optimization") or {}
        lines.append(
            f"- 实验矩阵: {em.get('runtime_revision', '?')} · "
            f"{em.get('hardware_topology', '?')} · "
            f"dtype={prec.get('dtype', '?')}/kv={prec.get('kv_cache_dtype', '?')} · "
            f"量化={em.get('quantization', '?')} · "
            f"prefix_cache={'开' if opt.get('prefix_cache') else '关'}/"
            f"kv_offload={'开' if opt.get('kv_offload_hicache') else '关'}/"
            f"cuda_graph={'开' if opt.get('cuda_graph') else '关'}")
    lines.append("")

    # ---- workload 覆盖清单（§4 最低要求）----
    metrics_top = load_json(os.path.join(eval_dir, "metrics.json"), {}) or {}
    cov = metrics_top.get("workload_coverage")
    if cov:
        lines.append("## workload 覆盖清单（framework.md §4）")
        lines.append("")
        lines.append("| 要求维度 | 状态 | 覆盖 workload |")
        lines.append("|---|---|---|")
        for name, c in cov.items():
            if c["status"] == "covered":
                st, by = "✅ 已覆盖", ", ".join(c.get("by", []))
            elif c["status"] == "n/a":
                st, by = "➖ 不适用", c.get("note", "")
            else:
                st, by = "❌ 缺失", c.get("note", "")
            lines.append(f"| {name} | {st} | {by} |")
        lines.append("")

    # ---- 质量门槛（§2 quality_pass）----
    lines.append("## 质量判定（quality_pass）")
    lines.append("")
    if quality:
        lines.append("| suite | 得分 | 冻结基线 | 判定 |")
        lines.append("|---|---|---|---|")
        thresholds = (baseline or {}).get("thresholds", {})
        tol = (baseline or {}).get("tolerance", 0.02)
        for name, s in sorted(quality.get("suites", {}).items()):
            score = s.get("score")
            base = thresholds.get(name)
            if base is None:
                verdict = "（无基线）"
            else:
                verdict = "✅ PASS" if score >= base - tol else "❌ FAIL"
            lines.append(f"| {name} | {fmt(score, 4)} | {fmt(base, 4)} | {verdict} |")
        lines.append("")
        lines.append(f"> 质量 run: `{quality.get('run_id', '?')}`；"
                     f"容差 ±{tol}（quality_baseline.json）")
    else:
        lines.append("本次评估未包含质量测试。")
    lines.append("")

    # ---- 性能与容量（§5）----
    lines.append("## 性能与容量（per workload）")
    lines.append("")
    perf_dirs = sorted(glob.glob(os.path.join(eval_dir, "perf", "*")))
    for pd in perf_dirs:
        m = load_json(os.path.join(pd, "metrics.json"))
        if not m:
            continue
        name = os.path.basename(pd)
        lines.append(f"### {m.get('workload', name)}")
        lines.append("")
        lines.append("| stage | offered rps | 成功率 | 超时率 | TTFT p95/p99 | TPOT p99 | E2E p99 | SLO |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for s in m.get("stages", []):
            lines.append(
                f"| {s['label']} | {s['offered_request_rate']} "
                f"| {fmt(s['success_rate'] * 100, 1)}% "
                f"| {fmt((s.get('timeout_rate') or 0) * 100, 2)}% "
                f"| {fmt(s['ttft_ms']['p95'])}/{fmt(s['ttft_ms']['p99'])} ms "
                f"| {fmt(s['tpot_ms']['p99'])} ms "
                f"| {fmt(s['e2e_ms']['p99'])} ms "
                f"| {'✅' if s['slo_pass'] else '❌'} |")
        knee = m.get("capacity_knee")
        if knee:
            lines.append("")
            lines.append(f"**容量拐点：最大稳定 {knee['max_stable_request_rate']} req/s**"
                         + (f"，{knee.get('breaking_point_rate')} req/s 处撞线"
                            if knee.get("breaking_point_rate") else "")
                         + f"；建议水位 {knee.get('recommended_operating_rate')} req/s")
        gpu = m.get("gpu")
        sm = m.get("server_metrics") or {}
        if gpu:
            lines.append(f"\nGPU：平均 {fmt(gpu['avg_power_w'], 1)}W · "
                         f"SM 利用率 {fmt(gpu['avg_sm_util_pct'], 1)}% · "
                         f"能耗 {fmt(gpu['energy_wh'], 1)}Wh")
        if "cache_hit_rate" in sm:
            lines.append(f"\nprefix cache 命中率：mean {fmt(sm['cache_hit_rate']['mean'], 3)}"
                         f" · 队列峰值 {fmt((sm.get('num_queue_reqs') or {}).get('max'))}")
        if "queue_time_ms" in sm:
            q = sm["queue_time_ms"]
            lines.append(f"\n排队时间（§3.1）：mean {fmt(q['mean'])} ms · "
                         f"p95 {fmt(q['p95'])} ms · p99 {fmt(q['p99'])} ms")
        if "preemptions_total" in sm:
            lines.append(f"\npreemption 次数：{fmt(sm['preemptions_total']['delta'])}")
        if "kv_cache_capacity_tokens" in sm:
            cap = sm["kv_cache_capacity_tokens"]["max"]
            used = (sm.get("kv_cache_used_tokens") or {}).get("max")
            pct = f"（峰值占用 {fmt(used / cap * 100, 1)}%）" if used and cap else ""
            lines.append(f"\nKV cache 容量：{fmt(cap)} tokens{pct}")
        if "reused_prefix_tokens_est" in sm:
            lines.append(f"\n外部复用 token（估算）："
                         f"{fmt(sm['reused_prefix_tokens_est']['value'])}")
        host = m.get("host")
        if host:
            lines.append(f"\n主机：CPU mean/max {fmt(host['cpu_util_pct']['mean'], 1)}"
                         f"/{fmt(host['cpu_util_pct']['max'], 1)}% · "
                         f"内存峰值 {fmt(host['mem_used_pct']['max'], 1)}% · "
                         f"网络峰值 rx {fmt(host['net_rx_mbps_max'], 1)} MB/s · "
                         f"磁盘写峰值 {fmt(host['disk_write_mbps_max'], 1)} MB/s")
        for fe in (m.get("fault_recovery") or []):
            lines.append(f"\n**故障恢复**：{fe.get('fault_type')} 注入 → "
                         f"重启到健康 {fe.get('restart_to_healthy_s')}s · "
                         f"恢复后 stage `{fe.get('stage_dir')}`")
        lines.append("")

    # ---- 成本（§3.2/§3.3，四维口径）----
    lines.append("## 成本汇总")
    lines.append("")
    cost = load_json(os.path.join(eval_dir, "cost.json"))
    if cost:
        pa = cost.get("price_assumptions") or {}
        lines.append(f"换算标准：GPU 卡时 {pa.get('gpu_hourly_price_cny', '?')} 元/卡时 × "
                     f"{cost.get('gpu_count', pa.get('gpu_count', '?'))} 卡 · 电价 "
                     f"{pa.get('electricity_price_per_kwh', '?')} 元/kWh × PUE "
                     f"{pa.get('pue_factor', 1.0)} · 共享设施 "
                     f"{pa.get('shared_infra_hourly_cny', 0)} 元/h"
                     f"（完整定义见 cost_config.json）")
        lines.append("")
        lines.append("| 维度 | 金额（元） | 含义 |")
        lines.append("|---|---|---|")
        lines.append(f"| 分配成本 | {cost.get('allocated_cost_cny', '-')} | 独占 GPU 卡时，无论是否跑满 |")
        lines.append(f"| 实际使用成本 | {cost.get('usage_cost_cny', '-')} | 实测能耗电费（dmon 功率积分） |")
        lines.append(f"| 共享成本 | {cost.get('shared_cost_cny', '-')} | 控制面/共享设施分摊 |")
        lines.append(f"| 空闲成本 | {cost.get('idle_cost_cny', '-')} | 分配成本 × (1−平均SM利用率)，构成分析不计入总额 |")
        lines.append(f"| **总成本** | **{cost.get('total_cost_cny', '-')}** | 分配+使用+共享 |")
        lines.append("")
        lines.append(f"- 每 accepted request：{cost.get('cost_per_accepted_request_cny', '-')} 元")
        lines.append(f"- 每百万 accepted input token："
                     f"{cost.get('cost_per_1m_accepted_input_tokens_cny', '-')} 元")
        lines.append(f"- 每百万 accepted output token："
                     f"{cost.get('cost_per_1m_accepted_output_tokens_cny', '-')} 元")
        lines.append(f"- 总能耗：{cost.get('total_energy_kwh', '-')} kWh")
        lines.append("")
        lines.append("per workload：")
        for k, v in (cost.get("per_workload") or {}).items():
            lines.append(f"- `{k}`: {v} 元")
    else:
        lines.append("未配置 cost_config.json，无成本数据。")
    lines.append("")

    report = "\n".join(lines)
    with open(os.path.join(eval_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
