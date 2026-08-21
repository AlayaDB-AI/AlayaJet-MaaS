# trace 回放：深入资料

正常跑真实业务测试只需要看 README 里的 `run_trace.py`。本文件是给排查问题、
自建转换器、或想绕过自动化逐条理解命令的人准备的。

## 0. run_trace.py 参数格式详解

每个参数的类型、取值和写法（命令里 `=` 两侧不要空格；有值的参数用
`--key value`，布尔开关不带值）。**优先级：命令行参数 > 配置文件
（`benchmark/trace_env.json`，或 `--env` 指定）> 内置默认值**；先跑
`--show-config` 可以看到最终生效的合并结果。

| 参数 | 类型 | 取值 | 示例 | 说明 |
|---|---|---|---|---|
| `--env` | 本地路径 | JSON 文件 | `--env benchmark/trace_env_kimi.json` | 指定配置文件，默认 `benchmark/trace_env.json` |
| `--show-config` | 开关 | 无值 | `--show-config` | 打印合并后的配置并退出，不执行 |
| `--trace` | 本地路径 | 存在的 JSONL 文件 | `--trace benchmark/traces/business-peak-5min.jsonl` | 相对路径按你当前所在目录解析，建议从仓库根目录运行 |
| `--time-scale` | 浮点数 | > 0 | `--time-scale 1`、`--time-scale 60`、`--time-scale 0.5` | 1=原速；60=快 60 倍；只压缩“发出时刻”，不减少总计算量 |
| `--tp` | 整数 | 1 或 2 | `--tp 2` | 目前只支持这两个值，其他值直接报错 |
| `--port` | 整数 | 1~65535 | `--port 30000` | 服务监听端口，检查/启动/回放都用它 |
| `--ssh` | 字符串 | `用户@地址` | `--ssh qiyu@100.64.0.8` | 地址可以是 IP、域名或 ssh config 别名；本机 `ssh`/`scp` 必须能直连 |
| `--model` | 字符串 | served model name | `--model Qwen/Qwen3.8-27B-FP8` | 必须和服务端 `--served-model-name` 完全一致，含大小写和斜杠 |
| `--model-path` | 远端路径 | 目录 | `--model-path "~/.cache/huggingface/hub/.../snapshots/017b9..."` | 是 gpu10 上的路径，`~` 会展开成 `/home/qiyu`；必须写到 `snapshots/<hash>` 层 |
| `--template` | 远端路径 | Jinja 文件 | `--template ~/trace-run/qwen35-chat-template-dev.jinja` | gpu10 上的 chat 模板文件 |
| `--context` | 整数 | token 数 | `--context 65536` | 受显存限制，放不下就调小 |
| `--server-log` | 远端路径 | 文件 | `--server-log ~/sglang-trace.log` | 跑完把它拉回 run 目录存成 `server.log` |
| `--start-server` | 枚举 | auto / always / never | `--start-server never` | auto=不健康才启动；always=先杀旧进程再启动；never=假定已在跑，不健康直接退出 |
| `--keep-server` | 开关 | 无值 | `--keep-server` | 默认结束时停掉“本次启动的服务”；加了就保留；复用已有服务时本来就不会停 |
| `--wait-ready-s` | 整数 | 秒 | `--wait-ready-s 240` | 启动服务后的最长等待时间 |
| `--client-timeout-s` | 整数 | 秒 | `--client-timeout-s 900` | 单请求超时，长回复/排队时别设太小 |
| `--note` | 字符串 | 可重复 | `--note "TP=2 / context=65536"` | 每条会写进 `report.md` 备注；含空格要加引号 |

配置文件 `benchmark/trace_env.json` 的字段与上表同名（snake_case）：
`ssh / model / model_path / template / context / port / tp / trace / time_scale /
server_log / start_server / keep_server / wait_ready_s / client_timeout_s /
notes`。换机器/换模型就是编辑这些字段，或复制一份用 `--env` 指向。

两类容易写错的：

- **远端路径 vs 本地路径**：`--model-path`、`--template`、`--server-log` 都是
  **gpu10 上的**路径（支持 `~`）；只有 `--trace` 是**你本机上的**路径。
- **JSON 参数**（`log_to_trace.py --params`）：PowerShell 和 bash 都用**单引号**
  包住整个 JSON，别用双引号：
  `--params '{"temperature":0.7}'`；不注入就写 `--params "{}"`。

## 1. trace 文件格式（与引擎无关）
## 1. trace 文件格式（与引擎无关）

每行一个请求，三种写法等价、可混用：

```jsonc
// A. 完整请求体（最高保真：消息/工具/图片/采样参数原样发送）
{"ts": 0.0, "endpoint": "chat", "body": {"messages": [{"role":"user","content":"..."}], "temperature": 0.7, "max_tokens": 128}}

// B. 字段简写（body 未给时使用）
{"ts": 1.2, "messages": [{"role":"user","content":"..."}], "params": {"temperature": 0.7}, "output_len": 128}

// C. completions 接口
{"ts": 2.0, "endpoint": "completions", "prompt": "继续写...", "params": {"max_tokens": 256}}
```

