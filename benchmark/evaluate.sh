#!/usr/bin/env bash
# AlayaJet-MaaS 模型部署评估总编排（framework.md 全链路）
#
# 一条命令完成对一个模型部署的完整 E2E 评估：
#   P0 前置检查 → P1 拉起服务 → P2 冒烟 → P3 质量基线 →
#   P4 性能压测（多 workload）→ P5 配对 + 报告
#
# 用法（仓库根目录）：
#   ./benchmark/evaluate.sh \
#       --model Qwen/Qwen3-8B --model-path /path/on/executor --tp 1
#
# 参数：
#   --model NAME        服务模型名（必传）
#   --model-path PATH   执行机上的模型权重路径（--attach 时不需要）
#   --tp N              TP 并行度（默认 1）
#   --port PORT         服务端口（默认 30000）
#   --attach            复用已在运行的服务（跳过 P0/P1 启动）
#   --skip-quality      跳过质量测试
#   --skip-fault-recovery  跳过故障恢复负载（SIGKILL 注入，默认包含在末尾执行）
#   --e2e-local         负载发生器在本机运行（E2E 含网络段，§6）；默认在被测机上 loopback
#   --workloads "..."   性能 workload 列表（默认 steady ramp burst overload rag_prefix longctx decode_heavy fault_recovery）
#   --suites "..."      质量 suite 列表（默认 niah gsm8k ifeval longbench_v2）
#
# 产物（framework.md §7 规范）：
#   benchmark/runs/<model>-tp<N>-<timestamp>/
#     manifest.json  model_service_profile.json  kubernetes_snapshot/
#     workload.json  requests.jsonl  metrics.json  quality.json  cost.json
#     status.json  logs/  perf/  quality/  pairing.json  report.md
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
source "$SCRIPT_DIR/env.sh"

BENCH_SSH=${BENCH_SSH:-gpu10-qiyu}
VENV_PATH=${VENV_PATH:-'/home/qiyu/sglang-env'}
SERVER_LOG=${SERVER_LOG:-'/home/qiyu/sglang-qwen3-8b.log'}

MODEL= MODEL_PATH= TP=1 PORT=${BENCH_PORT:-30000} ATTACH=false SKIP_QUALITY=false SKIP_FAULT_RECOVERY=false
BENCH_CLIENT=${BENCH_CLIENT:-remote}
BENCH_HOST=${BENCH_HOST:-127.0.0.1}
WORKLOADS=${WORKLOADS:-"steady ramp burst overload rag_prefix longctx decode_heavy"}
SUITES=${SUITES:-"niah gsm8k ifeval longbench_v2"}

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL=$2; shift 2;;
    --model-path) MODEL_PATH=$2; shift 2;;
    --tp) TP=$2; shift 2;;
    --port) PORT=$2; shift 2;;
    --attach) ATTACH=true; shift;;
    --skip-quality) SKIP_QUALITY=true; shift;;
    --skip-fault-recovery) SKIP_FAULT_RECOVERY=true; shift;;
    --e2e-local) BENCH_CLIENT=local; shift;;
    --workloads) WORKLOADS=$2; shift 2;;
    --suites) SUITES=$2; shift 2;;
    *) echo "未知参数: $1" >&2; exit 1;;
  esac
done
[ -n "$MODEL" ] || { echo "必须指定 --model" >&2; exit 1; }
# E2E 模式下本机访问服务的地址无法从被测机推导，必须显式给出
if [ "$BENCH_CLIENT" = "local" ] && [ -z "${E2E_TARGET:-}" ]; then
  echo "--e2e-local 必须同时设置 E2E_TARGET（你本机访问服务的地址，如 http://<执行机IP>:<端口>）" >&2
  exit 1
fi
# 故障恢复负载放最后执行（会 kill 并重启服务，不影响其他 workload 的采样）
[ "$SKIP_FAULT_RECOVERY" = false ] && WORKLOADS="$WORKLOADS fault_recovery"

MODEL_SLUG=$(echo "$MODEL" | tr '/.' '--')
EVAL_ID="${MODEL_SLUG}-tp${TP}-$(date +%Y%m%d-%H%M%S)"
EVAL_DIR="$ROOT_DIR/benchmark/runs/$EVAL_ID"
mkdir -p "$EVAL_DIR/perf" "$EVAL_DIR/quality" "$EVAL_DIR/logs"

GIT_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=false; git -C "$ROOT_DIR" diff --quiet 2>/dev/null || GIT_DIRTY=true
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "== 评估开始: $EVAL_ID"
echo "   executor: $BENCH_SSH  model: $MODEL  tp: $TP  port: $PORT  attach: $ATTACH"

