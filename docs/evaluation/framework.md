# 模型服务评测框架

## 1. 评估目标

评估体系用于回答四个决策问题：

1. 同一个模型在什么硬件、runtime 和配置下更快、更稳、更省；
2. 一个模型服务在指定 workload 和 SLO 下能稳定处理多少负载；
3. prefix cache、KV offload、并行策略等优化是否产生真实收益；
4. 同一批算力能否交付更多满足质量与 SLO 的合格 Token。

Profile 选型依据同时包含质量、SLO、workload 和吞吐。

## 2. 统一判定语义

每个请求按以下顺序判定：

```text
request_success
  -> slo_pass
  -> quality_pass（有 ground truth 或 judge 时）
  -> accepted
```

- `request_success`：协议成功、响应完整、Token 统计有效；
- `slo_pass`：TTFT、ITL/TPOT、E2E 和成功率满足 workload 门槛；
- `quality_pass`：结果满足准确率、任务指标或已冻结的质量门槛；
- `accepted`：进入最终容量和成本统计的合格请求。

合成性能压测与质量评测分开执行。性能 run 与相同模型 revision、配置和数据分布下通过的质量 run 配对，
共同形成有效测试结论。

## 3. 核心指标

### 3.1 请求与 Token

- request throughput；
- input/output token throughput；
- accepted request goodput；
- accepted output token goodput；
- TTFT、ITL/TPOT、E2E 的 p50/p95/p99；
- 成功率、超时率、排队时间、preemption 和重试次数。

### 3.2 资源与效率

- GPU utilization、显存、功率和能耗；
- KV cache 容量、使用率、命中率和外部复用 Token；
- CPU、内存、网络和存储吞吐；
- 每个 accepted request 的成本；
- 每百万 accepted input/output Token 的成本；
- 分配成本、实际使用成本、共享成本和空闲成本。

### 3.3 核心决策量

```text
accepted_request_goodput = accepted_requests / duration
accepted_token_goodput   = accepted_output_tokens / duration
cost_per_accepted_unit   = total_cost / accepted_units
```

质量作为准入门槛，吞吐和成本作为通过门槛后的优化指标。

## 4. 实验矩阵

最小实验维度为：

```text
model revision
  x runtime revision
  x hardware topology
  x precision / quantization
  x parallelism
  x workload
  x SLO
  x runtime configuration
  x optimization
```

workload 至少覆盖：

- 短对话；
- RAG / 共享前缀；
- 长上下文；
- decode-heavy；
- 多模态（适用时）；
- steady、burst 和故障恢复负载。

## 5. 容量测试

容量测试结果必须绑定：

```text
service_id
+ model_revision
+ runtime_revision
+ hardware_topology
+ workload_profile
+ SLO
+ benchmark_run
```

测试结果至少给出：

- 最大稳定 request rate；
- 最大稳定 input/output token rate；
- 对应的 TTFT、ITL/TPOT、E2E 和成功率；
- 质量门槛及配对证据；
- GPU 数量、利用率、能耗和单位成本；
- 测试时间、适用条件和安全余量。

模型或 Tokenizer revision、runtime revision、量化方式、并行策略、硬件拓扑、关键调度参数、workload 或
SLO 发生变化时，需要重新执行 benchmark，不能直接复用旧结果。

## 6. 端到端测量范围

数据面测量从负载发生器发送请求开始，经过 Gateway、Kubernetes 网络和推理 workload，直到完整接收响应。
控制组件异步运行，其 CPU、内存和共享资源成本计入总成本。

每个 run 必须记录：

- Git commit 与未提交状态；
- Kubernetes context、namespace 和工作负载 revision；
- Node、GPU、driver、runtime、镜像和资源限制；
- Model Service Profile 与实际启动参数；
- `Service` / `EndpointSlice` 快照；
- workload、SLO、请求级结果、指标和日志；
- 质量 run 或质量门槛的配对证据。

## 7. 结果产物

每个 benchmark run 至少保存：

```text
runs/<run_id>/
  manifest.json
  model_service_profile.json
  kubernetes_snapshot/
  workload.json
  requests.jsonl
  metrics.json
  quality.json
  cost.json
  status.json
  logs/
```

`manifest.json` 是复现入口，引用其余产物及内容摘要。报告从完整 run 自动生成关键指标。

## 8. 决策报告

第一期生成四类报告：

1. **模型/内部 Runtime Top3**：每个模型、workload 和 SLO 下的合格配置排名；
2. **容量测试表**：稳定 request/token 速率和对应的安全余量；
3. **优化收益报告**：prefix cache、KV offload、量化、并行等优化的对照实验；
4. **资源与成本报告**：GPU 利用率、能耗、空闲成本和单位合格 Token 成本。

排名先选择质量与 SLO 达标的配置，再比较 accepted goodput 和单位成本。
这些报告只用于平台内部选择 Profile 和实例数，不进入模型服务发布请求，也不是 Controller 或 Kubernetes
需要管理的运行时对象。
