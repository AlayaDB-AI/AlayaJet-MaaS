#!/usr/bin/env python3
"""AlayaJet 质量评测 runner（docs/evaluation/framework.md §2 的 quality_pass 执行器）。

对部署好的 OpenAI 兼容服务执行质量评测，产出结构化分数。
冻结解码参数：temperature=0、关闭 thinking（Qwen3）、固定 prompt 模板。

suite:
  niah          大海捞针（本地合成长上下文，无需下载数据）
  gsm8k         数学推理（HF openai/gsm8k test 子集，数值精确匹配）
  ifeval        指令遵循（HF google/IFEval，官方规则判分，vendor/ifeval）
  longbench_v2  长文理解（HF THUDM/LongBench-v2，多选题，选项字母匹配）

用法:
  python3 quality_runner.py --suite niah --output-dir <dir> [--num-samples N]
环境:
  QUALITY_BASE_URL  默认 http://127.0.0.1:30000/v1
  QUALITY_MODEL     默认 Qwen/Qwen3-8B
"""
import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_URL = os.environ.get("QUALITY_BASE_URL", "http://127.0.0.1:30000/v1")
MODEL = os.environ.get("QUALITY_MODEL", "Qwen/Qwen3-8B")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "vendor"))

# ---------------------------------------------------------------- 推理调用

def chat(session, prompt, max_tokens, system=None, retries=3):
    """冻结解码参数的 chat 调用，返回 (content, usage) 或 (None, error)。"""
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    # Qwen3 支持 enable_thinking 开关（关闭 thinking 降低判分噪声）；
    # 其他模型模板不认识该 kwarg，传入可能直接 400。
    if "qwen3" in MODEL.lower():
        body["chat_template_kwargs"] = {"enable_thinking": False}
    for attempt in range(retries):
        try:
            r = session.post(f"{BASE_URL}/chat/completions", json=body, timeout=600)
            if r.status_code == 200:
                d = r.json()
                return d["choices"][0]["message"]["content"], d.get("usage")
        except (requests.RequestException, ValueError, KeyError):
            pass
        time.sleep(2 * (attempt + 1))
    return None, "request_failed"


def run_items(items, worker, concurrency):
    """items: list[dict]，worker(item)->dict(结果字段)，并发执行并保持顺序。"""
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for idx, res in zip(range(len(items)), ex.map(worker, items)):
            results[idx] = res
    return results


# ---------------------------------------------------------------- 判分工具

def extract_number(text):
    """GSM8K：优先 #### 后的数字，否则最后一个数字。返回规范化字符串或 None。"""
    if not text:
        return None
    m = re.search(r"####\s*([\-0-9.,]+)", text)
    cand = m.group(1) if m else None
    if cand is None:
        nums = re.findall(r"-?\d[\d,]*\.?\d*", text)
        cand = nums[-1] if nums else None
    if cand is None:
        return None
    return cand.replace(",", "").rstrip(".")


def norm_number(s):
    try:
        f = float(str(s).replace(",", ""))
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return None


def extract_choice(text):
    """LongBench v2：提取选项字母 A-D。"""
    if not text:
        return None
    m = re.search(r"\b([A-D])\b", text.strip())
    return m.group(1) if m else None


# ---------------------------------------------------------------- NIAH

NIAH_FILLER = (
    "The quarterly report describes ordinary operations across regional offices. "
    "Staff members discussed logistics, scheduling, and routine maintenance tasks. "
    "No significant events were recorded during this period of observation. "
)
NIAH_DEPTHS = [0.0, 0.25, 0.5, 0.75, 1.0]
NIAH_LENGTHS = [4000, 8000, 16000, 32000]  # 目标 token 数（按 4 字符/token 近似）


def niah_make(length_tokens, depth, needle, question):
    target_chars = length_tokens * 4
    unit = NIAH_FILLER
    reps = target_chars // len(unit) + 1
    haystack = unit * reps
    pos = int(len(haystack) * depth)
    text = haystack[:pos] + "\n" + needle + "\n" + haystack[pos:]
    return (
        "Read the following document and answer the question.\n\n"
        + text
        + f"\n\nQuestion: {question}\nAnswer with only the magic number."
    )


def run_niah(args, session):
    rng = random.Random(42)
    items = []
    for L in NIAH_LENGTHS:
        for d in NIAH_DEPTHS:
            magic = rng.randint(10000, 99999)
            needle = f"The special magic number mentioned in the report is {magic}."
            items.append({
                "length": L, "depth": d, "expected": str(magic),
                "prompt": niah_make(L, d, needle, "What is the special magic number?"),
            })
    if args.num_samples:
        items = items[: args.num_samples]

    def worker(it):
        t0 = time.time()
        content, usage = chat(session, it["prompt"], max_tokens=32)
        pred = extract_number(content)
        return {**{k: it[k] for k in ("length", "depth", "expected")},
                "response": (content or "")[:200], "pred": pred,
                "correct": pred == it["expected"],
                "latency_s": round(time.time() - t0, 2), "error": content is None}

    results = run_items(items, worker, args.concurrency)
    per_len = {}
    for r in results:
        per_len.setdefault(r["length"], []).append(r["correct"])
    return {
        "score": sum(r["correct"] for r in results) / max(len(results), 1),
        "n": len(results),
        "per_length": {str(k): sum(v) / len(v) for k, v in per_len.items()},
    }, results


# ---------------------------------------------------------------- GSM8K

GSM8K_PROMPT = (
    "Solve the following grade-school math problem step by step. "
    "End your answer with '#### <final number>'.\n\nProblem: {q}"
)


