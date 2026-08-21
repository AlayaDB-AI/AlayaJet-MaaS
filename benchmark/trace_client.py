#!/usr/bin/env python3
"""真实业务流量重放客户端：按 trace 中的时间戳/请求体原样回放到 OpenAI 兼容服务。

与 e2e_client.py 的定位差异：
  - e2e_client：合成负载（随机 token id），只测引擎/调度/网络；
  - trace_client：真实业务请求重放（真实消息、多轮、图片、采样参数、到达时间），
    用于验证「生产流量形态」下服务的 SLO 与吞吐。

输出与 e2e_client.py 同构（raw_result.json 的字段和 key 完全一致），
因此 run_local_benchmark.sh / judge_stage.py / collect_results.py 无需改动即可汇总。

依赖：仅标准库（argparse/json/urllib/threading 等），可同时跑在 Windows 本机与远端执行机。

Trace 文件格式（JSONL，每行一个请求）：
  {"ts": 0.0, "endpoint": "chat", "body": {...}}      # A：完整 OpenAI 请求体
  {"ts": 1.2, "messages": [{"role":"user","content":"..."}],
   "params": {"temperature": 0.7}, "output_len": 128}  # B：日志字段 + 参数

字段说明：
  ts         到达时刻（秒）。可相对可绝对，内部统一减去最小值；缺省时退化为
             按 --request-rate 的泊松到达。
  endpoint   "chat"（/v1/chat/completions，默认）或 "completions"。
  body       完整请求体，原样发送（仅补充 model/stream/stream_options 缺省值）。
  messages   仅 endpoint=chat 且未给 body 时使用；支持 OpenAI 多模态 parts，
             image_url 的 http(s) 链接会被客户端拉取并转成 data URL 再发送。
  params     采样参数（temperature/top_p 等），合并进请求体。
  output_len 未给 body 时的 max_tokens 兜底。

用法:
  python3 trace_client.py --url http://127.0.0.1:30000 --model Qwen/Qwen3.8-27B-FP8 \
      --trace-file benchmark/traces/sample.jsonl \
      --num-prompts 3 --time-scale 1.0 \
      --output-file raw_result.json
"""
import argparse
import base64
import importlib.util
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 与 e2e_client 一致：压测必须直连被测服务，显式绕过系统代理
# （Windows 注册表代理会让内网地址经代理转发而得到 502，测量失去意义）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _as_data_url(image_url):
    """把 http(s) 图片拉成本地 data URL；拉取失败则原样透传（由服务端决定成败）。"""
    if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
        return image_url
    try:
        req = urllib.request.Request(
            image_url, headers={"User-Agent": "alayajet-trace-client/1.0"})
        with _OPENER.open(req, timeout=30) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        return "data:{};base64,{}".format(ctype, base64.b64encode(data).decode())
    except Exception:
        return image_url


def _resolve_messages(messages):
    """把 messages 里 image_url 的 http(s) 链接替换为 data URL，其余内容保持原样。"""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url = part.get("image_url")
                    if isinstance(image_url, dict) and "url" in image_url:
                        image_url = dict(image_url)
                        image_url["url"] = _as_data_url(image_url["url"])
                    else:
                        image_url = _as_data_url(image_url)
                    part = dict(part)
                    part["image_url"] = image_url
                parts.append(part)
            content = parts
        m = dict(m)
        m["content"] = content
        out.append(m)
    return out


def build_payload(line, model):
    endpoint = line.get("endpoint", "chat")
    body = line.get("body")
    if isinstance(body, dict):
        payload = dict(body)
        if endpoint == "chat" and isinstance(payload.get("messages"), list):
            payload["messages"] = _resolve_messages(payload["messages"])
    else:
        payload = {}
        if endpoint == "completions":
            prompt = line.get("prompt", line.get("messages"))
            if isinstance(prompt, list):
                # 兜底：把消息列表拍平成单条文本，避免接口格式不匹配
                prompt = "\n".join(
                    (p.get("content") if isinstance(p, dict) else str(p))
                    for p in prompt)
            payload["prompt"] = prompt
        else:
            payload["messages"] = _resolve_messages(line.get("messages", []))
        params = line.get("params") or line.get("sampling_params") or {}
        if isinstance(params, dict):
            payload.update(params)
        if "max_tokens" not in payload and isinstance(line.get("output_len"), int):
            payload["max_tokens"] = line["output_len"]
    payload.setdefault("model", model)
    payload.setdefault("stream", True)
    payload.setdefault("stream_options", {"include_usage": True})
    # 不替用户强制 temperature：缺省时交给服务端按模型默认值采样
    # （Qwen3.8-27B 官方默认 1.0/0.95/20；合成压测的 0.0 贪心不适合真实流量重放）。
    return endpoint, payload


