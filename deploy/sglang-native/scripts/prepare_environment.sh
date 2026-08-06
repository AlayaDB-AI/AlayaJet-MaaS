#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)
TOOLS_DIR="$ROOT_DIR/deploy/sglang-native/.tools/bin"
KUBECTL_VERSION=${KUBECTL_VERSION:-v1.32.9}
HELM_VERSION=${HELM_VERSION:-v3.17.4}
NVIDIA_TOOLKIT_VERSION=${NVIDIA_TOOLKIT_VERSION:-}

install_local_packages() {
  local missing=()
  local command_name

  for command_name in ssh scp rsync curl tar python3; do
    command -v "$command_name" >/dev/null || missing+=("$command_name")
  done
  ((${#missing[@]} == 0)) && return

  case "$(uname -s)" in
    Darwin)
      if ! command -v brew >/dev/null; then
        NONINTERACTIVE=1 /bin/bash -c \
          "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      fi
      brew install openssh rsync curl python
      ;;
    Linux)
      if command -v apt-get >/dev/null; then
        sudo apt-get update || echo "警告: 部分 APT 软件源刷新失败，将继续验证所需软件包" >&2
        sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
          openssh-client rsync curl tar python3 ca-certificates
      elif command -v dnf >/dev/null; then
        sudo dnf install -y openssh-clients rsync curl tar python3 ca-certificates
      else
        echo "无法自动安装本机依赖: 不支持的包管理器" >&2
        exit 1
      fi
      ;;
    *)
      echo "无法自动安装本机依赖: 不支持的操作系统" >&2
      exit 1
      ;;
  esac
}

sha256_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

platform_name() {
  case "$(uname -s)" in
    Darwin) printf 'darwin\n' ;;
    Linux) printf 'linux\n' ;;
    *) return 1 ;;
  esac
}

architecture_name() {
  case "$(uname -m)" in
    x86_64 | amd64) printf 'amd64\n' ;;
    arm64 | aarch64) printf 'arm64\n' ;;
    *) return 1 ;;
  esac
}

install_local_tools() {
  local platform architecture work_dir kubectl_url kubectl_sha actual_sha helm_archive
  platform=$(platform_name)
  architecture=$(architecture_name)
  mkdir -p "$TOOLS_DIR"
  work_dir=$(mktemp -d)

  if ! "$TOOLS_DIR/kubectl" version --client -o json 2>/dev/null |
    grep -q "\"gitVersion\": \"$KUBECTL_VERSION\""; then
    kubectl_url="https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$platform/$architecture/kubectl"
    curl -fsSL "$kubectl_url" -o "$work_dir/kubectl"
    kubectl_sha=$(curl -fsSL "$kubectl_url.sha256")
    actual_sha=$(sha256_file "$work_dir/kubectl")
    test "$actual_sha" = "$kubectl_sha"
    install -m 0755 "$work_dir/kubectl" "$TOOLS_DIR/kubectl"
  fi

  if ! "$TOOLS_DIR/helm" version --short 2>/dev/null | grep -q "^$HELM_VERSION"; then
    helm_archive="helm-$HELM_VERSION-$platform-$architecture.tar.gz"
    curl -fsSL "https://get.helm.sh/$helm_archive" -o "$work_dir/$helm_archive"
    curl -fsSL "https://get.helm.sh/$helm_archive.sha256sum" -o "$work_dir/helm.sha256"
    kubectl_sha=$(awk '{print $1}' "$work_dir/helm.sha256")
    actual_sha=$(sha256_file "$work_dir/$helm_archive")
    test "$actual_sha" = "$kubectl_sha"
    tar -xzf "$work_dir/$helm_archive" -C "$work_dir"
    install -m 0755 "$work_dir/$platform-$architecture/helm" "$TOOLS_DIR/helm"
  fi

  rm -rf "$work_dir"
}

wait_for_ssh() {
  local target=$1
  local attempt
  for attempt in $(seq 1 90); do
    if ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$target" true 2>/dev/null; then
      return
    fi
    sleep 10
  done
  echo "节点重启后未恢复 SSH: $target" >&2
  exit 1
}

