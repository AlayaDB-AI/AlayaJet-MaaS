#!/usr/bin/env python3
"""把「会话数据1.ndjson」这类真实会话日志转成 trace 重放文件。

输入日志（JSONL，每行一个事件/流式 chunk）的关键字段：
  conversation_id / sequence / event_type / direction / role / content /
  created_at / model_name / prompt_tokens / completion_tokens

转换规则（与生产日志口径相关，换日志格式时改这里）：
  1. 按 (conversation_id, sequence, event_type) 分组，把同一事件被拆开的流式
     chunk 合并成一条：若后一行 content 以已合并内容开头（累计快照）取最长行，
     否则按到达顺序拼接。
  2. 按 (sequence, created_at) 排序，重建每个 user_message 之前的完整消息历史：
     system_message -> system；developer_message -> developer；
     assistant_message -> assistant；tool_call 扁平化为 assistant 的
     "[工具调用] <json>"；tool_result 扁平化为 user 的 "[工具返回] <content>"
     （避免回放时缺 tool_call_id 报错）；reasoning_marker / request_aborted 丢弃。
  3. 每个 user_message 输出一行 trace：
     ts=created_at，endpoint=chat，body.messages=截至该轮的历史，
     body.max_tokens=紧随其后 assistant 回复的 completion_tokens（还原真实生成长度）。
  4. 脱敏（默认开启）：conversation_id/turn_id/request_id 哈希；内容中的邮箱、
     手机号、身份证、IP、密钥、URL、连续长数字统一替换为占位符。

注意：日志里没有采样参数（temperature 等），无法还原「哪些请求当时显式传过自定义
值」。转换器默认不注入任何采样参数（--params "{}"），完全交给服务端按引擎/模型
默认值采样；若要复刻网关的统一参数画像，用 --params 显式指定。

用法:
  python3 log_to_trace.py 会话数据1.ndjson -o business-trace.jsonl
  python3 log_to_trace.py 会话数据1.ndjson -o business-trace.jsonl \
      --max-requests 500 --cap-output-len 4096 --no-deidentify
  # 只截取开头 15 分钟的连续流量（适合短时冒烟，offset 相对原始最小时间戳）
  python3 log_to_trace.py 会话数据1.ndjson -o business-15min.jsonl --window-s 900
"""
import argparse
import collections
import hashlib
import json
import random
import re
import sys


