# 真实业务 trace 回放

用真实业务流量压 OpenAI 兼容服务。一条命令跑完整条链：

```bash
python benchmark/run_trace.py
```

自动完成：检查/启动服务 → 上传切片与客户端 → 按原始时间戳回放（实时进度）→
拉回结果与服务日志 → 生成 `report.md` → 关停本次启动的服务。默认目标：
gpu10 / Qwen3.8-27B-FP8 / TP=2 / 15 分钟切片。

Windows 上若 `python` 是微软商店占位符（报 cannot be accessed），用
`%APPDATA%\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe`
代替。

## 常用变体

```bash
python benchmark/run_trace.py --trace benchmark/traces/business-peak-5min.jsonl  # 换切片
python benchmark/run_trace.py --tp 1                                             # 单卡
python benchmark/run_trace.py --time-scale 60                                    # 时间压缩
python benchmark/run_trace.py --start-server never --keep-server                 # 服务已在跑，别动它
```

| 参数 | 含义 |
|---|---|
| `--trace PATH` | 回放文件，默认 15 分钟切片 |
| `--tp 1/2` | 模型并行度，默认 2 |
| `--time-scale T` | 到达间隔压缩/拉伸，默认 1 |
| `--start-server auto/always/never` | 服务管理方式，默认 auto |
| `--keep-server` | 结束后保留服务 |
| `--env FILE` | 指定配置文件（默认 `benchmark/trace_env.json`） |
| `--show-config` | 只打印合并后的配置，不执行 |
| `--note "..."` | 写进报告的备注，可多次传 |

## 每次产物（固定五个文件）

```text
benchmark/runs/trace-YYYYMMDD-HHMMSS/
├── raw_result.json   逐请求原始数据
├── report.md         汇总报告
├── server.log        服务端日志
├── run_meta.json     本次配置与关键数字
└── trace-input.jsonl 本次回放文件的副本
```

## 截取别的时间段

```bash
# 从日志最早时刻跳 28500s，向后取 300s
python benchmark/traces/log_to_trace.py 会话数据1.ndjson \
  -o benchmark/traces/my-window.jsonl --start-offset-s 28500 --window-s 300
python benchmark/run_trace.py --trace benchmark/traces/my-window.jsonl
```

转换默认脱敏、默认不注入采样参数（走服务端默认）；`--window-s 900` 就是
开头 15 分钟。

## 换机器 / 换模型

不用改代码、也不用每次敲参数：直接编辑 [trace_env.json](trace_env.json) 里的
字段即可（`ssh` 执行机、`model` 服务名、`model_path` 权重目录、`template`
chat 模板、`context`、`tp`、`trace` 等）。多套环境就复制成多个文件，用
`--env` 指定：

```bash
python benchmark/run_trace.py --env benchmark/trace_env_kimi.json
```

优先级：命令行参数 > 配置文件 > 内置默认值。不确定当前生效配置时先跑
`python benchmark/run_trace.py --show-config`。模型不认日志里的 `developer`
角色时，参考 [qwen35-chat-template.jinja](qwen35-chat-template.jinja) 改模板，
并把模板路径写进配置文件。

## 报告怎么看

打开 `report.md`：成功率、吞吐、TTFT/TPOT/E2E 分位数，重点看“TTFT 按到达
分钟”——同一分钟 TTFT 单调抬升说明在排队。TTFT 高而 TPOT 正常 = 排队/prefill
问题；TPOT 也高 = 解码到硬件瓶颈。

## 常见坑

- 双卡无 NVLink 时 TP=2 报 `cudaErrorPeerAccessUnsupported`：`run_trace.py
  --tp 2` 已自动带兼容参数；手动起服务要加
  `--disable-custom-all-reduce --mm-feature-transport cpu`。
- HF 模型路径必须到 `snapshots/<hash>`，不能停在 `models--<org>--<model>` 层。
- 客户端不带 API Key 鉴权头，需要鉴权请走内网免密或网关。
- `max_tokens` 只是上限，回放的实际输出长度可能和原日志不同。
- 整份 trace 只要有一条缺 `ts`，就整份退化为泊松到达。
- 首次启动服务可能要 5~10 分钟（内核 JIT 编译），`run_trace.py` 会自己等到就绪。

## 深入资料

trace 文件格式、任意日志 → trace 的通用映射、手动拆解每条命令、
`run_local_benchmark.sh` / `bench_serving` 的替代用法，见
[DETAILS.md](DETAILS.md)。

## 数据与隐私

真实日志、`business-*.jsonl`、`benchmark/runs/` 都在 .gitignore 里，不进
版本库；脱敏后的 trace 也别外发，仅限内网压测。
