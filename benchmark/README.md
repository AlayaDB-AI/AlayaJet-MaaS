# Benchmark 使用手册

对一个模型部署做完整 E2E 评估：质量基线 + 性能压测 + 容量拐点 + 故障恢复 + 成本。
产物按 `docs/evaluation/framework.md` §7 规范落盘，报告自动生成。

> 脚本内置的默认值（`gpu10-qiyu`、`/home/qiyu/...` 等）只是最初开发环境的快捷方式，
> **在其他机器上务必显式覆盖**。下面的命令都不依赖这些默认值。

## 0. 前置条件

| 位置 | 要求 |
|---|---|
| 你的本机 | Git Bash 或 Linux shell；`ssh`/`scp`/`curl`/`tar`/`tee` 可用；Python 由 `benchmark/env.sh` 自动探测（也可设 `PYTHON_BIN=<路径>`） |
| 执行机 | `~/.ssh/config` 配好免密 Host（脚本用 BatchMode，不交互）；sglang venv；模型权重已下载且含 `config.json`；跑质量测试还需要该 venv 里有 `requests` 和 `datasets` |
| 网络 | 执行机能访问被测服务的 `BENCH_HOST:PORT`；E2E 模式还需要你的本机能访问该服务 |

## 1. 三步上手

```bash
# ① 先把环境显式写清楚（换成你自己的值）
export BENCH_SSH=<被测机ssh别名>          # 例如 dev-gpu
export VENV_PATH=<执行机上的sglang venv>  # 例如 /home/deploy/sglang-env
export SERVER_LOG=<执行机上的日志文件>    # 例如 /home/deploy/sglang.log

# ② 完整评估：拉起新服务 + 质量 + 7个性能workload + 故障恢复 + 报告（约 50 分钟）
./benchmark/evaluate.sh \
  --model Qwen/Qwen3-8B \
  --model-path <执行机上的模型权重目录> \
  --tp 1

# ③ 服务已经在跑，直接复用（跳过启动）
./benchmark/evaluate.sh --model Qwen/Qwen3-8B --attach
#   注意：attach 时脚本不知道 tokenizer 在哪，random-ids 压测需要显式给
#   TOKENIZER_PATH=<执行机上的 tokenizer/权重路径>，否则回落到脚本内置默认值，
#   输入长度统计会错。

# ④ E2E 模式：压测流量从你本机经真实网络打过去（含网络段延迟）
E2E_TARGET=http://<服务在本机可达的地址>:<端口> \
  ./benchmark/evaluate.sh --model Qwen/Qwen3-8B --attach --e2e-local
```

跑完看报告：`benchmark/runs/<run_id>/report.md`。

## 2. evaluate.sh 参数与常用环境变量

| 参数 | 说明 |
|---|---|
| `--model NAME` | 服务的 served-model-name（必传） |
| `--model-path PATH` | 执行机上的权重路径（非 `--attach` 时必传） |
| `--tp N` | TP 并行度，默认 1 |
| `--port PORT` | 服务端口，默认 30000（等价于 `BENCH_PORT` 环境变量） |
| `--attach` | 复用已在运行的服务，跳过启动 |
| `--skip-quality` | 跳过质量测试 |
| `--skip-fault-recovery` | 跳过故障恢复（默认包含，会 kill 并重启服务，放最后执行） |
| `--e2e-local` | 负载发生器在本机运行（E2E 含网络段）；**必须同时设置 `E2E_TARGET`** |
| `--workloads "..."` | 自定义性能 workload 列表（默认 `steady ramp burst overload rag_prefix longctx decode_heavy`，末尾自动追加 `fault_recovery`） |
| `--suites "..."` | 自定义质量 suite 列表（默认 `niah gsm8k ifeval longbench_v2`） |

