#!/usr/bin/env python3
"""汇总一个 benchmark run 的 stage 结果，执行四级判定并生成 metrics/status/cost。

判定语义（docs/evaluation/framework.md §2）：
  request_success -> slo_pass -> quality_pass -> accepted

合成性能压测没有 ground truth，quality_pass 记为 None（由配对的质量 run 提供）。

v2 补齐（framework.md §3/§7）：
  - p50/p95/p99 由请求级明细（requests.jsonl）计算，无明细时回退 bench 聚合字段
  - 超时/错误率、accepted output token goodput
  - gpu_dmon.log 功率积分 → 能耗 kWh
  - logs/server_metrics.txt → KV cache 命中率/队列长度/token 使用率汇总
  - benchmark/cost_config.json 存在时产出 cost.json（§3.2 成本）

用法: python3 collect_results.py <run_dir>
"""
import glob
import json
import os
import re
import sys


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def g(d, key):
    """bench_serving 输出字段的容错读取。"""
    v = d.get(key)
    return v if isinstance(v, (int, float)) else None


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def load_stage_requests(stage_dir):
    """读取 stage 的请求级明细，返回 (records, 是否存在)。"""
    path = os.path.join(stage_dir, "requests.jsonl")
    if not os.path.isfile(path):
        return [], False
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return recs, True


def latency_stats(recs, key):
    vals = sorted(r[key] for r in recs
                  if isinstance(r.get(key), (int, float)))
    if not vals:
        return {"p50": None, "mean": None, "p95": None, "p99": None}
    return {"p50": percentile(vals, 0.50),
            "mean": sum(vals) / len(vals),
            "p95": percentile(vals, 0.95),
            "p99": percentile(vals, 0.99)}


def per_request_tpot(rec):
    itl = rec.get("itl_ms")
    if isinstance(itl, list) and itl:
        return sum(itl) / len(itl)
    return None


# ---------------------------------------------------------------- dmon / metrics

def parse_dmon(path):
    """nvidia-smi dmon -s pum -o T 输出 → 能耗与利用率汇总。

    支持多卡（-i 0,1,...）：同一时刻的多行按 GPU 聚合——功率求和（整组能耗）、
    SM 利用率取均值、显存取各卡峰值。
    """
    if not os.path.isfile(path):
        return None
    per_t = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                hh, mm, ss = parts[0].split(":")
                t = int(hh) * 3600 + int(mm) * 60 + int(ss)
                slot = per_t.setdefault(t, {"pwr": 0.0, "sm": [], "fb": []})
                slot["pwr"] += float(parts[2])
                slot["sm"].append(float(parts[5]))
                if len(parts) > 11:
                    slot["fb"].append(float(parts[11]))
            except (ValueError, IndexError):
                continue
    if len(per_t) < 2:
        return None
    rows = []
    for t in sorted(per_t):
        s = per_t[t]
        rows.append({"t": t, "pwr": s["pwr"],
                     "sm": sum(s["sm"]) / len(s["sm"]),
                     "fb": max(s["fb"]) if s["fb"] else None})
    # 处理跨天
    for i in range(1, len(rows)):
        if rows[i]["t"] < rows[i - 1]["t"]:
            rows[i]["t"] += 86400
    energy_wh = sum((rows[i]["pwr"] + rows[i - 1]["pwr"]) / 2
                    * (rows[i]["t"] - rows[i - 1]["t"]) for i in range(1, len(rows))) / 3600
    return {
        "samples": len(rows),
        "gpu_count": max(len(per_t[t]["sm"]) for t in per_t),
        "duration_s": rows[-1]["t"] - rows[0]["t"],
        "energy_wh": round(energy_wh, 2),
        "avg_power_w": round(sum(r["pwr"] for r in rows) / len(rows), 1),
        "max_power_w": max(r["pwr"] for r in rows),
        "avg_sm_util_pct": round(sum(r["sm"] for r in rows) / len(rows), 1),
        "max_fb_mb": max((r["fb"] for r in rows if r["fb"] is not None), default=None),
    }