def one_request(url, endpoint, payload, timeout_s, idx):
    path = "/v1/chat/completions" if endpoint == "chat" else "/v1/completions"
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft, itls, usage = None, [], None
    last = None
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            for raw in resp:
                text = raw.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                now = time.perf_counter()
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                token_present = False
                if choices:
                    delta = choices[0].get("delta")
                    if delta is None:
                        delta = choices[0]
                    token_present = bool(
                        delta.get("content") or delta.get("text")
                        or delta.get("reasoning_content") or delta.get("reasoning"))
                if token_present:
                    if ttft is None:
                        ttft = now - t0
                    elif last is not None:
                        itls.append(now - last)
                    last = now
        e2e = time.perf_counter() - t0
        if ttft is None:
            return idx, {"error": "no content chunk received"}
        out_len = (usage or {}).get("completion_tokens") or (len(itls) + 1)
        in_len = (usage or {}).get("prompt_tokens")
        return idx, {"ttft_s": ttft, "itls_s": itls, "e2e_s": e2e,
                     "output_len": out_len, "input_len": in_len, "error": None}
    except TimeoutError:
        return idx, {"error": "request timeout"}
    except urllib.error.HTTPError as e:
        return idx, {"error": "http {}".format(e.code)}
    except Exception as e:  # 连接重置/拒绝等
        return idx, {"error": "{}: {}".format(type(e).__name__, e)}


