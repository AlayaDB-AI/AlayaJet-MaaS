#!/usr/bin/env bash
# AlayaJet-MaaS 本地/独立部署 benchmark 执行器 v2
#
# 与 run_benchmark.sh（K8s Job 模式）对应，用于未加入集群的独立部署：
# 通过 ssh 在目标机上直接运行 sglang.bench_serving。
#
# v2 对齐 docs/evaluation/framework.md §3/§6/§7 补齐：
#   - 压测期间采样服务端 /metrics（KV cache 命中率、队列、吞吐）→ logs/server_metrics.txt
#   - 硬件与驱动快照 nvidia-smi -q → machine_snapshot.json
#   - server 端日志归档 → logs/server.log
#   - workload json 支持 dataset_name / extra_args 字段（RAG 共享前缀等场景）
#
# 用法（仓库根目录，Git Bash / Linux 均可）：
#   ./benchmark/run_local_benchmark.sh <workload-name> [run-id]
#
# 结果写入 benchmark/runs/<workload>-<timestamp>/（manifest.json 为复现入口）。
#
# 环境变量覆盖：
#   BENCH_SSH       压测执行机 ssh 别名（默认 gpu10-qiyu）
#   BENCH_HOST      被测服务 host（执行机视角，默认 127.0.0.1）
#   BENCH_PORT      被测服务 port（默认 30000）
#   MODEL_NAME      覆盖 workload.json 的 model 字段
#   TOKENIZER_PATH  执行机上的 tokenizer/模型路径（random-ids 需要）
#   VENV_PATH       执行机 sglang 虚拟环境（默认 ~/sglang-env）
#   GPU_INDEX       dmon 采样 GPU 序号（默认 0）
#   SERVER_LOG      执行机上 server 日志路径（默认 ~/sglang-qwen3-8b.log）
#   BENCH_CLIENT    remote=在被测机上跑 bench_serving（loopback，默认）；
#                   local=在本机跑 e2e_client.py（含真实网络段，framework §6 E2E）
#   E2E_TARGET      BENCH_CLIENT=local 时的服务地址（必填，本机视角）
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
source "$SCRIPT_DIR/env.sh"

WORKLOAD_NAME=${1:?用法: $0 <workload-name> [run-id]}
WORKLOAD_FILE="$SCRIPT_DIR/workloads/$WORKLOAD_NAME.json"
[ -f "$WORKLOAD_FILE" ] || { echo "找不到 workload: $WORKLOAD_FILE" >&2; exit 1; }

BENCH_SSH=${BENCH_SSH:-gpu10-qiyu}
BENCH_HOST=${BENCH_HOST:-127.0.0.1}
BENCH_PORT=${BENCH_PORT:-30000}
VENV_PATH=${VENV_PATH:-'~/sglang-env'}
TOKENIZER_PATH=${TOKENIZER_PATH:-/home/qiyu/.cache/huggingface/hub/models--Qwen--Qwen3-8B/snapshots/b968826d9c46dd6066d109eabc6255188de91218}
# GPU_INDEX 未显式指定时按 SERVER_TP 展开（tp=2 → "0,1"），dmon 支持逗号多卡
if [ -z "${GPU_INDEX:-}" ]; then
  if [ -n "${SERVER_TP:-}" ] && [ "$SERVER_TP" -gt 1 ] 2>/dev/null; then
    GPU_INDEX=$(seq -s, 0 $((SERVER_TP - 1)))
  else
    GPU_INDEX=0
  fi
fi
SERVER_LOG=${SERVER_LOG:-'/home/qiyu/sglang-qwen3-8b.log'}

RUN_ID=${2:-"$WORKLOAD_NAME-$(date +%Y%m%d-%H%M%S)"}
RUN_DIR="$SCRIPT_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR/stages" "$RUN_DIR/logs"

[ -f "$RUN_DIR/workload.json" ] && [ -z "${2:-}" ] || cp "$WORKLOAD_FILE" "$RUN_DIR/workload.json"

