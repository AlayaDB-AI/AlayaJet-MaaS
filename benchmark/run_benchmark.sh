#!/usr/bin/env bash
# AlayaJet-MaaS benchmark 执行器
#
# 用法（仓库根目录，Git Bash / Linux 均可）：
#   ./benchmark/run_benchmark.sh <workload-name>
#
# workload-name 对应 benchmark/workloads/<name>.json，内置：
#   steady    稳态负载，测稳态 SLO 与 goodput
#   burst     突发 + 恢复，观察排队与二次过载
#   ramp      阶梯加压，找容量拐点
#   overload  过饱和，验证拒绝语义与重试放大
#
# 结果写入 benchmark/runs/<workload>-<timestamp>/，结构遵循
# docs/evaluation/framework.md 第 7 节（manifest.json 为复现入口）。
#
# 可用环境变量覆盖：
#   MODEL_NAMESPACE  模型服务 namespace（默认 qwen2-5-0-5b-instruct）
#   BENCH_HOST       压测目标 host（默认 nodes.json serviceAddress）
#   BENCH_PORT       压测目标 port（默认 nodes.json serviceNodePort）
#   KUBECONFIG_PATH  管理用 kubeconfig（默认 nodes.json kubeconfig）
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
source "$ROOT_DIR/deploy/sglang-native/scripts/common.sh"
# common.sh 会覆盖 SCRIPT_DIR/ROOT_DIR，恢复为 benchmark 目录
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
source "$SCRIPT_DIR/env.sh"

WORKLOAD_NAME=${1:?用法: $0 <workload-name> [run-id]  (steady|burst|ramp|overload)}
WORKLOAD_FILE="$SCRIPT_DIR/workloads/$WORKLOAD_NAME.json"
[ -f "$WORKLOAD_FILE" ] || { echo "找不到 workload: $WORKLOAD_FILE" >&2; exit 1; }

NAMESPACE=${MODEL_NAMESPACE:-qwen2-5-0-5b-instruct}
KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
export KUBECONFIG=$KUBECONFIG_PATH

BENCH_HOST=${BENCH_HOST:-$(cluster_field serviceAddress)}
BENCH_PORT=${BENCH_PORT:-$(cluster_field serviceNodePort)}

# 第二个参数可指定既有 run ID：重跑时跳过已完成的 stage（断点续跑）
RUN_ID=${2:-"$WORKLOAD_NAME-$(date +%Y%m%d-%H%M%S)"}
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR/stages" "$RUN_DIR/kubernetes_snapshot"

[ -f "$RUN_DIR/workload.json" ] && [ -z "${2:-}" ] || cp "$WORKLOAD_FILE" "$RUN_DIR/workload.json"

# ---- 复现信息：git 状态 + 集群快照（framework.md §6）----
GIT_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=false
git -C "$ROOT_DIR" diff --quiet 2>/dev/null || GIT_DIRTY=true

kubectl get nodes -o json > "$RUN_DIR/kubernetes_snapshot/nodes.json" 2>/dev/null || true
kubectl -n "$NAMESPACE" get pods -o json > "$RUN_DIR/kubernetes_snapshot/pods.json" 2>/dev/null || true
kubectl -n "$NAMESPACE" get services -o json > "$RUN_DIR/kubernetes_snapshot/services.json" 2>/dev/null || true
kubectl -n "$NAMESPACE" get endpointslice -o json > "$RUN_DIR/kubernetes_snapshot/endpointslices.json" 2>/dev/null || true

cat > "$RUN_DIR/manifest.json" <<EOF
{
  "run_id": "$RUN_ID",
  "workload": "$WORKLOAD_NAME",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_commit": "$GIT_COMMIT",
  "git_dirty": $GIT_DIRTY,
  "kubernetes_context": "$(kubectl config current-context 2>/dev/null || echo unknown)",
  "namespace": "$NAMESPACE",
  "target": "http://$BENCH_HOST:$BENCH_PORT",
  "artifacts": {
    "workload": "workload.json",
    "kubernetes_snapshot": "kubernetes_snapshot/",
    "stages": "stages/",
    "metrics": "metrics.json",
    "status": "status.json"
  }
}
EOF

