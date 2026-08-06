#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

TARGET=${MODEL_TARGET:-/mnt/data/models/Qwen/Qwen2.5-0.5B-Instruct}
requested_source_node=${MODEL_SOURCE_NODE:-}
requested_source_path=${MODEL_SOURCE_PATH:-}
targets=()
source_ssh=
source_path=

inventory validate >/dev/null

remote_has_full_model() {
  local target_ssh=$1
  local model_path=$2
  ssh "$target_ssh" bash -s -- "$model_path" <<'REMOTE'
set -euo pipefail
model_path=$1
test -f "$model_path/config.json"
find "$model_path" -maxdepth 1 -type f \
  \( -name '*.safetensors' -o -name '*.bin' -o -name '*.gguf' \) \
  -print -quit | grep -q .
REMOTE
}

if (($# > 0)); then
  targets=("$@")
else
  while IFS=$'\t' read -r node_name _; do
    read_node "$node_name"
    [ "$NODE_MODEL_MODE" = none ] || targets+=("$NODE_NAME")
  done < <(inventory nodes --enabled)
fi

if [ -n "$requested_source_node" ]; then
  [ -n "$requested_source_path" ] || {
    echo "设置 MODEL_SOURCE_NODE 时必须同时设置 MODEL_SOURCE_PATH" >&2
    exit 1
  }
  read_node "$requested_source_node"
  remote_has_full_model "$NODE_SSH" "$requested_source_path"
  source_ssh=$NODE_SSH
  source_path=$requested_source_path
else
  while IFS=$'\t' read -r node_name _; do
    read_node "$node_name"
    if [ "$NODE_MODEL_SOURCE" = true ] &&
      remote_has_full_model "$NODE_SSH" "$NODE_MODEL_SOURCE_PATH" 2>/dev/null; then
      source_ssh=$NODE_SSH
      source_path=$NODE_MODEL_SOURCE_PATH
      break
    fi
  done < <(inventory nodes)
fi

if [ -z "$source_ssh" ]; then
  while IFS=$'\t' read -r node_name _; do
    read_node "$node_name"
    if [ "$NODE_MODEL_MODE" = full ] &&
      remote_has_full_model "$NODE_SSH" "$TARGET" 2>/dev/null; then
      source_ssh=$NODE_SSH
      source_path=$TARGET
      break
    fi
  done < <(inventory nodes)
fi

[ -n "$source_ssh" ] || {
  echo "找不到完整模型源，请在 nodes.json 中配置可访问的 modelSource 节点" >&2
  exit 1
}

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
rsync -a -e ssh "$source_ssh:$source_path/" "$work_dir/"

for node_name in "${targets[@]}"; do
  read_node "$node_name"
  [ "$NODE_MODEL_MODE" = none ] && continue

  ssh -n "$NODE_SSH" "
    remote_user=\$(id -un)
    remote_group=\$(id -gn)
    sudo -n install -d -o \"\$remote_user\" -g \"\$remote_group\" '$TARGET'
  "

  if [ "$NODE_MODEL_MODE" = tokenizer ]; then
    rsync -a --delete \
      --exclude='*.safetensors' \
      --exclude='*.bin' \
      --exclude='*.gguf' \
      --exclude='*.pt' \
      --exclude='*.pth' \
      -e ssh "$work_dir/" "$NODE_SSH:$TARGET/"
    ssh -n "$NODE_SSH" "
      set -euo pipefail
      test -f '$TARGET/config.json'
      test -f '$TARGET/tokenizer_config.json' || test -f '$TARGET/tokenizer.json'
      sha256sum '$TARGET/config.json'
    "
  else
    rsync -a --delete -e ssh "$work_dir/" "$NODE_SSH:$TARGET/"
    remote_has_full_model "$NODE_SSH" "$TARGET"
    ssh -n "$NODE_SSH" "
      weight=\$(find '$TARGET' -maxdepth 1 -type f \\
        \\( -name '*.safetensors' -o -name '*.bin' -o -name '*.gguf' \\) \\
        -print -quit)
      sha256sum '$TARGET/config.json' \"\$weight\"
    "
  fi
done