# ---- P0+P1 拉起服务（或复用）----
if [ "$ATTACH" = false ]; then
  [ -n "$MODEL_PATH" ] || { echo "非 --attach 模式必须指定 --model-path" >&2; exit 1; }
  echo "-- P0 前置检查"
  ssh -o BatchMode=yes "$BENCH_SSH" "
    test -f '$MODEL_PATH/config.json' || { echo '模型路径无效' >&2; exit 1; }
    nvidia-smi --query-gpu=memory.used --format=csv,noheader | awk -v tp=$TP \
      'NR<=tp && \$1+0>1000 {print \"GPU \" NR-1 \" 已被占用 \" \$1; bad=1} END{exit bad}'
  "
  echo "-- P1 拉起服务"
  ssh -o BatchMode=yes "$BENCH_SSH" "nohup bash -c '
    source $VENV_PATH/bin/activate
    HF_HUB_OFFLINE=1 python3 -m sglang.launch_server \
      --model-path $MODEL_PATH --served-model-name $MODEL \
      --host 0.0.0.0 --port $PORT --tp-size $TP \
      --mem-fraction-static 0.85 --enable-metrics \
      > $SERVER_LOG 2>&1' > /dev/null 2>&1 &" 
  echo "   等待服务 Ready..."
  for attempt in $(seq 1 60); do
    CODE=$(ssh -o BatchMode=yes "$BENCH_SSH" \
      "curl -s -o /dev/null -w '%{http_code}' http://$BENCH_HOST:$PORT/health" 2>/dev/null || echo 000)
    [ "$CODE" = "200" ] && break
    [ "$attempt" = 60 ] && { echo "服务启动超时" >&2; exit 1; }
    sleep 10
  done
fi

# ---- P2 冒烟 ----
echo "-- P2 冒烟"
SMOKE=$(ssh -o BatchMode=yes "$BENCH_SSH" "curl -s --max-time 120 \
  http://$BENCH_HOST:$PORT/v1/chat/completions -H 'Content-Type: application/json' -d '{
    \"model\": \"$MODEL\", \"temperature\": 0, \"max_tokens\": 16,
    \"messages\": [{\"role\": \"user\", \"content\": \"Say OK\"}]}'" 2>/dev/null || true)
echo "$SMOKE" | grep -q '"content"' || { echo "冒烟失败: $SMOKE" >&2; exit 1; }
echo "   冒烟通过"

# ---- 公共快照（§6/§7）----
ssh -o BatchMode=yes "$BENCH_SSH" "curl -s http://$BENCH_HOST:$PORT/get_server_info" \
  > "$EVAL_DIR/model_service_profile.json" 2>/dev/null || true
ssh -o BatchMode=yes "$BENCH_SSH" "nvidia-smi -q | sed -n '1,80p'" \
  > "$EVAL_DIR/machine_snapshot.txt" 2>/dev/null || true

# ---- P3 质量基线 ----
QUALITY_RUN_ID=""
if [ "$SKIP_QUALITY" = false ]; then
  echo "-- P3 质量测试: $SUITES"
  QUALITY_BASE_URL="http://$BENCH_HOST:$PORT/v1" QUALITY_MODEL="$MODEL" BENCH_SSH="$BENCH_SSH" \
    VENV_PATH="$VENV_PATH" \
    "$SCRIPT_DIR/quality/run_quality.sh" $SUITES 2>&1 | tee "$EVAL_DIR/logs/quality_console.txt"
  # 用子脚本自己输出的 run-id 配对，避免 ls -dt 抢到并发/历史 run
  QUALITY_RUN_ID=$(sed -n 's/^== quality run: //p' "$EVAL_DIR/logs/quality_console.txt" | tail -1)
  QRUN="$ROOT_DIR/benchmark/runs/$QUALITY_RUN_ID"
  [ -n "$QUALITY_RUN_ID" ] && [ -d "$QRUN" ] || { echo "质量 run 目录未找到: $QUALITY_RUN_ID" >&2; exit 1; }
  cp -r "$QRUN"/* "$EVAL_DIR/quality/"
fi

# ---- P4 性能压测 ----
PERF_RUN_IDS=()
for wl in $WORKLOADS; do
  echo "-- P4 性能: $wl"
  MODEL_NAME="$MODEL" BENCH_SSH="$BENCH_SSH" BENCH_PORT="$PORT" \
    BENCH_CLIENT="$BENCH_CLIENT" E2E_TARGET="${E2E_TARGET:-}" \
    TOKENIZER_PATH="${MODEL_PATH:-${TOKENIZER_PATH:-}}" \
    SERVER_TP="$TP" GPU_COUNT="$TP" SERVER_LOG="$SERVER_LOG" \
    RESTART_MODEL_PATH="${MODEL_PATH:-${TOKENIZER_PATH:-}}" \
    "$SCRIPT_DIR/run_local_benchmark.sh" "$wl" 2>&1 | tee "$EVAL_DIR/logs/perf_${wl}_console.txt"
  PRUN_ID=$(sed -n 's/^== local benchmark run: //p' "$EVAL_DIR/logs/perf_${wl}_console.txt" | tail -1)
  PRUN="$ROOT_DIR/benchmark/runs/$PRUN_ID"
  [ -n "$PRUN_ID" ] && [ -d "$PRUN" ] || { echo "workload $wl 的 run 目录未找到: $PRUN_ID" >&2; exit 1; }
  PERF_RUN_IDS+=("$PRUN_ID")
  mv "$PRUN" "$EVAL_DIR/perf/"
done

# ---- P5 聚合为 §7 规范产物 + 报告 ----
"$PYTHON_BIN" "$SCRIPT_DIR/finalize_run.py" "$EVAL_DIR" "$EVAL_ID" "$MODEL" "$TP" \
    "$GIT_COMMIT" "$GIT_DIRTY" "$STARTED_AT" "$QUALITY_RUN_ID" "${PERF_RUN_IDS[@]}"

"$PYTHON_BIN" "$SCRIPT_DIR/generate_report.py" "$EVAL_DIR" > "$EVAL_DIR/logs/report_console.txt" 2>&1 || true

echo ""
echo "== 评估完成: $EVAL_DIR"
echo "   报告: $EVAL_DIR/report.md"