| 环境变量 | 说明 |
|---|---|
| `BENCH_SSH` | 压测执行机 ssh 别名 |
| `BENCH_HOST` / `BENCH_PORT` | 被测服务地址，**执行机视角**（默认 127.0.0.1:30000） |
| `VENV_PATH` / `SERVER_LOG` | 执行机 venv / server 日志路径 |
| `TOKENIZER_PATH` | 执行机上的 tokenizer 路径（random-ids 依赖；evaluate.sh 默认取 `--model-path`） |
| `E2E_TARGET` | `--e2e-local` 时本机访问服务的地址，必填 |
| `GPU_INDEX` / `SERVER_TP` | dmon 采样哪张卡 / 故障恢复重启时用的 TP（evaluate.sh 自动传） |
| `PYTHON_BIN` | 覆盖本机 Python 解释器 |

## 3. 单独跑一个 workload

```bash
# 在被测机上压（loopback，不含网络段，用于容量判定）
BENCH_SSH=<执行机> TOKENIZER_PATH=<执行机tokenizer路径> \
  MODEL_NAME=<服务模型名> ./benchmark/run_local_benchmark.sh steady

# E2E 模式（本机 → 被测机，含网络段）
BENCH_CLIENT=local E2E_TARGET=http://<服务地址>:<端口> \
  MODEL_NAME=<服务模型名> ./benchmark/run_local_benchmark.sh steady
```

> 为什么给 `MODEL_NAME=`：workload json 里的 `model` 字段只是默认值
> （内置文件里还是旧的 Qwen2.5-0.5B），`MODEL_NAME` 会覆盖它；不给就会拿错误的
> 模型名去打服务，全部请求 404。

可用 workload（`benchmark/workloads/*.json`）：`steady`（稳态）、`ramp`（阶梯找拐点）、
`burst`（突发+恢复）、`overload`（过饱和）、`rag_prefix`（共享前缀）、`longctx`（长上下文）、
`decode_heavy`（长输出）、`fault_recovery`（故障恢复）、`multimodal`（多模态，**仅 VL 模型、
仅 remote 模式**，需 `MODEL_NAME`/`TOKENIZER_PATH` 指向 VL 模型）。每个 workload 的用途
与设计理由见 §4。
`sustain*.json` 是早期容量探索遗留，一般不用。

断点续跑：同一 run-id 重跑会跳过已完成的 stage——
`./benchmark/run_local_benchmark.sh steady <run-id>`。
注意：若该 run 是 evaluate.sh 跑的，目录已被移到 `runs/<EVAL_ID>/perf/` 下，
要先移回 `benchmark/runs/` 再续跑，跑完移回去后用 `finalize_run.py` 重新汇总。

## 4. Workload 一览与设计理由

所有 workload 都定义在 `benchmark/workloads/*.json`，都是**合成负载**：随机 token id、
长度精确可控，隔离掉数据集内容的影响，只测引擎/调度/网络的性能；质量维度由 §5 的
质量 suites 单独覆盖。判定统一走 `framework.md` §2 的四级语义：
`request_success → slo_pass → quality_pass（无 ground truth 时为 None）→ accepted`。

`evaluate.sh` 默认跑 `steady ramp burst overload rag_prefix longctx decode_heavy` 七个，
并在末尾追加 `fault_recovery`；`multimodal` 只在测 VL 模型时手动加。每个 workload 只回答
一个决策问题，组合起来覆盖 framework.md §4 的最小实验矩阵。

### steady —— 稳态

| stage | 速率 | 请求数 | 输入/输出 |
|---|---|---|---|
| steady-8rps | 8 req/s | 240 | 512 / 128（±50%） |

回答：**在给定水位下能不能稳定满足 SLO，accepted goodput 是多少。**

为什么这样设计：

- 240 个请求按 8 req/s 到达约 30 秒，窗口足够长，p99 有统计意义，也能看出队列是稳定还是缓慢上涨；
- 单一水位本身不做容量判断，和 ramp 分工：steady 是“指定水位验收”，ramp 是“找水位”；
- `range_ratio=0.5` 让输入/输出长度在 50%~100% 之间波动，避免所有请求同样长带来的伪平滑。

### ramp —— 阶梯加压找拐点

