#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
OME_COMMIT=${OME_COMMIT:-015070c9661c704addf25ce8d0f6e71fba7f7df9}
CERT_MANAGER_VERSION=${CERT_MANAGER_VERSION:-v1.18.2}

export KUBECONFIG=$KUBECONFIG_PATH

kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.yaml"
kubectl -n cert-manager wait --for=condition=Available deployment/cert-manager deployment/cert-manager-cainjector deployment/cert-manager-webhook --timeout=5m

kubectl apply -f "$ROOT_DIR/deploy/sglang-native/platform/nvidia-device-plugin.yaml"
kubectl -n kube-system rollout status daemonset/nvidia-device-plugin --timeout=10m

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
curl -fsSL "https://github.com/ome-projects/ome/archive/$OME_COMMIT.tar.gz" -o "$work_dir/ome.tar.gz"
tar -xzf "$work_dir/ome.tar.gz" -C "$work_dir"
ome_dir="$work_dir/ome-$OME_COMMIT"

helm upgrade --install ome-crd "$ome_dir/charts/ome-crd" \
  --namespace ome --create-namespace
helm upgrade --install ome-resources "$ome_dir/charts/ome-resources" \
  --namespace ome \
  --values "$ROOT_DIR/deploy/sglang-native/platform/ome-values.yaml"

kubectl -n ome wait --for=condition=Available deployment/ome-controller-manager --timeout=10m
kubectl -n ome rollout status daemonset/ome-model-agent-daemonset --timeout=10m
kubectl get pods -n ome -o wide
