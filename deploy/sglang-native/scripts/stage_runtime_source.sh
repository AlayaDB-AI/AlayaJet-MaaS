#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

SOURCE_PATH=$(ensure_runtime_source_tree)
REPO_TARGET=${SGLANG_REPO_TARGET:-$(cluster_field runtimeRepoTarget)}
SANDBOX_TARGET=${SGLANG_SANDBOX_TARGET:-$(cluster_field runtimeSandboxTarget)}
targets=()

inventory validate >/dev/null

source_revision() {
  if command -v sha256sum >/dev/null; then
    (
      cd "$SOURCE_PATH"
      find . -type f \
        -not -path './.git/*' \
        -not -path './__pycache__/*' \
        -not -path './.mypy_cache/*' \
        -not -path './.pytest_cache/*' \
        -not -path './build/*' \
        -not -path './dist/*' \
        -print0 |
        sort -z |
        xargs -0 sha256sum |
        sha256sum |
        awk '{print $1}'
    )
  elif command -v git >/dev/null && git -C "$SOURCE_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$SOURCE_PATH" rev-parse --short=12 HEAD
  else
    date -u +%Y%m%d%H%M%S
  fi
}

if (($# > 0)); then
  targets=("$@")
else
  while IFS=$'\t' read -r node_name _; do
    read_node "$node_name"
    if [ "$NODE_ROLE" = server ] || [ "$NODE_MODEL_MODE" != none ]; then
      targets+=("$NODE_NAME")
    fi
  done < <(inventory nodes --enabled)
fi

revision=$(source_revision)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

rsync -a --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.mypy_cache' \
  --exclude='.pytest_cache' \
  --exclude='build' \
  --exclude='dist' \
  "$SOURCE_PATH/" "$work_dir/"
printf '%s\n' "$revision" >"$work_dir/.alayajet-source-revision"

for node_name in "${targets[@]}"; do
  read_node "$node_name"

  ssh "$NODE_SSH" bash -s -- "$REPO_TARGET" "$SANDBOX_TARGET" <<'REMOTE'
set -euo pipefail
repo_target=$1
sandbox_target=$2
remote_user=$(id -un)
remote_group=$(id -gn)
sudo -n install -d -o "$remote_user" -g "$remote_group" "$repo_target" "$sandbox_target"
REMOTE

  rsync -a --delete -e ssh "$work_dir/" "$NODE_SSH:$REPO_TARGET/"
  ssh "$NODE_SSH" bash -s -- "$REPO_TARGET" "$SANDBOX_TARGET" "$revision" <<'REMOTE'
set -euo pipefail
repo_target=$1
sandbox_target=$2
expected_revision=$3
test -f "$repo_target/.alayajet-source-revision"
test "$(cat "$repo_target/.alayajet-source-revision")" = "$expected_revision"
test -d "$sandbox_target"
printf '%s: SGLang 源码已同步到 %s，沙箱目录 %s，revision=%s\n' \
  "$(hostname)" "$repo_target" "$sandbox_target" "$expected_revision"
REMOTE
done