install_remote_base() {
  local target=$1
  ssh "$target" 'bash -s' <<'REMOTE'
set -euo pipefail

missing=()
for command_name in curl rsync ip tar gpg; do
  command -v "$command_name" >/dev/null || missing+=("$command_name")
done
((${#missing[@]} == 0)) && exit 0

if command -v apt-get >/dev/null; then
  sudo -n apt-get update || echo "警告: 部分 APT 软件源刷新失败，将继续验证所需软件包" >&2
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl gnupg2 iproute2 rsync tar
elif command -v dnf >/dev/null; then
  sudo -n dnf install -y ca-certificates curl gnupg2 iproute rsync tar
else
  echo "不支持的远端包管理器" >&2
  exit 1
fi
REMOTE
}

install_gpu_driver() {
  local target=$1
  local minimum_major=$2
  if ssh -n "$target" "
    version=\$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)
    major=\${version%%.*}
    test -n \"\$major\" && test \"\$major\" -ge '$minimum_major'
  "; then
    return
  fi

  ssh "$target" 'bash -s' <<'REMOTE'
set -euo pipefail
command -v apt-get >/dev/null || {
  echo "GPU Driver 自动安装当前要求 Ubuntu/Debian" >&2
  exit 1
}
sudo -n apt-get update || echo "警告: 部分 APT 软件源刷新失败，将继续验证 NVIDIA Driver 软件包" >&2
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y ubuntu-drivers-common
sudo -n ubuntu-drivers install --gpgpu
REMOTE

  ssh -n "$target" 'sudo -n systemctl reboot' >/dev/null 2>&1 || true
  wait_for_ssh "$target"
  ssh -n "$target" "
    version=\$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
    major=\${version%%.*}
    test \"\$major\" -ge '$minimum_major'
    nvidia-smi -L
  "
}

install_nvidia_toolkit() {
  local target=$1
  if ssh -n "$target" "
    test \"\$(dpkg-query -W -f='\${Version}' nvidia-container-toolkit 2>/dev/null)\" = '$NVIDIA_TOOLKIT_VERSION' &&
      command -v nvidia-container-runtime >/dev/null
  "; then
    return
  fi

  ssh "$target" "NVIDIA_TOOLKIT_VERSION='$NVIDIA_TOOLKIT_VERSION' bash -s" <<'REMOTE'
set -euo pipefail
command -v apt-get >/dev/null || {
  echo "NVIDIA Container Toolkit 自动安装当前要求 Ubuntu/Debian" >&2
  exit 1
}

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
  sudo -n gpg --dearmor --yes \
    -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' |
  sudo -n tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo -n apt-get update || echo "警告: 部分 APT 软件源刷新失败，将继续验证 NVIDIA Toolkit 软件包" >&2
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  "nvidia-container-toolkit=$NVIDIA_TOOLKIT_VERSION" \
  "nvidia-container-toolkit-base=$NVIDIA_TOOLKIT_VERSION" \
  "libnvidia-container-tools=$NVIDIA_TOOLKIT_VERSION" \
  "libnvidia-container1=$NVIDIA_TOOLKIT_VERSION"
REMOTE
}

ensure_k3s_gpu_runtime() {
  local target=$1
  ssh "$target" 'bash -s' <<'REMOTE'
set -euo pipefail

for unit in k3s-agent k3s; do
  if sudo -n systemctl is-active --quiet "$unit"; then
    config=/var/lib/rancher/k3s/agent/etc/containerd/config.toml
    if ! sudo -n grep -q 'nvidia-container-runtime' "$config" 2>/dev/null; then
      sudo -n systemctl restart "$unit"
    fi
    break
  fi
done
REMOTE
}

prepare_node() {
  local node_name=$1
  local minimum_driver_major
  read_node "$node_name"

  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$NODE_SSH" true
  ssh -n "$NODE_SSH" 'sudo -n true'
  install_remote_base "$NODE_SSH"
  if [ "$NODE_GPU" = true ]; then
    minimum_driver_major=$(cluster_field minimumNvidiaDriverMajor)
    install_gpu_driver "$NODE_SSH" "$minimum_driver_major"
    install_nvidia_toolkit "$NODE_SSH"
    ensure_k3s_gpu_runtime "$NODE_SSH"
  fi
  echo "$NODE_NAME: 环境准备完成"
}

install_local_packages
install_local_tools
source "$SCRIPT_DIR/common.sh"
inventory validate >/dev/null
NVIDIA_TOOLKIT_VERSION=${NVIDIA_TOOLKIT_VERSION:-$(cluster_field nvidiaContainerToolkitVersion)}

if (($# > 0)); then
  for requested_node in "$@"; do
    prepare_node "$requested_node"
  done
else
  while IFS=$'\t' read -r node_name _; do
    prepare_node "$node_name"
  done < <(inventory nodes --enabled)
fi

echo "本机和远端环境准备完成"
