#!/usr/bin/env bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)
CONFIG_PATH=${CONFIG_PATH:-"$ROOT_DIR/deploy/sglang-native/cluster/nodes.json"}
TOOLS_DIR="$ROOT_DIR/deploy/sglang-native/.tools/bin"
INVENTORY="$SCRIPT_DIR/inventory.py"

export PATH="$TOOLS_DIR:$PATH"

inventory() {
  python3 "$INVENTORY" "$CONFIG_PATH" "$@"
}

cluster_field() {
  inventory cluster "$1"
}

configured_kubeconfig() {
  local relative_path
  relative_path=$(cluster_field kubeconfig)
  printf '%s/%s\n' "$HOME" "$relative_path"
}

read_node() {
  local node_name=$1
  IFS=$'\t' read -r \
    NODE_NAME NODE_ROLE NODE_ENABLED NODE_SSH NODE_HOSTNAME NODE_IP NODE_INTERFACE \
    NODE_GPU NODE_MODEL_MODE NODE_MODEL_SOURCE NODE_MODEL_SOURCE_PATH NODE_LABELS \
    < <(inventory node "$node_name")
  if [ "$NODE_MODEL_SOURCE_PATH" = "-" ]; then
    NODE_MODEL_SOURCE_PATH=
  fi
  if [ "$NODE_LABELS" = "-" ]; then
    NODE_LABELS=
  fi
}