def run_gsm8k(args, session):
    from datasets import load_dataset

    ds = load_dataset("openai/gsm8k", "main", split="test")
    n = args.num_samples or 100
    ds = ds.shuffle(seed=42).select(range(min(n, len(ds))))
    items = [{"question": r["question"],
              "expected": norm_number(r["answer"].split("####")[-1].strip())}
             for r in ds]

    def worker(it):
        t0 = time.time()
        content, usage = chat(session, GSM8K_PROMPT.format(q=it["question"]),
                              max_tokens=1024)
        pred = norm_number(extract_number(content))
        return {"question": it["question"][:120], "expected": it["expected"],
                "response": (content or "")[-300:], "pred": pred,
                "correct": pred is not None and pred == it["expected"],
                "latency_s": round(time.time() - t0, 2), "error": content is None}

    results = run_items(items, worker, args.concurrency)
    return {"score": sum(r["correct"] for r in results) / max(len(results), 1),
            "n": len(results)}, results


# ---------------------------------------------------------------- IFEval

def run_ifeval(args, session):
    from datasets import load_dataset
    from ifeval import instructions_registry

    ds = load_dataset("google/IFEval", split="train")
    if args.num_samples:
        ds = ds.select(range(min(args.num_samples, len(ds))))

    def worker(r):
        t0 = time.time()
        content, usage = chat(session, r["prompt"], max_tokens=1024)
        resp = content or ""
        checks = []
        for inst_id, kwargs in zip(r["instruction_id_list"], r["kwargs"]):
            cls = instructions_registry.INSTRUCTION_DICT[inst_id]
            inst = cls(inst_id)
            inst.build_description(**(kwargs or {}))
            checks.append(bool(resp and inst.check_following(resp)))
        return {"key": r["key"], "n_instructions": len(checks),
                "n_followed": sum(checks), "all_followed": all(checks),
                "response": resp[-300:], "latency_s": round(time.time() - t0, 2),
                "error": content is None}

    results = run_items(list(ds), worker, args.concurrency)
    total_inst = sum(r["n_instructions"] for r in results)
    return {
        # instruction-level strict accuracy（官方口径之一）
        "score": sum(r["n_followed"] for r in results) / max(total_inst, 1),
        "prompt_level_strict": sum(r["all_followed"] for r in results) / max(len(results), 1),
        "n": len(results), "n_instructions": total_inst,
    }, results


# ---------------------------------------------------------------- LongBench v2

LBV2_PROMPT = (
    "You are given a long context below. Answer the multiple-choice question "
    "based on the context. Respond with only the letter of the correct option "
    "(A, B, C, or D).\n\nContext:\n{ctx}\n\nQuestion: {q}\n"
    "A. {a}\nB. {b}\nC. {c}\nD. {d}\n\nAnswer:"
)
LBV2_MAX_CHARS = 30000 * 4  # 只测能装进 32k 上下文窗口的样本（留输出余量）


def run_longbench_v2(args, session):
    from datasets import load_dataset

    ds = load_dataset("THUDM/LongBench-v2", split="train")
    # 只保留装得进当前上下文窗口的样本
    ds = ds.filter(lambda r: len(r["context"]) <= LBV2_MAX_CHARS)
    n = args.num_samples or 100
    ds = ds.shuffle(seed=42).select(range(min(n, len(ds))))

    def worker(r):
        t0 = time.time()
        prompt = LBV2_PROMPT.format(ctx=r["context"], q=r["question"],
                                    a=r["choice_A"], b=r["choice_B"],
                                    c=r["choice_C"], d=r["choice_D"])
        content, usage = chat(session, prompt, max_tokens=16)
        pred = extract_choice(content)
        return {"_id": r["_id"], "domain": r["domain"],
                "difficulty": r["difficulty"], "expected": r["answer"],
                "response": (content or "")[:100], "pred": pred,
                "correct": pred == r["answer"],
                "context_chars": len(r["context"]),
                "latency_s": round(time.time() - t0, 2), "error": content is None}

    results = run_items(list(ds), worker, args.concurrency)
    by_diff = {}
    for r in results:
        by_diff.setdefault(r["difficulty"], []).append(r["correct"])
    return {
        "score": sum(r["correct"] for r in results) / max(len(results), 1),
        "n": len(results),
        "per_difficulty": {k: sum(v) / len(v) for k, v in by_diff.items()},
        "note": f"仅含 context <= {LBV2_MAX_CHARS} 字符（适配 32k 窗口）的样本",
    }, results


# ---------------------------------------------------------------- 主流程

SUITES = {
    "niah": run_niah,
    "gsm8k": run_gsm8k,
    "ifeval": run_ifeval,
    "longbench_v2": run_longbench_v2,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=SUITES.keys())
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--num-samples", type=int, default=None,
                    help="调试用：限制样本数（默认各 suite 的正式规模）")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    suite_dir = os.path.join(args.output_dir, "suites", args.suite)
    os.makedirs(suite_dir, exist_ok=True)

    session = requests.Session()
    print(f"[{args.suite}] base_url={BASE_URL} model={MODEL} "
          f"samples={args.num_samples or 'default'}", flush=True)
    t0 = time.time()
    summary, results = SUITES[args.suite](args, session)
    summary["suite"] = args.suite
    summary["model"] = MODEL
    summary["elapsed_s"] = round(time.time() - t0, 1)

    with open(os.path.join(suite_dir, "results.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(suite_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[{args.suite}] score={summary['score']:.4f} n={summary['n']} "
          f"elapsed={summary['elapsed_s']}s", flush=True)


if __name__ == "__main__":
    main()
