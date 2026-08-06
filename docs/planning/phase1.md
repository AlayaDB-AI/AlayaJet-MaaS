# AlayaJet-MaaS 第一期计划

## 1. 第一期目标

在三个月内跑通以下闭环：

```text
模型资产
  -> AlayaJet Inference Engine 部署
  -> 标准推理接口
  -> 稳定服务
  -> 用量计量
```

第一期聚焦模型服务闭环：模型可以在多机异构算力上被可重复部署、稳定调用和准确计量。

## 2. 核心交付

1. 选定两到三个代表性模型，覆盖文本生成和至少一种非纯文本能力；
2. 为每个模型建立版本化 Model Service Profile；
3. 使用 Kubernetes 原生资源完成部署、readiness、滚动更新、drain 和回滚；
4. 打通标准推理接口的流式与非流式请求；
5. 建立 InferencePool 和 Request Scheduler（EPP 实现）请求调度路径；
6. 打通 Engine 负载状态、KV Cache 事件和精确 Prefix-aware 路由；
7. 在协议中保留请求优先级、公平队列、负载评分和 fallback endpoint；
8. 建立 `request_id`、trace 和幂等用量账本；
9. 记录 input、generated output、delivered output、cached 和 reasoning Token（适用时）；
10. 对首批模型完成性能、质量、稳定性和容量 benchmark；
11. 输出可复现的测试结果，作为 Profile、实例数和调度策略的选择依据；
12. 验证实例故障、节点故障、容量耗尽、流式中断、rollout 和回滚；
13. 建立新节点待审核、资源核对、批准激活、停用和重新审核的准入链路；
14. 建立服务状态、调度决策、推理指标、GPU 指标、告警和用量对账视图。

## 3. 第一期技术基线

- 使用 Kubernetes `Node`、`Lease` 和 NVIDIA GPU Operator 管理多机异构资源；
- GPU 设备分配基线为 Kubernetes `1.34.2`、GPU Operator `26.3.3`、NVIDIA DRA Driver `0.4.1`、
  NVIDIA Driver `580` 和 CDI；
- 新节点使用 `NoSchedule` 准入隔离 taint 注册；发现组件采集资源并由运维人员审核，批准清单与
  Kubernetes 实际状态一致后才标记为 `active` 并进入可部署资源视图；
- NVIDIA DRA Driver 通过 `ResourceSlice` 发布每张 GPU 的产品、架构、显存、UUID 和拓扑；
- 运维人员将 GPU 原始型号关联为调度别名，并在 Model Service Profile 中指定别名和数量；Controller
  将固化的别名版本解析为 `ResourceClaimTemplate`；
- Kubernetes 通过 `ResourceClaim` 选择 Node 和具体设备，第一期支持单节点混装不同 GPU 型号；
- 使用 `Deployment`、`StatefulSet` 和 `Job` 管理在线服务与离线任务；
- 使用 startup/readiness/liveness probe 表达实例健康；
- 使用 `InferencePool` 声明基础模型、Engine 配置和加速器类型一致的 Ready Engine endpoints；
- 使用 Gateway API Inference Extension 的 Endpoint Picker Protocol 连接 Gateway 与 Request Scheduler；
- 使用 `Service` 为 Gateway、Request Scheduler、Controller 等内部组件提供稳定地址；
- 使用 Control Plane `Deployment` 提供 Management API、节点准入与审核、模型服务 reconcile
  和状态发布；
- 使用 Helm/manifest 和版本化 Model Service Profile 交付模型服务；
- 对外统一使用 AlayaJet Inference Engine，具体 Runtime 由内部 Profile 选择；
- Engine 输出标准化负载状态与 KV Cache 事件，Request Scheduler 执行 `Filter -> Score -> Pick`；
- 第一期使用固定的 Engine 实例数，后续再根据运行指标引入 HPA/KEDA；
- 使用 Kubernetes Job 执行 benchmark 和质量验证；
- 使用持久化用量记录、幂等事件和周期对账交付计量事实；
- 跨模型选择采用上层显式提供的候选集合与路由策略。

## 4. 里程碑

### M0：冻结架构与契约