def redact(text):
    """脱敏：把可识别个人的内容替换为占位符，尽量保持文本长度形状。"""
    if not isinstance(text, str):
        return text
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    # 手机号（含 +86）、座机、400 电话
    text = re.sub(r"(?:\+?86[- ]?)?1[3-9]\d{9}", "[PHONE]", text)
    text = re.sub(r"\b0\d{2,3}[- ]\d{7,8}\b", "[PHONE]", text)
    text = re.sub(r"400[- ]?\d{3}[- ]?\d{4}", "[PHONE]", text)
    # 身份证（18/15 位）
    text = re.sub(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b", "[ID]", text)
    text = re.sub(r"\b\d{6}\d{9}\b", "[ID]", text)
    # IPv4
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", text)
    # 密钥 / token
    text = re.sub(r"\b(sk|pk|ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_-]{8,}\b", "[KEY]", text)
    text = re.sub(r"\b(?:Bearer|token=)[A-Za-z0-9._-]{16,}\b", "[KEY]", text)
    # URL
    text = re.sub(r"https?://[^\s\"'<>，。；：）】]+", "[URL]", text)
    # 连续长数字（订单号/银行卡等，保留短数字与时间戳形态以外的长串）
    text = re.sub(r"\b\d{9,}\b", "[NUM]", text)
    return text


def hash_id(value, salt="alayajet-trace"):
    return hashlib.sha256((salt + str(value)).encode("utf-8")).hexdigest()[:12]


def merge_group(rows):
    """同一事件的流式 chunk 合并：累计快照取最长，否则顺序拼接。"""
    rows = sorted(rows, key=lambda r: (r.get("created_at") or 0, r.get("sequence") or 0))
    merged = ""
    for r in rows:
        content = r.get("content") or ""
        if content.startswith(merged) and content:
            merged = content
        else:
            merged += content
    last = rows[-1]
    return {**last, "content": merged}


EVENT_ROLE = {
    "system_message": "system",
    "developer_message": "developer",
    "user_message": "user",
    "assistant_message": "assistant",
}


def build_history(events, deidentify=True):
    messages = []
    for e in events:
        et = e.get("event_type")
        content = e.get("content") or ""
        if et in EVENT_ROLE:
            messages.append({"role": EVENT_ROLE[et], "content": content})
        elif et == "tool_call":
            messages.append({"role": "assistant", "content": "[工具调用] " + content})
        elif et == "tool_result":
            messages.append({"role": "user", "content": "[工具返回] " + content})
        # reasoning_marker / request_aborted：跳过
    if deidentify:
        for m in messages:
            m["content"] = redact(m["content"])
    return messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log_file", help="会话日志 JSONL 文件")
    ap.add_argument("-o", "--output", required=True, help="输出 trace JSONL 路径")
    ap.add_argument("--max-requests", type=int, default=-1,
                    help="最多输出多少条重放请求（-1=全部；超出则按时间均匀抽样）")
    ap.add_argument("--start-offset-s", type=float, default=0.0,
                    help="只保留 ts >= 原始最小 ts + 该偏移（秒）的请求，用于截取时间窗口")
    ap.add_argument("--window-s", type=float, default=None,
                    help="从窗口起点往后保留多少秒，配合 --start-offset-s 截取一段连续流量")
    ap.add_argument("--cap-output-len", type=int, default=None,
                    help="max_tokens 上限；默认按原始回复长度，不做截断")
    ap.add_argument("--params", type=str,
                    default="{}",
                    help="注入到每条请求的采样参数画像（JSON）；默认 \"{}\" 即不注入、"
                         "让服务端用引擎/模型默认值，如需统一画像例如 '{\"temperature\":0.7}'")
    ap.add_argument("--max-history-turns", type=int, default=None,
                    help="每条请求历史最多保留的轮数（None=完整历史）")
    ap.add_argument("--deidentify", dest="deidentify", action="store_true", default=True)
    ap.add_argument("--no-deidentify", dest="deidentify", action="store_false",
                    help="关闭脱敏（仅限测试环境，输出会含真实数据！）")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise SystemExit("--params 必须是 JSON 对象，例如 '{\"temperature\": 0.7}'")

    # 读入并按会话/事件分组
    groups = collections.defaultdict(list)
    convs = collections.defaultdict(list)
    with open(args.log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (rec.get("conversation_id"), rec.get("sequence"), rec.get("event_type"))
            groups[key].append(rec)

    events_by_conv = collections.defaultdict(list)
    for key, rows in groups.items():
        conv_id = key[0]
        events_by_conv[conv_id].append(merge_group(rows))

    # 每个会话内排序，并找到每个 user_message 之后的 assistant 回复长度
    requests = []
    for conv_id, events in events_by_conv.items():
        events.sort(key=lambda e: (e.get("sequence") or 0, e.get("created_at") or 0))
        for idx, e in enumerate(events):
            if e.get("event_type") != "user_message":
                continue
            # 该行的 completion_tokens 已冗余记录本轮的回复长度；
            # 缺失时再向后找紧随的 assistant_message 兜底。
            reply_len = e.get("completion_tokens")
            if not reply_len:
                for later in events[idx + 1:]:
                    if later.get("event_type") == "user_message":
                        break
                    if later.get("event_type") == "assistant_message":
                        reply_len = later.get("completion_tokens")
                        break
            history = build_history(events[:idx + 1], deidentify=args.deidentify)
            if args.max_history_turns:
                history = history[-args.max_history_turns:]
            body = {"messages": history}
            if params:
                body.update(params)
            if reply_len:
                body["max_tokens"] = reply_len
                if args.cap_output_len and reply_len > args.cap_output_len:
                    body["max_tokens"] = args.cap_output_len
            requests.append({
                "ts": e.get("created_at") or 0,
                "endpoint": "chat",
                "body": body,
                "source_conversation": hash_id(conv_id) if args.deidentify else conv_id,
                "source_model": e.get("model_name"),
                "source_turn": e.get("turn_id") if not args.deidentify else None,
            })

    requests.sort(key=lambda r: r["ts"])
    if requests:
        t_min = requests[0]["ts"]
        t_start = t_min + args.start_offset_s
        t_end = None if args.window_s is None else t_start + args.window_s
        requests = [r for r in requests
                    if r["ts"] >= t_start and (t_end is None or r["ts"] <= t_end)]
    if not requests:
        raise SystemExit("时间窗口内没有请求，请检查 --start-offset-s / --window-s")
    if args.max_requests >= 0 and len(requests) > args.max_requests:
        rng = random.Random(args.seed)
        requests = sorted(rng.sample(requests, args.max_requests), key=lambda r: r["ts"])

    with open(args.output, "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_msgs = sum(len(r["body"]["messages"]) for r in requests)
    print("converted {} replay requests -> {}".format(len(requests), args.output))
    print("  avg history messages/request: {:.1f}".format(n_msgs / len(requests)) if requests else "-")
    print("  deidentify: {}".format("on" if args.deidentify else "OFF"))


if __name__ == "__main__":
    main()
