#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

KUBECONFIG_PATH=${KUBECONFIG_PATH:-"$(configured_kubeconfig)"}
service_address=$(cluster_field serviceAddress)
service_node_port=$(cluster_field serviceNodePort)
ENDPOINT=${ENDPOINT:-"http://$service_address:$service_node_port"}
MODEL=Qwen/Qwen2.5-0.5B-Instruct

export KUBECONFIG=$KUBECONFIG_PATH

kubectl get nodes
wait_for_inferenceservice_ready qwen2-5-0-5b-instruct qwen2-5-0-5b-instruct 600
kubectl -n qwen2-5-0-5b-instruct wait \
  --for=condition=Ready pod -l component=engine \
  --timeout=10m
kubectl -n qwen2-5-0-5b-instruct wait \
  --for=condition=Ready pod -l component=router \
  --timeout=10m
kubectl -n qwen2-5-0-5b-instruct get pods -o wide
curl -fsS "$ENDPOINT/v1/models"
curl -fsS "$ENDPOINT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [{\"role\": \"user\", \"content\": \"用一句话介绍深圳。\"}],
    \"max_tokens\": 64,
    \"temperature\": 0
  }"
curl -fsSN "$ENDPOINT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [{\"role\": \"user\", \"content\": \"列出三个深圳地标。\"}],
    \"max_tokens\": 32,
    \"temperature\": 0,
    \"stream\": true
  }"