交付：

- 项目目标、平台组成和职责；
- 新节点准入状态、资源审核字段、隔离方式和审计契约；
- 首批模型、硬件范围、workload 和 SLO；
- 推理、模型服务管理和用量事件的最小契约；
- Gateway、Request Scheduler 与 Engine 的内部调度契约；
- Model Service Profile 字段定义。

验收门槛：节点、工作负载、服务实例、模型配置、测试结果和用量分别具有明确的权威来源。

### M1：Kubernetes 模型服务基线

交付：

- 统一的 `alayajet-maas` namespace、label、ServiceAccount、NetworkPolicy、resource quota 和 Pod 拓扑约定；
- NVIDIA GPU Operator，以及 Node Feature Discovery、GPU Feature Discovery、Container Toolkit/CDI
  和 DCGM exporter；GPU 分配由 NVIDIA DRA Driver 统一负责；
- 新节点 `pending-review -> active/disabled` 准入流程、初始 taint、Engine active label
  约束和运维审核界面；
- H200/B300 原始设备属性、调度别名、逐 GPU `ResourceSlice`、`ResourceClaim`、设备健康和 CDI
  分配链路；
- Control Plane `Deployment + Service`、多个 Control Plane Pods 和 leader election；
- 首个 AlayaJet Inference Engine 服务的版本化部署规格；
- 模型准备 init container、Inference Engine container、资源请求和 volume；
- startup/readiness/liveness probe；
- `InferencePool` 与路由配置；
- Gateway、Request Scheduler（EPP 实现）与 Usage Metering `Deployment`；
- benchmark `Job`；
- rollout、drain 和 rollback 操作记录。

验收门槛：各类 Pod 的生命周期、资源边界、扩缩容方式和持久化数据归属明确；新节点审核前不能承载
Engine Pod，GPU 类型、数量和逐卡 UUID 与批准清单一致后才能进入可部署资源视图；Kubernetes 可以检测
节点与 Pod 状态，在同质或混装节点上按 Profile 指定的 GPU 型号别名和数量分配具体设备，并恢复期望
Engine 实例数。

### M2：标准推理与用量闭环

交付：

- 流式与非流式推理；
- 统一 `request_id` 和 trace；
- SchedulingContext、调度决策日志和 primary/fallback endpoint；
- Engine 队列、活动 Token、KV Cache 状态与事件；
- Prefix-aware 与 load-aware 路由；
- 请求优先级字段、调度队列和 Engine 本地队列的传递边界；
- 幂等用量记录与重放；
- 客户端中断和内部 retry 的计量测试。

验收门槛：相同前缀在负载允许时路由到已有 KV Cache 的 Engine；过载实例会被避开；一次逻辑请求只有
一条最终权威用量记录，且能解释 generated/delivered Token 差异。

### M3：性能与容量评测

交付：

- 代表性 workload 和 SLO；
- 模型/internal-runtime/hardware/config 实验矩阵；
- Profile 对比、容量测试、优化收益和成本报告；
- Service 基础分发、load-aware 和 prefix-aware 路由对照；
- 可复现的原始 run artifact。

验收门槛：所有性能与容量结论均可追溯到可复现的 benchmark run。

### M4：联调与故障演练

交付：

- 上层服务调用和用量事件联调；
- 节点、Pod、Inference Engine、Gateway、网络和容量故障演练；
- dashboard、告警路由、值班负责人和复盘模板；
- canary 与回滚记录。

验收门槛：模型服务可以独立部署、升级、故障恢复、计量和对账，并在目标负载下稳定运行。

## 5. 需要尽快确认的输入

1. 首批两到三个模型及精确 revision；
2. 可用机器、GPU 型号、网络和存储条件；
3. 首期采用的 Kubernetes 集群和环境划分；
4. 每个模型的目标 workload、输入/输出长度和 SLO；
5. 第一优先的内部 Runtime 实现：vLLM、SGLang 或二者并行；
6. 标准推理接口首期覆盖范围；
7. 上层能够提供的可信请求优先级语义；
8. 用量事件接收方式和对账周期；
9. 联调、canary 和正式服务的负责人及时间点。
