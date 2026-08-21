#!/usr/bin/env python3
"""E2E 压测客户端：在「被测机器之外」发起负载，测量路径包含真实网络段。

对齐 framework.md §6：数据面测量从负载发生器发送请求开始，经过网络/Gateway，
直到完整接收响应。与 sglang bench_serving（服务端 loopback）互补：
  - bench_serving：服务端内部性能，排除网络干扰，用于容量/拐点判定
  - e2e_client：客户端真实感知延迟（含网络 RTT），用于 E2E SLO 验收

实现要点：
  - OpenAI /v1/completions，prompt 直接给 token id 列表（无需本地 tokenizer，
    输入长度精确可控，与 bench_serving 的 random-ids 数据集等价）
  - stream=true + include_usage：TTFT=首内容块时间，ITL=内容块间隔，
    token 计数取服务端 usage，不吃本地 tokenizer 误差
  - 开环泊松到达，记录 schedule_lag（协调遗漏自检：客户端跟不上时数据要存疑）
  - 只依赖标准库（urllib + 线程池），Windows 托管 Python 直接可跑

产物与 bench_serving 输出 JSON 同构，run_local_benchmark.sh / collect_results.py
无需改动即可汇总。

用法:
  python3 e2e_client.py --url http://100.64.0.8:30000 --model Qwen/Qwen3-8B \
      --num-prompts 60 --request-rate 8 --input-len 512 --output-len 128 \
      --output-file raw_result.json
"""
import argparse
import json
import random
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

# 压测必须直连被测服务：显式禁用系统代理（Windows 注册表代理会被 urllib 自动拾取，
# 内网/Tailscale 地址经代理转发会得到 502，测量毫无意义）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 避开特殊 token 区间的随机 id（与 bench_serving random-ids 思路一致）
TOKEN_LO, TOKEN_HI = 1000, 30000


def one_request(url, model, prompt_ids, max_tokens, timeout_s, idx):
    body = {
        "model": model,
        "prompt": prompt_ids,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft, itls, usage = None, [], None
    last = None
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
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
                if choices and choices[0].get("text"):
                    if ttft is None:
                        ttft = now - t0
                    elif last is not None:
                        itls.append(now - last)
                    last = now
        e2e = time.perf_counter() - t0
        if ttft is None:
            return idx, {"error": "no content chunk received"}
        out_len = (usage or {}).get("completion_tokens") or (len(itls) + 1)
        return idx, {"ttft_s": ttft, "itls_s": itls, "e2e_s": e2e,
                     "output_len": out_len,
                     "input_len": (usage or {}).get("prompt_tokens") or len(prompt_ids),
                     "error": None}
    except TimeoutError:
        return idx, {"error": "request timeout"}
    except urllib.error.HTTPError as e:
        return idx, {"error": f"http {e.code}"}
    except Exception as e:  # 连接重置/拒绝等
        return idx, {"error": f"{type(e).__name__}: {e}"}


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="服务地址，如 http://100.64.0.8:30000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--num-prompts", type=int, required=True)
    ap.add_argument("--request-rate", type=float, required=True)
    ap.add_argument("--input-len", type=int, default=512)
    ap.add_argument("--output-len", type=int, default=128)
    ap.add_argument("--range-ratio", type=float, default=0.5)
    ap.add_argument("--timeout-s", type=float, default=600)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--output-file", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    def sample_len(base):
        lo = max(1, int(base * args.range_ratio))
        return rng.randint(lo, base)

    jobs = []
    for i in range(args.num_prompts):
        in_len = sample_len(args.input_len)
        out_len = sample_len(args.output_len)
        ids = [rng.randint(TOKEN_LO, TOKEN_HI) for _ in range(in_len)]
        jobs.append((ids, out_len))

    results = [None] * args.num_prompts
    schedule_lags = []
    lock = threading.Lock()
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.num_prompts, 512)) as pool:
        # 开环泊松到达：逐请求按计划时刻提交，记录 schedule lag 做协调遗漏自检
        futures = []
        t_plan = t_start
        for i, (ids, out_len) in enumerate(jobs):
            if i > 0:
                t_plan += (rng.expovariate(args.request_rate)
                           if args.request_rate != float("inf") else 0)
            delay = t_plan - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            schedule_lags.append(time.perf_counter() - t_plan)
            futures.append(pool.submit(one_request, args.url, args.model,
                                       ids, out_len, args.timeout_s, i))
        for fut in futures:
            idx, rec = fut.result()
            with lock:
                results[idx] = rec
    duration = time.perf_counter() - t_start

    ok = [r for r in results if r and not r.get("error")]
    completed = len(ok)
    ttfts = [r["ttft_s"] for r in ok]
    itls = [r["itls_s"] for r in ok]
    total_in = sum(r["input_len"] for r in ok)
    total_out = sum(r["output_len"] for r in ok)
    tpot_per_req = sorted(sum(r["itls_s"]) / len(r["itls_s"])
                          for r in ok if r["itls_s"])
    ttft_sorted = sorted(ttfts)
    e2e_sorted = sorted(r["e2e_s"] for r in ok)

    out = {
        "client": "e2e_client.py（含网络段的真实 E2E 测量）",
        "url": args.url,
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
        # 请求级明细（与 bench_serving --output-details 同构，秒单位；失败请求占位置 0）
        "ttfts": [(r.get("ttft_s") or 0) if r else 0 for r in results],
        "itls": [(r.get("itls_s") or []) if r else [] for r in results],
        "input_lens": [(r.get("input_len") or 0) if r else 0 for r in results],
        "output_lens": [(r.get("output_len") or 0) if r else 0 for r in results],
        "errors": [(r.get("error") if r else "no result") for r in results],
    }
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    lag_max = out['schedule_lag_ms_max']
    print(f"[e2e_client] completed={completed}/{args.num_prompts} "
          f"duration={duration:.1f}s "
          f"ttft_p99={out['p99_ttft_ms']:.0f}ms " if out['p99_ttft_ms'] is not None
          else f"[e2e_client] completed={completed}/{args.num_prompts} duration={duration:.1f}s ttft_p99=- ", end="")
    print(f"lag_max={lag_max:.1f}ms" if lag_max is not None else "")


if __name__ == "__main__":
    main()
