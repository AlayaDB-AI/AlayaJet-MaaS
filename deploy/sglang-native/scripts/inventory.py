#!/usr/bin/env python3

import argparse
import json
import os
import tempfile
from pathlib import Path


NODE_FIELDS = (
    "name",
    "role",
    "enabled",
    "ssh",
    "hostname",
    "nodeIP",
    "networkInterface",
    "gpu",
    "modelMode",
    "modelSource",
    "modelSourcePath",
    "labels",
)


def load_inventory(path: Path) -> dict:
    with path.open(encoding="utf-8") as inventory_file:
        inventory = json.load(inventory_file)
    validate_inventory(inventory)
    return inventory


def validate_inventory(inventory: dict) -> None:
    cluster = inventory.get("cluster")
    nodes = inventory.get("nodes")
    if not isinstance(cluster, dict) or not isinstance(nodes, list):
        raise ValueError("配置必须包含 cluster 对象和 nodes 数组")

    required_cluster_fields = (
        "name",
        "server",
        "k3sVersion",
        "kubeconfig",
        "apiAddress",
        "serviceAddress",
        "serviceNodePort",
        "minimumDiskGiB",
        "minimumNvidiaDriverMajor",
        "nvidiaContainerToolkitVersion",
    )
    for field in required_cluster_fields:
        if field not in cluster:
            raise ValueError(f"cluster 缺少字段: {field}")

    string_cluster_fields = (
        "name",
        "server",
        "k3sVersion",
        "kubeconfig",
        "apiAddress",
        "serviceAddress",
        "nvidiaContainerToolkitVersion",
    )
    for field in string_cluster_fields:
        if not isinstance(cluster[field], str) or not cluster[field]:
            raise ValueError(f"cluster.{field} 必须是非空字符串")
    if type(cluster["serviceNodePort"]) is not int or not (
        30000 <= cluster["serviceNodePort"] <= 32767
    ):
        raise ValueError("cluster.serviceNodePort 必须在 30000-32767 之间")
    for field in ("minimumDiskGiB", "minimumNvidiaDriverMajor"):
        if type(cluster[field]) is not int or cluster[field] <= 0:
            raise ValueError(f"cluster.{field} 必须是正整数")
    if not nodes:
        raise ValueError("nodes 不能为空")

    names = set()
    node_ips = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("nodes 中的每一项都必须是对象")
        for field in NODE_FIELDS:
            if field not in node and field != "modelSourcePath":
                raise ValueError(f"节点缺少字段 {field}: {node.get('name', '<unknown>')}")
        name = node["name"]
        for field in ("name", "ssh", "hostname", "nodeIP", "networkInterface"):
            if not isinstance(node[field], str) or not node[field]:
                raise ValueError(f"节点 {name or '<unknown>'} 的 {field} 必须是非空字符串")
        if name in names:
            raise ValueError(f"节点名称重复: {name}")
        names.add(name)
        if node["nodeIP"] in node_ips:
            raise ValueError(f"节点地址重复: {node['nodeIP']}")
        node_ips.add(node["nodeIP"])
        if node["role"] not in ("server", "worker"):
            raise ValueError(f"节点 role 必须是 server 或 worker: {name}")
        for field in ("enabled", "gpu", "modelSource"):
            if not isinstance(node[field], bool):
                raise ValueError(f"节点 {name} 的 {field} 必须是布尔值")
        if node["modelMode"] not in ("full", "tokenizer", "none"):
            raise ValueError(f"节点 modelMode 无效: {name}")
        if not isinstance(node["labels"], list) or not all(
            isinstance(label, str) and label for label in node["labels"]
        ):
            raise ValueError(f"节点 labels 必须是非空字符串数组: {name}")
        if node["modelSource"] and (
            not isinstance(node.get("modelSourcePath"), str)
            or not node["modelSourcePath"]
        ):
            raise ValueError(f"模型源节点必须配置 modelSourcePath: {name}")

    server_name = cluster["server"]
    servers = [node for node in nodes if node["name"] == server_name]
    if len(servers) != 1 or servers[0]["role"] != "server" or not servers[0]["enabled"]:
        raise ValueError("cluster.server 必须指向一个启用的 server 节点")
    if sum(node["role"] == "server" for node in nodes) != 1:
        raise ValueError("当前集群必须且只能配置一个 server 节点")


def node_row(node: dict) -> str:
    values = []
    for field in NODE_FIELDS:
        value = node.get(field, "")
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, list):
            value = ",".join(value)
        if value == "":
            value = "-"
        values.append(str(value))
    return "\t".join(values)


def set_enabled(path: Path, inventory: dict, node_name: str, enabled: bool) -> None:
    for node in inventory["nodes"]:
        if node["name"] == node_name:
            if node["role"] == "server" and not enabled:
                raise ValueError("server 节点必须保持启用")
            node["enabled"] = enabled
            break
    else:
        raise ValueError(f"配置中不存在节点: {node_name}")

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            json.dump(inventory, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate")

    cluster_parser = subparsers.add_parser("cluster")
    cluster_parser.add_argument("field")

    node_parser = subparsers.add_parser("node")
    node_parser.add_argument("name")

    nodes_parser = subparsers.add_parser("nodes")
    nodes_parser.add_argument("--role", choices=("server", "worker"))
    nodes_parser.add_argument("--enabled", action="store_true")
    nodes_parser.add_argument("--gpu", action="store_true")

    set_parser = subparsers.add_parser("set-enabled")
    set_parser.add_argument("name")
    set_parser.add_argument("enabled", choices=("true", "false"))

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inventory = load_inventory(args.config)

    if args.command == "validate":
        print("机器配置检查通过")
    elif args.command == "cluster":
        if args.field not in inventory["cluster"]:
            raise ValueError(f"cluster 不存在字段: {args.field}")
        print(inventory["cluster"][args.field])
    elif args.command == "node":
        for node in inventory["nodes"]:
            if node["name"] == args.name:
                print(node_row(node))
                break
        else:
            raise ValueError(f"配置中不存在节点: {args.name}")
    elif args.command == "nodes":
        for node in inventory["nodes"]:
            if args.role and node["role"] != args.role:
                continue
            if args.enabled and not node["enabled"]:
                continue
            if args.gpu and not node["gpu"]:
                continue
            print(node_row(node))
    elif args.command == "set-enabled":
        set_enabled(args.config, inventory, args.name, args.enabled == "true")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"机器配置错误: {error}")