# gauge：瞬时值，按 mean/max 汇总
GAUGE_METRICS = {
    "sglang:cache_hit_rate": "cache_hit_rate",
    "sglang:num_queue_reqs": "num_queue_reqs",
    "sglang:token_usage": "token_usage",
    "sglang:gen_throughput": "gen_throughput",
    "sglang:num_running_reqs": "num_running_reqs",
    "sglang:max_total_num_tokens": "kv_cache_capacity_tokens",
    "sglang:num_used_tokens": "kv_cache_used_tokens",
}
# counter：单调递增，按 first/last/delta 汇总（服务中途重启会出现负 delta，钳为 0）
COUNTER_METRICS = {
    "sglang:prompt_tokens_total": "prompt_tokens_total",
    "sglang:generation_tokens_total": "generation_tokens_total",
    # num_retractions 是"每请求 retraction 次数"的直方图：_sum 才是 preemption 事件总数
    "sglang:num_retractions_sum": "preemptions_total",
}
METRIC_LINE = re.compile(r"^(sglang:[a-z_]+)(?:\{[^}]*\})?\s+(-?[0-9.eE+]+)\s*$")
QUEUE_BUCKET_LINE = re.compile(
    r'^sglang:queue_time_seconds_bucket\{[^}]*\ble="([^"]+)"[^}]*\}\s+(-?[0-9.eE+]+)\s*$')
QUEUE_SUM_LINE = re.compile(
    r"^sglang:queue_time_seconds_sum(?:\{[^}]*\})?\s+(-?[0-9.eE+]+)\s*$")
QUEUE_COUNT_LINE = re.compile(
    r"^sglang:queue_time_seconds_count(?:\{[^}]*\})?\s+(-?[0-9.eE+]+)\s*$")


def _hist_quantile(cum_buckets, total, q):
    """从累积桶（{le: count}）估计分位数，桶内线性插值。"""
    if total <= 0 or not cum_buckets:
        return None
    target = q * total
    prev_le, prev_c = 0.0, 0.0
    for le in sorted(cum_buckets):
        c = cum_buckets[le]
        if c >= target:
            if c == prev_c:
                return le
            return prev_le + (le - prev_le) * (target - prev_c) / (c - prev_c)
        prev_le, prev_c = le, c
    return max(cum_buckets)  # 超出最大桶，返回上界


def parse_server_metrics(path):
    """/metrics 采样文件 → gauge/counter/直方图汇总（framework §3.1/§3.2）。

    产出：
      gauges   → mean/max/samples
      counters → first/last/delta（preemption 次数、prompt token 总量等）
      queue_time_ms → 每请求排队时间分布（queue_time_seconds 直方图首末差分）
      reused_prefix_tokens_est → 外部复用 token 估算（prompt delta × 命中率均值）
    """
    if not os.path.isfile(path):
        return None
    gauges, counters = {}, {}
    queue_snaps = []
    cur_buckets, cur_sum, cur_cnt = {}, None, None

    def flush():
        nonlocal cur_buckets, cur_sum, cur_cnt
        if cur_buckets and cur_cnt is not None:
            queue_snaps.append((dict(cur_buckets), cur_sum, cur_cnt))
        cur_buckets, cur_sum, cur_cnt = {}, None, None

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("===SCRAPE"):
                flush()
                continue
            mb = QUEUE_BUCKET_LINE.match(line)
            if mb:
                if mb.group(1) != "+Inf":
                    cur_buckets[float(mb.group(1))] = float(mb.group(2))
                continue
            ms = QUEUE_SUM_LINE.match(line)
            if ms:
                cur_sum = float(ms.group(1))
                continue
            mc = QUEUE_COUNT_LINE.match(line)
            if mc:
                cur_cnt = float(mc.group(1))
                continue
            m = METRIC_LINE.match(line)
            if not m:
                continue
            name = m.group(1)
            try:
                val = float(m.group(2))
            except ValueError:
                continue
            if name in GAUGE_METRICS:
                gauges.setdefault(GAUGE_METRICS[name], []).append(val)
            elif name in COUNTER_METRICS:
                counters.setdefault(COUNTER_METRICS[name], []).append(val)
    flush()

    if not gauges and not counters:
        return None
    out = {}
    for name, vals in gauges.items():
        out[name] = {"mean": round(sum(vals) / len(vals), 4),
                     "max": round(max(vals), 4), "samples": len(vals)}
    for name, vals in counters.items():
        delta = max(vals[-1] - vals[0], 0.0) if len(vals) >= 2 else 0.0
        out[name] = {"first": vals[0], "last": vals[-1],
                     "delta": round(delta, 1), "samples": len(vals)}
    if len(queue_snaps) >= 2:
        b0, s0, c0 = queue_snaps[0]
        b1, s1, c1 = queue_snaps[-1]
        n = c1 - c0
        if n > 0:
            delta_buckets = {le: max(b1.get(le, 0) - b0.get(le, 0), 0)
                             for le in set(b0) | set(b1)}
            total = max(delta_buckets.values()) if delta_buckets else 0
            mean_s = max((s1 or 0) - (s0 or 0), 0) / n
            # le="0.0" 桶计数恒为 0，不能作为有效下界；取第一个计数为正的桶
            first_le = min((le for le, c in delta_buckets.items() if c > 0),
                           default=None)
            # 所有样本都落在最小桶内时，桶内线性插值会把中位数/高分位数放大到
            # 桶上界的线性比例，与 mean 严重矛盾（如 mean=0.2ms 却报 p50=50ms）。
            # 此时只能诚实报告上界，避免误导。
            all_in_first = bool(first_le is not None and total > 0
                                and delta_buckets[first_le] >= total)
            p50 = _hist_quantile(delta_buckets, total, 0.50)
            p95 = _hist_quantile(delta_buckets, total, 0.95)
            p99 = _hist_quantile(delta_buckets, total, 0.99)
            if all_in_first:
                p50 = p95 = p99 = first_le
                q_note = ("queue_time_seconds 直方图首末差分；全部样本落在最小桶内，"
                          "p50/p95/p99 只能按桶上界报告")
            else:
                q_note = "queue_time_seconds 直方图首末差分，桶内线性插值"
            out["queue_time_ms"] = {
                "requests": int(n),
                "mean": round(mean_s * 1000, 1),
                "p50": round((p50 or 0) * 1000, 1),
                "p95": round((p95 or 0) * 1000, 1),
                "p99": round((p99 or 0) * 1000, 1),
                "quantiles_upper_bound": all_in_first,
                "note": q_note,
            }
    prompt_delta = out.get("prompt_tokens_total", {}).get("delta")
    hit_mean = out.get("cache_hit_rate", {}).get("mean")
    if prompt_delta is not None and hit_mean is not None:
        out["reused_prefix_tokens_est"] = {
            "value": round(prompt_delta * hit_mean),
            "estimated": True,
            "method": "prompt_tokens_total delta × cache_hit_rate 均值（窗口期估算）",
        }
    return out


