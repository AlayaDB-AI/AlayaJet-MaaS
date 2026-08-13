#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
MANIFEST="$ROOT_DIR/deploy/sglang-native/model/qwen2.5-0.5b-instruct.yaml"
runtime_base_image=$(cluster_field runtimeBaseImage)
runtime_router_base_image=$(cluster_field runtimeRouterBaseImage)
runtime_repo_target=${SGLANG_REPO_TARGET:-$(cluster_field runtimeRepoTarget)}
runtime_sandbox_target=${SGLANG_SANDBOX_TARGET:-$(cluster_field runtimeSandboxTarget)}

export KUBECONFIG=$KUBECONFIG_PATH

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'
}

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
sed \
  -e "s|__RUNTIME_BASE_IMAGE__|$(escape_sed_replacement "$runtime_base_image")|g" \
  -e "s|__RUNTIME_ROUTER_BASE_IMAGE__|$(escape_sed_replacement "$runtime_router_base_image")|g" \
  -e "s|__RUNTIME_REPO_TARGET__|$(escape_sed_replacement "$runtime_repo_target")|g" \
  -e "s|__RUNTIME_SANDBOX_TARGET__|$(escape_sed_replacement "$runtime_sandbox_target")|g" \
  "$MANIFEST" >"$work_dir/qwen2.5-0.5b-instruct.yaml"

runtime_manifest_revision=$(sha256sum "$work_dir/qwen2.5-0.5b-instruct.yaml" | awk '{print $1}')
kubectl apply -f "$work_dir/qwen2.5-0.5b-instruct.yaml"
kubectl -n qwen2-5-0-5b-instruct patch deployment qwen2-5-0-5b-instruct-engine \
  --type=merge -p '{"spec":{"progressDeadlineSeconds":3600,"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}' \
  || true
kubectl -n qwen2-5-0-5b-instruct annotate inferenceservice qwen2-5-0-5b-instruct \
  "alayajet.io/runtime-manifest-revision=$runtime_manifest_revision" \
  --overwrite
sleep 10
kubectl -n qwen2-5-0-5b-instruct patch deployment qwen2-5-0-5b-instruct-engine \
  --type=merge -p '{"spec":{"progressDeadlineSeconds":3600,"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'
kubectl -n qwen2-5-0-5b-instruct patch deployment qwen2-5-0-5b-instruct-router \
  --type=merge -p '{"spec":{"progressDeadlineSeconds":1800}}'
wait_for_inferenceservice_ready qwen2-5-0-5b-instruct qwen2-5-0-5b-instruct 3600
wait_for_deployment_rollout qwen2-5-0-5b-instruct qwen2-5-0-5b-instruct-engine 3600
wait_for_deployment_rollout qwen2-5-0-5b-instruct qwen2-5-0-5b-instruct-router 1800
kubectl -n qwen2-5-0-5b-instruct get inferenceservice,pods,services -o wide