def load_trace(trace_file):
    entries = []
    with open(trace_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit("trace 行解析失败: {}: {}".format(line[:80], e))
            entries.append(entry)
    return entries


def arrival_plan(entries, rng, request_rate):
    """返回 [(target_dt_s, entry), ...]：优先按 trace 的 ts，缺省按泊松到达。"""
    timestamps = [e.get("ts") for e in entries]
    has_ts = all(isinstance(t, (int, float)) for t in timestamps)
    plan = []
    if has_ts:
        t_min = min(timestamps)
        for e, t in zip(entries, timestamps):
            plan.append((max(0.0, t - t_min), e))
        return plan, "trace-timestamps"
    t = 0.0
    for e in entries:
        if t > 0:
            t += (rng.expovariate(request_rate)
                  if request_rate != float("inf") else 0)
        plan.append((t, e))
    return plan, "poisson"


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="服务地址，如 http://127.0.0.1:30000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--trace-file", required=True, help="JSONL trace 文件")
    ap.add_argument("--num-prompts", type=int, default=-1,
                    help="最多重放的行数，-1=全部")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="时间缩放：>1 加快（间隔/time-scale），<1 放慢")
    ap.add_argument("--request-rate", type=float, default=8.0,
                    help="trace 行缺 ts 时的泊松到达速率（req/s）")
    ap.add_argument("--timeout-s", type=float, default=600)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--output-file", required=True)
    args = ap.parse_args()

    if args.time_scale <= 0:
        raise SystemExit("--time-scale 必须 > 0")

    entries = load_trace(args.trace_file)
    if args.num_prompts >= 0 and args.num_prompts > len(entries):
        raise SystemExit(
            "num_prompts({}) 大于 trace 行数({})：判定口径是 completed/num_prompts，"
            "请把 stage.num_prompts 设为 <= trace 行数（或 -1 重放全部）".format(
                args.num_prompts, len(entries)))
    if args.num_prompts >= 0:
        entries = entries[:args.num_prompts]
    if not entries:
        raise SystemExit("trace 为空或 --num-prompts=0")

    rng = random.Random(args.seed)
    raw_plan, arrival_model = arrival_plan(entries, rng, args.request_rate)
    jobs = []
    for dt, entry in raw_plan:
        endpoint, payload = build_payload(entry, args.model)
        jobs.append((dt / args.time_scale, endpoint, payload))

    results = [None] * len(jobs)
    schedule_lags = []
    lock = threading.Lock()
    t_start = time.perf_counter()
    progress = {"done": 0, "last_print": t_start, "last_done": 0}

    def _progress_loop():
        # 客户端原本只在结束打印，这里每 30s（或每完成 20 条）输出一次进度，
        # 便于观察长回放的推进；flush=True 防止经 ssh 管道时被缓冲。
        while True:
            time.sleep(5)
            with lock:
                done = progress["done"]
                if done >= len(jobs):
                    break
                now = time.perf_counter()
                if (done > 0 and done != progress["last_done"]
                        and (now - progress["last_print"] >= 30 or done % 20 == 0)):
                    print("[trace_client] progress: done={}/{} elapsed={:.0f}s".format(
                        done, len(jobs), now - t_start), flush=True)
                    progress["last_print"] = now
                    progress["last_done"] = done

    progress_thread = threading.Thread(target=_progress_loop, daemon=True)
    progress_thread.start()
    print("[trace_client] plan: {} requests, arrival={}, time_scale={}".format(
        len(jobs), arrival_model, args.time_scale), flush=True)
    with ThreadPoolExecutor(max_workers=min(len(jobs), 512)) as pool:
        futures = []
        for i, (dt, endpoint, payload) in enumerate(jobs):
            delay = dt - (time.perf_counter() - t_start)
            if delay > 0:
                time.sleep(delay)
            schedule_lags.append(time.perf_counter() - t_start - dt)
            futures.append(pool.submit(
                one_request, args.url, endpoint, payload, args.timeout_s, i))
        for fut in futures:
            idx, rec = fut.result()
            with lock:
                results[idx] = rec
                progress["done"] += 1
    duration = time.perf_counter() - t_start

    ok = [r for r in results if r and not r.get("error")]
    completed = len(ok)
    ttfts = [r["ttft_s"] for r in ok]
    total_in = sum(r["input_len"] for r in ok if r["input_len"])
    total_out = sum(r["output_len"] for r in ok)
    tpot_per_req = sorted(sum(r["itls_s"]) / len(r["itls_s"])
                          for r in ok if r["itls_s"])
    ttft_sorted = sorted(ttfts)
    e2e_sorted = sorted(r["e2e_s"] for r in ok)

    out = {
        # 与 e2e_client.py 完全同构，collect/judge 可直接消费
        "client": "trace_client.py（真实业务流量重放）",
        "url": args.url,
        "model": args.model,
        "trace_file": args.trace_file,
        "time_scale": args.time_scale,
        "arrival_model": arrival_model,
        "replayed": len(jobs),
        "trace_lines": len(entries),
        "duration": duration,
        "completed": completed,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "request_throughput": completed / duration if duration else 0,
        "input_throughput": total_in / duration if duration else 0,
        "output_throughput": total_out / duration if duration else 0,
        "mean_ttft_ms": (sum(ttfts) / len(ttfts) * 1000) if ttfts else None,
        "median_ttft_ms": (pct(ttft_sorted, 0.5) or 0) * 1000 if ttfts else None,
        "p99_ttft_ms": (pct(ttft_sorted, 0.99) or 0) * 1000 if ttfts else None,
        "mean_tpot_ms": (sum(tpot_per_req) / len(tpot_per_req) * 1000)
                        if tpot_per_req else None,
        "median_tpot_ms": (pct(tpot_per_req, 0.5) or 0) * 1000 if tpot_per_req else None,
        "p99_tpot_ms": (pct(tpot_per_req, 0.99) or 0) * 1000 if tpot_per_req else None,
        "mean_e2e_latency_ms": (sum(e2e_sorted) / len(e2e_sorted) * 1000)
                               if e2e_sorted else None,
        "median_e2e_latency_ms": (pct(e2e_sorted, 0.5) or 0) * 1000 if e2e_sorted else None,
        "p99_e2e_latency_ms": (pct(e2e_sorted, 0.99) or 0) * 1000 if e2e_sorted else None,
        "schedule_lag_ms_max": max(schedule_lags) * 1000 if schedule_lags else None,
        "schedule_lag_ms_mean": (sum(schedule_lags) / len(schedule_lags) * 1000)
                                if schedule_lags else None,
        # 请求级明细（与 bench_serving --output-details / e2e_client 同构，秒单位）
        "ttfts": [(r.get("ttft_s") or 0) if r else 0 for r in results],
        "itls": [(r.get("itls_s") or []) if r else [] for r in results],
        "input_lens": [(r.get("input_len") or 0) if r else 0 for r in results],
        "output_lens": [(r.get("output_len") or 0) if r else 0 for r in results],
        "errors": [(r.get("error") if r else "no result") for r in results],
    }
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    # 尽力而为：同目录有 trace_report.py 时，自动生成 report.md。
    # 失败不影响回放结果本身；服务端日志需要自行用 --server-log 补。
    report_py = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "trace_report.py")
    if os.path.isfile(report_py):
        try:
            spec = importlib.util.spec_from_file_location("trace_report", report_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            report_path = os.path.join(
                os.path.dirname(os.path.abspath(args.output_file)), "report.md")
            sys.argv = ["trace_report.py",
                        "--result", args.output_file,
                        "--model", args.model,
                        "--trace", args.trace_file,
                        "-o", report_path]
            mod.main()
        except Exception as e:
            print("[trace_client] auto report skipped: {}: {}".format(
                type(e).__name__, e), flush=True)

    lag_max = out["schedule_lag_ms_max"]
    print("[trace_client] replayed={}/{} completed={} duration={:.1f}s "
          "arrival={} time_scale={}".format(
              len(jobs), len(entries), completed, duration,
              arrival_model, args.time_scale))
    print("[trace_client] ttft_p99={} lag_max={}ms".format(
        "{:.0f}ms".format(out["p99_ttft_ms"]) if out["p99_ttft_ms"] is not None else "-",
        "{:.1f}".format(lag_max) if lag_max is not None else "-"))


if __name__ == "__main__":
    main()
