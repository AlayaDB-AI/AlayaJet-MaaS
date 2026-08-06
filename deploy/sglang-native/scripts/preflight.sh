#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

minimum_disk_gib=$(cluster_field minimumDiskGiB)
minimum_disk_kib=$((minimum_disk_gib * 1024 * 1024))
minimum_driver_major=$(cluster_field minimumNvidiaDriverMajor)
nvidia_toolkit_version=$(cluster_field nvidiaContainerToolkitVersion)
model_source_ready=false

inventory validate
for command_name in ssh scp rsync curl tar python3 kubectl helm; do
  command -v "$command_name" >/dev/null || {
    echo "缺少本机命令: $command_name；请先运行 prepare_environment.sh" >&2
    exit 1
  }
done

while IFS=$'\t' read -r node_name _; do
  read_node "$node_name"
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$NODE_SSH" "
    set -euo pipefail
    test \"\$(hostname)\" = '$NODE_HOSTNAME'
    sudo -n true
    command -v curl >/dev/null
    command -v rsync >/dev/null
    ip link show '$NODE_INTERFACE' >/dev/null
    test \"\$(df -Pk /var/lib | awk 'NR == 2 {print \$4}')\" -ge '$minimum_disk_kib'
  "

  if [ "$NODE_GPU" = true ]; then
    ssh -n "$NODE_SSH" "
      set -euo pipefail
      command -v nvidia-container-runtime >/dev/null
      test \"\$(dpkg-query -W -f='\${Version}' nvidia-container-toolkit 2>/dev/null)\" = '$nvidia_toolkit_version'
      version=\$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
      major=\${version%%.*}
      test \"\$major\" -ge '$minimum_driver_major'
      nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
      if sudo -n systemctl is-active --quiet k3s-agent || sudo -n systemctl is-active --quiet k3s; then
        sudo -n grep -q 'nvidia-container-runtime' /var/lib/rancher/k3s/agent/etc/containerd/config.toml
      fi
    "
  fi

  if [ "$NODE_MODEL_SOURCE" = true ] &&
    ssh -n "$NODE_SSH" \
      "test -f '$NODE_MODEL_SOURCE_PATH/config.json' && test -f '$NODE_MODEL_SOURCE_PATH/model.safetensors'"; then
    model_source_ready=true
  elif [ "$NODE_MODEL_MODE" = full ] &&
    ssh -n "$NODE_SSH" \
      'test -f /mnt/data/models/Qwen/Qwen2.5-0.5B-Instruct/config.json && test -f /mnt/data/models/Qwen/Qwen2.5-0.5B-Instruct/model.safetensors'; then
    model_source_ready=true
  fi
  echo "$NODE_NAME: 部署条件检查通过"
done < <(inventory nodes --enabled)

[ "$model_source_ready" = true ] || {
  echo "找不到可用的完整模型源" >&2
  exit 1
}

echo "部署前检查全部通过"