| stage | 速率 | 请求数 |
|---|---|---|
| r1 / r2 / r4 / r8 / r16 / r32 / r64 / r96 / r128 / r192 / r256 | 1 → 256 req/s | 30 / 40 / 60 / 80 / 96 / 128 / 160 / 192 / 256 / 288 / 384 |

回答：**容量拐点在哪——最大稳定速率、撞线速率、建议运行水位。**

为什么这样设计：

- 速率逐档翻倍，指数覆盖 1~256 req/s，档数是 log 级的，不用从 1 慢慢往上爬；
- 每档请求数只随速率线性增长（r1 约 30 秒、r256 约 1.5 秒）：低档保证统计量，高档控制总时长；
- `early_stop_after_fails: 2`：连续两档 SLO 失败就停。第一档失败可能只是偶发，连续两档基本确定到顶了；
  深度过饱和留给 overload，不在这里烧 GPU 时间。基础设施失败档不计入连续计数；
- 汇总时的拐点规则：最后一个 SLO 通过的档 = 最大稳定速率，下一失败档 = 撞线点，
  建议水位 = 最大稳定速率的 70%（留安全余量）。

### burst —— 突发 + 恢复

| stage | 速率 | 请求数 | 时长约 |
|---|---|---|---|
| burst-32rps | 32 req/s | 96 | 3 s |
| recovery-8rps | 8 req/s | 60 | 7.5 s |

回答：**突发冲击下排队和 TTFT 恶化多少，流量回落后延迟能否恢复。**

为什么这样设计：

- 突发档是稳态水位的 4 倍，用短窗口集中打 96 个请求模拟流量尖峰；
- 恢复档立即回到稳态速率，专门抓“回涌后的二次过载”：前面的积压请求还在处理，
  延迟不会马上回落，这一档验证系统能不能自己消化积压；
- 两档共用一套 SLO，第一档允许失败（重点看恶化曲线），第二档要求回到正常。

### overload —— 过饱和

| stage | 速率 | 请求数 |
|---|---|---|
| overload-64rps | 64 req/s | 256 |

回答：**过饱和时怎么拒绝、错误类型分布、延迟恶化曲线、有没有重试放大。**

为什么这样设计：

- 按约 2 倍拐点速率冲击，SLO 大面积失败是**预期结果**，该 workload 的 status 记
  partial_failure 不代表出错，重点是可观测性：成功率、超时率、错误类型、延迟分布；
- 和 ramp 明确分工：ramp 找到第一档失败就停，overload 负责深度过饱和下的行为观察；
- 速率标注了“跑完 ramp 后按实际拐点 2 倍修正”，因为每台机器拐点不同。

### rag_prefix —— RAG / 共享前缀

| stage | 速率 | 请求数 | 数据集 |
|---|---|---|---|
| gsp-8rps | 8 req/s | 240 | generated-shared-prefix |

额外参数：16 个组 × 每组 15 个请求，共享 2048 token 的 system prompt，问题 128 token，
输出 128 token。

回答：**prefix cache（radix cache）命中率收益、共享前缀下的 TTFT 和吞吐。**

为什么这样设计：

- 直接用 sglang 内建的 generated-shared-prefix 数据集，不必自造带共享前缀的语料；
- 16×15 的组合保证每组前缀有 15 个重复请求预热 radix cache，命中率有统计意义；
- 和 steady 对照使用：输入规模接近，唯一差别是前缀可复用，`cache_hit_rate` 是核心观察量。

### longctx —— 长上下文

| stage | 速率 | 请求数 | 输入 / 输出 | SLO 放宽 |
|---|---|---|---|---|
| ctx8k-2rps | 2 req/s | 60 | 8192 / 256 | TTFT 5 s、E2E 30 s |
| ctx16k-1rps | 1 req/s | 40 | 16384 / 256 | 同上 |

回答：**长 prefill 下 TTFT 随长度怎么涨、吞吐多少。**

为什么这样设计：