字段与优先级规则：

- `ts`：到达时刻（秒）。epoch 或相对值都可以，客户端统一减去最小值归零；
  **只有整份 trace 每一条都带数值 `ts` 时才按时间戳回放**，只要有一条缺 `ts`，
  整份都退化为按 `request_rate` 的泊松到达。
- `endpoint`：`chat`（默认，`/v1/chat/completions`）或 `completions`
  （`/v1/completions`，用 `prompt` 字段）。
- `body`：完整请求体。**给了 `body` 时，同行的 `messages`/`params`/`output_len`
  全部被忽略**；客户端只补 `model`、`stream=true`、`stream_options.include_usage`
  三个缺省值，其余原样发送。
- `messages`：仅 `body` 未给、且 `endpoint=chat` 时使用；支持 OpenAI 多模态
  parts，`image_url` 的 http(s) 链接会被客户端拉取并转成 data URL 再发送
  （拉取失败则原样透传）。
- `params` / `sampling_params`：合并进请求体的采样参数（`body` 未给时才生效）。
- `output_len`：`max_tokens` 未给时的兜底上限。
- `max_tokens` 是**上限不是精确长度**：回放时输出是否更早 EOS、实际生成多少，
  取决于模型和采样参数，所以重放出来的长度分布可能和原始日志不同。

## 2. 拿到一份 trace

### 2.1 从任意系统的请求日志生成（通用映射）

任何能拿到“请求到达时间 + 请求内容”的系统都可以自己拼 trace：

| 你日志里的信息 | 写到 trace 的哪里 |
|---|---|
| 请求到达时间 | `ts`（epoch 秒；缺了就退化为泊松） |
| 完整请求体 | `body`（推荐，保真度最高） |
| 或 消息列表 + 采样参数 | `messages` + `params` |
| 输出上限 | `body.max_tokens` 或 `output_len` |
| 会话 ID / 轮次 | 用来重建多轮历史，拼进 `messages` |
| 接口类型 | `endpoint`（chat 或 completions） |

注意：多轮会话要把历史拼进每个请求的 `messages`；工具调用/结果要按引擎接受的
格式放（或扁平化成文本）；含个人信息的字段先脱敏再落盘。

### 2.2 内置转换器 `log_to_trace.py`（仅针对本仓库的日志格式）

它只认识本仓库 `会话数据1.ndjson` 的事件格式：每行一个事件或流式 chunk，
关键字段为 `conversation_id / sequence / event_type / content / created_at /
model_name / prompt_tokens / completion_tokens`。会合并流式 chunk、按会话重建
多轮历史、把 tool 链路扁平化为文本、默认脱敏。其他系统按 2.1 自建转换器。

```bash
python3 benchmark/traces/log_to_trace.py 会话数据1.ndjson -o business.jsonl
python3 benchmark/traces/log_to_trace.py 会话数据1.ndjson -o sample.jsonl \
  --max-requests 500 --cap-output-len 4096 --no-deidentify
python3 benchmark/traces/log_to_trace.py 会话数据1.ndjson -o win.jsonl \
  --start-offset-s 28500 --window-s 300
```

| 选项 | 含义 |
|---|---|
| `--params '<json>'` | 注入到每条请求的采样画像；默认 `"{}"` 即不注入、走服务端默认 |
| `--start-offset-s N` / `--window-s W` | 按时间截取连续流量（offset 相对原始最小 ts） |
| `--max-requests N` | 抽样到 N 条（按 `--seed` 均匀抽样后重排时间） |
| `--cap-output-len N` | 截断超长回复的 `max_tokens`，控制回放成本 |
| `--max-history-turns N` | 每条请求只保留最后 N 轮历史 |
| `--deidentify` / `--no-deidentify` | 脱敏开关（默认开） |
| `--seed N` | 抽样随机种子 |

日志里通常没有逐请求采样参数，“哪些请求当时显式改过 temperature/top_p/top_k”
无法还原；要贴近生产行为，把 `--params` 设成网关实际注入的画像。

## 3. 被测服务（通用要求，SGLang 只是示例）

任何 OpenAI 兼容服务都行：暴露 `/v1/chat/completions`（或 `/v1/completions`）、
支持 `stream` 并返回 `usage`、有一个 served model name。客户端不带 API Key
鉴权头，需要鉴权请走内网免密、SSH 转发或网关前置鉴权。

SGLang 启动示例（`run_trace.py` 内部就是这条，拆开供排查）：

```bash
nohup python3 -m sglang.launch_server \
  --model-path <模型目录>/snapshots/<hash> \
  --served-model-name <模型名> \
  --chat-template <模板文件> \
  --host 0.0.0.0 --port 30000 --tp 2 \
  --disable-custom-all-reduce --mm-feature-transport cpu \
  --context-length 65536 --mem-fraction-static 0.88 \
  --sampling-defaults model > sglang-trace.log 2>&1 &
curl -sf http://127.0.0.1:30000/health && echo ready
```

