# 集群与模型状态检查

本文用于日常查看 K3s、OME、GPU Worker、模型资产、SGLang Engine、Model Gateway 和推理接口状态。
集群的 Kubernetes Node 名为 `s04`、`s05`、`s07`；对应宿主机 hostname 为 `gpu04`、`gpu05`、
`gpu07`。
名称来源和变更规则见[节点命名与身份管理](../deployment/sglang_native.md#421-节点命名与身份管理)。

## 1. 在哪台机器执行

| 执行位置 | 适合查看的状态 | 命令入口 |
|---|---|---|
| 管理机 | 整个集群、OME、模型服务、Pod、调度、日志和推理接口 | `kubectl` 与仓库脚本 |
| s04 | K3s Server 和整个集群的备用管理入口 | `sudo k3s kubectl` |
| s05、s07 | 本机 K3s Agent、GPU、模型文件和宿主机资源 | `systemctl`、`nvidia-smi`、文件系统命令 |

日常检查优先在管理机执行。管理机保存仓库和远程 kubeconfig，不需要登录 GPU Worker 即可查看集群状态。
各机器的安装发起方、常驻系统服务和 Pod 启动链见
[启动与持续控制职责](../deployment/sglang_native.md#启动与持续控制职责)。
Node 注册、Lease 心跳、Service、EndpointSlice 以及 Router 发现 Engine 的完整链路见
[节点注册与服务发现](../deployment/sglang_native.md#节点注册与服务发现)。

## 2. 管理机快速检查

在管理机的仓库根目录执行：

```bash
cd /Users/haotianliu/workspace/alayadb/AlayaJet-MaaS

export KUBECONFIG="$HOME/.kube/alayajet-sglang-native.yaml"
export OME_MODEL_NS=qwen2-5-0-5b-instruct
export OME_MODEL_SERVICE=qwen2-5-0-5b-instruct
export OME_BASE_MODEL=qwen2-5-0-5b-instruct
export OME_RUNTIME=sglang-qwen2-5-0-5b-instruct
```

依次检查控制面、节点、OME 和模型服务：

```bash
kubectl cluster-info
kubectl get nodes -o wide
kubectl -n ome get deployment,pod -o wide
kubectl get clusterbasemodel,clusterservingruntime
kubectl get inferenceservice -A
kubectl -n "$OME_MODEL_NS" get deployment,pod,service -o wide
```

健康状态应满足：

1. s04、s05、s07 都是 `Ready`；
2. OME Controller 和 Model Agent Pod 都是 `Running`；
3. `InferenceService` 的 `READY` 为 `True`；
4. Router 和 Engine Pod 都通过 readiness；
5. Engine Pod 分布在预期的 GPU Worker 上。

仓库也提供两条汇总命令：

```bash
./deploy/sglang-native/scripts/node_manager.sh status
./deploy/sglang-native/scripts/verify.sh
```

`node_manager.sh status` 查看节点配置期望态和 Kubernetes 实际状态；`verify.sh` 还会发送模型发现、非流式
和流式推理请求。

## 3. 查看集群和 GPU 资源

查看节点状态、地址和角色：

```bash
kubectl get nodes -o wide
kubectl get nodes \
  -L alayajet.io/role,alayajet.io/accelerator-pool
```

查看每个节点上报的 GPU 容量：

```bash
kubectl get nodes \
  -o custom-columns='NODE:.metadata.name,ROLE:.metadata.labels.alayajet\.io/role,GPU_CAPACITY:.status.capacity.nvidia\.com/gpu,GPU_ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'
```

查看全部 namespace 的 Pod 及其所在节点：

```bash
kubectl get pods -A -o wide
```

查看某个节点的资源、污点、标签和状态事件：

```bash
kubectl describe node s05
kubectl describe node s07
```

查看指定 Accelerator Pool 的节点及 GPU 数量：

```bash
export ACCELERATOR_POOL=h200-sxm-8gpu

kubectl get nodes \
  -l alayajet.io/accelerator-pool="$ACCELERATOR_POOL" \
  -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.alayajet\.io/accelerator-pool,GPU:.status.allocatable.nvidia\.com/gpu'
```

## 4. 查看 OME 状态

查看 OME Controller 和各节点的 Model Agent：

```bash
kubectl -n ome get deployment,daemonset,pod -o wide
```

查看 OME Controller 日志：

```bash
kubectl -n ome logs deployment/ome-controller-manager \
  --tail=200
```

持续观察 Controller 日志：

```bash
kubectl -n ome logs deployment/ome-controller-manager \
  --follow
```

## 5. 查看模型资产和 Runtime

列出模型资产与 Runtime：

```bash
kubectl get clusterbasemodel
kubectl get clusterservingruntime
```

查看当前模型资产的完整 Spec 和 Status：

```bash
kubectl get clusterbasemodel "$OME_BASE_MODEL" -o yaml
```

查看当前 SGLang Runtime，包括镜像、参数、GPU 请求和放置约束：

```bash
kubectl get clusterservingruntime "$OME_RUNTIME" -o yaml
```

查看 GPU Worker 上的节点标签，包括 Model Agent 写入的模型状态标签：

```bash
kubectl get node s05 --show-labels
kubectl get node s07 --show-labels
```

## 6. 查看 InferenceService 状态

列出全部模型服务：

```bash
kubectl get inferenceservice -A
```

查看当前模型服务的完整声明与状态：

```bash
kubectl -n "$OME_MODEL_NS" get \
  inferenceservice "$OME_MODEL_SERVICE" \
  -o yaml
```

查看 Ready 条件：

```bash
kubectl -n "$OME_MODEL_NS" get \
  inferenceservice "$OME_MODEL_SERVICE" \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"  reason="}{.reason}{"\n"}{end}'
```

查看 Controller 写入的事件和状态说明：

```bash
kubectl -n "$OME_MODEL_NS" describe \
  inferenceservice "$OME_MODEL_SERVICE"
```

## 7. 查看 Engine、Router 和节点放置

查看模型服务生成的全部 Kubernetes 工作负载：

```bash
kubectl -n "$OME_MODEL_NS" get \
  deployment,pod,service,hpa,pdb \
  -l ome.io/inferenceservice="$OME_MODEL_SERVICE" \
  -o wide
```

查看 Engine 的运行节点和 readiness：

```bash
kubectl -n "$OME_MODEL_NS" get pods \
  -l component=engine \
  -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,READY:.status.containerStatuses[0].ready,GPU:.spec.containers[0].resources.requests.nvidia\.com/gpu'
```

查看 Router 的运行节点和 readiness：

```bash
kubectl -n "$OME_MODEL_NS" get pods \
  -l component=router \
  -o wide
```

查看 OME 写入 Engine Deployment 的最终调度约束：

```bash
kubectl -n "$OME_MODEL_NS" get deployment \
  -l component=engine \
  -o jsonpath='{range .items[*]}{"Deployment: "}{.metadata.name}{"\nnodeSelector: "}{.spec.template.spec.nodeSelector}{"\naffinity: "}{.spec.template.spec.affinity}{"\ntopologySpread: "}{.spec.template.spec.topologySpreadConstraints}{"\n\n"}{end}'
```

## 8. 查看事件和日志

按时间查看模型 namespace 的最近事件：

```bash
kubectl -n "$OME_MODEL_NS" get events \
  --sort-by=.lastTimestamp |
  tail -n 30
```

查看 Engine 调度、镜像、启动和探针事件：

```bash
kubectl -n "$OME_MODEL_NS" describe pod \
  -l component=engine
```

查看全部 Engine 日志：

```bash
kubectl -n "$OME_MODEL_NS" logs \
  -l component=engine \
  --prefix \
  --tail=200
```

查看 Router 日志：

```bash
kubectl -n "$OME_MODEL_NS" logs \
  deployment/qwen2-5-0-5b-instruct-router \
  --tail=200
```

## 9. 验证模型接口

查看 Gateway 暴露的模型：

```bash
curl -sS http://100.64.0.14:30080/v1/models
```

发送非流式推理请求：

```bash
curl -sS http://100.64.0.14:30080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [
      {"role": "user", "content": "用一句话介绍深圳。"}
    ],
    "max_tokens": 64,
    "temperature": 0
  }'
```

发送流式推理请求：

```bash
curl -N http://100.64.0.14:30080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [
      {"role": "user", "content": "列出三个深圳地标。"}
    ],
    "max_tokens": 96,
    "temperature": 0,
    "stream": true
  }'
```

## 10. 在 s04 查看控制面

从管理机登录 s04：

```bash
ssh yujun@10.16.71.35
```

检查 K3s Server：

```bash
sudo systemctl status k3s --no-pager
sudo journalctl -u k3s -n 200 --no-pager
```

通过 s04 本地的 K3s 管理入口查看集群：

```bash
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get pods -A -o wide
sudo k3s kubectl get inferenceservice -A
```

## 11. 控制面备份与故障恢复

当前 s04 是唯一 K3s Server，使用 K3s 默认 SQLite 数据存储。控制面恢复依赖三类持久状态：

| 恢复材料 | 位置 | 作用 |
|---|---|---|
| 集群数据库 | s04 `/var/lib/rancher/k3s/server/db/` | 保存 Kubernetes、OME、Deployment、Service 和 Secret 等对象状态 |
| Server Token | s04 `/var/lib/rancher/k3s/server/token` | 解密数据库中的 K3s bootstrap 数据，并供 Worker 加入集群 |
| 声明式配置 | 管理机仓库中的 `nodes.json`、平台清单和模型清单 | 固定节点身份、K3s 版本和平台期望状态 |

数据库与 Server Token 必须属于同一次集群备份。备份文件包含集群凭据，权限设为 `0600`，并保存到 s04
之外的加密存储。

### 11.1 创建控制面备份

在管理机执行。该过程会短暂停止 K3s Server，以获得一致的 SQLite 备份；退出时会自动重新启动 K3s：

```bash
(
  set -euo pipefail

  SERVER_SSH=yujun@10.16.71.35
  BACKUP_ROOT="$HOME/secure-backups/alayajet-k3s"
  BACKUP_ID=$(date +%Y%m%dT%H%M%S)
  BACKUP_FILE="$BACKUP_ROOT/$BACKUP_ID.tar.gz"

  mkdir -p "$BACKUP_ROOT"
  start_k3s() {
    ssh -n "$SERVER_SSH" 'sudo -n systemctl start k3s'
  }
  trap start_k3s EXIT

  ssh -n "$SERVER_SSH" 'sudo -n systemctl stop k3s'
  ssh -n "$SERVER_SSH" \
    'sudo -n tar -C / -czf - \
      var/lib/rancher/k3s/server/db \
      var/lib/rancher/k3s/server/token \
      etc/rancher/k3s/k3s.yaml \
      etc/rancher/k3s/registries.yaml' >"$BACKUP_FILE"

  start_k3s
  trap - EXIT
  chmod 600 "$BACKUP_FILE"
  shasum -a 256 "$BACKUP_FILE" >"$BACKUP_FILE.sha256"
  tar -tzf "$BACKUP_FILE"
  echo "控制面备份已生成: $BACKUP_FILE"
)
```

等待 API 恢复并确认备份后的集群状态：

```bash
export KUBECONFIG="$HOME/.kube/alayajet-sglang-native.yaml"

until kubectl --request-timeout=5s get --raw=/readyz; do
  sleep 5
done
kubectl get nodes -o wide
```

在平台配置变更后、K3s 升级前和日常备份周期内执行该备份，并保留对应版本的仓库配置。

### 11.2 K3s 或 OME 进程故障

先从管理机判断是 API Server 故障还是 OME Controller 故障：

```bash
export KUBECONFIG="$HOME/.kube/alayajet-sglang-native.yaml"

kubectl --request-timeout=5s get --raw=/readyz
ssh yujun@10.16.71.35 \
  'sudo -n systemctl status k3s --no-pager; \
   sudo -n journalctl -u k3s -n 200 --no-pager'
```

K3s 服务退出且 s04 系统与数据库仍正常时，在 s04 重启服务：

```bash
ssh yujun@10.16.71.35 'sudo -n systemctl restart k3s'

until kubectl --request-timeout=5s get --raw=/readyz; do
  sleep 5
done
kubectl get nodes -o wide
kubectl get pods -A -o wide
```

OME Controller Pod 故障时，Kubernetes Deployment 会创建新 Pod。检查其收敛状态和日志：

```bash
kubectl -n ome rollout status deployment/ome-controller-manager \
  --timeout=10m
kubectl -n ome get deployment,pod -o wide
kubectl -n ome logs deployment/ome-controller-manager \
  --tail=200
kubectl get inferenceservice -A
```

### 11.3 从备份恢复 SQLite 控制面

该流程用于 s04 数据损坏或更换系统盘。恢复机器使用 `nodes.json` 中 s04 的稳定身份：Kubernetes Node
名 `s04`、系统 hostname `gpu04`、Node IP `12.12.12.14`、集群网卡 `ibs18`，并安装固定版本
`v1.32.9+k3s1`。

在管理机指定已经校验的备份并传到 s04：

```bash
export SERVER_SSH=yujun@10.16.71.35
export BACKUP_FILE="$HOME/secure-backups/alayajet-k3s/<backup-id>.tar.gz"

shasum -a 256 -c "$BACKUP_FILE.sha256"
tar -tzf "$BACKUP_FILE"
scp "$BACKUP_FILE" "$SERVER_SSH:/var/tmp/alayajet-k3s-restore.tar.gz"
```

如果 s04 是重新安装的系统，先安装固定版本 K3s，但保持服务停止：

```bash
./deploy/sglang-native/scripts/prepare_environment.sh s04
scp deploy/sglang-native/cluster/registries.yaml \
  "$SERVER_SSH:/var/tmp/alayajet-registries.yaml"

ssh "$SERVER_SSH" 'bash -s' <<'REMOTE'
set -euo pipefail

sudo -n install -D -m 0600 \
  /var/tmp/alayajet-registries.yaml \
  /etc/rancher/k3s/registries.yaml

curl -sfL https://get.k3s.io |
  sudo -n env \
    INSTALL_K3S_VERSION='v1.32.9+k3s1' \
    INSTALL_K3S_SKIP_START=true \
    sh -s - server \
      --node-name s04 \
      --node-ip 12.12.12.14 \
      --advertise-address 12.12.12.14 \
      --flannel-iface ibs18 \
      --disable traefik \
      --disable servicelb \
      --write-kubeconfig-mode 600 \
      --tls-san 12.12.12.14 \
      --tls-san 100.64.0.14 \
      --node-label alayajet.io/role=control
REMOTE
```

停止 K3s，保留故障现场，并显式恢复数据库和匹配的 Server Token：

```bash
ssh "$SERVER_SSH" 'bash -s' <<'REMOTE'
set -euo pipefail

ARCHIVE=/var/tmp/alayajet-k3s-restore.tar.gz
RECOVERY_ID=$(date +%Y%m%dT%H%M%S)

sudo -n systemctl stop k3s 2>/dev/null || true
sudo -n tar -tzf "$ARCHIVE"

if sudo -n test -d /var/lib/rancher/k3s/server/db; then
  sudo -n mv /var/lib/rancher/k3s/server/db \
    "/var/lib/rancher/k3s/server/db.failed.$RECOVERY_ID"
fi
if sudo -n test -f /var/lib/rancher/k3s/server/token; then
  sudo -n cp -a /var/lib/rancher/k3s/server/token \
    "/var/lib/rancher/k3s/server/token.failed.$RECOVERY_ID"
fi

sudo -n tar -xzf "$ARCHIVE" -C / \
  var/lib/rancher/k3s/server/db \
  var/lib/rancher/k3s/server/token \
  etc/rancher/k3s/registries.yaml
sudo -n chmod 600 /var/lib/rancher/k3s/server/token
sudo -n systemctl enable --now k3s
REMOTE
```

从恢复后的 s04 重新生成管理机 kubeconfig：

```bash
mkdir -p "$HOME/.kube"
ssh -n "$SERVER_SSH" \
  'sudo -n cat /etc/rancher/k3s/k3s.yaml' \
  >"$HOME/.kube/alayajet-sglang-native.yaml"
chmod 600 "$HOME/.kube/alayajet-sglang-native.yaml"
sed -i.bak \
  's#https://127.0.0.1:6443#https://100.64.0.14:6443#' \
  "$HOME/.kube/alayajet-sglang-native.yaml"
rm -f "$HOME/.kube/alayajet-sglang-native.yaml.bak"
```

最后让 Worker、平台组件和模型服务按仓库期望状态收敛，并执行完整验收：

```bash
export KUBECONFIG="$HOME/.kube/alayajet-sglang-native.yaml"

kubectl wait --for=condition=Ready node/s04 --timeout=5m
./deploy/sglang-native/scripts/node_manager.sh reconcile
./deploy/sglang-native/scripts/install_platform.sh
./deploy/sglang-native/scripts/deploy_model.sh
./deploy/sglang-native/scripts/verify.sh
```

恢复成功的判定标准：

1. s04、s05、s07 均为 `Ready`，节点名、内部地址和角色标签与 `nodes.json` 一致；
2. OME Controller 与 Model Agent 就绪；
3. `InferenceService READY=True`，Engine 和 Router Pod 就绪；
4. 模型发现、非流式和流式推理验证全部通过。

K3s 官方恢复要求 SQLite 数据目录与 Server Token 配套恢复，详见
[K3s Backup and Restore](https://docs.k3s.io/datastore/backup-restore)。

### 11.4 从仓库声明式配置重建控制面

仓库中的 `nodes.json`、平台清单和模型清单共同构成可重建的控制面期望状态。恢复前确认这些文件已经进入
版本控制，并与需要恢复的环境版本一致。

新 s04 系统可以直接执行完整部署流程：

```bash
./deploy/sglang-native/scripts/bootstrap_cluster.sh
./deploy/sglang-native/scripts/install_platform.sh
./deploy/sglang-native/scripts/deploy_model.sh
./deploy/sglang-native/scripts/verify.sh
```

原 s04 的数据库无法继续使用时，先停止 K3s 并将原 Server 数据目录改名保留，再执行相同的完整部署流程：

```bash
ssh yujun@10.16.71.35 'bash -s' <<'REMOTE'
set -euo pipefail

RECOVERY_ID=$(date +%Y%m%dT%H%M%S)
sudo -n systemctl stop k3s 2>/dev/null || true
if sudo -n test -d /var/lib/rancher/k3s/server; then
  sudo -n mv /var/lib/rancher/k3s/server \
    "/var/lib/rancher/k3s/server.failed.$RECOVERY_ID"
fi
REMOTE

./deploy/sglang-native/scripts/bootstrap_cluster.sh
./deploy/sglang-native/scripts/install_platform.sh
./deploy/sglang-native/scripts/deploy_model.sh
./deploy/sglang-native/scripts/verify.sh
```

`bootstrap_cluster.sh` 使用 `nodes.json` 中固定的 s04 身份创建 K3s Server，生成新的管理机 kubeconfig，
并让启用的 Worker 使用新集群 Token 重新加入；后续脚本重新创建平台组件和模型服务对象。

## 12. 在 s05 和 s07 查看 GPU Worker

登录 s05：

```bash
ssh haotian@100.64.0.15
```

登录 s07：

```bash
ssh haotian@100.64.0.17
```

在对应 Worker 上检查 K3s Agent、GPU 和模型目录：

```bash
sudo systemctl status k3s-agent --no-pager
sudo journalctl -u k3s-agent -n 100 --no-pager
nvidia-smi
nvidia-smi topo -m
ls -lah /mnt/data/models/Qwen/Qwen2.5-0.5B-Instruct
```

## 13. 状态定位

| 现象 | 重点检查 |
|---|---|
| `kubectl cluster-info` 连接失败 | s04 的 `k3s.service`、API 地址和 `6443` 端口 |
| s04 数据库损坏或系统盘更换 | SQLite 备份、匹配的 Server Token、固定 K3s 版本和 s04 节点身份 |
| OME Controller 不可用 | Deployment rollout、Controller Pod 日志、OME CRD 和 API Server 状态 |
| Worker 为 `NotReady` | 对应 Worker 的 `k3s-agent.service`、节点网络和系统日志 |
| 节点没有 `nvidia.com/gpu` | NVIDIA Driver、NVIDIA Device Plugin 和节点污点 |
| `ClusterBaseModel` 未就绪 | 模型目录、文件权限、Model Agent Pod 和节点模型标签 |
| `InferenceService` 未就绪 | InferenceService Conditions、namespace Events 和 OME Controller 日志 |
| Engine Pod 为 `Pending` | GPU 数量、cordon、nodeSelector、affinity 和 topology spread |
| Engine Pod 反复重启 | Engine 日志、启动参数、模型路径、显存和健康探针 |
| API 请求失败 | NodePort Service、Router 日志、Ready Engine endpoints 和 Engine 日志 |