def parse_sysmon(path):
    """sysmon.csv（远端 /proc 采样）→ CPU/内存/网络/磁盘时间序列汇总（§3.2）。

    行格式: epoch,cpu_pct,mem_used_pct,net_rx_kbps,net_tx_kbps,disk_r_kbps,disk_w_kbps
    """
    if not os.path.isfile(path):
        return None
    cols = list(zip(*[r for r in (
        line.strip().split(",") for line in open(path, encoding="utf-8", errors="replace"))
        if len(r) == 7 and r[0][0].isdigit()]))
    if not cols or len(cols[0]) < 2:
        return None
    def f(idx):
        return [float(x) for x in cols[idx]]
    def mm(vals, nd=1):
        return {"mean": round(sum(vals) / len(vals), nd), "max": round(max(vals), nd)}
    return {
        "samples": len(cols[0]),
        "duration_s": int(float(cols[0][-1]) - float(cols[0][0])),
        "cpu_util_pct": mm(f(1)),
        "mem_used_pct": mm(f(2)),
        "net_rx_mbps_max": round(max(f(3)) / 1024, 2),
        "net_tx_mbps_max": round(max(f(4)) / 1024, 2),
        "disk_read_mbps_max": round(max(f(5)) / 1024, 2),
        "disk_write_mbps_max": round(max(f(6)) / 1024, 2),
    }


# ---------------------------------------------------------------- 成本（§3.2）

