#!/usr/bin/env python3
"""真实业务 trace 回放的唯一入口（端到端自动化）。

一条命令完成：检查/启动服务 -> 上传 trace 与客户端 -> 回放 -> 拉回结果与服务日志
-> 生成 report.md -> 关停由本次启动的服务。

每次运行产出固定结构（benchmark/runs/trace-YYYYMMDD-HHMMSS/）：
    raw_result.json   report.md   server.log   run_meta.json   trace-input.jsonl

用法（在仓库根目录，Windows PowerShell / Git Bash / Linux 通用）：
    python benchmark/run_trace.py
    python benchmark/run_trace.py --trace benchmark/traces/business-peak-5min.jsonl
    python benchmark/run_trace.py --tp 1 --keep-server

默认值针对 gpu10 当前部署，其余机器用参数覆盖；只依赖本机 python + ssh/scp。
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def run_local(cmd, check=True, capture=False):
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise SystemExit("本地命令失败: {} (exit {})".format(" ".join(cmd), r.returncode))
    return r


def ssh(host, remote_cmd, capture=False):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote_cmd],
        capture_output=capture, text=True)
    return r


def scp(src, dst):
    return run_local(["scp", "-o", "BatchMode=yes", src, dst])


def health_code(host, port):
    cmd = ("curl -s -o /dev/null -w '%{http_code}' --max-time 5 "
           "http://127.0.0.1:" + str(port) + "/health")
    r = ssh(host, cmd, capture=True)
    return r.stdout.strip() if r.returncode == 0 else "ssh-error"


def port_listening(host, port):
    r = ssh(host, "ss -ltn 2>/dev/null | grep -q ':{} '".format(port), capture=True)
    return r.returncode == 0


def start_server(args):
    extra = ""
    if args.tp > 1:
        extra = "--disable-custom-all-reduce --mm-feature-transport cpu"
    cmd = (
        "cd ~/trace-run && "
        "FLASHINFER_DISABLE_VERSION_CHECK=1 nohup ~/sglang-env/bin/python "
        "-m sglang.launch_server "
        "--model-path {model_path} "
        "--served-model-name {model} "
        "--chat-template {template} "
        "--host 0.0.0.0 --port {port} --tp {tp} {extra} "
        "--context-length {context} --mem-fraction-static 0.88 "
        "--sampling-defaults model "
        "> ~/sglang-trace.log 2>&1 & echo started pid=$!"
    ).format(model_path=args.model_path, model=args.model, template=args.template,
             port=args.port, tp=args.tp, extra=extra, context=args.context)
    r = ssh(args.ssh, cmd, capture=True)
    if r.returncode != 0 or "started" not in r.stdout:
        raise SystemExit("服务启动命令失败: {}".format(r.stderr or r.stdout))
    m = re.search(r"pid=(\d+)", r.stdout)
    args._launched_pid = int(m.group(1)) if m else None
    print("[run_trace] server launching on {}:{} (pid={})".format(
        args.ssh, args.port, args._launched_pid))


def stop_server(args):
    pid = getattr(args, "_launched_pid", None)
    if pid:
        ssh(args.ssh, "kill {} 2>/dev/null || true".format(pid))
    time.sleep(3)
    print("[run_trace] server stopped")


_LOG_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_MEMORY_LINES = re.compile(
    r"(Load weight end|KV Cache is allocated|Mamba Cache is allocated|"
    r"Memory pool end|server is fired up)")


def filter_server_log(src, dst, t0, t1):
    """只保留本次运行时间窗内的日志行，避免报告里的峰值观测混入其他活动；
    服务启动/显存分配这类上下文行无条件保留。"""
    pad = timedelta(minutes=1)
    kept = []
    with open(src, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_TS.match(line)
            if m:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = ts.replace(tzinfo=timezone.utc)
                if t0 - pad <= ts <= t1 + pad or _MEMORY_LINES.search(line):
                    kept.append(line)
            else:
                kept.append(line)
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(kept)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "trace_env.json"),
        help="配置文件（默认 benchmark/trace_env.json）；换机器/换模型就改这个文件")
    ap.add_argument("--show-config", action="store_true",
                    help="只打印合并后的配置并退出，不真正执行")
    # 以下参数默认 None：命令行 > 配置文件 > 内置默认值
    ap.add_argument("--trace", default=None,
                    help="回放的 trace JSONL（相对仓库根目录）")
    ap.add_argument("--time-scale", type=float, default=None,
                    help="时间缩放：1=原速，60=快60倍")
    ap.add_argument("--tp", type=int, default=None,
                    help="模型并行度（1 或 2）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--ssh", default=None, help="执行机 ssh 地址")
    ap.add_argument("--model", default=None, help="served model name")
    ap.add_argument("--model-path", default=None)
    ap.add_argument("--template", default=None)
    ap.add_argument("--context", type=int, default=None)
    ap.add_argument("--server-log", default=None)
    ap.add_argument("--start-server", default=None,
                    choices=("auto", "always", "never"),
                    help="auto=不健康才启动；always=先杀掉再启动；never=假定已在跑")
    ap.add_argument("--keep-server", action="store_true", default=None,
                    help="结束后保留服务（默认：由本次启动的服务会被停掉）")
    ap.add_argument("--wait-ready-s", type=int, default=None)
    ap.add_argument("--client-timeout-s", type=int, default=None)
    ap.add_argument("--note", action="append", default=None)
    args = ap.parse_args()

    # 读配置文件（没有就回落到内置默认值）
    cfg = {}
    if os.path.isfile(args.env):
        try:
            with open(args.env, encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit("配置文件 {} 读取失败: {}".format(args.env, e))
    else:
        print("[run_trace] 未找到配置文件 {}，使用内置默认值".format(args.env))

    def pick(name, default):
        v = getattr(args, name)
        return default if v is None else v

    args.ssh = pick("ssh", cfg.get("ssh", "qiyu@100.64.0.8"))
    args.model = pick("model", cfg.get("model", "Qwen/Qwen3.8-27B-FP8"))
    args.model_path = pick("model_path", cfg.get(
        "model_path",
        "~/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B-FP8/"
        "snapshots/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"))
    args.template = pick("template", cfg.get(
        "template", "~/trace-run/qwen35-chat-template-dev.jinja"))
    args.context = pick("context", cfg.get("context", 65536))
    args.port = pick("port", cfg.get("port", 30000))
    args.tp = pick("tp", cfg.get("tp", 2))
    args.time_scale = pick("time_scale", cfg.get("time_scale", 1.0))
    args.trace = pick("trace", cfg.get(
        "trace", "benchmark/traces/business-slice-15min.jsonl"))
    args.server_log = pick("server_log", cfg.get("server_log", "~/sglang-trace.log"))
    args.start_server = pick("start_server", cfg.get("start_server", "auto"))
    args.wait_ready_s = pick("wait_ready_s", cfg.get("wait_ready_s", 600))
    args.client_timeout_s = pick("client_timeout_s", cfg.get("client_timeout_s", 900))
    args.keep_server = (args.keep_server if args.keep_server is not None
                        else bool(cfg.get("keep_server", False)))
    cfg_notes = cfg.get("notes") or []
    args.note = (cfg_notes if isinstance(cfg_notes, list) else []) + (args.note or [])

    if args.tp not in (1, 2):
        raise SystemExit("--tp 只支持 1 或 2，当前为 {}".format(args.tp))
    if args.start_server not in ("auto", "always", "never"):
        raise SystemExit("--start-server 只支持 auto/always/never，当前为 {}"
                         .format(args.start_server))
    if args.time_scale <= 0:
        raise SystemExit("--time-scale 必须 > 0")

    if args.show_config:
        print(json.dumps({
            "env": args.env, "ssh": args.ssh, "model": args.model,
            "model_path": args.model_path, "template": args.template,
            "context": args.context, "port": args.port, "tp": args.tp,
            "time_scale": args.time_scale, "trace": args.trace,
            "server_log": args.server_log, "start_server": args.start_server,
            "keep_server": args.keep_server,
            "wait_ready_s": args.wait_ready_s,
            "client_timeout_s": args.client_timeout_s,
            "notes": args.note,
        }, ensure_ascii=False, indent=2))
        return

    if not os.path.isfile(args.trace):
        raise SystemExit(
            "找不到 trace 文件: {}\n"
            "可用转换器生成，例如：\n"
            "python benchmark/traces/log_to_trace.py 会话数据1.ndjson "
            "-o benchmark/traces/my-15min.jsonl --window-s 900".format(args.trace))

    run_id = "trace-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(REPO_ROOT, "benchmark", "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)

    # 1) 检查/启动服务
    started_by_us = False
    args._launched_pid = None
    try:
        code = health_code(args.ssh, args.port)
        if args.start_server == "never" and code != "200":
            raise SystemExit("{}:{} 上没有健康服务，请先启动或去掉 --start-server never"
                             .format(args.ssh, args.port))
        if args.start_server == "always":
            ssh(args.ssh, "pkill -f 'sglang.launch_server.*--port {} ' || true".format(args.port))
            time.sleep(3)
            started_by_us = True
            start_server(args)
        elif code == "200":
            print("[run_trace] reuse running server {}:{}".format(args.ssh, args.port))
        elif port_listening(args.ssh, args.port):
            print("[run_trace] 端口 {} 已被占用但未就绪（大概率正在加载），等待其完成..."
                  .format(args.port))
        else:
            print("[run_trace] starting server (tp={}, port={})...".format(args.tp, args.port))
            started_by_us = True
            start_server(args)

        # 统一等待就绪：无论服务是自己启动还是别人正在加载
        if code != "200" or args.start_server == "always":
            for _ in range(args.wait_ready_s // 10):
                time.sleep(10)
                if health_code(args.ssh, args.port) == "200":
                    print("[run_trace] server ready on {}:{}".format(args.ssh, args.port))
                    break
            else:
                raise SystemExit("等待服务就绪超时（{}s），请查看 ~/sglang-trace.log"
                                 .format(args.wait_ready_s))

        # 2) 上传文件
        remote = "{}:~/trace-run/".format(args.ssh)
        print("[run_trace] uploading trace + client + report tools...")
        scp(os.path.abspath(args.trace), remote + "trace-input.jsonl")
        scp(os.path.join(REPO_ROOT, "benchmark", "trace_client.py"), remote + "trace_client.py")
        scp(os.path.join(REPO_ROOT, "benchmark", "trace_report.py"), remote + "trace_report.py")

        # 3) 回放（输出直接透传到本机控制台，能看到实时进度）
        print("[run_trace] replay started, run_id={}".format(run_id))
        run_cmd = (
            "cd ~/trace-run && python3 trace_client.py "
            "--url http://127.0.0.1:{port} --model {model} "
            "--trace-file trace-input.jsonl --num-prompts -1 "
            "--time-scale {scale} --timeout-s {to} "
            "--output-file raw_result_{run_id}.json"
        ).format(port=args.port, model=args.model, scale=args.time_scale,
                 to=args.client_timeout_s, run_id=run_id)
        replay_start = datetime.now(timezone.utc)
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", args.ssh, run_cmd], text=True)
        replay_end = datetime.now(timezone.utc)
        if r.returncode != 0:
            raise SystemExit("回放失败，请查看远端 ~/trace-run/ 与服务日志")

        # 4) 拉回结果与服务日志
        print("[run_trace] collecting results...")
        scp("{}:~/trace-run/raw_result_{}.json".format(args.ssh, run_id),
            os.path.join(run_dir, "raw_result.json"))
        server_log_path = os.path.join(run_dir, "server.log")
        scp("{}:{}".format(args.ssh, args.server_log),
            server_log_path)
        filter_server_log(server_log_path, server_log_path, replay_start, replay_end)
        shutil.copyfile(os.path.abspath(args.trace),
                        os.path.join(run_dir, "trace-input.jsonl"))

        # 5) 生成报告
        report_py = os.path.join(REPO_ROOT, "benchmark", "trace_report.py")
        spec = importlib.util.spec_from_file_location("trace_report", report_py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        notes = list(args.note) or []
        notes.append("ssh={} tp={} port={} context={} time_scale={}".format(
            args.ssh, args.tp, args.port, args.context, args.time_scale))
        sys.argv = ["trace_report.py",
                    "--result", os.path.join(run_dir, "raw_result.json"),
                    "--trace", args.trace,
                    "--model", args.model,
                    "--server-log", os.path.join(run_dir, "server.log"),
                    "--output", os.path.join(run_dir, "report.md")] + \
                   [x for n in notes for x in ("--note", n)]
        mod.main()

        # 6) 元信息
        with open(os.path.join(run_dir, "raw_result.json"), encoding="utf-8") as f:
            result = json.load(f)
        meta = {
            "run_id": run_id,
            "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "ssh": args.ssh, "model": args.model, "tp": args.tp,
            "port": args.port, "context": args.context,
            "trace": args.trace, "time_scale": args.time_scale,
            "server_started_by_run": started_by_us,
            "replayed": result.get("replayed"),
            "completed": result.get("completed"),
            "duration_s": result.get("duration"),
            "p99_ttft_ms": result.get("p99_ttft_ms"),
        }
        with open(os.path.join(run_dir, "run_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        print("\n== 完成 ==")
        print("run dir : {}".format(run_dir))
        print("report  : {}".format(os.path.join(run_dir, "report.md")))
        print("replayed={}/{} completed={} duration={:.1f}s ttft_p99={}ms".format(
            meta["replayed"], result.get("trace_lines"), meta["completed"],
            meta["duration_s"],
            "{:.0f}".format(meta["p99_ttft_ms"]) if meta["p99_ttft_ms"] else "-"))
    finally:
        if started_by_us and not args.keep_server:
            stop_server(args)


if __name__ == "__main__":
    main()