常见环境坑（按类别 → 例子）：

1. **模型路径**：HF 缓存要指到 `snapshots/<hash>` 子目录，否则 transformers 报
   “Unrecognized model / Should have a model_type key”。
2. **内核库版本不匹配**：例如 flashinfer 与 flashinfer-cubin 不一致会在 import
   时报错；要么重装匹配版本，要么用引擎的绕过开关
   （flashinfer 是 `FLASHINFER_DISABLE_VERSION_CHECK=1`）。
3. **chat 模板不认角色**：例如 `developer` 让原生模板返回 400
   “Unexpected message role.”；仿照
   [qwen35-chat-template.jinja](qwen35-chat-template.jinja) 加角色分支，或在
   网关侧映射成引擎支持的等价角色。
4. **多卡 TP 与 P2P**：卡间无 NVLink/P2P 时 `--tp 2` 报
   `cudaErrorPeerAccessUnsupported`；用
   `--disable-custom-all-reduce --mm-feature-transport cpu` 回退。
5. **采样默认值**：不同引擎缺省 temperature/top_p/top_k 取值不同，别假设等于
   模型 `generation_config.json`；要么注入，要么显式配置引擎。

## 4. 手动回放（`run_trace.py` 的拆解）

### 4.1 trace_client.py

```bash
python3 benchmark/trace_client.py \
  --url http://127.0.0.1:30000 --model <served-model-name> \
  --trace-file business.jsonl --num-prompts -1 --time-scale 1 \
  --timeout-s 600 --output-file raw_result.json
```

| 参数 | 含义 |
|---|---|
| `--url` | 服务根地址，不带 `/v1` |
| `--model` | 请求体里的模型名，须等于 served-model-name |
| `--num-prompts N` | 只回放前 N 条；`-1`=全部；大于 trace 行数报错 |
| `--time-scale T` | 到达间隔 = 原始间隔 / T |
| `--request-rate R` | 仅 trace 缺 `ts` 时生效（泊松） |
| `--timeout-s T` / `--seed N` | 单请求超时 / 随机种子 |

行为：最多 512 并发、绕过系统代理、统一带 stream 与 usage；TTFT 含服务端排队；
同目录有 `trace_report.py` 时结束自动生成 report.md。输出 JSON 字段：
`completed/replayed/duration/total_*_tokens/*_throughput/mean|median|p99_ttft_ms/
mean|median|p99_tpot_ms/mean|median|p99_e2e_latency_ms`，逐请求数组
`ttfts/itls/input_lens/output_lens/errors`。

### 4.2 run_local_benchmark.sh（合成负载/SLO 流水线）

它也能跑 trace，但产物在 `stages/*/` 下、与 `run_trace.py` 的固定结构不同，
真实业务测试不推荐用。相关环境变量：`TRACE_PATH / TRACE_TIME_SCALE /
MODEL_NAME / BENCH_SSH / BENCH_HOST / BENCH_PORT / BENCH_CLIENT / E2E_TARGET /
VENV_PATH / SERVER_LOG`。

## 5. trace_report.py

```bash
python3 benchmark/trace_report.py \
  --result raw_result.json --trace business.jsonl \
  --model <served-model-name> --server-log server.log \
  --note "TP=2 / context=65536" -o report.md
```

参数：`--result`（必需）、`--trace`、`--server-log`、`--model`、`--title`、
`-n/--note`（可多次）、`-o/--output`。报告九段：概览与成功率、吞吐、
TTFT/TPOT/E2E 分位数、请求规模、到达直方图、TTFT 按到达分钟、失败明细、
服务端峰值、备注。

## 6. 指标怎么读

- 成功率 = completed / replayed。
- TTFT：首 token 延迟，含排队与 prefill；TPOT：token 间延迟，纯解码；
  E2E = TTFT + 全部解码时间。
- 输入/输出吞吐是全程平均（受到达节奏影响），不是服务端峰值能力。
- “TTFT 按到达分钟”里同一分钟单调抬升 = FIFO 排队，后面的分钟继承积压。
- 定位法：TTFT 高而 TPOT 正常 → 排队/prefill；TPOT 也高 → 解码硬件瓶颈。

## 7. 已知限制

- 无 API Key 鉴权头；`max_tokens` 只是上限；图片 URL 拉不到会原样透传；
  缺 `ts` 整份退化为泊松；`num_prompts` 大于行数报错；
  `min_success_rate` 建议 0.99（真实流量有偶发边界请求）。

## 8. bench_serving 快速替代（无逐请求时间戳）

- 文本会话：`dataset_name: "sharegpt"` + `extra_args: "--dataset-path <jsonl>"`；
- agent 轨迹：`dataset_name: "agentic-trace"` + `extra_args: "--dataset-path <json>"`；
- 统一采样参数：`extra_args: "--extra-request-body '{\"temperature\":0.6}'"`。

局限：只能泊松近似，丢掉逐请求 `ts` 与 TTFT/ITL 明细；要还原真实到达节奏仍用
`trace_client.py` / `run_trace.py`。