# ---- 读取 workload：stage 数量和公共字段 ----
# 注意：路径必须作为独立 argv 传给 python3，让 MSYS 自动转换为 Windows 路径；
# 嵌进 -c 代码字符串里不会被转换，Windows Python 打不开 /e/... 路径。
read -r BACKEND MODEL STAGE_COUNT < <("$PYTHON_BIN" -c "
import json, sys
w = json.load(open(sys.argv[1], encoding='utf-8'))
print(w.get('backend', 'sglang-oai'), w['model'], len(w['stages']))
" "$WORKLOAD_FILE" | tr -d '\r')

echo "== benchmark run: $RUN_ID"
echo "   target: http://$BENCH_HOST:$BENCH_PORT  namespace: $NAMESPACE  stages: $STAGE_COUNT"

# ---- 逐 stage 执行 ----
for ((i = 0; i < STAGE_COUNT; i++)); do
  read -r LABEL NUM_PROMPTS REQUEST_RATE INPUT_LEN OUTPUT_LEN RANGE_RATIO < <("$PYTHON_BIN" -c "
import json, sys
w = json.load(open(sys.argv[1], encoding='utf-8'))
s = w['stages'][int(sys.argv[2])]
print(s['label'], s['num_prompts'], s['request_rate'],
      s.get('input_len', 512), s.get('output_len', 128), s.get('range_ratio', 0.5))
" "$WORKLOAD_FILE" "$i" | tr -d '\r')

  STAGE_DIR="$RUN_DIR/stages/$(printf '%02d' "$i")-$LABEL"
  mkdir -p "$STAGE_DIR"

  # 断点续跑：该 stage 已有有效结果则跳过
  if [ -s "$STAGE_DIR/raw_result.json" ] && \
     [ "$(cat "$STAGE_DIR/stage_ok.txt" 2>/dev/null)" = "true" ]; then
    echo "-- stage $i [$LABEL]: 已完成，跳过"
    continue
  fi

  JOB_NAME="bench-${WORKLOAD_NAME}-${LABEL}-$(date +%H%M%S)"
  # request_rate 可能带小数，不能用 bash 整数运算
  WAIT_TIMEOUT=$("$PYTHON_BIN" -c "
import sys
n = float('$NUM_PROMPTS'); r = float('$REQUEST_RATE')
print(int(n / r * 3 + 1200) + 1)")

  echo "-- stage $i [$LABEL]: rate=${REQUEST_RATE}/s prompts=$NUM_PROMPTS (timeout ${WAIT_TIMEOUT}s)"

  # 断点续跑：上次中断时 Job 可能已在集群里完成，先尝试按标签打捞日志
  SALVAGED=false
  OLD_POD=$(kubectl -n "$NAMESPACE" get pods \
    -l "alayajet.io/benchmark-run=$RUN_ID,alayajet.io/benchmark-stage=$LABEL" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$OLD_POD" ]; then
    kubectl -n "$NAMESPACE" logs "$OLD_POD" > "$STAGE_DIR/bench.log" 2>&1 || true
    sed -n '/===BENCH_RESULT_JSON_BEGIN===/,/===BENCH_RESULT_JSON_END===/p' \
      "$STAGE_DIR/bench.log" | sed '1d;$d' > "$STAGE_DIR/raw_result.json" || true
    sed -n '/===BENCH_DETAILS_JSONL_BEGIN===/,/===BENCH_DETAILS_JSONL_END===/p' \
      "$STAGE_DIR/bench.log" | sed '1d;$d' > "$STAGE_DIR/requests.jsonl" || true
    if [ -s "$STAGE_DIR/raw_result.json" ]; then
      SALVAGED=true
      echo "true" > "$STAGE_DIR/stage_ok.txt"
      echo "   打捞到集群中已完成的结果"
      OLD_JOB=$(kubectl -n "$NAMESPACE" get pod "$OLD_POD" \
        -o jsonpath='{.metadata.labels.job-name}' 2>/dev/null || true)
      [ -n "$OLD_JOB" ] && kubectl -n "$NAMESPACE" delete "job/$OLD_JOB" --wait=false >/dev/null 2>&1 || true
    fi
  fi
  [ "$SALVAGED" = true ] && continue

  sed -e "s|__JOB_NAME__|$JOB_NAME|g" \
      -e "s|__NAMESPACE__|$NAMESPACE|g" \
      -e "s|__RUN_ID__|$RUN_ID|g" \
      -e "s|__STAGE_LABEL__|$LABEL|g" \
      -e "s|__BACKEND__|$BACKEND|g" \
      -e "s|__HOST__|$BENCH_HOST|g" \
      -e "s|__PORT__|$BENCH_PORT|g" \
      -e "s|__MODEL__|$MODEL|g" \
      -e "s|__INPUT_LEN__|$INPUT_LEN|g" \
      -e "s|__OUTPUT_LEN__|$OUTPUT_LEN|g" \
      -e "s|__RANGE_RATIO__|$RANGE_RATIO|g" \
      -e "s|__NUM_PROMPTS__|$NUM_PROMPTS|g" \
      -e "s|__REQUEST_RATE__|$REQUEST_RATE|g" \
      -e "s|__MAX_CONCURRENCY__|$NUM_PROMPTS|g" \
      "$SCRIPT_DIR/job.yaml" > "$STAGE_DIR/job.yaml"

  kubectl apply -f "$STAGE_DIR/job.yaml" >/dev/null

  STAGE_OK=true
  if ! kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$JOB_NAME" \
      --timeout="${WAIT_TIMEOUT}s" >/dev/null 2>&1; then
    STAGE_OK=false
    echo "   stage 未正常完成，保留现场日志"
  fi

  POD=$(kubectl -n "$NAMESPACE" get pods -l "job-name=$JOB_NAME" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$POD" ]; then
    kubectl -n "$NAMESPACE" logs "$POD" > "$STAGE_DIR/bench.log" 2>&1 || true
    sed -n '/===BENCH_RESULT_JSON_BEGIN===/,/===BENCH_RESULT_JSON_END===/p' \
      "$STAGE_DIR/bench.log" | sed '1d;$d' > "$STAGE_DIR/raw_result.json" || true
    sed -n '/===BENCH_DETAILS_JSONL_BEGIN===/,/===BENCH_DETAILS_JSONL_END===/p' \
      "$STAGE_DIR/bench.log" | sed '1d;$d' > "$STAGE_DIR/requests.jsonl" || true
  fi

  [ -s "$STAGE_DIR/raw_result.json" ] || STAGE_OK=false
  echo "$STAGE_OK" > "$STAGE_DIR/stage_ok.txt"
  kubectl -n "$NAMESPACE" delete "job/$JOB_NAME" --wait=false >/dev/null 2>&1 || true
done

# ---- 汇总判定 ----
"$PYTHON_BIN" "$SCRIPT_DIR/collect_results.py" "$RUN_DIR"

echo "== 完成: $RUN_DIR"
