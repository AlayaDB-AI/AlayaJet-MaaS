# 推理、模型服务管理与用量计量契约

## 1. 契约目标

AlayaJet-MaaS 对上层提供稳定的推理接口和用量事件，对平台运维人员提供模型服务管理接口。
GPU、Kubernetes、Pod、内部 endpoint、Runtime 类型与参数和并行拓扑由平台内部统一管理。

外部协作围绕三个接口面展开：

| 接口面 | 方向 | 作用 |
| --- | --- | --- |
| Inference API | 上层 -> AlayaJet-MaaS | 标准流式和非流式推理 |
| Management API | 平台运维人员 -> Control Plane | 审核节点资源，维护 GPU 型号别名，发布、更新、扩缩、回滚和查询模型服务 |
| Usage Events | AlayaJet-MaaS -> 上层 | 传递请求和 Token 用量事实 |

## 2. 协作职责

| 领域 | 上层 MaaS | AlayaJet-MaaS |
| --- | --- | --- |
| 身份与权限 | 租户、项目、API Key、商品授权 | 校验可信服务身份和透传的租户上下文 |
| 商业限制 | 余额、套餐、商业配额、客户级限流 | 服务容量、排队、过载保护和安全上限 |
| 模型目录 | 客户可见名称、商品和渠道 | 实际 serving model、revision、能力和运行状态 |
| 推理请求 | 客户协议、商业准入 | 模型服务解析、请求调度、过载保护和执行 |
| 用量与账单 | 单价、折扣、账单和对账 | 权威请求与 Token 用量事实 |
| 故障 | 客户认证、商品、计费和公网入口 | 模型服务、Gateway、Request Scheduler、AlayaJet Inference Engine、GPU 和执行路径 |

## 3. Inference API

接口以 OpenAI-compatible 语义为基础，第一期至少覆盖：

- Chat 或 Responses 风格的文本生成；
- 流式与非流式响应；
- 至少一种非纯文本生成能力，例如 Embedding 或图像理解；
- 客户端提供的 `request_id` / trace 上下文；
- 稳定的错误码、可重试标记和可选 `retry_after_ms`。

调用方指定模型时，Gateway 在该逻辑模型对应的健康服务实例中路由。跨模型切换使用上层显式提供的候选
集合与授权策略。推理服务统一由 AlayaJet Inference Engine 交付，Inference API 不返回底层 Runtime
类型、启动参数或物理实例信息。

### 3.1 请求身份

每个请求至少贯穿以下标识：

| 标识 | 用途 |
| --- | --- |
| `request_id` | 响应、trace、错误、日志和用量事件的统一关联键 |
| `traceparent` | 分布式追踪上下文 |
| `idempotency_key` | 非流式、可安全重放请求的幂等约束 |
| tenant/project context | 上层签名后的可信调用上下文，商品规则和请求优先级由上层完成解释 |

Gateway 根据可信上下文生成内部调度优先级；普通客户端不能通过自定义 header 任意提高优先级。

### 3.2 重试与 failover

- Request Scheduler 为 Gateway 返回 primary 和 fallback Engine endpoint；
- primary 在响应开始前失败时，Gateway 使用 fallback，并在重新调度时排除已经失败的 endpoint；
- Request Scheduler 不可用时，Gateway 可以在 Ready endpoints 中做不带 KV/负载策略的基础选择；
- 流式响应在选定实例上完成；
- 上层根据稳定错误码、`retryable` 和 `retry_after_ms` 判断重试；
- 每次内部执行尝试归属于同一个 `request_id`，最终形成一份权威用量记录；
- 商业配额和推理服务容量使用不同的稳定错误码表达。

## 4. Management API

Management API 面向平台运维人员，管理模型服务并提供运行状态。每个模型服务至少包含：

```json
{
  "service_id": "qwen-example",
  "model_id": "Qwen/example",
  "model_revision": "sha256:...",
  "status": "ready",
  "capabilities": ["chat"],
  "modalities": ["text"],
  "max_context_tokens": 32768,
  "engine_instances": 2,
  "ready_instances": 2
}
```

Management API 报告模型服务的期望实例数和健康实例数。吞吐、延迟、队列和 Token 速率作为运行指标
单独观测。

## 5. 用量计量

AlayaJet-MaaS 在请求结束或确定终止后生成权威用量事实。最小字段包括：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 用量事件幂等键 |
| `request_id` | 对应推理请求 |
| `service_id` | 实际使用的模型服务 |
| `model_id` / `model_revision` | 实际模型及不可变版本 |
| `started_at` / `completed_at` | 执行时间范围 |
| `input_tokens` | 输入 Token |
| `output_tokens_generated` | AlayaJet Inference Engine 已生成的输出 Token |
| `output_tokens_delivered` | 已写入响应连接的输出 Token |
| `cached_input_tokens` | 命中缓存的输入 Token（若可获得） |
| `reasoning_tokens` | reasoning Token（若模型提供） |
| `attempt_count` | 内部执行尝试次数 |
| `status` / `error_code` | 最终结果 |

已生成 Token 表示已经发生的推理计算，已交付 Token 表示已经写入响应流。流式连接中断时两者可能不同，
因此必须分别记录。具体采用哪一项计价由上层决定。

### 5.1 投递语义

- 用量事件至少一次投递；消费方按 `event_id` 幂等；
- 同一请求需要修正时产生新的 correction 事件，历史记录保持可追溯；
- 投递失败进入可重放队列，确保用量完整交付；
- 定期按 `request_id` 对账推理日志、用量账本和上层消费结果；
- 用量事实采用稳定服务字段，Kubernetes Secret、容器命令、模型文件路径和内部 endpoint 保留在平台内部。

## 6. 模型服务状态

管理接口中的模型服务状态保持最小集合：

| 状态 | 含义 |
| --- | --- |
| `pending` | 正在部署或加载，生产流量关闭 |
| `ready` | 存在健康实例，可以接收请求 |
| `degraded` | 部分实例不可用，但仍可提供服务 |
| `draining` | 新流量关闭，等待在途请求结束 |
| `paused` | 用户期望暂停；路由关闭、运行工作负载已停止，服务配置仍保留 |
| `unavailable` | 当前服务容量为零 |

`paused` 是用户选择的稳定期望状态；`unavailable` 表示服务期望运行，但当前没有可用容量。

平台运维人员通过 Management API 查看模型服务状态；Gateway 使用已发布的服务视图路由流量。
Pod、节点和内部 endpoint 状态由平台内部汇聚。
