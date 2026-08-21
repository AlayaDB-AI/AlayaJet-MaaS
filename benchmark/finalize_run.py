#!/usr/bin/env python3
"""把一次评估的 perf/quality 明细聚合为 framework.md §7 规范的 run 产物。

目标布局（runs/<run_id>/）：
  manifest.json             复现入口：引用其余产物 + 内容摘要
  model_service_profile.json
  kubernetes_snapshot/      standalone 模式存放 machine_snapshot.txt（附说明）
  workload.json             workload 索引（各 workload 明细在 perf/<run>/workload.json）
  requests.jsonl            全部 perf stage 的请求级明细（带 workload 标签）
  metrics.json              性能汇总 + 质量分数 + 容量拐点
  quality.json              质量结果（quality_pass 证据）
  cost.json                 成本汇总（跨 workload 合计 + 单位成本）
  status.json               总体结果
  logs/                     server 日志等
  perf/  quality/           各子 run 的完整明细（各自仍是 §7 结构）
  report.md                 由 generate_report.py 生成

用法:
  python3 finalize_run.py <run_dir> <eval_id> <model> <tp> <git_commit> \\
      <git_dirty> <started_at> <quality_run_id> [perf_run_id ...]
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


def main():
    run_dir = sys.argv[1]
    eval_id, model, tp, commit, dirty, started_at, qrun = sys.argv[2:9]
    perf_runs = sys.argv[9:]

    perf_dir = os.path.join(run_dir, "perf")
    quality_dir = os.path.join(run_dir, "quality")

    # ---- workload.json：workload 索引 ----
    workloads = {}
    slo_all = {}
    for pr in perf_runs:
        w = load_json(os.path.join(perf_dir, pr, "workload.json")) or {}
        workloads[w.get("name", pr)] = f"perf/{pr}/workload.json"
        if w.get("slo"):
            slo_all[w.get("name", pr)] = w["slo"]
    workload_index = {"workloads": workloads, "slo": slo_all,
                      "note": "各 workload 的完整定义见 perf/<run>/workload.json"}
    _dump(run_dir, "workload.json", workload_index)

    # ---- requests.jsonl：合并所有 perf run 的请求明细 ----
    n_req = 0
    with open(os.path.join(run_dir, "requests.jsonl"), "w", encoding="utf-8") as out:
        for pr in perf_runs:
            p = os.path.join(perf_dir, pr, "requests.jsonl")
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    rec["workload"] = pr
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n_req += 1

    # ---- metrics.json：性能 + 质量汇总 ----
    perf_summary = {}
    knees = {}
    for pr in perf_runs:
        m = load_json(os.path.join(perf_dir, pr, "metrics.json"))
        if not m:
            continue
        perf_summary[pr] = {
            "stages": m.get("stages"),
            "gpu": m.get("gpu"),
            "server_metrics": m.get("server_metrics"),
            "host": m.get("host"),
            "fault_recovery": m.get("fault_recovery"),
        }
        if m.get("capacity_knee"):
            knees[m.get("workload", pr)] = m["capacity_knee"]

    # workload 覆盖清单（framework.md §4 最低要求）
    wl_names = set()
    for pr in perf_runs:
        w = load_json(os.path.join(perf_dir, pr, "workload.json")) or {}
        wl_names.add(w.get("name", pr))

    def _cov(*names):
        hit = sorted(wl_names & set(names))
        return {"status": "covered", "by": hit} if hit else {"status": "missing"}

    coverage = {
        "短对话": _cov("steady", "ramp", "burst", "overload"),
        "RAG/共享前缀": _cov("rag_prefix"),
        "长上下文": _cov("longctx"),
        "decode-heavy": _cov("decode_heavy"),
        "多模态": _cov("multimodal") if any("multimodal" in w for w in wl_names) else
                  {"status": "n/a", "note": "纯文本模型不适用；多模态模型必须补该负载"},
        "steady": _cov("steady"),
        "burst": _cov("burst"),
        "故障恢复": _cov("fault_recovery"),
    }
    missing = [k for k, v in coverage.items() if v["status"] == "missing"]

    quality = load_json(os.path.join(quality_dir, "quality.json"))
    if quality:
        _dump(run_dir, "quality.json", quality)

    metrics = {
        "run_id": eval_id,
        "model": model,
        "perf": perf_summary,
        "capacity_knees": knees,
        "quality_scores": {k: v.get("score")
                           for k, v in (quality or {}).get("suites", {}).items()},
        "workload_coverage": coverage,
        "workload_coverage_missing": missing,
    }
    _dump(run_dir, "metrics.json", metrics)

    # ---- cost.json：跨 workload 合计（四维成本，§3.2）----
    costs = {pr: load_json(os.path.join(perf_dir, pr, "cost.json"))
             for pr in perf_runs}
    costs = {k: v for k, v in costs.items() if v}
    cost = None
    if costs:
        def _sum(key):
            return round(sum(c.get(key) or 0 for c in costs.values()), 4)
        total = _sum("total_cost_cny")
        acc_req = sum(c.get("accepted_requests") or 0 for c in costs.values())
        acc_in = sum(c.get("accepted_input_tokens") or 0 for c in costs.values())
        acc_tok = sum(c.get("accepted_output_tokens") or 0 for c in costs.values())
        cost = {
            "per_workload": {k: v["total_cost_cny"] for k, v in costs.items()},
            "total_cost_cny": total,
            "gpu_count": max((c.get("gpu_count") or 0 for c in costs.values()), default=None),
            "allocated_cost_cny": _sum("allocated_cost_cny"),
            "usage_cost_cny": _sum("usage_cost_cny"),
            "shared_cost_cny": _sum("shared_cost_cny"),
            "idle_cost_cny": _sum("idle_cost_cny"),
            "total_energy_kwh": _sum("energy_kwh"),
            "accepted_requests": acc_req,
            "accepted_input_tokens": acc_in,
            "accepted_output_tokens": acc_tok,
            "price_assumptions": next(iter(costs.values())).get("price_assumptions"),
        }
        if acc_req:
            cost["cost_per_accepted_request_cny"] = round(total / acc_req, 6)
        if acc_in:
            cost["cost_per_1m_accepted_input_tokens_cny"] = round(total / acc_in * 1e6, 2)
        if acc_tok:
            cost["cost_per_1m_accepted_output_tokens_cny"] = round(total / acc_tok * 1e6, 2)
        _dump(run_dir, "cost.json", cost)

    # ---- kubernetes_snapshot/：standalone 替代说明 ----
    ksnap = os.path.join(run_dir, "kubernetes_snapshot")
    os.makedirs(ksnap, exist_ok=True)
    src = os.path.join(run_dir, "machine_snapshot.txt")
    if os.path.isfile(src):
        with open(src, encoding="utf-8", errors="replace") as f:
            data = f.read()
        with open(os.path.join(ksnap, "machine_snapshot.txt"), "w", encoding="utf-8") as f:
            f.write(data)
    with open(os.path.join(ksnap, "README.md"), "w", encoding="utf-8") as f:
        f.write("standalone 部署模式，无 Kubernetes 对象；"
                "硬件与驱动快照见 machine_snapshot.txt。\n")

    # ---- status.json ----
    stage_status = {}
    for pr in perf_runs:
        s = load_json(os.path.join(perf_dir, pr, "status.json")) or {}
        stage_status[pr] = s.get("result", "unknown")
    status = {
        "result": "ok" if all(v == "ok" for v in stage_status.values()) else "partial_failure",
        "perf": stage_status,
        "quality_run": qrun or None,
    }
    _dump(run_dir, "status.json", status)

    # ---- manifest.json：复现入口 + 内容摘要 ----
    def _summ_perf():
        parts = []
        for wl, knee in knees.items():
            parts.append(f"{wl}: 拐点 {knee['max_stable_request_rate']}rps")
        return "; ".join(parts) or f"{len(perf_runs)} 个 workload"

    def _summ_quality():
        if not quality:
            return "未执行"
        return "; ".join(f"{k}={v:.4f}" for k, v in metrics["quality_scores"].items())

    artifacts = {
        "model_service_profile.json": "服务启动参数/引擎版本（/get_server_info 快照）",
        "kubernetes_snapshot/": "standalone 模式：machine_snapshot.txt 硬件快照",
        "workload.json": f"{len(workloads)} 个 workload 索引 + SLO",
        "requests.jsonl": f"{n_req} 条请求级明细（ttft/itl/e2e/输入输出长度）",
        "metrics.json": f"性能与质量汇总；{_summ_perf()}",
        "quality.json": f"质量分数：{_summ_quality()}",
        "cost.json": (f"总成本 {cost['total_cost_cny']} 元 / "
                      f"每百万 accepted token "
                      f"{cost.get('cost_per_1m_accepted_output_tokens_cny', '-')} 元"
                      if costs else "未配置 cost_config.json"),
        "status.json": status["result"],
        "logs/": "server 日志与各 run 的 /metrics 采样",
        "perf/": "各 workload 的完整 run 明细（§7 结构）",
        "quality/": "质量 run 完整明细（逐题 results.jsonl）",
        "pairing.json": "质量×性能配对证据（§2）",
        "report.md": "自动生成的决策报告（§8）",
    }
    # ---- 实验矩阵显式记录（framework.md §4）----
    profile = {}
    for pr in perf_runs:
        profile = load_json(os.path.join(perf_dir, pr, "model_service_profile.json")) or {}
        if profile:
            break
    if not profile:
        profile = load_json(os.path.join(run_dir, "model_service_profile.json")) or {}
    hw = "unknown"
    snap = os.path.join(run_dir, "machine_snapshot.txt")
    if os.path.isfile(snap):
        with open(snap, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Product Name" in line:
                    hw = line.split(":", 1)[1].strip()
                    break
    model_path = profile.get("model_path") or profile.get("tokenizer_path") or ""
    experiment_matrix = {
        "model_revision": (model_path.rstrip("/").split("/")[-1]
                           if "snapshots" in model_path else model_path or "unknown"),
        "runtime_revision": f"sglang {profile.get('version', 'unknown')}",
        "hardware_topology": hw,
        "precision": {"dtype": profile.get("dtype", "unknown"),
                      "kv_cache_dtype": profile.get("kv_cache_dtype", "unknown")},
        "quantization": profile.get("quantization") or "none",
        "parallelism": {"tp": int(tp)},
        "workload": sorted(wl_names),
        "slo": {k: v for k, v in slo_all.items()},
        "runtime_configuration": "model_service_profile.json（/get_server_info 全量启动参数）",
        "optimization": {
            "prefix_cache": not profile.get("disable_radix_cache", False),
            "kv_offload_hicache": profile.get("enable_hierarchical_cache", False),
            "cuda_graph": not profile.get("disable_cuda_graph", False),
        },
    }

    manifest = {
        "run_id": eval_id,
        "type": "deployment_evaluation",
        "model": model,
        "deployment": f"standalone sglang, tp={tp}",
        "git_commit": commit,
        "git_dirty": dirty == "true" or dirty is True,
        "started_at": started_at,
        "experiment_matrix": experiment_matrix,
        "workload_coverage": coverage,
        "artifacts": artifacts,
    }
    _dump(run_dir, "manifest.json", manifest)

    pairing = {
        "model": model,
        "model_service_profile": "model_service_profile.json",
        "quality_run": qrun or None,
        "perf_runs": perf_runs,
        "rule": "framework.md §2: 性能 run 与相同模型 revision、配置下的质量 run 配对",
    }
    _dump(run_dir, "pairing.json", pairing)
    print(f"finalize 完成: {run_dir}（requests={n_req}, workloads={len(perf_runs)}）")


def _dump(run_dir, name, obj):
    with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
