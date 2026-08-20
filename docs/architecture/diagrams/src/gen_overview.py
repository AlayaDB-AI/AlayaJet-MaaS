#!/usr/bin/env python3
"""总架构图 v9：全图单一字号；箭头加粗放大；Kueue 移到 OME 左侧；
gateway 左右箭头标签统一为"传输内容"；GPU 机器内 Pod 在上、K8s 节点代理在下（与平台节点一致）。"""
import math

W, H = 1400, 850
BK = "#111827"
KBG, PBG, EBG, OBG, SBG = "#DCE9F7", "#EEF4FB", "#DFF0D8", "#EDE4F6", "#FDEBD3"
GBG = "#E8EAED"
FONT = "system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"
FS = 9.5          # 全图统一字号

P = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">',
     f'<rect width="{W}" height="{H}" rx="10" fill="#FFFFFF"/>']

def text(x, y, s, anchor="middle", weight="normal"):
    P.append(f'<text x="{x}" y="{y}" font-size="{FS}" fill="{BK}" '
             f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>')

def rect(x, y, w, h, fill="#FFFFFF", rx=6, sw=1.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    P.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
             f'fill="{fill}" stroke="{BK}" stroke-width="{sw}"{d}/>')

def hw(x, y, w, h, rx=10):
    rect(x, y, w, h, "#FFFFFF", rx=rx, sw=2.4)

def linee(x1, y1, x2, y2, sw=1.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    P.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{BK}" '
             f'stroke-width="{sw}"{d}/>')

def head(x, y, ang, s=9):
    x1, y1 = x - s*math.cos(ang-0.40), y - s*math.sin(ang-0.40)
    x2, y2 = x - s*math.cos(ang+0.40), y - s*math.sin(ang+0.40)
    P.append(f'<polygon points="{x},{y} {x1:.1f},{y1:.1f} {x2:.1f},{y2:.1f}" fill="{BK}"/>')

def dot(x, y):
    P.append(f'<circle cx="{x}" cy="{y}" r="3.6" fill="{BK}"/>')

def chip(x, y, w, h, label, fill, sw=1.2):
    rect(x, y, w, h, fill, rx=5, sw=sw)
    text(x + w/2, y + h/2 + 3.4, label, weight="bold")

def pill(x, y, w, h, label):
    rect(x, y, w, h, "#FFFFFF", rx=h/2, sw=1.8)
    text(x + w/2, y + h/2 + 3.4, label, weight="bold")

# ==================== 平台节点 ====================
CY, CH = 96, 252
hw(76, CY - 16, 1024, CH); hw(68, CY - 8, 1024, CH); hw(60, CY, 1024, CH)
text(80, CY + 20, "平台节点", anchor="start", weight="bold")

# — 普通 Pod
rect(76, CY + 28, 992, 124, PBG, rx=8, dash="5,4")
text(88, CY + 44, "普通 Pod", anchor="start", weight="bold")
GW_X, GW_W, GW_Y, GW_H = 392, 360, CY + 52, 38
GC, GWM = GW_X + GW_W/2, CY + 71          # gateway 中心 x、箭头基准 y
chip(GW_X, GW_Y, GW_W, GW_H, "sgl-model-gateway", EBG, sw=2.2)

RY, RH = CY + 100, 44
KA_X, KA_W = 92, 96
LG_X, LG_W = 216, 170
PO_X, PO_W = 410, 118
KQ_X, KQ_W = 552, 136
OM_X, OM_W = 728, 88
CP_X, CP_W = 872, 180
chip(KA_X, RY, KA_W, RH, "Kafka", GBG)
chip(LG_X, RY, LG_W, RH, "Usage Ledger", SBG)
chip(PO_X, RY + 1, PO_W, 20, "Prometheus", GBG)
chip(PO_X, RY + 23, PO_W, 20, "OTel", GBG)
chip(KQ_X, RY, KQ_W, RH, "Kueue（gang）", KBG)
chip(OM_X, RY, OM_W, RH, "OME", OBG)
chip(CP_X, RY, CP_W, RH, "Control Plane", SBG)
linee(LG_X, RY + 22, KA_X + KA_W + 3, RY + 22); head(KA_X + KA_W, RY + 22, math.pi, 7)
linee(CP_X, RY + 22, OM_X + OM_W + 3, RY + 22); head(OM_X + OM_W, RY + 22, math.pi, 7)
text((OM_X + OM_W + CP_X)/2, RY + 12, "写 CR")
# gateway 左：日志 · usage → Usage Ledger；右：转发请求 → 两池
LGC = LG_X + LG_W/2
linee(GW_X, GWM, LGC, GWM)
linee(LGC, GWM, LGC, RY - 3); head(LGC, RY, math.pi/2, 7)
text((GW_X + LGC)/2, GWM - 8, "日志 · usage")
linee(GW_X + GW_W, GWM, 1112, GWM)
text(GW_X + GW_W + 12, GWM - 8, "转发请求 · 直连 Pod IP", anchor="start")

# — static Pod
rect(76, CY + 160, 992, 76, PBG, rx=8, dash="5,4")
text(88, CY + 176, "static Pod", anchor="start", weight="bold")
KY, KH = CY + 182, 48
AS_X, AS_W = 92, 220
chip(AS_X, KY, AS_W, KH, "API Server", KBG)
chip(352, KY, 160, KH, "etcd", KBG)
chip(552, KY, 200, KH, "Scheduler", KBG)
chip(792, KY, 260, KH, "Controller Manager", KBG)
linee(AS_X + AS_W, KY + 24, 352, KY + 24, 1.2)
ASC, OMC = AS_X + AS_W/2, OM_X + OM_W/2
linee(OMC, RY + RH, OMC, CY + 170)
linee(OMC, CY + 170, ASC, CY + 170)
linee(ASC, CY + 170, ASC, KY - 3); head(ASC, KY, math.pi/2, 7)
text(OMC + 12, CY + 158, "写对象", anchor="start")

# ==================== 外部交互 ====================
pill(GC - 78, 8, 156, 30, "上游业务平台")
linee(GC, 38, GC, GW_Y - 3); head(GC, GW_Y, math.pi/2)
text(GC + 92, 19, "Inference API", anchor="start")
text(GC + 92, 34, "推理请求（OpenAI 兼容 · 流式 / 非流式）", anchor="start")

KC = KA_X + KA_W/2
pill(KC - 78, 8, 156, 30, "上游计费 / 消费方")
linee(KC, RY, KC, 41); head(KC, 38, -math.pi/2)
text(KC + 92, 19, "Usage Events（Kafka）", anchor="start")
text(KC + 92, 34, "用量事件 · 修正事件", anchor="start")

pill(960, 8, 140, 30, "平台运维")
CPC = CP_X + CP_W/2
linee(1030, 38, 1030, 64); linee(1030, 64, CPC, 64)
linee(CPC, 64, CPC, RY - 3); head(CPC, RY, math.pi/2)
text(1112, 19, "Management API", anchor="start")
text(1112, 34, "发布 / 暂停 / 扩缩 · 节点准入", anchor="start")

# ==================== GPU 池 ====================
def machine(mx, my, name):
    mw, mh = 168, 200
    hw(mx, my, mw, mh, rx=8)
    text(mx + mw/2, my + 18, name, weight="bold")
    ix, iw = mx + 10, mw - 20
    rect(ix, my + 26, iw, 88, PBG, rx=7, dash="5,4")      # Pod 在上
    text(ix + 10, my + 42, "Pod", anchor="start", weight="bold")
    rect(ix + 10, my + 50, iw - 20, 52, EBG, rx=5)
    text(mx + mw/2, my + 80, "SGLang 进程", weight="bold")
    chip(ix, my + 122, iw, 20, "K8s 节点代理", KBG)         # 代理在下
    gy, gw = my + 152, (mw - 26)/8
    for i in range(8):
        rect(mx + 13 + i * gw, gy, gw - 3, 30, rx=2, sw=1.6)
        text(mx + 13 + i * gw + (gw - 3)/2, gy + 20, str(i), weight="bold")
    return mx + mw/2

MY = 418
PB_Y, PB_H = 380, 260
rect(60, PB_Y, 470, PB_H, rx=12, dash="7,5", sw=1.3)
text(76, PB_Y + 20, "Prefill 池", anchor="start", weight="bold")
c1 = machine(90, MY, "b300-01")
c2 = machine(300, MY, "b300-02")
rect(570, PB_Y, 530, PB_H, rx=12, dash="7,5", sw=1.3)
text(586, PB_Y + 20, "Decode 池", anchor="start", weight="bold")
rect(582, PB_Y + 26, 506, 222, PBG, rx=10, dash="6,4", sw=1.4)
c3 = machine(600, MY, "b300-03")
c4 = machine(830, MY, "b300-04")
text(1036, PB_Y + 242, "LWS 组", weight="bold")

# ==================== 软件关系（下行）====================
linee(1112, GWM, 1112, 356); linee(295, 356, 1112, 356)
linee(295, 356, 295, PB_Y - 3); head(295, PB_Y, math.pi/2)
linee(845, 356, 845, PB_Y - 3); head(845, PB_Y, math.pi/2)
linee(ASC, KY + KH, ASC, PB_Y - 3); head(ASC, PB_Y, math.pi/2); head(ASC, KY + KH + 3, -math.pi/2)
text(ASC + 12, 370, "watch · 上报", anchor="start")

# ==================== 网络与接线 ====================
NET1, NET2, NH = 668, 712, 26
hw(60, NET1, 1040, NH, rx=6)
text(580, NET1 + 17, "计算网 · RDMA（IB / RoCE）", weight="bold")
hw(60, NET2, 1040, NH, rx=6)
text(580, NET2 + 17, "前端网 · ToR ×2（以太）", weight="bold")
hw(1130, NET2 - 16, 150, 56, rx=6)
text(1205, NET2 + 2, "管理机", weight="bold")
text(1205, NET2 + 17, "集群外 · 引导救援")
text(1205, NET2 + 31, "权重预热源")
linee(1100, NET2 + 13, 1130, NET2 + 13, 1.3); dot(1106, NET2 + 13)
linee(430, PB_Y + PB_H, 430, NET1 - 3, 1.8); head(430, NET1, math.pi/2, 9)
text(444, NET1 - 8, "KV 出", anchor="start", weight="bold")
linee(1000, NET1, 1000, PB_Y + PB_H + 3, 1.8); head(1000, PB_Y + PB_H, -math.pi/2, 9)
text(1014, NET1 - 8, "KV 入", anchor="start", weight="bold")
for cx in (c1, c2, c3, c4):
    linee(cx, MY + 200, cx, NET2 + 13, 1.3)
    dot(cx, NET1 + 13); dot(cx, NET2 + 13)
linee(60, CY + 212, 46, CY + 212, 1.3); linee(46, CY + 212, 46, NET2 + 13, 1.3)
linee(46, NET2 + 13, 60, NET2 + 13, 1.3); dot(66, NET2 + 13)

# ==================== 图例（两行，统一字号）====================
ly1, ly2 = 786, 814
hw(60, ly1 - 13, 26, 16, rx=3); text(94, ly1, "硬件（机器 · 交换机）", anchor="start")
rect(240, ly1 - 13, 26, 16, rx=3, dash="5,4"); text(274, ly1, "逻辑概念（Pod · 池 · LWS 组）", anchor="start")
rect(480, ly1 - 13, 30, 16, "#FFFFFF", rx=8, sw=1.8); text(518, ly1, "外部系统 / 角色", anchor="start")
linee(650, ly1 - 5, 680, ly1 - 5); head(680, ly1 - 5, 0, 7); text(690, ly1, "关系箭头：端点＝具体进程", anchor="start")
linee(920, ly1 - 5, 950, ly1 - 5, 1.3); dot(950, ly1 - 5); text(960, ly1, "物理网线 · ●＝接入该网", anchor="start")
text(60, ly2, "软件进程：", anchor="start")
rect(126, ly2 - 13, 20, 16, SBG, rx=3); text(152, ly2, "自研", anchor="start")
text(196, ly2, "｜开源：", anchor="start")
rect(256, ly2 - 13, 20, 16, EBG, rx=3); text(282, ly2, "SGLang", anchor="start")
rect(340, ly2 - 13, 20, 16, OBG, rx=3); text(366, ly2, "OME", anchor="start")
rect(410, ly2 - 13, 20, 16, KBG, rx=3); text(436, ly2, "K8s", anchor="start")
rect(478, ly2 - 13, 20, 16, GBG, rx=3); text(504, ly2, "通用件", anchor="start")
text(580, ly2, "嵌套＝部署于 / 属于　｜　堆叠＝多台对等机器", anchor="start")

P.append("</svg>")
svg = "\n".join(P)
open("../overview.svg", "w").write(svg)
print("written:", len(svg), "bytes")