def compute_cost(run_dir, accepted_requests, accepted_input_tokens,
                 accepted_output_tokens, duration_s, dmon):
    """四维成本模型（framework §3.2），换算标准见 cost_config.json 注释。

    分配成本 = 卡时价 × 独占时长；实际使用成本 = 实测能耗 × 电价 × PUE；
    共享成本 = 共享设施分摊 × 时长；空闲成本 = 分配成本 × (1 − 平均SM利用率)。
    总成本 = 分配 + 使用 + 共享（空闲是分配成本的构成分析，不重复计入）。
    """
    cfg = load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "cost_config.json"))
    if not cfg or not duration_s:
        return None
    hours = duration_s / 3600
    # 卡数优先取环境变量 GPU_COUNT（evaluate.sh 按 --tp 自动传入），否则用配置值
    gpu_count = int(os.environ.get("GPU_COUNT") or cfg.get("gpu_count", 1))
    allocated = cfg.get("gpu_hourly_price_cny", 0) * gpu_count * hours
    energy_kwh = (dmon["energy_wh"] / 1000) if dmon else None
    usage = (energy_kwh or 0) * cfg.get("electricity_price_per_kwh", 0) \
        * cfg.get("pue_factor", 1.0)
    shared = cfg.get("shared_infra_hourly_cny", 0.0) * hours
    avg_util = (dmon["avg_sm_util_pct"] / 100) if dmon else None
    idle = allocated * (1 - avg_util) if avg_util is not None else None
    total = allocated + usage + shared
    cost = {
        "duration_s": round(duration_s, 1),
        "gpu_count": gpu_count,
        "gpu_hours": round(hours * gpu_count, 4),
        "energy_kwh": round(energy_kwh, 4) if energy_kwh is not None else None,
        "allocated_cost_cny": round(allocated, 4),
        "usage_cost_cny": round(usage, 4),
        "shared_cost_cny": round(shared, 4),
        "idle_cost_cny": round(idle, 4) if idle is not None else None,
        "avg_sm_util_pct": dmon["avg_sm_util_pct"] if dmon else None,
        "total_cost_cny": round(total, 4),
        "accepted_requests": accepted_requests,
        "accepted_input_tokens": accepted_input_tokens,
        "accepted_output_tokens": accepted_output_tokens,
        "price_assumptions": cfg,
    }
    if accepted_requests:
        cost["cost_per_accepted_request_cny"] = round(total / accepted_requests, 6)
    if accepted_input_tokens:
        cost["cost_per_1m_accepted_input_tokens_cny"] = \
            round(total / accepted_input_tokens * 1e6, 2)
    if accepted_output_tokens:
        cost["cost_per_1m_accepted_output_tokens_cny"] = \
            round(total / accepted_output_tokens * 1e6, 2)
    with open(os.path.join(run_dir, "cost.json"), "w", encoding="utf-8") as f:
        json.dump(cost, f, ensure_ascii=False, indent=2)
    return cost


# ---------------------------------------------------------------- 判定

def judge_stage(workload, stage_cfg, result, ok_flag, req_recs):
    slo = workload.get("slo", {})
    num_prompts = stage_cfg.get("num_prompts", 0)

    completed = g(result, "completed") or 0
    success_rate = completed / num_prompts if num_prompts else 0.0
    request_success = ok_flag and num_prompts > 0 and completed > 0

    n_err = sum(1 for r in req_recs if r.get("error"))
    error_rate = n_err / len(req_recs) if req_recs else None
    # 超时率单列（§3.1）：error 字符串含 timeout 的归类为超时，其余为其他错误
    n_timeout = sum(1 for r in req_recs
                    if r.get("error") and "timeout" in str(r["error"]).lower())
    timeout_rate = n_timeout / len(req_recs) if req_recs else None

    # 延迟：优先用请求级明细算 p95/p99，回退 bench 聚合字段
    if req_recs:
        ttft = latency_stats(req_recs, "ttft_ms")
        e2e = latency_stats(req_recs, "e2e_ms")
        tpot_vals = sorted(v for v in (per_request_tpot(r) for r in req_recs)
                           if v is not None)
        tpot = {"p50": percentile(tpot_vals, 0.50),
                "mean": sum(tpot_vals) / len(tpot_vals) if tpot_vals else None,
                "p95": percentile(tpot_vals, 0.95),
                "p99": percentile(tpot_vals, 0.99)}
    else:
        ttft = {"p50": g(result, "median_ttft_ms"), "mean": g(result, "mean_ttft_ms"),
                "p95": None, "p99": g(result, "p99_ttft_ms")}
        tpot = {"p50": g(result, "median_tpot_ms"), "mean": g(result, "mean_tpot_ms"),
                "p95": None, "p99": g(result, "p99_tpot_ms")}
        e2e = {"p50": g(result, "median_e2e_latency_ms"),
               "mean": g(result, "mean_e2e_latency_ms"),
               "p95": None, "p99": g(result, "p99_e2e_latency_ms")}

    checks = {}
    for slo_key, val in (("ttft_p99_ms", ttft["p99"]), ("tpot_p99_ms", tpot["p99"]),
                         ("e2e_p99_ms", e2e["p99"])):
        if slo_key in slo:
            checks[slo_key] = {"threshold": slo[slo_key], "actual": val,
                               "pass": val is not None and val <= slo[slo_key]}
    min_sr = slo.get("min_success_rate")
    if min_sr is not None:
        checks["success_rate"] = {"threshold": min_sr, "actual": round(success_rate, 4),
                                  "pass": success_rate >= min_sr}

    slo_pass = request_success and all(c["pass"] for c in checks.values())
    accepted = slo_pass  # quality_pass=None（合成压测），不阻断 accepted

    duration = g(result, "duration")
    total_output = g(result, "total_output_tokens") or 0
    return {
        "label": stage_cfg.get("label"),
        "offered_request_rate": stage_cfg.get("request_rate"),
        "num_prompts": num_prompts,
        "completed": completed,
        "request_success": request_success,
        "slo_pass": slo_pass,
        "quality_pass": None,
        "accepted": accepted,
        "success_rate": round(success_rate, 4),
        "error_rate": round(error_rate, 4) if error_rate is not None else None,
        "timeout_count": n_timeout,
        "timeout_rate": round(timeout_rate, 4) if timeout_rate is not None else None,
        "other_error_count": n_err - n_timeout,
        "slo_checks": checks,
        "duration_s": duration,
        "request_throughput": g(result, "request_throughput"),
        "input_token_throughput": g(result, "input_throughput"),
        "output_token_throughput": g(result, "output_throughput"),
        "accepted_request_goodput": round(completed / duration, 4) if (accepted and duration) else 0.0,
        "accepted_output_token_goodput": round(total_output / duration, 1) if (accepted and duration) else 0.0,
        "ttft_ms": ttft, "tpot_ms": tpot, "e2e_ms": e2e,
        "total_input_tokens": g(result, "total_input_tokens"),
        "total_output_tokens": total_output,
    }