# ---- 复现信息：git + 服务端信息 + 硬件快照（framework.md §6）----
GIT_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=false
git -C "$ROOT_DIR" diff --quiet 2>/dev/null || GIT_DIRTY=true

ssh -o BatchMode=yes -o ConnectTimeout=10 "$BENCH_SSH" \
  "curl -s http://$BENCH_HOST:$BENCH_PORT/get_server_info" \
  > "$RUN_DIR/model_service_profile.json" 2>/dev/null || true

ssh -o BatchMode=yes "$BENCH_SSH" \
  "nvidia-smi -q | sed -n '1,80p'" \
  > "$RUN_DIR/machine_snapshot.txt" 2>/dev/null || true

read -r BACKEND MODEL STAGE_COUNT EARLY_STOP_AFTER < <("$PYTHON_BIN" -c "
import json, sys
w = json.load(open(sys.argv[1], encoding='utf-8'))
print(w.get('backend', 'sglang-oai'), w['model'], len(w['stages']),
      w.get('early_stop_after_fails', 0))
" "$WORKLOAD_FILE" | tr -d '\r')
MODEL=${MODEL_NAME:-$MODEL}

if [ "${BENCH_CLIENT:-remote}" = "local" ]; then
  # 本机访问服务的地址无法从被测机推导，不能给写死的默认值
  [ -n "${E2E_TARGET:-}" ] || { echo "BENCH_CLIENT=local 时必须设置 E2E_TARGET（本机访问服务的地址）" >&2; exit 1; }
  MEASURE_PATH="E2E（含网络段）: 负载发生器在本机，经真实网络到 ${E2E_TARGET}"
else
  MEASURE_PATH="服务端 loopback（不含网络段）: 负载发生器在被测机上打 http://$BENCH_HOST:$BENCH_PORT"
fi

cat > "$RUN_DIR/manifest.json" <<EOF
{
  "run_id": "$RUN_ID",
  "workload": "$WORKLOAD_NAME",
  "deployment_mode": "standalone",
  "measurement_path": "$MEASURE_PATH",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_commit": "$GIT_COMMIT",
  "git_dirty": $GIT_DIRTY,
  "bench_executor": "$BENCH_SSH",
  "target": "http://$BENCH_HOST:$BENCH_PORT",
  "model": "$MODEL",
  "artifacts": {
    "workload": "workload.json",
    "model_service_profile": "model_service_profile.json",
    "machine_snapshot": "machine_snapshot.txt",
    "stages": "stages/",
    "gpu_dmon": "gpu_dmon.log",
    "sysmon": "sysmon.log",
    "server_metrics": "logs/server_metrics.txt",
    "server_log": "logs/server.log",
    "metrics": "metrics.json",
    "status": "status.json"
  }
}
EOF

echo "== local benchmark run: $RUN_ID"
echo "   executor: $BENCH_SSH  target: http://$BENCH_HOST:$BENCH_PORT  model: $MODEL  stages: $STAGE_COUNT"

# ---- 采样器：GPU dmon（1s）+ 服务端 /metrics（5s）+ 主机 sysmon（1s）----
DMON_REMOTE=/tmp/dmon_${RUN_ID}.log
METRICS_REMOTE=/tmp/smetrics_${RUN_ID}.txt
SYSMON_REMOTE=/tmp/sysmon_${RUN_ID}.log
scp -q -o BatchMode=yes "$SCRIPT_DIR/sysmon_sampler.py" \
  "$BENCH_SSH:/tmp/sysmon_sampler_${RUN_ID}.py"
ssh -o BatchMode=yes "$BENCH_SSH" "
  nohup nvidia-smi dmon -s pum -d 1 -i $GPU_INDEX -o T > '$DMON_REMOTE' 2>&1 &
  echo \$! > '${DMON_REMOTE}.pid'
  nohup bash -c 'while true; do
    echo \"===SCRAPE \$(date -u +%Y-%m-%dT%H:%M:%SZ)===\"
    curl -s --max-time 3 http://$BENCH_HOST:$BENCH_PORT/metrics
    sleep 5
  done' > '$METRICS_REMOTE' 2>&1 &
  echo \$! > '${METRICS_REMOTE}.pid'
  nohup python3 '/tmp/sysmon_sampler_${RUN_ID}.py' > '$SYSMON_REMOTE' 2>&1 &
  echo \$! > '${SYSMON_REMOTE}.pid'
"
echo "   采样已启动: gpu dmon + server /metrics + host sysmon"

SAMPLERS_STOPPED=false
stop_samplers() {
  [ "$SAMPLERS_STOPPED" = true ] && return 0
  SAMPLERS_STOPPED=true
  ssh -o BatchMode=yes "$BENCH_SSH" "
    [ -f '${DMON_REMOTE}.pid' ] && kill \$(cat '${DMON_REMOTE}.pid') 2>/dev/null
    [ -f '${METRICS_REMOTE}.pid' ] && kill \$(cat '${METRICS_REMOTE}.pid') 2>/dev/null
    [ -f '${SYSMON_REMOTE}.pid' ] && kill \$(cat '${SYSMON_REMOTE}.pid') 2>/dev/null
  " >/dev/null 2>&1 || true
  scp -q -o BatchMode=yes "$BENCH_SSH:$DMON_REMOTE" "$RUN_DIR/gpu_dmon.log" 2>/dev/null || true
  scp -q -o BatchMode=yes "$BENCH_SSH:$METRICS_REMOTE" "$RUN_DIR/logs/server_metrics.txt" 2>/dev/null || true
  scp -q -o BatchMode=yes "$BENCH_SSH:$SYSMON_REMOTE" "$RUN_DIR/sysmon.log" 2>/dev/null || true
  # server 端日志归档（取运行时段的尾部，最长 2000 行）
  ssh -o BatchMode=yes "$BENCH_SSH" "tail -2000 '$SERVER_LOG'" \
    > "$RUN_DIR/logs/server.log" 2>/dev/null || true
  ssh -o BatchMode=yes "$BENCH_SSH" \
    "rm -f '$DMON_REMOTE' '${DMON_REMOTE}.pid' '$METRICS_REMOTE' '${METRICS_REMOTE}.pid' \
           '$SYSMON_REMOTE' '${SYSMON_REMOTE}.pid' '/tmp/sysmon_sampler_${RUN_ID}.py'" >/dev/null 2>&1 || true
}
trap stop_samplers EXIT

# ---- 逐 stage 执行 ----
CONSEC_FAIL=0
for ((i = 0; i < STAGE_COUNT; i++)); do
  read -r LABEL NUM_PROMPTS REQUEST_RATE INPUT_LEN OUTPUT_LEN RANGE_RATIO DATASET EXTRA_ARGS INJECT < <("$PYTHON_BIN" -c "
import json, sys
w = json.load(open(sys.argv[1], encoding='utf-8'))
s = w['stages'][int(sys.argv[2])]
print(s['label'], s['num_prompts'], s['request_rate'],
      s.get('input_len', 512), s.get('output_len', 128), s.get('range_ratio', 0.5),
      s.get('dataset_name', w.get('dataset_name', 'random-ids')),
      s.get('extra_args', w.get('extra_args', '')).replace(' ', '___') or '-',
      s.get('inject_fault_before', '-'))
" "$WORKLOAD_FILE" "$i" | tr -d '\r')
  EXTRA_ARGS=${EXTRA_ARGS//___/ }; [ "$EXTRA_ARGS" = "-" ] && EXTRA_ARGS=
  [ "$INJECT" = "-" ] && INJECT=

  STAGE_DIR="$RUN_DIR/stages/$(printf '%02d' "$i")-$LABEL"
  mkdir -p "$STAGE_DIR"

  if [ -s "$STAGE_DIR/raw_result.json" ] && \
     [ "$(cat "$STAGE_DIR/stage_ok.txt" 2>/dev/null)" = "true" ]; then
    echo "-- stage $i [$LABEL]: 已完成，跳过"
    continue
  fi

  # ---- 故障注入（framework.md §4 故障恢复负载）：kill 服务 → 重启 → 测恢复时间 ----
  if [ -n "$INJECT" ]; then
    echo "   故障注入: $INJECT（kill 服务进程并重启，测量恢复时间）"
    RESTART_MODEL_PATH=${RESTART_MODEL_PATH:-$TOKENIZER_PATH}
    SERVER_TP=${SERVER_TP:-1}
    T_KILL=$(date +%s)
    # 安全：按「本用户 + 精确端口」限定 pkill 范围，避免误杀共享机器上他人的 sglang 服务
    ssh -o BatchMode=yes "$BENCH_SSH" \
      "pkill -9 -u \"\$(whoami)\" -f 'sglang\.launch_server.*--port $BENCH_PORT ' || true"
    sleep 3
    DOWN_CODE=$(ssh -o BatchMode=yes "$BENCH_SSH" \
      "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://$BENCH_HOST:$BENCH_PORT/health" \
      2>/dev/null || echo 000)
    DOWN_CODE=${DOWN_CODE:0:3}
    ssh -o BatchMode=yes "$BENCH_SSH" "nohup bash -c '
      source $VENV_PATH/bin/activate
      HF_HUB_OFFLINE=1 python3 -m sglang.launch_server \
        --model-path $RESTART_MODEL_PATH --served-model-name $MODEL \
        --host 0.0.0.0 --port $BENCH_PORT --tp-size $SERVER_TP \
        --mem-fraction-static 0.85 --enable-metrics \
        >> $SERVER_LOG 2>&1' > /dev/null 2>&1 &"
    READY=false
    for attempt in $(seq 1 90); do
      CODE=$(ssh -o BatchMode=yes "$BENCH_SSH" \
        "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://$BENCH_HOST:$BENCH_PORT/health" \
        2>/dev/null || echo 000)
      [ "$CODE" = "200" ] && { READY=true; break; }
      sleep 5
    done
    T_HEALTHY=$(date +%s)
    cat > "$STAGE_DIR/fault.json" <<EOF
{
  "fault_type": "$INJECT",
  "injected_at_epoch": $T_KILL,
  "down_confirmed_http_code": "$DOWN_CODE",
  "restart_to_healthy_s": $((T_HEALTHY - T_KILL)),
  "recovered": $READY,
  "restart_cmd": "sglang.launch_server --model-path $RESTART_MODEL_PATH --served-model-name $MODEL --port $BENCH_PORT --tp-size $SERVER_TP"
}
EOF
    if [ "$READY" != true ]; then
      echo "   故障后服务 450s 内未恢复健康，stage 记失败"
      echo false > "$STAGE_DIR/stage_ok.txt"
      continue
    fi
    echo "   服务已恢复，重启到健康耗时 $((T_HEALTHY - T_KILL))s"
  fi

  # request_rate 可能带小数，不能用 bash 整数运算
  WAIT_TIMEOUT=$("$PYTHON_BIN" -c "
import sys
n = float('$NUM_PROMPTS'); r = float('$REQUEST_RATE')
print(int(n / r * 3 + 1200) + 1)")
  REMOTE_BASE="/tmp/bench_${RUN_ID}_$(printf '%02d' "$i")_${LABEL}"

  echo "-- stage $i [$LABEL]: rate=${REQUEST_RATE}/s prompts=$NUM_PROMPTS dataset=$DATASET (timeout ${WAIT_TIMEOUT}s)"

  # 瞬时基础设施故障（ssh/scp 断开、结果文件未落盘）必须与真实 SLO 失败区分：
  # 结果缺失/损坏时重试一次；重试仍失败则写 stage_infra_error.txt，
  # 由 collect_results 标记，避免把基础设施噪声当成容量拐点。
  STAGE_OK=true
  RAW_OK=false
  rm -f "$STAGE_DIR/stage_infra_error.txt"
  for STAGE_ATTEMPT in 1 2; do
    [ "$STAGE_ATTEMPT" -gt 1 ] && echo "   stage 重试（$STAGE_ATTEMPT/2，疑似瞬时故障）..."
    STAGE_OK=true
    if [ "${BENCH_CLIENT:-remote}" = "local" ]; then
      # E2E 模式：负载从本机（被测机器之外）发出，经真实网络到服务（framework §6）
      timeout "${WAIT_TIMEOUT}s" "$PYTHON_BIN" "$SCRIPT_DIR/e2e_client.py" \
        --url "$E2E_TARGET" --model "$MODEL" \
        --num-prompts "$NUM_PROMPTS" --request-rate "$REQUEST_RATE" \
        --input-len "$INPUT_LEN" --output-len "$OUTPUT_LEN" \
        --range-ratio "$RANGE_RATIO" \
        --output-file "$STAGE_DIR/raw_result.json" > "$STAGE_DIR/bench.log" 2>&1 \
        || STAGE_OK=false
    else
    ssh -o BatchMode=yes "$BENCH_SSH" bash -s -- \
      "$VENV_PATH" "$BACKEND" "$BENCH_HOST" "$BENCH_PORT" "$MODEL" "$TOKENIZER_PATH" \
      "$INPUT_LEN" "$OUTPUT_LEN" "$RANGE_RATIO" "$NUM_PROMPTS" "$REQUEST_RATE" \
      "$REMOTE_BASE" "$WAIT_TIMEOUT" "$DATASET" "$EXTRA_ARGS" > "$STAGE_DIR/bench.log" 2>&1 <<'REMOTE' || STAGE_OK=false
set -uo pipefail
venv_path=$1; backend=$2; host=$3; port=$4; model=$5; tokenizer=$6
input_len=$7; output_len=$8; range_ratio=$9; num_prompts=${10}; request_rate=${11}
remote_base=${12}; wait_timeout=${13}; dataset=${14}
# ssh 会把 argv 用空格拼接后由远端 shell 重新解析，extra_args 的多个 token
# 到远端已是独立位置参数；shift 后整体收取
shift 14 || true
extra_args="$*"

source "$venv_path/bin/activate"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
# shellcheck disable=SC2086
timeout "${wait_timeout}s" python3 -m sglang.bench_serving \
  --backend "$backend" \
  --host "$host" --port "$port" \
  --model "$model" \
  --tokenizer "$tokenizer" \
  --dataset-name "$dataset" \
  --random-input-len "$input_len" \
  --random-output-len "$output_len" \
  --random-range-ratio "$range_ratio" \
  --num-prompts "$num_prompts" \
  --request-rate "$request_rate" \
  --max-concurrency "$num_prompts" \
  --warmup-requests 0 \
  --disable-tqdm \
  --output-file "${remote_base}.json" \
  --output-details \
  $extra_args
REMOTE

    fi  # BENCH_CLIENT 分支结束

    if [ "${BENCH_CLIENT:-remote}" != "local" ]; then
    scp -q -o BatchMode=yes "$BENCH_SSH:${REMOTE_BASE}.json" "$STAGE_DIR/raw_result.json" 2>/dev/null || true
    ssh -o BatchMode=yes "$BENCH_SSH" "rm -f '${REMOTE_BASE}.json'" >/dev/null 2>&1 || true
    fi

    # 结果 JSON 必须存在且可解析才算 stage 执行成功
    if [ -s "$STAGE_DIR/raw_result.json" ] && \
       "$PYTHON_BIN" -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" \
           "$STAGE_DIR/raw_result.json" 2>/dev/null; then
      RAW_OK=true
      break
    fi

    # 判断是否值得重试：日志为空 = ssh/执行链路失败；日志已有成功表 = 结果文件丢失
    if [ ! -s "$STAGE_DIR/bench.log" ] || grep -q 'Serving Benchmark Result' "$STAGE_DIR/bench.log"; then
      :
    else
      break
    fi
  done

  if [ "$RAW_OK" != true ]; then
    STAGE_OK=false
    if [ ! -s "$STAGE_DIR/bench.log" ]; then
      echo "stage 执行失败：bench.log 为空（ssh/执行链路瞬时故障）" > "$STAGE_DIR/stage_infra_error.txt"
    elif grep -q 'Serving Benchmark Result' "$STAGE_DIR/bench.log"; then
      echo "stage 压测完成但结果文件缺失/损坏（scp 或落盘失败）" > "$STAGE_DIR/stage_infra_error.txt"
    fi
  fi

  # sglang 0.5.10 的请求级明细内嵌在结果 JSON（--output-details），展开为 requests.jsonl
  if [ "$RAW_OK" = true ]; then
    "$PYTHON_BIN" -c "
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
ttfts, itls = d.get('ttfts') or [], d.get('itls') or []
inl, outl = d.get('input_lens') or [], d.get('output_lens') or []
errors = d.get('errors') or []
n = max(len(ttfts), len(inl), len(outl), len(errors))
with open(sys.argv[2], 'w', encoding='utf-8') as f:
    for i in range(n):
        err = errors[i] if i < len(errors) else None
        itl_ms = [x * 1000 for x in itls[i]] if i < len(itls) and itls[i] and not err else []
        ttft_ms = (ttfts[i] * 1000) if i < len(ttfts) and not err else None
        rec = {
            'input_len': inl[i] if i < len(inl) else None,
            'output_len': outl[i] if i < len(outl) else None,
            'ttft_ms': ttft_ms,
            'itl_ms': itl_ms or None,
            'e2e_ms': (ttft_ms + sum(itl_ms)) if ttft_ms is not None else None,
            'error': err or None,
        }
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
" "$STAGE_DIR/raw_result.json" "$STAGE_DIR/requests.jsonl" || true
  fi

  [ "$RAW_OK" = true ] && [ "$STAGE_OK" = true ] || STAGE_OK=false
  echo "$STAGE_OK" > "$STAGE_DIR/stage_ok.txt"
  [ "$STAGE_OK" = true ] || echo "   stage 未正常完成，保留 bench.log 现场"

  # early-stop：workload 声明 early_stop_after_fails 时，连续 N 档 SLO FAIL 即终止。
  # 判定复用 judge_stage.py（与 collect_results 同一套口径），失败 stage 也计入。
  if [ "${EARLY_STOP_AFTER:-0}" -gt 0 ] 2>/dev/null; then
    if [ -f "$STAGE_DIR/stage_infra_error.txt" ]; then
      # 基础设施失败没有 SLO 结论，不计入连续失败，也不打断连续计数
      echo "   infra 失败不计入 early-stop 连续计数"
    else
      VERDICT=$("$PYTHON_BIN" "$SCRIPT_DIR/judge_stage.py" "$STAGE_DIR" "$WORKLOAD_FILE" "$i" 2>/dev/null || echo FAIL)
      if [ "$VERDICT" = "FAIL" ]; then
        CONSEC_FAIL=$((CONSEC_FAIL + 1))
      else
        CONSEC_FAIL=0
      fi
      if [ "$CONSEC_FAIL" -ge "$EARLY_STOP_AFTER" ]; then
        echo "   early-stop: 连续 $CONSEC_FAIL 档 SLO 失败，剩余 $((STAGE_COUNT - i - 1)) 档不再执行"
        break
      fi
    fi
  fi
done

# ---- 先停采样并拉回数据，再汇总（顺序不能反：collect 依赖采样产物）----
stop_samplers

# ---- 汇总判定 ----
"$PYTHON_BIN" "$SCRIPT_DIR/collect_results.py" "$RUN_DIR"

echo "== 完成: $RUN_DIR"