- prefill 时间近似随输入长度线性增长，速率必须压到 1~2 req/s，否则第一档自己就把系统打爆；
- TTFT 阈值放宽到 5 秒（长 prefill 下 500ms 不现实），TPOT 仍保持 100ms——生成速度不该因上下文变长而变差；
- 8k/16k 都在 32k 上下文窗口内并留了输出余量；测更大窗口时按模型实际窗口调整。

### decode_heavy —— 长输出

| stage | 速率 | 请求数 | 输入 / 输出 | SLO 放宽 |
|---|---|---|---|---|
| dec-4rps | 4 req/s | 80 | 128 / 1024 | E2E 60 s |

回答：**decode 阶段吞吐、TPOT 稳定性、长输出下的整请求延迟。**

为什么这样设计：

- 输入短输出长，把瓶颈全部压在 decode（逐 token 生成）阶段，和 longctx（压 prefill）互补；
- 4 req/s × 1024 token 是对 decode 吞吐的显式压力，80 个请求足够统计 p99；
- E2E 放宽到 60 秒（1024 token 输出本身就耗时），TTFT/TPOT 阈值保持严格。

### fault_recovery —— 故障恢复

| stage | 速率 | 请求数 | 特殊动作 |
|---|---|---|---|
| pre-fault-8rps | 8 req/s | 60 | 无（基线） |
| post-recovery-8rps | 8 req/s | 60 | 注入 SIGKILL 并重启后再压 |

回答：**服务被 SIGKILL 后多久重启到健康，恢复后 SLO 是否回落正常。**

为什么这样设计：

- 先打一档正常基线，再在完全相同的负载下注入故障，恢复前后可比；
- 注入用 pkill 限定“本用户 + 精确端口”，不会误杀共享机器上别人的服务；重启到健康的时间、
  停机确认码、重启命令都记在 stage 的 `fault.json`；
- evaluate.sh 把它放到最后，因为它会 kill 并重启服务，不能影响其他 workload 的采样。

### multimodal —— 多模态（仅 VL 模型）

| stage | 速率 | 请求数 | 输入 | 特殊参数 |
|---|---|---|---|---|
| mm-4rps | 4 req/s | 80 | 256 token + 1 张 1080p 随机 jpeg | image 数据集 |

回答：**图像 prefill 下的 SLO 与吞吐。**

为什么这样设计：

- 图像 prefill 重，速率保守，TTFT/E2E 阈值放宽到 2 s / 15 s；
- 只支持 remote 模式：E2E 客户端只发 token id、不带图；
- 必须用 `MODEL_NAME`/`TOKENIZER_PATH` 指向 VL 模型；纯文本模型评估时该项标记为 n/a。

### sustain / sustain2 / sustain3 —— 早期持续容量探索（遗留）

| 文件 | 速率档 | 每档时长 |
|---|---|---|
| sustain | 80、120 req/s | 60 s |
| sustain2 | 160、200 req/s | 60 s |
| sustain3 | 240、280 req/s | 60 s |

回答（历史用途）：**“真实持续容量”——到达率超过服务率时，队列无界增长、TTFT 必然爆掉。**

为什么基本不用：

- 每档 60 秒、数千到上万请求，跑得久、成本高；
- ramp + overload 用少得多的请求回答同一个拐点问题；
- 文件保留在仓库里供参考，常规评估不跑。

### SLO 的通用口径

- `ttft_p99_ms=500`：交互场景首 token 体感阈值；长上下文场景按 prefill 成本放宽；
- `tpot_p99_ms=100`：生成速度体感阈值（约等于 ≥10 token/s），所有 workload 基本保持严格；
- `e2e_p99_ms=10000`：整请求耐心上限，长输出放宽到 60 s；
- `min_success_rate=0.999`：合成压测里任何失败都是容量/稳定性信号，所以只容忍 0.1%；
  小样本的档（如 ramp r1 的 30 条）会因此对单点失败敏感，这是有意的严格口径。

## 5. 单独跑质量测试

```bash
BENCH_SSH=<执行机> VENV_PATH=<执行机venv> \
QUALITY_BASE_URL=http://<执行机视角的服务地址>/v1 QUALITY_MODEL=<服务模型名> \
  ./benchmark/quality/run_quality.sh niah gsm8k ifeval longbench_v2
```

