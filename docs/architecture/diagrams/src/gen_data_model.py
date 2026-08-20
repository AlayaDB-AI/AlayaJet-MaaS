#!/usr/bin/env python3
"""数据模型图：4 排 × 5 列；字号加大（FS=13），标题拆两行以收窄框宽、压缩画布空白。"""
import math

BK = "#111827"
CTL, RES, REQ = "#FDEBD3", "#DCE9F7", "#DFF0D8"   # 模型控制 / 资源管理 / 服务请求
FONT = "system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"
FS = 13
EW = 176
W, H = 1256, 950

P = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">',
     f'<rect width="{W}" height="{H}" rx="10" fill="#FFFFFF"/>']

def text(x, y, s, anchor="start", weight="normal"):
    P.append(f'<text x="{x}" y="{y}" font-size="{FS}" fill="{BK}" '
             f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>')

def rect(x, y, w, h, fill="#FFFFFF", rx=6, sw=1.3, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    P.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
             f'fill="{fill}" stroke="{BK}" stroke-width="{sw}"{d}/>')

def linee(x1, y1, x2, y2, sw=1.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    P.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{BK}" '
             f'stroke-width="{sw}"{d}/>')

def head(x, y, ang, s=10):
    x1, y1 = x - s*math.cos(ang-0.40), y - s*math.sin(ang-0.40)
    x2, y2 = x - s*math.cos(ang+0.40), y - s*math.sin(ang+0.40)
    P.append(f'<polygon points="{x},{y} {x1:.1f},{y1:.1f} {x2:.1f},{y2:.1f}" fill="{BK}"/>')

def mid(x, y, s):
    text(x, y, s, anchor="middle")

HH = 46            # 标题区（两行）高度
def entity(x, y, name, holder, fields, fill):
    h = HH + len(fields) * 18 + 12
    rect(x, y, EW, h, "#FFFFFF")
    rect(x, y, EW, HH, fill, rx=6)
    rect(x, y + HH - 8, EW, 8, fill, rx=0, sw=0)
    linee(x, y + HH, x + EW, y + HH, 1.3)
    text(x + EW/2, y + 19, name, anchor="middle", weight="bold")
    text(x + EW/2, y + 37, holder, anchor="middle")
    for i, f in enumerate(fields):
        text(x + 11, y + HH + 17 + i * 18, f)
    return {"x": x, "y": y, "w": EW, "h": h, "cx": x + EW/2, "cy": y + h/2,
            "r": x + EW, "b": y + h}

def hlink(a, b, label, ca="1", cb="N", dash=None):
    y = a["cy"]
    linee(a["r"], y, b["x"] - 4, y, dash=dash); head(b["x"], y, 0, 9 if dash else 10)
    mid((a["r"] + b["x"]) / 2, y + 19, label)
    mid(a["r"] + 18, y - 10, ca); mid(b["x"] - 18, y - 10, cb)

def hlink_rev(a, b, label, ca="1", cb="N", dash=None):
    y = a["cy"]
    linee(a["x"], y, b["r"] + 4, y, dash=dash); head(b["r"], y, math.pi, 9 if dash else 10)
    mid((a["x"] + b["r"]) / 2, y + 19, label)
    mid(a["x"] - 18, y - 10, ca); mid(b["r"] + 18, y - 10, cb)

def vlink(a, b, label, ca="1", cb="N", dash=None):
    x = a["cx"]
    linee(x, a["b"], x, b["y"] - 4, dash=dash); head(x, b["y"], math.pi/2, 9 if dash else 10)
    text(x + 13, (a["b"] + b["y"]) / 2 + 5, label)
    mid(x - 17, a["b"] + 19, ca); mid(x - 17, b["y"] - 12, cb)

# ==================== 栅格 ====================
CP = EW + 68
C1, C2, C3, C4, C5 = 52, 52+CP, 52+2*CP, 52+3*CP, 52+4*CP
R1, R2, R3, R4 = 80, 284, 470, 656

# 排 1
ma = entity(C1, R1, "模型资产", "@Control Plane",
            ["model_id", "revision（不可变）", "权重地址 · 内容摘要", "登记人 · 登记时间"], CTL)
pf = entity(C2, R1, "配置记录", "@Control Plane",
            ["profile_id · revision", "model 段（公共）", "prefill 段 · decode 段",
             "固化的别名版本"], CTL)
rl = entity(C3, R1, "发布记录", "@Control Plane",
            ["release_id", "profile_ref", "prefill_instances", "decode_instances",
             "创建人 · 创建时间"], CTL)
sv = entity(C4, R1, "模型服务", "@Control Plane",
            ["service_id", "current_release", "（当前生效）", "desired_state"], CTL)
rq = entity(C5, R1, "请求", "@Gateway",
            ["request_id", "tenant_ref（透传）", "service_id"], REQ)

# 排 2
pp = entity(C3, R2, "prefill / decode 池", "@OME",
            ["pool_id", "role：engine | decoder"], CTL)
it = entity(C4, R2, "实例", "@K8s（LWS 组）",
            ["instance_id", "role", "size：机器数", "（单机 = 1）"], CTL)
at = entity(C5, R2, "执行尝试", "@Gateway",
            ["attempt_no", "instance_id", "engine_rid", "结果 / 失败原因"], REQ)

# 排 3
wr = entity(C1, R3, "节点权重副本", "@OME",
            ["node_name", "model revision", "状态：ready / 下载中"], RES)
nd = entity(C2, R3, "Node", "@K8s",
            ["node_name", "admission_state", "instance_class", "serving_pool"], RES)
gp = entity(C3, R3, "GPU", "@DRA",
            ["uuid", "product_name · 显存", "运维别名（审核关联）"], RES)
pd = entity(C4, R3, "Pod", "@K8s",
            ["pod_uid", "endpoint IP:Port", "claim：整机 8 卡"], RES)
ur = entity(C5, R3, "用量记录", "@Usage Ledger",
            ["request_id（1:1）", "input · cached_input", "generated · delivered",
             "finish_reason"], REQ)

# 排 4
sb = entity(C1, R4, "权重来源", "@管理机",
            ["模型官方发布 / HF 仓库", "运维下载 · 计算摘要"], "#FFFFFF")
ue = entity(C5, R4, "用量事件", "@Kafka",
            ["event_id（幂等键）", "correction_of", "region", "schema_version"], REQ)

# ==================== 关系 ====================
_lc = 26
linee(sb["x"], sb["cy"], _lc, sb["cy"])
linee(_lc, sb["cy"], _lc, ma["cy"])
linee(_lc, ma["cy"], ma["x"] - 4, ma["cy"]); head(ma["x"], ma["cy"], 0)
text(_lc + 10, sb["y"] - 42, "登记")
mid(_lc + 16, sb["cy"] - 12, "1"); mid(_lc + 16, ma["cy"] + 22, "1")

hlink(ma, pf, "配置")
hlink(pf, rl, "发布")
hlink(rl, sv, "生效", "N", "1")
hlink_rev(rq, sv, "引用", "N", "1", dash="5,4")
vlink(rl, pp, "生成", "1", "2")
hlink(pp, it, "包含")
hlink_rev(at, it, "执行于", "N", "1", dash="5,4")
vlink(it, pd, "拆分为", "1", "N")
vlink(ma, wr, "分发", "1", "N")
hlink(wr, nd, "预热于", "N", "1", dash="5,4")
hlink(nd, gp, "装有")
hlink_rev(pd, gp, "claim", "1", "8")

_ndy = pd["b"] + 42
linee(pd["cx"], pd["b"], pd["cx"], _ndy)
linee(pd["cx"], _ndy, nd["cx"], _ndy)
linee(nd["cx"], _ndy, nd["cx"], nd["b"] + 4); head(nd["cx"], nd["b"], -math.pi/2)
mid((pd["cx"] + nd["cx"]) / 2, _ndy - 10, "绑定（当前独占整机）")
mid(pd["cx"] - 17, pd["b"] + 19, "1"); mid(nd["cx"] + 17, nd["b"] + 19, "1")

vlink(rq, at, "重试产生", "1", "N")
vlink(at, ur, "汇总", "N", "1")
vlink(ur, ue, "投递", "1", "N")

# ==================== 图例 ====================
LY = 826
rect(52, LY - 12, 24, 15, CTL, rx=3)
text(88, LY, "模型控制：模型资产 → 配置 → 发布 → 服务，以及发布出的池与实例")
rect(52, LY + 26, 24, 15, RES, rx=3)
text(88, LY + 38, "资源管理：权重来源、机器、GPU 与落到机器上的 Pod")
rect(52, LY + 64, 24, 15, REQ, rx=3)
text(88, LY + 76, "服务请求：一次调用产生的请求、执行尝试、用量记录与事件")

P.append("</svg>")
svg = "\n".join(P)
open("../data_model.svg", "w").write(svg)
print("written:", len(svg), "bytes")
