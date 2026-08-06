#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
REGISTRY_CONFIG="$ROOT_DIR/deploy/sglang-native/cluster/registries.yaml"
export KUBECONFIG=$KUBECONFIG_PATH

server_name=$(cluster_field server)
read_node "$server_name"
SERVER_SSH=$NODE_SSH
SERVER_IP=$NODE_IP
K3S_VERSION=$(cluster_field k3sVersion)

install_registry_config() {
  local target=$1
  scp -q "$REGISTRY_CONFIG" "$target:/tmp/alayajet-registries.yaml"
  ssh -n "$target" \
    'sudo -n install -D -m 0600 /tmp/alayajet-registries.yaml /etc/rancher/k3s/registries.yaml && rm -f /tmp/alayajet-registries.yaml'
}

wait_for_gpu_resource() {
  local node_name=$1
  local attempt allocatable

  [ "$NODE_GPU" = true ] || return 0
  kubectl -n kube-system get daemonset nvidia-device-plugin >/dev/null 2>&1 || return 0

  for attempt in $(seq 1 15); do
    allocatable=$(kubectl get node "$node_name" \
      -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
    [ "${allocatable:-0}" -ge 1 ] && return
    sleep 2
  done

  kubectl -n kube-system delete pod \
    -l app.kubernetes.io/name=nvidia-device-plugin \
    --field-selector "spec.nodeName=$node_name" \
    --ignore-not-found

  for attempt in $(seq 1 60); do
    allocatable=$(kubectl get node "$node_name" \
      -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
    if [ "${allocatable:-0}" -ge 1 ]; then
      echo "$node_name: GPU 资源已恢复，allocatable=$allocatable"
      return
    fi
    sleep 2
  done

  echo "$node_name: NVIDIA Device Plugin 未上报可调度 GPU" >&2
  exit 1
}

add_worker() {
  local node_name=$1
  local token label

  inventory set-enabled "$node_name" true
  read_node "$node_name"
  [ "$NODE_ROLE" = worker ] || {
    echo "只能动态添加 worker 节点: $node_name" >&2
    exit 1
  }

  "$SCRIPT_DIR/prepare_environment.sh" "$node_name"
  install_registry_config "$NODE_SSH"
  token=$(ssh -n "$SERVER_SSH" 'sudo -n cat /var/lib/rancher/k3s/server/node-token')

  ssh "$NODE_SSH" bash -s -- \
    "$K3S_VERSION" "$SERVER_IP" "$token" "$NODE_NAME" "$NODE_IP" \
    "$NODE_INTERFACE" "$NODE_LABELS" <<'REMOTE'
set -euo pipefail

k3s_version=$1
server_ip=$2
token=$3
node_name=$4
node_ip=$5
network_interface=$6
labels_csv=$7

sudo -n systemctl disable --now kubelet >/dev/null 2>&1 || true
arguments=(
  agent
  --node-name "$node_name"
  --node-ip "$node_ip"
  --flannel-iface "$network_interface"
)
IFS=',' read -r -a labels <<<"$labels_csv"
for label in "${labels[@]}"; do
  [ -n "$label" ] && arguments+=(--node-label "$label")
done

curl -sfL https://get.k3s.io |
  sudo -n env \
    INSTALL_K3S_VERSION="$k3s_version" \
    K3S_URL="https://$server_ip:6443" \
    K3S_TOKEN="$token" \
    sh -s - "${arguments[@]}"
sudo -n systemctl enable --now k3s-agent
REMOTE

  kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=5m
  IFS=',' read -r -a labels <<<"$NODE_LABELS"
  for label in "${labels[@]}"; do
    if [ -n "$label" ]; then
      label_output=$(kubectl label node "$NODE_NAME" "$label" --overwrite 2>&1) || {
        case "$label_output" in
          *"not labeled"*) ;;
          *)
            echo "$label_output" >&2
            exit 1
            ;;
        esac
      }
    fi
  done

  wait_for_gpu_resource "$NODE_NAME"

  if [ "$NODE_MODEL_MODE" != none ]; then
    "$SCRIPT_DIR/stage_model.sh" "$NODE_NAME"
  fi

  kubectl get node "$NODE_NAME" -o wide
}

remove_worker() {
  local node_name=$1

  read_node "$node_name"
  [ "$NODE_ROLE" = worker ] || {
    echo "只能动态移除 worker 节点: $node_name" >&2
    exit 1
  }
  inventory set-enabled "$node_name" false

  if kubectl get node "$NODE_NAME" >/dev/null 2>&1; then
    kubectl cordon "$NODE_NAME"
    kubectl drain "$NODE_NAME" \
      --ignore-daemonsets \
      --delete-emptydir-data \
      --force \
      --timeout=10m
    kubectl delete node "$NODE_NAME"
  fi

  if ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$NODE_SSH" true 2>/dev/null; then
    ssh -n "$NODE_SSH" 'sudo -n systemctl disable --now k3s-agent >/dev/null 2>&1 || true'
  fi
  echo "$NODE_NAME: 已从集群移除，模型文件和容器镜像保留"
}

reconcile_workers() {
  local node_name

  while IFS=$'\t' read -r node_name _; do
    read_node "$node_name"
    if [ "$NODE_ENABLED" = true ]; then
      if kubectl get node "$NODE_NAME" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null |
        grep -qx True; then
        echo "$NODE_NAME: 已经 Ready"
      else
        add_worker "$NODE_NAME"
      fi
    elif kubectl get node "$NODE_NAME" >/dev/null 2>&1; then
      remove_worker "$NODE_NAME"
    fi
  done < <(inventory nodes --role worker)
}

show_status() {
  printf '%-10s %-8s %-9s %-30s %-15s\n' NAME ROLE ENABLED SSH NODE_IP
  while IFS=$'\t' read -r \
    node_name node_role node_enabled node_ssh _ node_ip _; do
    printf '%-10s %-8s %-9s %-30s %-15s\n' \
      "$node_name" "$node_role" "$node_enabled" "$node_ssh" "$node_ip"
  done < <(inventory nodes)
  printf '\n'
  kubectl get nodes -o wide
}

usage() {
  echo "用法: $0 add <worker> | remove <worker> | reconcile | status" >&2
  exit 1
}

inventory validate >/dev/null
case "${1:-}" in
  add)
    [ "$#" -eq 2 ] || usage
    add_worker "$2"
    ;;
  remove)
    [ "$#" -eq 2 ] || usage
    remove_worker "$2"
    ;;
  reconcile)
    [ "$#" -eq 1 ] || usage
    reconcile_workers
    ;;
  status)
    [ "$#" -eq 1 ] || usage
    show_status
    ;;
  *)
    usage
    ;;
esac
