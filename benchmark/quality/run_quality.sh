#!/usr/bin/env bash
# AlayaJet-MaaS 质量评测执行器（framework.md §2 quality_pass）
#
# 用法（仓库根目录）：
#   ./benchmark/quality/run_quality.sh [suite ...]        # 默认全部四个
#   NUM_SAMPLES=4 ./benchmark/quality/run_quality.sh      # 冒烟模式
#
# 流程：把 benchmark/quality/ 同步到执行机 -> 远程逐 suite 运行 ->
# 结果拉回 benchmark/runs/quality-<timestamp>/ 并汇总 quality.json。
#
# 环境变量：
#   BENCH_SSH       执行机 ssh 别名（默认 gpu10-qiyu）
#   QUALITY_BASE_URL  被测服务（默认 http://127.0.0.1:30000/v1，执行机视角）
#   QUALITY_MODEL   模型名（默认 Qwen/Qwen3-8B）
#   NUM_SAMPLES     每个 suite 的样本数上限（冒烟调试用）
#   CONCURRENCY     推理并发（默认 16。质量测试与性能压测先后串行、互不重叠，
#                   且打分为逐条正误判定，与速度无关——并发只影响跑得多快）
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
source "$SCRIPT_DIR/../env.sh"

BENCH_SSH=${BENCH_SSH:-gpu10-qiyu}
QUALITY_BASE_URL=${QUALITY_BASE_URL:-http://127.0.0.1:30000/v1}
QUALITY_MODEL=${QUALITY_MODEL:-Qwen/Qwen3-8B}
NUM_SAMPLES=${NUM_SAMPLES:-}
CONCURRENCY=${CONCURRENCY:-16}
VENV_PATH=${VENV_PATH:-'/home/qiyu/sglang-env'}

SUITES=("$@")
[ ${#SUITES[@]} -gt 0 ] || SUITES=(niah gsm8k ifeval longbench_v2)

RUN_ID="quality-$(date +%Y%m%d-%H%M%S)"
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RUN_DIR="$ROOT_DIR/benchmark/runs/$RUN_ID"
mkdir -p "$RUN_DIR/suites"

GIT_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")

# 远端工作目录按目标用户 HOME 推导，不能写死 /home/qiyu（换机器/换用户会 permission denied）
if [ -z "${REMOTE_DIR:-}" ]; then
  REMOTE_HOME=$(ssh -o BatchMode=yes "$BENCH_SSH" 'printf %s "$HOME"' 2>/dev/null || echo "/tmp")
  REMOTE_DIR="$REMOTE_HOME/alayajet-quality"
fi

echo "== quality run: $RUN_ID"
echo "   executor: $BENCH_SSH  target: $QUALITY_BASE_URL  model: $QUALITY_MODEL"
echo "   suites: ${SUITES[*]}  samples=${NUM_SAMPLES:-default}  concurrency=$CONCURRENCY"

# ---- 同步 harness 到执行机（tar over ssh，兼容无 rsync 环境）----
ssh -o BatchMode=yes "$BENCH_SSH" "mkdir -p '$REMOTE_DIR'"
tar czf - -C "$SCRIPT_DIR" . | ssh -o BatchMode=yes "$BENCH_SSH" "tar xzf - -C '$REMOTE_DIR'"

# ---- 远程逐 suite 执行 ----
for suite in "${SUITES[@]}"; do
  echo "-- suite [$suite]"
  EXTRA_ARGS=()
  [ -n "$NUM_SAMPLES" ] && EXTRA_ARGS+=(--num-samples "$NUM_SAMPLES")
  ssh -o BatchMode=yes "$BENCH_SSH" bash -s -- \
    "$VENV_PATH" "$REMOTE_DIR" "$suite" "$QUALITY_BASE_URL" "$QUALITY_MODEL" \
    "$CONCURRENCY" "${EXTRA_ARGS[@]:-}" <<'REMOTE'
set -uo pipefail
venv_path=$1; remote_dir=$2; suite=$3; base_url=$4; model=$5; concurrency=$6
shift 6

source "$venv_path/bin/activate"
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
export QUALITY_BASE_URL=$base_url QUALITY_MODEL=$model
cd "$remote_dir"
python3 quality_runner.py --suite "$suite" --output-dir results \
  --concurrency "$concurrency" "$@"
REMOTE
done

# ---- 拉回结果 ----
for suite in "${SUITES[@]}"; do
  mkdir -p "$RUN_DIR/suites/$suite"
  scp -q -o BatchMode=yes "$BENCH_SSH:$REMOTE_DIR/results/suites/$suite/summary.json" \
    "$RUN_DIR/suites/$suite/summary.json" 2>/dev/null || true
  scp -q -o BatchMode=yes "$BENCH_SSH:$REMOTE_DIR/results/suites/$suite/results.jsonl" \
    "$RUN_DIR/suites/$suite/results.jsonl" 2>/dev/null || true
done

# ---- 汇总 quality.json + manifest ----
"$PYTHON_BIN" - "$RUN_DIR" "$RUN_ID" "$GIT_COMMIT" "$QUALITY_BASE_URL" "$QUALITY_MODEL" "$STARTED_AT" <<'EOF'
import json, os, sys

run_dir, run_id, commit, base_url, model, started_at = sys.argv[1:7]
suites = {}
for suite in sorted(os.listdir(os.path.join(run_dir, "suites"))):
    p = os.path.join(run_dir, "suites", suite, "summary.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            suites[suite] = json.load(f)

quality = {"run_id": run_id, "model": model, "suites": suites}
with open(os.path.join(run_dir, "quality.json"), "w", encoding="utf-8") as f:
    json.dump(quality, f, ensure_ascii=False, indent=2)

manifest = {
    "run_id": run_id,
    "type": "quality",
    "started_at": started_at,
    "git_commit": commit,
    "target": base_url,
    "model": model,
    "artifacts": {"quality": "quality.json", "suites": "suites/"},
}
with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

print("\n== quality 摘要 ==")
for name, s in suites.items():
    print(f"  {name:<14} score={s.get('score', 0):.4f}  n={s.get('n')}")
EOF

echo "== 完成: $RUN_DIR"