`QUALITY_BASE_URL` 是**执行机视角**：suite 会上传到 `BENCH_SSH` 执行机上运行，
`127.0.0.1` 指执行机的 loopback，不是你本机。执行机 venv 需装 `requests` 和 `datasets`
（数据集默认走 `HF_ENDPOINT=https://hf-mirror.com`，可覆盖）。同步到执行机的临时目录
默认放目标用户 HOME 下，可用 `REMOTE_DIR` 覆盖。

基线冻结在 `benchmark/quality/quality_baseline.json`（容差 ±0.02）。

## 6. 测别的机器上的别的模型

只变下面几样，代码不用改：

| 会变的 | 怎么给 |
|---|---|
| 机器和用户 | `~/.ssh/config` 加 Host 别名 → `BENCH_SSH=<别名>` |
| 模型名 | `--model` |
| 权重路径（远端） | `--model-path`（`TOKENIZER_PATH` 默认与它相同，不一致时单独给） |
| 用几张卡 | `--tp` |
| 远端环境 | `VENV_PATH`、`SERVER_LOG` |

**前提**：被测机上已有可用的 sglang venv 和模型权重；你的公钥已加到目标用户的
`authorized_keys`。

**A. 同机器换模型**：

```bash
export BENCH_SSH=<执行机> VENV_PATH=<venv路径> SERVER_LOG=<日志路径>
./benchmark/evaluate.sh --model Qwen/Qwen3-32B \
  --model-path <执行机上的Qwen3-32B权重目录> --tp 2
```

**B. 换机器**：

```bash
# 1. ~/.ssh/config:
#   Host <别名>
#     HostName <目标机IP或域名>
#     User <目标用户>
#     IdentityFile ~/.ssh/id_ed25519

# 2. 一条命令：
BENCH_SSH=<别名> \
VENV_PATH=<执行机venv> \
SERVER_LOG=<执行机日志路径> \
./benchmark/evaluate.sh --model <模型名> \
  --model-path <执行机权重路径> --tp <N>
```

**C. 服务在 K8s 集群里**（平台管服务，脚本不拉起）：

```bash
BENCH_SSH=<能访问集群的执行机> \
BENCH_HOST=<Gateway地址> BENCH_PORT=<端口> \
./benchmark/evaluate.sh --model <模型名> --attach --skip-fault-recovery
```

冒烟、质量、压测都会从执行机向 `BENCH_HOST:BENCH_PORT` 发请求，确保执行机到 Gateway
网络可达。平台托管的服务脚本无法 kill/重启，所以必须带 `--skip-fault-recovery`。

**D. 只要用户视角延迟（E2E，本机发流量）**：

```bash
BENCH_CLIENT=local E2E_TARGET=http://<服务地址>:<端口> \
  MODEL_NAME=<模型名> ./benchmark/run_local_benchmark.sh steady
```

## 7. K8s Job 模式（集群内压测客户端）

`run_benchmark.sh` 与 local 版不同：它不在执行机上跑 bench_serving，而是把压测渲染成
Kubernetes Job 提交进集群：

```bash
MODEL_NAMESPACE=<模型服务namespace> KUBECONFIG_PATH=<kubeconfig路径> \
  ./benchmark/run_benchmark.sh steady
```

说明：

- 默认从 `deploy/sglang-native/scripts/common.sh` 的 `nodes.json` 约定读取
  kubeconfig（`configured_kubeconfig`）、服务地址和 nodePort（`cluster_field`），
  也可用 `KUBECONFIG_PATH` / `BENCH_HOST` / `BENCH_PORT` 显式覆盖。
- Job 由 `benchmark/job.yaml` 渲染，调度到 gpu-worker 节点；模板假定模型挂在
  `hostPath /mnt/data/models` 并映射为 `/models/<模型名>`，集群挂载方式不同时改模板。
- 该模式只测请求侧指标，没有 dmon / `/metrics` / sysmon 采样。
- 结果同样落 `benchmark/runs/<workload>-<timestamp>/`，断点续跑传 run-id。