def find_knee(stages):
    """ramp 场景：最后一个 slo_pass 的 stage 即最大稳定速率，下一档为拐点。"""
    knee = None
    for idx, s in enumerate(stages):
        # 基础设施失败（ssh/scp 瞬时故障）不代表容量撞线，不参与拐点判定
        if s.get("infra_error"):
            continue
        if s["slo_pass"]:
            knee = {"max_stable_request_rate": s["offered_request_rate"],
                    "stage": s["label"]}
        elif knee is not None:
            knee["breaking_point_rate"] = s["offered_request_rate"]
            knee["breaking_stage"] = s["label"]
            break
    if knee is not None:
        knee["recommended_operating_rate"] = (
            round(knee["max_stable_request_rate"] * 0.7, 2)
            if knee["max_stable_request_rate"] else None)
    return knee


def main():
    run_dir = sys.argv[1]
    workload = load_json(os.path.join(run_dir, "workload.json"), {})
    stage_cfgs = workload.get("stages", [])

    stage_dirs = sorted(glob.glob(os.path.join(run_dir, "stages", "*")))
    stages, all_requests = [], []
    any_fail = False

    for idx, stage_dir in enumerate(stage_dirs):
        cfg = stage_cfgs[idx] if idx < len(stage_cfgs) else {}
        result = load_json(os.path.join(stage_dir, "raw_result.json"), {}) or {}
        ok_flag = False
        try:
            with open(os.path.join(stage_dir, "stage_ok.txt"), encoding="utf-8") as f:
                ok_flag = f.read().strip() == "true"
        except OSError:
            pass
        if not ok_flag:
            any_fail = True
        req_recs, _ = load_stage_requests(stage_dir)
        stages.append(judge_stage(workload, cfg, result, ok_flag, req_recs))
        infra_path = os.path.join(stage_dir, "stage_infra_error.txt")
        if os.path.isfile(infra_path):
            try:
                with open(infra_path, encoding="utf-8") as f:
                    stages[-1]["infra_error"] = f.read().strip()
            except OSError:
                pass
        for rec in req_recs:
            rec["stage"] = cfg.get("label")
            all_requests.append(rec)

    if all_requests:
        with open(os.path.join(run_dir, "requests.jsonl"), "w", encoding="utf-8") as f:
            for rec in all_requests:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 故障恢复事件（§4 故障恢复负载，stage 内 inject_fault_before 触发）
    fault_events = []
    for stage_dir in stage_dirs:
        fj = load_json(os.path.join(stage_dir, "fault.json"))
        if fj:
            fj["stage_dir"] = os.path.basename(stage_dir)
            fault_events.append(fj)

    # 资源与 KV 维度（§3.2）
    dmon = parse_dmon(os.path.join(run_dir, "gpu_dmon.log"))
    server_metrics = parse_server_metrics(os.path.join(run_dir, "logs", "server_metrics.txt"))
    host = parse_sysmon(os.path.join(run_dir, "sysmon.log"))

    metrics = {
        "workload": workload.get("name"),
        "model": workload.get("model"),
        "stages": stages,
        "capacity_knee": find_knee(stages),
        "gpu": dmon,
        "server_metrics": server_metrics,
        "host": host,
        "fault_recovery": fault_events or None,
    }
    with open(os.path.join(run_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    # 成本（§3.2/§3.3，cost_config.json 存在时）
    total_dur = sum(s["duration_s"] or 0 for s in stages)
    acc_req = sum(s["completed"] for s in stages if s["accepted"])
    acc_in_tok = sum(s["total_input_tokens"] or 0 for s in stages if s["accepted"])
    acc_tok = sum(s["total_output_tokens"] or 0 for s in stages if s["accepted"])
    compute_cost(run_dir, acc_req, acc_in_tok, acc_tok, total_dur, dmon)

    status = {
        "result": "partial_failure" if any_fail else "ok",
        "stage_results": {s["label"]: ("ok" if s["request_success"] else "failed")
                          for s in stages},
    }
    with open(os.path.join(run_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    # 控制台摘要表
    print("\n== metrics 摘要 ==")
    header = (f"{'stage':<18}{'offered':>8}{'ok%':>7}{'ttft95':>8}{'ttft99':>8}"
              f"{'tpot99':>8}{'e2e99':>9}{'slo':>6}")
    print(header)
    print("-" * len(header))
    for s in stages:
        print(f"{str(s['label']):<18}{s['offered_request_rate']:>8}"
              f"{s['success_rate'] * 100:>6.1f}"
              f"{_fmt(s['ttft_ms']['p95']):>8}{_fmt(s['ttft_ms']['p99']):>8}"
              f"{_fmt(s['tpot_ms']['p99']):>8}{_fmt(s['e2e_ms']['p99']):>9}"
              f"{('PASS' if s['slo_pass'] else 'FAIL'):>6}")
        if s.get("infra_error"):
            print(f"  ! {s['label']}: 基础设施失败（{s['infra_error']}），"
                  "该档不计入容量拐点判定")
    knee = metrics["capacity_knee"]
    if knee:
        print(f"\n容量拐点: 最大稳定速率 {knee['max_stable_request_rate']} req/s"
              f"（stage {knee['stage']}）"
              + (f"，在 {knee.get('breaking_point_rate')} req/s 处崩溃"
                 if knee.get("breaking_point_rate") else "")
              + f"；建议运行水位 {knee.get('recommended_operating_rate')} req/s")
    if server_metrics and "cache_hit_rate" in server_metrics:
        print(f"prefix cache 命中率: mean={server_metrics['cache_hit_rate']['mean']:.3f}")
    if server_metrics and "queue_time_ms" in server_metrics:
        q = server_metrics["queue_time_ms"]
        print(f"排队时间: mean={q['mean']:.0f}ms p99={q['p99']:.0f}ms (n={q['requests']})")
    if server_metrics and "preemptions_total" in server_metrics:
        print(f"preemption 次数: {int(server_metrics['preemptions_total']['delta'])}")
    if server_metrics and "kv_cache_capacity_tokens" in server_metrics:
        print(f"KV cache 容量: {int(server_metrics['kv_cache_capacity_tokens']['max'])} tokens")
    if server_metrics and "reused_prefix_tokens_est" in server_metrics:
        print(f"外部复用 token（估）: {server_metrics['reused_prefix_tokens_est']['value']}")
    if metrics.get("host"):
        h = metrics["host"]
        print(f"主机: cpu mean/max {h['cpu_util_pct']['mean']}/{h['cpu_util_pct']['max']}% "
              f"mem max {h['mem_used_pct']['max']}%")
    if dmon:
        print(f"GPU: avg {dmon['avg_power_w']}W / sm {dmon['avg_sm_util_pct']}% "
              f"/ 能耗 {dmon['energy_wh']}Wh")


def _fmt(v):
    return f"{v:.0f}" if isinstance(v, (int, float)) else "-"


if __name__ == "__main__":
    main()
