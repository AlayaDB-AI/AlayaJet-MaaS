#!/usr/bin/env python3
"""单 stage 的 SLO 判定（供 run_local_benchmark.sh 在 stage 间做 early-stop）。

复用 collect_results.judge_stage，保证判定口径与最终汇总完全一致——
early-stop 看到的 PASS/FAIL 和报告里的 ✅/❌ 永远是同一套逻辑。

用法: python3 judge_stage.py <stage_dir> <workload_json> <stage_index>
输出: PASS 或 FAIL（stdout 单行）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_results import judge_stage, load_json, load_stage_requests


def main():
    stage_dir, workload_file, idx = sys.argv[1], sys.argv[2], int(sys.argv[3])
    w = load_json(workload_file, {})
    cfg = w.get("stages", [])[idx]
    result = load_json(os.path.join(stage_dir, "raw_result.json"), {}) or {}
    ok_flag = False
    try:
        with open(os.path.join(stage_dir, "stage_ok.txt"), encoding="utf-8") as f:
            ok_flag = f.read().strip() == "true"
    except OSError:
        pass
    recs, _ = load_stage_requests(stage_dir)
    s = judge_stage(w, cfg, result, ok_flag, recs)
    print("PASS" if s["slo_pass"] else "FAIL")


if __name__ == "__main__":
    main()
