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

configured_runtime_source_path() {
  local source_path
  source_path=${SGLANG_SOURCE_PATH:-$(cluster_field runtimeSourcePath)}
  case "$source_path" in
    /*) printf '%s\n' "$source_path" ;;
    ~/*) printf '%s/%s\n' "$HOME" "${source_path#~/}" ;;
    *) printf '%s/%s\n' "$ROOT_DIR" "$source_path" ;;
  esac
}

configured_runtime_git_url() {
  printf '%s\n' "${SGLANG_GIT_URL:-$(cluster_field runtimeGitUrl)}"
}

configured_runtime_git_ref() {
  printf '%s\n' "${SGLANG_GIT_REF:-$(cluster_field runtimeGitRef)}"
}

is_runtime_source_tree() {
  local source_path=$1
  test -d "$source_path" &&
    test -d "$source_path/python" &&
    { test -f "$source_path/python/pyproject.toml" || test -f "$source_path/python/setup.py"; }
}

ensure_runtime_source_tree() {
  local source_path
  local git_url
  local git_ref
  source_path=$(configured_runtime_source_path)
  git_url=$(configured_runtime_git_url)
  git_ref=$(configured_runtime_git_ref)

  if is_runtime_source_tree "$source_path"; then
    printf '%s\n' "$source_path"
    return
  fi

  command -v git >/dev/null || {
    echo "missing local command: git; install git or run prepare_environment.sh first" >&2
    exit 1
  }

  if [ -e "$source_path" ] && [ ! -d "$source_path" ]; then
    cat >&2 <<EOF
Configured SGLang source path exists but is not a directory: $source_path
Please fix cluster.runtimeSourcePath or SGLANG_SOURCE_PATH, or move the existing file aside.
EOF
    exit 1
  fi

  if [ -e "$source_path" ] && [ -n "$(find "$source_path" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    cat >&2 <<EOF
Configured SGLang source path exists but is not a usable source tree: $source_path
Please fix cluster.runtimeSourcePath or SGLANG_SOURCE_PATH, or move the existing directory aside.
EOF
    exit 1
  fi

  mkdir -p "$(dirname "$source_path")"
  echo "SGLang source not found at $source_path; cloning $git_url into that path" >&2
  git clone "$git_url" "$source_path" >&2

  if [ "$git_ref" != "" ] && [ "$git_ref" != "-" ]; then
    if git -C "$source_path" rev-parse --verify --quiet "refs/remotes/origin/$git_ref" >/dev/null; then
      git -C "$source_path" checkout -B "$git_ref" "origin/$git_ref" >&2
    else
      git -C "$source_path" fetch --tags origin >&2
      git -C "$source_path" checkout --detach "$git_ref" >&2
    fi
  fi

  if ! is_runtime_source_tree "$source_path"; then
    cat >&2 <<EOF
Downloaded repository is not a usable SGLang source tree: $source_path
Repository: $git_url
Ref: $git_ref
EOF
    exit 1
  fi

  printf '%s\n' "$source_path"
}

wait_for_inferenceservice_ready() {
  local namespace=$1
  local name=$2
  local timeout_seconds=${3:-600}
  local status
  local end_time=$((SECONDS + timeout_seconds))

  while ((SECONDS < end_time)); do
    status=$(kubectl -n "$namespace" get inferenceservice "$name" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [ "$status" = True ]; then
      echo "inferenceservice.ome.io/$name Ready=True"
      return
    fi
    sleep 5
  done

  kubectl -n "$namespace" describe inferenceservice "$name" >&2 || true
  echo "等待 InferenceService Ready 超时: $namespace/$name" >&2
  exit 1
}

wait_for_deployment_rollout() {
  local namespace=$1
  local name=$2
  local timeout_seconds=${3:-600}
  local end_time=$((SECONDS + timeout_seconds))
  local generation
  local observed_generation
  local desired
  local total
  local updated
  local available

  while ((SECONDS < end_time)); do
    generation=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.metadata.generation}')
    observed_generation=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.status.observedGeneration}')
    desired=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.spec.replicas}')
    total=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.status.replicas}')
    updated=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.status.updatedReplicas}')
    available=$(kubectl -n "$namespace" get deployment "$name" -o jsonpath='{.status.availableReplicas}')

    total=${total:-0}
    updated=${updated:-0}
    available=${available:-0}
    if [[ "$observed_generation" == "$generation" && "$total" == "$desired" && "$updated" == "$desired" && "$available" == "$desired" ]]; then
      echo "deployment.apps/$name rolled out"
      return
    fi
    sleep 10
  done

  kubectl -n "$namespace" describe deployment "$name" >&2 || true
  echo "timed out waiting for deployment/$name rollout: desired=$desired total=$total updated=$updated available=$available" >&2
  exit 1
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
