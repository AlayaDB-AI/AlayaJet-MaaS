#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}

export KUBECONFIG=$KUBECONFIG_PATH

kubectl apply -f "$ROOT_DIR/deploy/sglang-native/model/qwen2.5-0.5b-instruct.yaml"
kubectl -n qwen2-5-0-5b-instruct wait \
  --for=condition=Ready inferenceservice/qwen2-5-0-5b-instruct \
  --timeout=30m
kubectl -n qwen2-5-0-5b-instruct get inferenceservice,pods,services -o wide