## 8. 结果产物

```text
benchmark/runs/<model>-tp<N>-<时间戳>/
  manifest.json            复现入口（含实验矩阵、workload 覆盖清单）
  report.md                决策报告（看这个就行）
  metrics.json             全部性能/质量/排队/preemption/KV 指标
  cost.json                四维成本（分配/使用/共享/空闲）+ 三个单位成本口径
  quality.json             质量分数与基线判定
  pairing.json             质量×性能配对证据
  requests.jsonl           逐请求明细（ttft/itl/e2e/error）
  status.json              总体与各 workload 的结果
  model_service_profile.json  服务端全量启动参数
  kubernetes_snapshot/     standalone 模式放硬件快照
  logs/                    server 日志、/metrics 采样、各子脚本控制台输出
  perf/<workload-run>/     各 workload 完整明细（sysmon.log、dmon、逐 stage 结果）
  quality/                 质量 run 逐题明细
```

stage 目录里出现 `stage_infra_error.txt` 表示该 stage 是 ssh/scp 瞬时故障，不是 SLO 失败；
这类 stage 不会计入容量拐点判定。

## 9. 成本配置

`benchmark/cost_config.json`：卡时价、电价、PUE、共享设施分摊，
**每个价格都带换算标准注释**，按实际采购/电价改。改完对存量 run 重算：

```bash
source benchmark/env.sh
"$PYTHON_BIN" benchmark/collect_results.py benchmark/runs/<某个perf-run目录>
```

## 10. 故障排查

| 症状 | 原因与处理 |
|---|---|
| 闪退、无任何输出 | 本机找不到 Python。设 `PYTHON_BIN=<你的python路径>` 重试 |
| E2E 全部 502 | Windows 系统代理截胡。`e2e_client.py` 已内置禁用代理，确认用最新代码 |
| 服务莫名消失（日志尾部 SIGKILL） | 共享机器上可能被他人进程/看门狗 kill。重跑即可，长时间评估前先确认没人用卡 |
| pkill: Operation not permitted | 正常——故障注入只杀本用户 + 指定端口的进程，这是故意的安全设计 |
| stage 失败 | 看 `runs/<run>/stages/XX-<label>/bench.log`；瞬时故障会自动重试一次 |
| `bench.log` 为空或 `raw_result.json` 0 字节 | 本机↔执行机 SSH/scp 瞬断。脚本会自动重试一次，仍失败会写 `stage_infra_error.txt`；排除网络抖动后删掉该 stage 目录，用同 run-id 续跑 |
| `--e2e-local` 报错要求 `E2E_TARGET` | E2E 模式的本地访问地址无法自动推导，显式传 `E2E_TARGET=http://<服务地址>:<端口>` |

## 11. 自定义 workload

在 `benchmark/workloads/` 加一个 json：

```json
{
  "name": "my_case",
  "backend": "sglang-oai",
  "model": "Qwen/Qwen3-8B",
  "stages": [{
    "label": "s1", "request_rate": 8, "num_prompts": 120,
    "input_len": 512, "output_len": 128, "range_ratio": 0.5
  }],
  "slo": {"ttft_p99_ms": 500, "tpot_p99_ms": 100,
          "e2e_p99_ms": 10000, "min_success_rate": 0.999}
}
```

stage 级可选字段：`dataset_name`（如 `generated-shared-prefix` 走 RAG 前缀）、
`extra_args`（bench_serving 额外参数）、`inject_fault_before: "sigkill"`（该 stage 前先杀服务再重启，测故障恢复）。
workload 级可选字段：`early_stop_after_fails: 2`（连续 N 档 SLO 失败即终止后续档，ramp 已启用，
判定口径与报告完全一致；基础设施失败不计数）。

注意 `model` 字段要与服务的 served-model-name 一致（或跑的时候用 `MODEL_NAME=` 覆盖），
`request_rate` 支持小数。然后 `./benchmark/run_local_benchmark.sh my_case`。
