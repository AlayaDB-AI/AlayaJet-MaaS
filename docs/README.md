# AlayaJet-MaaS 文档总览

本文是 `docs/` 的统一入口，用于说明各类文档的职责、推荐阅读顺序以及常见任务应查阅的位置。

## 1. 文档分层

| 类型 | 目录 | 说明 | 主要读者 |
|---|---|---|---|
| 系统架构 | `architecture/` | 描述 AlayaJet-MaaS 的目标架构、组件边界、状态来源和核心流程 | 架构、研发、平台负责人 |
| 服务契约 | `contracts/` | 固定推理、模型服务管理和用量计量的接口语义 | API、控制面、Gateway 研发 |
| 当前部署 | `deployment/` | 描述当前 s04、s05、s07 上 OME + SGLang-native 的可执行部署 | 集群部署与运维人员 |
| 操作手册 | `operations/` | 描述节点、模型、状态、请求、日志、备份和恢复的日常操作 | 集群与模型运维人员 |
| 评测方法 | `evaluation/` | 定义质量、性能、容量、稳定性和成本的统一评价方法 | 性能与容量评测人员 |
| 实施计划 | `planning/` | 描述阶段目标、交付物、里程碑和验收门槛 | 项目负责人和研发团队 |
| 调研依据 | `research/` | 保存产品、开源组件和资源管理方案的公开实现依据 | 架构与技术选型人员 |

其中，`architecture/` 表达平台目标设计，`deployment/` 和 `operations/` 表达当前集群的实际实现与操作入口。当前节点、地址、镜像、Pod 和服务状态以运行环境检查结果为准。

## 2. 完整阅读顺序

系统梳理整个项目时，建议按以下顺序阅读：

1. [项目 README](../README.md)：先了解 OME + SGLang 逻辑架构总图、平台目标和四条核心关系；
2. [系统架构](architecture/overview.md)：建立 Control Plane、Gateway、Request Scheduler、Engine、Kubernetes 和 Metering 的整体边界；
3. [资源发现与调度架构](architecture/resource_discovery_and_scheduling.md)：理解机器、Node、GPU、设备身份和调度申请；
4. [模型服务部署架构](architecture/model_service_deployment.md)：理解模型资产、Profile、发布请求、Controller 和 Kubernetes 工作负载；
5. [推理请求调度架构](architecture/inference_request_scheduling.md)：理解 Gateway、InferencePool、Engine endpoint、队列和 KV/Prefix-aware 选点；
6. [用量计量架构](architecture/usage_metering.md)：理解请求事实、Token 用量、重试、流式输出和 Ledger；
7. [服务契约](contracts/service_contract.md)：确认上述组件之间稳定的 API、字段和状态语义；
8. [OME + SGLang-native 集群部署](deployment/sglang_native.md)：将架构映射到当前 s04、s05、s07 集群，理解启动、注册、发现、放置和部署；
9. [集群与模型状态检查](operations/cluster_and_model_status.md)：从实际 Kubernetes 对象观察节点、OME、Engine、Router、备份和恢复；
10. [SGLang-native 集群、模型与请求管理](operations/sglang_native_model_service.md)：执行节点管理、模型切换、参数调整、扩缩容、请求和排障；
11. [模型服务评测框架](evaluation/framework.md)：建立发布后的质量、性能、容量和成本验收方法；
12. [第一期计划](planning/phase1.md)：查看近期实施范围、里程碑和交付要求；
13. [MaaS 产品与实现调研](research/maas_implementation_landscape.md)：追溯技术选型和行业实现依据。

这条顺序对应：

```text
平台目标
  → 系统边界
  → 资源进入系统
  → 模型变成服务
  → 请求选择 Engine
  → 用量形成事实
  → 接口语义固定
  → 当前集群落地
  → 日常操作与恢复
  → 评测验收
  → 实施计划与选型依据
```

## 3. 按任务选择阅读路径

### 3.1 接管当前 OME + SGLang 集群

按以下顺序阅读并操作：

1. [当前集群部署与系统边界](deployment/sglang_native.md)；
2. [集群与模型状态检查](operations/cluster_and_model_status.md)；
3. [集群、模型与请求管理](operations/sglang_native_model_service.md)。

这条路径覆盖管理机与 s04/s05/s07 的职责、节点命名、K3s 注册、Service discovery、GPU 放置、控制面
备份恢复和推理接口验证。

### 3.2 新增、移除或更换 GPU 节点

1. 阅读[资源发现与调度架构](architecture/resource_discovery_and_scheduling.md)，理解节点准入和设备身份；
2. 阅读[部署文档的集群配置与 Worker 生命周期](deployment/sglang_native.md)；
3. 使用[操作手册的动态节点管理](operations/sglang_native_model_service.md)执行；
4. 使用[状态检查手册](operations/cluster_and_model_status.md)验证 Node、Lease、GPU 和 Pod 放置。

