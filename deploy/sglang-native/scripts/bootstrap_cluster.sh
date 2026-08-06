#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

"$SCRIPT_DIR/prepare_environment.sh"
"$SCRIPT_DIR/preflight.sh"

server_name=$(cluster_field server)
api_address=$(cluster_field apiAddress)
k3s_version=$(cluster_field k3sVersion)
KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
REGISTRY_CONFIG="$ROOT_DIR/deploy/sglang-native/cluster/registries.yaml"

read_node "$server_name"
[ "$NODE_ROLE" = server ] || {
  echo "cluster.server 必须指向 server 节点" >&2
  exit 1
}

server_ssh=$NODE_SSH
server_node_ip=$NODE_IP
server_interface=$NODE_INTERFACE
server_labels=$NODE_LABELS

scp -q "$REGISTRY_CONFIG" "$server_ssh:/tmp/alayajet-registries.yaml"
ssh -n "$server_ssh" \
  'sudo -n install -D -m 0600 /tmp/alayajet-registries.yaml /etc/rancher/k3s/registries.yaml && rm -f /tmp/alayajet-registries.yaml'

ssh "$server_ssh" bash -s -- \
  "$k3s_version" "$server_name" "$server_node_ip" "$server_interface" \
  "$api_address" "$server_labels" <<'REMOTE'
set -euo pipefail

k3s_version=$1
node_name=$2
node_ip=$3
network_interface=$4
api_address=$5
labels_csv=$6

arguments=(
  server
  --node-name "$node_name"
  --node-ip "$node_ip"
  --advertise-address "$node_ip"
  --flannel-iface "$network_interface"
  --disable traefik
  --disable servicelb
  --write-kubeconfig-mode 600
  --tls-san "$node_ip"
  --tls-san "$api_address"
)
IFS=',' read -r -a labels <<<"$labels_csv"
for label in "${labels[@]}"; do
  [ -n "$label" ] && arguments+=(--node-label "$label")
done

curl -sfL https://get.k3s.io |
  sudo -n env INSTALL_K3S_VERSION="$k3s_version" \
    sh -s - "${arguments[@]}"
sudo -n systemctl enable --now k3s
REMOTE

mkdir -p "$(dirname "$KUBECONFIG_PATH")"
ssh -n "$server_ssh" 'sudo -n cat /etc/rancher/k3s/k3s.yaml' >"$KUBECONFIG_PATH"
chmod 600 "$KUBECONFIG_PATH"
sed -i.bak "s#https://127.0.0.1:6443#https://$api_address:6443#" "$KUBECONFIG_PATH"
rm -f "$KUBECONFIG_PATH.bak"

export KUBECONFIG=$KUBECONFIG_PATH
kubectl wait --for=condition=Ready "node/$server_name" --timeout=5m
"$SCRIPT_DIR/node_manager.sh" reconcile

expected_nodes=()
while IFS=$'\t' read -r node_name _; do
  expected_nodes+=("node/$node_name")
done < <(inventory nodes --enabled)
kubectl wait --for=condition=Ready "${expected_nodes[@]}" --timeout=5m
kubectl get nodes -o wide