### 3.3 发布、暂停、恢复、切换或调整模型

1. 阅读[模型服务部署架构](architecture/model_service_deployment.md)，理解模型、Runtime、服务、副本、暂停与请求取消的关系；
2. 阅读[部署文档的模型服务配置](deployment/sglang_native.md)；
3. 使用[操作手册的模型管理](operations/sglang_native_model_service.md)切换模型、调整 Runtime 和控制副本位置；
4. 使用[状态检查手册](operations/cluster_and_model_status.md)检查 `ClusterBaseModel`、`ClusterServingRuntime`、`InferenceService`、Engine 和 Router；
5. 使用[评测框架](evaluation/framework.md)完成发布验收。

### 3.4 排查请求为什么被转发到某个 Engine

1. 阅读[推理请求调度架构](architecture/inference_request_scheduling.md)；
2. 阅读[部署文档的节点注册与服务发现](deployment/sglang_native.md)；
3. 使用[状态检查手册](operations/cluster_and_model_status.md)查看 Router 参数、Service、EndpointSlice、Pod 放置和日志。

### 3.5 设计平台 API、租户策略或计费

1. [系统架构](architecture/overview.md)；
2. [推理请求调度架构](architecture/inference_request_scheduling.md)；
3. [用量计量架构](architecture/usage_metering.md)；
4. [服务契约](contracts/service_contract.md)；
5. [模型服务评测框架](evaluation/framework.md)。

## 4. 各文档解决的问题

| 文档 | 核心问题 |
|---|---|
| [系统架构](architecture/overview.md) | 系统由什么组成，各组件边界和权威状态是什么？ |
| [资源发现与调度架构](architecture/resource_discovery_and_scheduling.md) | 一台机器和一张 GPU 如何进入可调度资源视图？ |
| [模型服务部署架构](architecture/model_service_deployment.md) | 一个模型如何转换为可更新、可扩缩、可恢复的服务？ |
| [推理请求调度架构](architecture/inference_request_scheduling.md) | 一个请求如何选择具体 Engine，如何处理负载、队列和 KV 状态？ |
| [用量计量架构](architecture/usage_metering.md) | 一次请求如何形成可信、可重放的 Token 用量事实？ |
| [服务契约](contracts/service_contract.md) | 对外 API 和内部管理状态采用什么稳定语义？ |
| [OME + SGLang-native 集群部署](deployment/sglang_native.md) | 当前集群如何安装、启动、注册节点、部署模型并验收？ |
| [集群与模型状态检查](operations/cluster_and_model_status.md) | 在哪台机器输入什么命令查看状态、定位故障和恢复控制面？ |
| [SGLang-native 集群、模型与请求管理](operations/sglang_native_model_service.md) | 如何管理节点、切换模型、修改配置、扩缩容和发送请求？ |
| [模型服务评测框架](evaluation/framework.md) | 如何统一判断一个模型服务是否可交付？ |
| [第一期计划](planning/phase1.md) | 当前阶段先交付什么，如何验收？ |
| [MaaS 产品与实现调研](research/maas_implementation_landscape.md) | 当前架构选择有哪些公开实现和行业依据？ |

## 5. 文档维护规则

新增或修改内容时，按以下位置维护唯一事实来源：

| 内容 | 维护位置 |
|---|---|
| 系统组件、职责和总体数据流 | `architecture/overview.md` |
| 节点、GPU、资源发现和放置 | `architecture/resource_discovery_and_scheduling.md` |
| 模型、Profile、Controller 和工作负载生命周期 | `architecture/model_service_deployment.md` |
| Gateway、Router、Engine 和请求调度 | `architecture/inference_request_scheduling.md` |
| Token 用量、重试和 Ledger | `architecture/usage_metering.md` |
| API 字段和状态语义 | `contracts/service_contract.md` |
| 当前机器、版本、地址、Manifest 和部署步骤 | `deployment/sglang_native.md` |
| 可直接执行的日常命令、故障恢复和操作步骤 | `operations/` |
| 指标、实验矩阵和验收报告 | `evaluation/framework.md` |
| 里程碑和阶段范围 | `planning/phase1.md` |
| 外部方案、公开案例和选型证据 | `research/maas_implementation_landscape.md` |

其他文档通过链接引用该事实来源，避免复制后形成多份不一致描述。涉及当前集群状态的说明同时给出检查
命令；涉及配置变更的说明同时给出声明入口、应用步骤、验证步骤和恢复边界。
