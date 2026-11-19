#!/usr/bin/env python3
"""chase_live.py — 追涨杀跌 v2（纯动量 + 空仓开关 + 主题上限 + ATR 网格 + ATR 跟踪止损）。

**一份实现，两条用法**（回测=实盘同一套代码路径，不存在第二份策略）：
  * 回测: --backtest / --suite  → run() 逐日撮合，与 research/chase@988da00 逐位同源
  * 实盘: --plan / --targets    → latest_targets() 用同一个 run(return_state=True) 走完历史，
                                  取最后一日收盘后的指令 → build_plan() 换算成"每只目标股数/
                                  金额/买卖方向"（signal_generator --mode=chase 调这个）

数据: quant2/data/pxq（前复权）+ quant2/data/price（不复权，涨跌停/实物价），
      面板缓存 data/chase/panel.pkl，增量更新走 chase_data.py；参数全在 config.yaml chase: 段。

相对 research/chase@988da00 的改动只有两处，且默认值下逐位不变：
  1) 空仓开关的 MA 周期开参数（index_ma 默认 60，走原 ma60 通道）
  2) 加 return_state（实盘取最后一日状态）+ 目标权重/下单清单 + 每日持仓只数/主题占比统计

规则（与需求逐条对应）
  入场   20 日动量在当日池内的分位 >= 1-entry_pct（默认前 20%）。这是**唯一**选股条件。
  空仓   510300 收盘 < 其 MA60 → 次日开盘清仓，期间不建仓（日频判定，已验证有效的机制）
  决策日 固定锚点（2015-01-05 = 第 0 个交易日）之后**每 decide_every 个交易日**一天：
         交易日序号 % decide_every == decision_phase 就是决策日（默认 every=3、phase=0）。
         序号只由全局交易日历决定，与「上次运行是几天前」无关 → 回测和实盘必然同一组日子。
         decide_every=0 → 退回旧口径（每个 ISO 周最后一个交易日），历史报告可原样复现。
  轮动   决策日收盘出信号，**次一交易日开盘**成交（T+1，绝不当日收盘价成交）；
         缓冲带 = 已持有只要动量分位 >= hold_pct/buffer_quantile 就留，掉出去才卖
  非决策日 不调仓（止损/网格/护栏全关时）；唯一例外 = 空仓开关，日频判定、优先级最高
  换手   每个决策日最多替换 max_swap 只（默认 1），把单边换手压到 15%/周以下
  网格   每只持仓按 ATR 网格只调权重 0.6~1.4 倍（锚=买入价，档距 grid_step×ATR，
         越跌越加、越涨越减），不改持仓集合；权重归一化到总仓位 1.0（不加杠杆）
  出场   **不做**日频破 MA20 清仓；改 ATR 跟踪止损（峰值 - k*ATR14，k=2.5，日频检查）
         周频只做动量分位检查
  主题   同一主题最多 theme_cap 只（主题表由名称关键词构造，落盘 theme_map.json 可审计）

选股四步（顺序固定，前三步只剔除、第四步才选；改这个顺序 = 改策略）：
  1) 同指数去重      index_cap=1：同一只标的指数只留 T-1 日均成交额最大的一只（N=1 时无害保留）
  2) 相关性聚类上限  corr_cluster_cap=2：近 60 日收益两两 ρ ≥ corr_threshold(0.90) 单链接聚成一簇，
                     每簇最多 K 只，超出按动量分位砍尾部（同簇只留分位更高的）
  3) 赛道主题上限    theme_cap：同名主题最多几只，按分位砍尾部（0=不限；本档 0）
  4) 取满 n_hold     按动量分位从强到弱补满 n_hold 只（本档 1；1~3 步只做剔除，不参与排序）

撮合（回测=实盘口径）
  * 真实交易日历（meta/index_calendar.json），信号收盘算、次日开盘成交（T+1）
  * 涨跌停：开盘即封板（默认 10%，名称含创业板/科创/双创按 20%）→ 当日不成交，次日重试
  * 停牌/无成交：当日无 bar 或成交量为 0 → 不成交，指令顺延
  * 整手：买入股数向下取整到 100 股
  * 成本：单边 c（默认 0.1%），买入 c*qty*p、卖出同额，双边计
  * 不做杠杆、不做空；空仓（现金）允许

CLI
  .venv/bin/python3 chase_live.py --backtest --start 2023-11-17 --end 2026-11-16
  .venv/bin/python3 chase_live.py --suite              # 两窗口 + 基准 + 消融 + 成本敏感
  .venv/bin/python3 chase_live.py --caps               # 主题上限 5/6/8/不限 × 3y/5y 对照
  .venv/bin/python3 chase_live.py --plan               # 下一交易日建仓清单（只算不下单）
  .venv/bin/python3 chase_live.py --check-days        # 决策日序列校验（回测 vs 实盘同一组）
  .venv/bin/python3 chase_live.py --phases            # 每 N 日决策的三个相位 × 3y/5y
  .venv/bin/python3 chase_live.py --themes             # 只看主题映射
"""
import argparse
import csv
import json
import os
import pickle
import re
import sys
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "quant2", "data")
META = os.path.join(DATA, "meta")
CONFIG_PATH = os.path.join(HERE, "config.yaml")
OUT = os.path.join(HERE, "data", "chase")
CACHE = os.path.join(OUT, "cache")
DUMPS = os.path.join(OUT, "dumps")
PXQ = os.path.join(DATA, "pxq")
RAWP = os.path.join(DATA, "price")

BENCH = "sh510300"          # 空仓开关基准
DEC_ANCHOR = "2015-01-05"   # 每 N 个交易日决策的固定锚点 = 第 0 个交易日（2015 首个交易日）
MIN_HIST = 60               # 至少 60 根 bar 才进池
MIN_AMT = 5e6               # 20 日中位成交额下限（元）
LOT = 100

EXCL = re.compile(r"货币|现金(?!流)|快钱|快线|保证金|日利|添利|添益|日日鑫|财富宝|天天利|理财|短融|"
                  r"转债|可转债|债|城投|同业存单|信用")
LIMIT20 = re.compile(r"创业板|科创|双创|创业")


# ════════════════════════════════════════════════════════════════════
# 主题映射：名称关键词 → 粗主题（同一主题最多 N 只，用来压相关性）
# 顺序敏感：先匹配到的赢
# ════════════════════════════════════════════════════════════════════
THEME_RULES = [
    ("黄金", r"黄金|金ETF|上海金|白银|贵金属"),
    ("商品", r"原油|油气|石油|豆粕|商品|大宗|能源化工|饲料"),
    ("有色", r"有色|铜|铝|锌|镍|稀土|稀有金属|矿业|资源"),
    ("海外-美股", r"纳指|纳斯达克|标普|道琼斯|美国|巴西|德国|法国|欧洲|英国"),
    ("海外-亚太", r"日经|东证|日本|越南|印度|沙特|亚太|东南亚|中韩|中日|新兴亚洲|新兴市场|全球"),
    ("海外-港股", r"恒生|港股|中概|H股|恒指|香港"),
    ("半导体", r"芯片|半导体|集成电路|存储|光刻|设备材料"),
    ("电子", r"电子|消费电子|面板|消电"),
    ("通信", r"通信|5G|6G|电信"),
    ("计算机", r"软件|计算机|云计算|大数据|信创|信息技术|信息安全|数字经济|数据|算力|物联网|互联网|"
              r"科技|TMT|信息科技"),
    ("传媒游戏", r"游戏|传媒|影视|动漫|元宇宙|AR|VR|网购|电商"),
    ("AI制造", r"人工智能|AI|机器人|工业母机|机床|机械|工业|智能制造|高端制造|高端装备|专精特新|"
              r"先进制造|卫星|低空|科创机械"),
    ("医药", r"医药|生物|医疗|疫苗|创新药|CXO|中药|器械|健康|养老|药"),
    ("食品饮料", r"白酒|食品|饮料|酒"),
    ("消费", r"消费|家电|旅游|酒店|零售|免税|美妆|纺织|服装"),
    ("农业", r"农业|养殖|畜牧|种业|农牧|渔|粮食"),
    ("金融-证券", r"证券|券商"),
    ("金融-银行", r"银行"),
    ("金融-保险", r"保险|非银|多元金融"),
    ("金融其他", r"金融|金融科技"),
    ("地产", r"地产|房地产|REIT"),
    ("基建", r"基建|建筑|建材|水泥|工程|水利|交通|交运|物流|运输|公用事业|环保|水务|燃气"),
    ("钢铁煤炭", r"钢铁|煤炭|采掘"),
    ("化工", r"化工|化学|石化|材料|原材料|新材料"),
    ("新能源车", r"新能车|电动车|汽车|智能车|智能驾驶|智能电车"),
    ("电池", r"锂电|电池|储能"),
    ("光伏风电", r"光伏|风电|氢能|核电|绿电|碳中和|低碳"),
    ("电力", r"电力|电网|能源"),
    ("新能源", r"新能源|绿色|可持续发展"),
    ("军工", r"军工|国防|航天|航空|兵器|船舶"),
    ("红利价值", r"红利|高股息|股息|价值|低波|质量|基本面|自由现金流|央企|国企|国改|ESG|责任|"
                 r"现金流|国企共赢"),
    ("宽基", r"A\d{2,4}|增强|沪深300|中证500|中证1000|中证2000|中证100|中证800|中证A500|中证A50|中证全指|"
             r"上证50|上证180|上证380|上证综指|上证指数|上证中盘|中盘|深证100|深证300|深证成指|中小板|中小100|"
             r"创业板|科创50|科创100|科创200|科创综指|双创|MSCI|等权|全指|A股|沪市|深市|大盘|小盘|"
             r"百强|市值|成长|G60|民企|结构调整|一带一路|综指|成指|\d{2,4}|长三角|京津冀|"
             r"大湾区|四川|区域|治理"),
]


def theme_of(name):
    n = str(name).replace("标普A股", "A股").replace("标普中国A股", "A股")
    if EXCL.search(n):
        return None
    for t, pat in THEME_RULES:
        if re.search(pat, n):
            return t
    core = re.split(r"ETF|etf|LOF", n)[0].strip("()（） ")
    return f"其他:{core}" if core else "其他"


# ════════════════════════════════════════════════════════════════════
# 同指数去重：标的指数识别
#
# 首选「名称」而不是相关系数：名称是基金契约里写死的东西（同一只指数由多家公司发 ETF，
# 名称核心必然相同），而相关系数只是"风险等价"的必要不充分条件——实测 创业板ETF 与
# 创业板50ETF ρ=0.995、科创50 与 科创芯片 ρ=0.992、沪深300 与 A500 ρ=0.98，都是**不同的
# 指数**。所以：名称定身份，相关系数只做两件事——(1) 验证同名组是不是真的一回事，
# (2) 拆掉"名字一样但跟踪的指数不同"的泛名组（红利/消费/价值 这类）。
#
# 名称 → 键：取 "ETF" 之前的部分（本项目 1761 个名称 100% 适用，ETF 后是基金公司简称），
# 剥掉"增强"（增强产品跟踪同一指数），再按 IDX_ALIAS 归一化同一指数的不同叫法。
# ════════════════════════════════════════════════════════════════════
IDX_ALIAS = {
    # 同一只指数、不同叫法（人工确认过的，只有语义确定的才进这张表）
    "中证A500": "A500",          # 中证A500指数 000510
    "证券": "券商",               # 中证全指证券公司指数
    "科创创业": "科创创业50",      # 中证科创创业50指数
    "双创50": "科创创业50",
    "科创板50": "科创50",
    "科创AI": "科创人工智能",
    "深100": "深证100",
    "纳斯达克": "纳指",           # 纳斯达克100
    "纳斯达克100": "纳指",
    "纳指100": "纳指",
    "港股科技": "港股通科技",      # 中证港股通科技指数
    "香港科技": "港股通科技",
    "恒生港股通科技": "港股通科技",
    "港股通恒生科技": "港股通科技",
    "港股互联网": "港股通互联网",
    "港股创新药": "港股通创新药",
    "港股医药": "港股通医药",
    "港股医疗": "港股通医疗",
    "港股汽车": "港股通汽车",
    "消电": "消费电子",
    "畜牧养殖": "养殖",           # 中证畜牧养殖指数
    "有色金属": "有色",           # 中证细分有色金属产业主题指数
    "创50": "创业板50",
    "金": "黄金",                 # 金ETF = 黄金ETF（同一只现货合约）
}
IDX_ENH = re.compile(r"(指数增强|增强)$")


def index_core(name):
    """名称 → 标的指数核心词（不含基金公司简称/ETF/增强）。债券货币返回 None。"""
    s = str(name).strip()
    if EXCL.search(s):
        return None
    if "ETF" in s:
        s = s.split("ETF")[0]
    else:
        s = re.sub(r"(LOF|基金|指数|证券投资基金)$", "", s)
    s = IDX_ENH.sub("", s).strip(" ()（）")
    return s or None


def index_of(name):
    """名称 → 标的指数键（同键=同一只指数）。"""
    c = index_core(name)
    return IDX_ALIAS.get(c, c) if c else None


class _UF:
    """并查集（同指数聚簇用）。"""

    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def idx_snapshot(P, D, i, prm):
    """i 日收盘决策用的「同指数分组 + 组内流动性代表」，**全部用 T-1 及之前的数据**。

    返回 dict(gid=(ncol,) int32 每组一个整数 id, lead={gid: j} 该组 T-1 成交额最大的那只,
              n_groups, n_merged, n_split)
    分组规则（与 index_of 同源）：
      1) 同一指数键 = 同一组（名称身份）
      2) 组内最低相关 < idx_corr_split（默认 0.90）→ 泛名组，按 idx_corr_sub（0.97）再拆
         （例：红利 8 只里 上证红利/中证红利/红利低波 本来就是不同指数）
      3) idx_corr_merge > 0 时，不同键但 ρ ≥ 该值 → 合并（兜底档，默认关）
    相关系数窗口 = 近 idx_corr_window 个交易日收益率，截止 T-1；成交额 = T-1 的近 20 日均额。
    结果按 (i, 参数) 缓存在 P 里，suite/消融重复跑只算一次。
    """
    ck = (int(i), int(prm.idx_corr_window), float(prm.idx_corr_split),
          float(prm.idx_corr_sub), float(prm.idx_corr_merge), int(prm.index_cap))
    cache = P.setdefault("_idxsnap", {})
    hit = cache.get(ck)
    if hit is not None:
        return hit
    cols = P["cols"]
    ncol = len(cols)
    if "_idxkey" not in P:
        P["_idxkey"] = np.array([index_of(P["names"].get(c, "")) for c in cols], dtype=object)
    keys = P["_idxkey"]
    amt = D["amt20m"][max(i - 1, 0)]

    uf = _UF(ncol)
    # 每一组的全部成员（不看数据够不够：名称身份是主判据，新上市 ETF 也必须绑进同组，
    # 否则「数据不足 → 独立成组」会让 159048/159050 这类新机器人 ETF 绕过去重）
    allg = {}
    for j in range(ncol):
        k = keys[j]
        if k is not None:
            allg.setdefault(k, []).append(j)
    w = max(20, int(prm.idx_corr_window))
    lo = max(i - 1 - w, 1)
    R = P["C"][lo:i]                                       # 行 lo..i-1（不含当日）
    n_split = n_merged = 0
    sel = np.array([], dtype=int)
    Cm = None
    pos = {}
    if R.shape[0] >= 20:
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = R[1:] / R[:-1] - 1.0
        good = (np.isfinite(ret).mean(axis=0) >= 0.8) & np.isfinite(amt)
        sel = np.where(good)[0]
        pos = {int(j): k for k, j in enumerate(sel)}
        X = ret[:, sel]
        X = np.where(np.isfinite(X), X, 0.0)
        X = X - X.mean(axis=0)
        sd = X.std(axis=0)
        sd[sd <= 0] = np.inf
        Cm = (X / sd).T @ (X / sd) / X.shape[0]
    for k, js in allg.items():
        if len(js) < 2:
            continue
        mem = [j for j in js if int(j) in pos]
        if len(mem) < 2:                                   # 数据够的不够 2 只 → 整组一个指数
            for j in js[1:]:
                uf.union(js[0], j)
            continue
        ks = [pos[j] for j in mem]
        sub = Cm[np.ix_(ks, ks)]
        iu = np.triu_indices(len(mem), 1)
        vals = sub[iu]
        # 触发拆分用「组内最低相关」：只要有一对明显不相关，就说明这个泛名组里装着
        # 不同的指数（红利/消费/价值 这类）；拆分后的同质大多数仍会聚成一簇。
        if np.nanmin(vals) >= prm.idx_corr_split:          # 同质 → 整组一个指数
            for j in js[1:]:
                uf.union(js[0], j)
            continue
        n_split += 1                                       # 泛名组：按相关系数再拆
        for t in np.where(vals >= prm.idx_corr_sub)[0]:
            uf.union(mem[iu[0][t]], mem[iu[1][t]])
        roots = {}
        for j in mem:
            roots.setdefault(uf.find(j), []).append(j)
        big = max(roots.values(), key=len)[0]              # 数据不足的成员挂进最大子簇
        for j in js:
            if int(j) not in pos:
                uf.union(big, j)
    if prm.idx_corr_merge > 0 and Cm is not None and len(sel) > 1:
        iu = np.triu_indices(len(sel), 1)
        for t in np.where(Cm[iu] >= prm.idx_corr_merge)[0]:
            a, b = int(sel[iu[0][t]]), int(sel[iu[1][t]])
            if keys[a] != keys[b] and keys[a] is not None and keys[b] is not None:
                n_merged += 1
            uf.union(a, b)
    gid = np.array([uf.find(j) for j in range(ncol)], dtype=np.int32)
    # 组内流动性代表：T-1 近 20 日均成交额最大的一只
    best = {}
    for j in range(ncol):
        v = amt[j]
        if not np.isfinite(v):
            continue
        g = int(gid[j])
        if g not in best or v > best[g][0]:
            best[g] = (float(v), int(j))
    lead = {int(g): int(v[1]) for g, v in best.items()}
    snap = dict(gid=gid, lead=lead, n_groups=len(set(gid.tolist())),
                n_split=int(n_split), n_merged=int(n_merged))
    cache[ck] = snap
    return snap


# ════════════════════════════════════════════════════════════════════
# 相关性聚类上限：同指数去重挡不住「同一产业链」
#
# 2026-11-17 去重后实测：8 只目标分属 8 个不同指数，但 5 只在消费链上
# （消费ETF/消费50/港股消费/港股通消费/消费龙头/影视），两两 ρ 最高 0.981
# ——名义 8 只、实际 3 个赌注。指数身份是「基金契约」，产业链是「风险暴露」，
# 前者查名称即可，后者只能看相关系数。
#
# 做法：近 corr_window(60) 个交易日的前复权日收益（截止 T-1，绝不含当日）
#      → 两两 ρ ≥ corr_threshold(0.90) 的边 → 并查集单链接聚类
#      → 每簇最多持有 corr_cluster_cap(2) 只，超出的按动量分位砍尾部。
# 聚类池 = 当日 PIT 可交易池（mask[i]，与分位/主题同一个池），池外的标的自成一簇（不受限）。
# 注意（实测结论，别忽略）：单链接会「链式传递」（A~B、B~C ⇒ A/B/C 同簇），而 A 股权益之间
#      ρ 常年 0.9+，所以 ρ≥0.90 切出来的最大簇不是"某条产业链"，而是「A 股 beta 链」
#      （实测 564/1053 只，决策日最大簇中位 432/690）。K=2 于是等价于"国内权益最多 2 个仓位"，
#      3y 年化 18.88%→9.67%、5y 6.85%→2.30%（Calmar 变差）。这是方法粒度问题，不是实现 bug；
#      完整数据见 audit_corr_cluster_2026-11-17.md，回退只需 corr_cluster_cap: 0。
# ════════════════════════════════════════════════════════════════════
def corr_snapshot(P, D, i, prm, mask):
    """i 日收盘决策用的「相关性聚类」，**全部用 T-1 及之前的数据**。

    返回 dict(cid=(ncol,) int32 每只的簇 id, n_clusters, sizes={cid: n},
              big=[(size, [idx...]), ...] 前 5 大簇, n_edges 合并边数)
    结果按 (i, 窗口, 阈值) 缓存在 P 里：同一 i 的多个参数变体共享一次计算。
    """
    ck = (int(i), int(prm.idx_corr_window), float(prm.corr_threshold))
    cache = P.setdefault("_corrsnap", {})
    hit = cache.get(ck)
    if hit is not None:
        return hit
    C = P["C"]
    ncol = C.shape[1]
    w = max(20, int(prm.idx_corr_window))
    lo = max(i - 1 - w, 1)
    R = C[lo:i]                                    # 行 lo..i-1：收益只用 T-1 及之前
    cid = np.full(ncol, -1, dtype=np.int32)
    n_cl = 0
    sizes = {}
    big = []
    n_edges = 0
    if R.shape[0] >= 20:
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = R[1:] / R[:-1] - 1.0
        good = (np.isfinite(ret).mean(axis=0) >= 0.8) & mask[i] & np.isfinite(C[i - 1])
        sel = np.where(good)[0]
        if len(sel) >= 2:
            X = ret[:, sel]
            X = np.where(np.isfinite(X), X, 0.0)
            X = X - X.mean(axis=0)
            sd = X.std(axis=0)
            sd[sd <= 0] = np.inf
            Z = X / sd
            Cm = Z.T @ Z / X.shape[0]
            uf = _UF(len(sel))
            iu = np.triu_indices(len(sel), 1)
            hit_e = np.where(Cm[iu] >= prm.corr_threshold)[0]
            n_edges = int(len(hit_e))
            for t in hit_e:
                uf.union(int(iu[0][t]), int(iu[1][t]))
            roots = {}
            for k in range(len(sel)):
                roots.setdefault(uf.find(k), []).append(int(sel[k]))
            for _, js in sorted(roots.items(), key=lambda kv: -len(kv[1])):
                for j in js:
                    cid[j] = n_cl
                sizes[n_cl] = len(js)
                n_cl += 1
            big = [(len(js), js[:12]) for _, js in
                   sorted(roots.items(), key=lambda kv: -len(kv[1]))[:5]]
    for j in range(ncol):                          # 池外/数据不足：自成一簇，永不被上限拦
        if cid[j] < 0:
            cid[j] = n_cl
            sizes[n_cl] = 1
            n_cl += 1
    snap = dict(cid=cid, n_clusters=int(n_cl), sizes=sizes, big=big, n_edges=int(n_edges))
    cache[ck] = snap
    return snap


# ════════════════════════════════════════════════════════════════════
# 数据层：日频面板 + 磁盘缓存
# ════════════════════════════════════════════════════════════════════
def calendar():
    """交易日历。收盘后（>=15:05 且**当天 K 线已入库**）把当天算进来，其余时间不含当天。

    口径：日线在收盘后是终值，用"T 日收盘"出"T+1 开盘"的指令仍是严格 T-1；
    盘中/盘前含当天 = 拿半根 bar 出信号（未来函数），所以一律排除。
    当天是否入库以基准 510300 的最后一根 bar 判定 —— 日历里先有这一天、K 线还没到时，
    如果直接算进来会多出一行全 NaN，空仓开关会静默放过（on=True）、动量分位全空。
    回测窗口 3y/5y 都截止 2026-11-16，与这条规则无关，逐位不变。
    """
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    now = datetime.now()
    d0 = pd.Timestamp(now).normalize()
    if (now.hour, now.minute) >= (15, 5) and d0 in cal:
        try:
            bars = json.load(open(os.path.join(PXQ, f"{BENCH}.json")))["bars"]
            if bars and str(bars[-1][0])[:10] == str(d0.date()):
                return cal[cal <= d0]
        except Exception:                                    # noqa: BLE001
            pass
    return cal[cal < d0]


def stale_warning(data_last):
    """数据陈旧判据：库内最新日期 < 最近一个**已收盘**交易日 → 返回告警文案，否则 None。

    「最近已收盘交易日」用 flow_data.target_day()（收盘后=今天，盘中/盘前=上一交易日），
    与数据管道落库用的目标日同一口径：早盘跑不算陈旧，15:05 之后当天那根没到才算。
    """
    try:
        import flow_data as FD
        want = FD.target_day()
    except Exception:                                        # noqa: BLE001
        return None
    if data_last is None or str(data_last) >= str(want):
        return None
    return f"数据陈旧：库内最新 {data_last} < 最近交易日 {want}"


def _pxq_stamp():
    """pxq 目录的数据指纹（文件数 + 最新 mtime），用于判断面板缓存是否过期。"""
    try:
        m = 0.0
        n = 0
        for fn in os.listdir(PXQ):
            if fn.endswith(".json"):
                n += 1
                m = max(m, os.path.getmtime(os.path.join(PXQ, fn)))
        return f"{n}:{int(m)}"
    except Exception:                                        # noqa: BLE001
        return ""


def build_panel(force=False):
    """面板（含磁盘缓存）。缓存自带 last_date：日历变新（当天 K 线已入库）就自动重建。"""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, "daily.pkl")
    cal = calendar()
    stamp = _pxq_stamp()
    if os.path.exists(p) and not force:
        try:
            with open(p, "rb") as f:
                panel = pickle.load(f)
            # 日历变新（有新的交易日）或 pxq 文件被增量更新过 → 必须重建，
            # 否则实盘会拿旧面板算信号（补历史缺口时日历不变，只靠 mtime 抓）
            if (panel.get("last_cal") is not None and panel["last_cal"] >= cal[-1]
                    and panel.get("pxq_stamp") == stamp):
                return panel
        except Exception:                                    # noqa: BLE001
            pass

    uni = pd.read_csv(os.path.join(META, "universe.csv"), dtype={"code": str})
    names = dict(zip(uni["code"], uni["name"]))
    first = dict(zip(uni["code"], pd.to_datetime(uni["first"])))
    cols = [c for c in uni["code"] if os.path.exists(os.path.join(PXQ, f"{c}.json"))]
    idx = cal
    n, m = len(idx), len(cols)
    O = np.full((n, m), np.nan, dtype=np.float32)
    C = np.full((n, m), np.nan, dtype=np.float32)
    H = np.full((n, m), np.nan, dtype=np.float32)
    L = np.full((n, m), np.nan, dtype=np.float32)
    A = np.full((n, m), np.nan, dtype=np.float32)
    V = np.full((n, m), np.nan, dtype=np.float32)
    RC = np.full((n, m), np.nan, dtype=np.float32)      # 不复权收盘（涨跌停参照）
    pos = pd.Series(np.arange(n), index=idx)
    for j, c in enumerate(cols):
        try:
            bars = json.load(open(os.path.join(PXQ, f"{c}.json")))["bars"]
        except Exception:                                     # noqa: BLE001
            continue
        if not bars:
            continue
        d = pd.to_datetime([b[0] for b in bars])
        r = pos.reindex(d).values
        ok = ~pd.isna(r)
        if not ok.any():
            continue
        r = r[ok].astype(int)
        arr = [b for b, k in zip(bars, ok) if k]
        O[r, j] = [b[1] for b in arr]
        C[r, j] = [b[2] for b in arr]
        H[r, j] = [b[3] for b in arr]
        L[r, j] = [b[4] for b in arr]
        V[r, j] = [b[5] for b in arr]
        A[r, j] = [np.nan if b[6] is None else b[6] for b in arr]
        rp = os.path.join(RAWP, f"{c}.json")
        if os.path.exists(rp):
            try:
                rb = json.load(open(rp))["bars"]
                rd = pd.to_datetime([b[0] for b in rb])
                rr = pos.reindex(rd).values
                okr = ~pd.isna(rr)
                RC[rr[okr].astype(int), j] = [b[2] for b, k in zip(rb, okr) if k]
            except Exception:                                 # noqa: BLE001
                pass
    for arr in (O, C, H, L):                     # 脏数据：0/负价一律当缺失，否则会算出 inf 收益
        arr[~(arr > 0)] = np.nan
    RC = np.where(np.isnan(RC) | (RC <= 0), C, RC)
    panel = dict(idx=idx, cols=np.array(cols), names=names, first=first,
                 O=O, C=C, H=H, L=L, A=A, V=V, RC=RC, last_cal=cal[-1], pxq_stamp=stamp)
    with open(p, "wb") as f:
        pickle.dump(panel, f, protocol=4)
    return panel


def derivatives(P):
    """在面板上算：20 日动量、ATR14、MA20/MA60、成交额 20 日中位、历史根数。"""
    C, H, L, A, O, V = P["C"], P["H"], P["L"], P["A"], P["O"], P["V"]
    n, m = C.shape
    Cs = pd.DataFrame(C)
    # 20 日动量
    mom = Cs / Cs.shift(20) - 1.0
    # ATR14（真实波幅的 14 日均值）
    pc = Cs.shift(1)
    tr = pd.DataFrame(np.maximum.reduce([H - L, np.abs(H - pc.values), np.abs(L - pc.values)]))
    atr = tr.rolling(14, min_periods=8).mean()
    ma20 = Cs.rolling(20, min_periods=15).mean()
    ma60 = Cs.rolling(60, min_periods=40).mean()
    # 20 日中位成交额
    amt = pd.DataFrame(A)
    amt20 = amt.rolling(20, min_periods=10).median()
    amt20m = amt.rolling(20, min_periods=10).mean()      # 同指数去重：近 20 日"日均"成交额
    hist = pd.DataFrame(np.cumsum(~np.isnan(C), axis=0))
    traded = pd.DataFrame(V) > 0
    return dict(mom=mom.to_numpy(np.float32), atr=atr.to_numpy(np.float32),
                ma20=ma20.to_numpy(np.float32), ma60=ma60.to_numpy(np.float32),
                amt20=amt20.to_numpy(np.float32), amt20m=amt20m.to_numpy(np.float32),
                hist=hist.to_numpy(np.float32), traded=traded.to_numpy(bool))


def eligible_mask(P, D):
    """PIT 可交易池：有历史>=MIN_HIST、中位成交额够、非货币/债券。"""
    ok = (~np.isnan(P["C"])) & (D["hist"] >= MIN_HIST) & (D["amt20"] >= MIN_AMT)
    ok &= D["traded"]
    keep_cols = np.array([theme_of(P["names"].get(c, "")) is not None for c in P["cols"]])
    ok &= keep_cols[None, :]
    return ok


def pctile(x, ok):
    """截面分位：1 = 动量最强，0 = 最弱。只在 ok 内比较。"""
    out = np.full(x.shape, np.nan, dtype=np.float32)
    for i in range(x.shape[0]):
        row = x[i]
        m = ok[i] & ~np.isnan(row)
        k = int(m.sum())
        if k < 2:
            continue
        r = pd.Series(row[m]).rank(ascending=True, method="average").to_numpy()
        out[i, m] = (r - 1) / (k - 1)
    return out


# ════════════════════════════════════════════════════════════════════
# 参数与引擎
# ════════════════════════════════════════════════════════════════════
@dataclass
class Params:
    start: str = "2023-11-17"
    end: str = "2026-11-16"
    n_hold: int = 8
    entry_pct: float = 0.20        # 入场：动量分位 >= 1-entry_pct（前 20%）
    hold_pct: float = 0.70         # 持有：分位 >= 0.70 就留（跌出前 30% 才卖）
    only_old: bool = False         # True = 只用 2022 年底前上市的代码（数据补齐前口径）
    min_hold: int = 5              # 最短持有交易日
    max_swap: int = 0              # 每周最多主动替换几只（空缺补位不受限）
    swap_margin: float = 0.0       # 主动换仓要求的分位改善幅度
    cooldown: int = 10             # 止损后同一只冷却多少个交易日才可再买
    grid: bool = True
    grid_step: float = 1.0         # 网格档距 = grid_step × ATR（0.5 档太密，换手翻倍且不增收益）
    grid_lo: float = 0.6
    grid_hi: float = 1.4
    grid_dead: float = 0.01        # 单只权重偏离目标 1% 才动（防碎单）
    stop_k: float = 3.0            # ATR 跟踪止损倍数（0 = 不启用）
    stop_freq: str = "weekly"      # daily | weekly（止损检查频率；周频=需求口径）
    exit_mode: str = "atr"         # atr | ma20（破线清仓，用于消融对照）
    regime: bool = True            # 510300 < MA60 → 空仓
    index_ma: int = 60             # 空仓开关的 MA 周期（默认 60；≠60 时按该周期重算，与 60 同公式）
    theme_cap: int = 2             # 同主题最多几只（0 = 不限）
    index_cap: int = 2             # 同一标的指数最多几只（0 = 关掉同指数去重；默认见 DEFAULTS）
    idx_corr_window: int = 60      # 同指数验证/拆分的收益率窗口（交易日，截止 T-1）
    idx_corr_split: float = 0.90   # 同名组内最低相关 < 该值 → 泛名组，按相关再拆
    idx_corr_sub: float = 0.97     # 泛名组拆分时的聚类阈值
    idx_corr_merge: float = 0.0    # >0：不同名但 ρ ≥ 该值 → 合并（兜底档；0=关）
    corr_threshold: float = 0.90   # 相关性聚类阈值：两两 ρ ≥ 该值 → 同一簇（单链接）
    corr_cluster_cap: int = 2      # 同一相关性簇最多持有几只（0 = 关掉相关性上限）
    theme_mode: str = "cap"        # cap=按 theme_cap 限赛道 | top1=强制全仓动量第一的赛道
    # ── 硬护栏（默认全关；打开后与主逻辑同一段 run()，回测/实盘同源）──
    guard_day: float = 0.0         # 单日亏损熔断阈值（0=关）：T 日收盘跌幅 <= -X → T+1 开盘减仓
    guard_day_act: str = "weak1"   # weak1=清掉动量分位最弱的一只 | half=整体减半
    guard_theme: float = 0.0       # 赛道崩塌阈值（0=关）：持有赛道内跌破 MA20 的比例 > 该值 → 降半仓
    guard_theme_lead: bool = False # True=只盯市值占比最大的那条赛道，且它 ≥ guard_theme_min 才算
    guard_theme_min: float = 0.50  # 最大赛道占市值达到该比例才启用"崩塌"判定（单吊口径）
    guard_scale: float = 0.5       # 赛道崩塌后的目标仓位倍数（0.5=半仓）
    # ── 决策频率（固定锚点，回测=实盘同一组日子）──
    decide_every: int = 0          # >1 = 固定锚点每 N 个交易日决策一次；0 = 旧周频（ISO 周最后一天）
    decision_anchor: str = DEC_ANCHOR   # 序号 0 的交易日
    decision_phase: int = 0        # 相位：序号 % N == phase 才是决策日（0..N-1）
    spec_stop: bool = True         # ATR 跟踪止损总闸：False = 不设止损（3a 整段跳过）
    cost: float = 0.001            # 单边成本
    cash: float = 1_000_000.0
    warmup: int = 0


# ── 实盘参数默认值（config.yaml chase: 段缺键时的兜底；研究已冻结，别按历史调）──
DEFAULTS = dict(
    n_hold=8, entry_pct=0.20, hold_pct=0.70, min_hold=5, max_swap=0, swap_margin=0.0,
    cooldown=10, grid=True, grid_step=1.0, grid_lo=0.6, grid_hi=1.4, grid_dead=0.01,
    stop_k=3.0, stop_freq="weekly", exit_mode="atr", regime=True, theme_cap=2,
    index_cap=1, idx_corr_window=60, idx_corr_split=0.90, idx_corr_sub=0.97,
    idx_corr_merge=0.0, corr_threshold=0.90, corr_cluster_cap=2,
    cost=0.001, index_code="510300", index_ma=60, live_start="2023-11-17",
    max_order_pct=0.15, limit_buffer_pct=0.01, min_order_shares=5000,
    theme_mode="cap", guard_day=0.0, guard_day_act="weak1", guard_theme=0.0, guard_scale=0.5,
    guard_theme_lead=False, guard_theme_min=0.50,
    decide_every=0, decision_anchor=DEC_ANCHOR, decision_phase=0, spec_stop=True,
    market_filter="ma60",
)

_MODE_TO_PRM = dict(                      # config 键 → Params 字段
    n_hold="n_hold", entry_pct="entry_pct", hold_pct="hold_pct", min_hold="min_hold",
    max_swap="max_swap", swap_margin="swap_margin", cooldown="cooldown", grid="grid",
    grid_step="grid_step", grid_lo="grid_lo", grid_hi="grid_hi", grid_dead="grid_dead",
    stop_k="stop_k", stop_freq="stop_freq", exit_mode="exit_mode", regime="regime",
    index_ma="index_ma", theme_cap="theme_cap", index_cap="index_cap",
    idx_corr_window="idx_corr_window", idx_corr_split="idx_corr_split",
    idx_corr_sub="idx_corr_sub", idx_corr_merge="idx_corr_merge", cost="cost",
    corr_threshold="corr_threshold", corr_cluster_cap="corr_cluster_cap",
    theme_mode="theme_mode", guard_day="guard_day", guard_day_act="guard_day_act",
    guard_theme="guard_theme", guard_scale="guard_scale",
    guard_theme_lead="guard_theme_lead", guard_theme_min="guard_theme_min",
    decide_every="decide_every", decision_anchor="decision_anchor",
    decision_phase="decision_phase", spec_stop="spec_stop",
)


def load_cfg():
    """config.yaml -> chase: 段（缺键用 DEFAULTS 兜底）。"""
    raw = {}
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            raw = (yaml.safe_load(f) or {}).get("chase") or {}
    except Exception as e:                                   # noqa: BLE001
        print(f"[chase] config.yaml 读取失败({e})，全用默认参数", file=sys.stderr)
    cfg = dict(DEFAULTS)
    for k, v in raw.items():
        if k in cfg:
            cfg[k] = v
    # 别名（同一个东西只留一个内部字段，config 里用新名字）：
    #   buffer_quantile → hold_pct（缓冲带分位）
    if raw.get("buffer_quantile") is not None:
        cfg["hold_pct"] = float(raw["buffer_quantile"])
    for k in ("n_hold", "min_hold", "max_swap", "cooldown", "index_ma", "theme_cap",
              "min_order_shares", "index_cap", "idx_corr_window", "corr_cluster_cap",
              "decide_every", "decision_phase"):
        cfg[k] = int(cfg[k])
    for k in ("entry_pct", "hold_pct", "swap_margin", "grid_step", "grid_lo", "grid_hi",
              "grid_dead", "stop_k", "cost", "max_order_pct", "limit_buffer_pct",
              "idx_corr_split", "idx_corr_sub", "idx_corr_merge", "corr_threshold",
              "guard_day", "guard_theme", "guard_scale", "guard_theme_min"):
        cfg[k] = float(cfg[k])
    cfg["grid"] = bool(cfg["grid"])
    cfg["regime"] = bool(cfg["regime"])
    cfg["guard_theme_lead"] = bool(cfg["guard_theme_lead"])
    cfg["spec_stop"] = bool(cfg["spec_stop"])
    # 空仓开关：market_filter=maNN → 开（基准收盘 < MA(NN) 就空仓）；off/false/0 → 关。
    # 旧键 regime/index_ma 仍然生效，market_filter 只是把两者合成一个可读的开关。
    mf = str(cfg.get("market_filter") or "ma60").strip().lower()
    if mf in ("off", "false", "0", "none"):
        cfg["regime"] = False
    else:
        _m = re.match(r"ma(\d+)$", mf)
        if _m:
            cfg["index_ma"] = int(_m.group(1))
            cfg["regime"] = True
    cfg["decision_phase"] = int(cfg["decision_phase"]) % max(int(cfg["decide_every"] or 1), 1)
    return cfg


def params_from_cfg(cfg, start=None, end=None):
    """config chase: 段 → Params（回测/实盘共用同一份参数，不存在两套默认值）。"""
    kw = {p: cfg[k] for k, p in _MODE_TO_PRM.items() if k in cfg}
    return Params(start=start or cfg.get("live_start") or "2023-11-17",
                  end=end or str(calendar()[-1].date()), **kw)


def weekly_decisions(idx, start, end):
    """每个 ISO 周最后一个交易日 = 决策日；返回 {决策日位置: 是否决策}。"""
    d = pd.Series(idx, index=idx)
    iso = d.dt.isocalendar()
    last_of_week = pd.Series(idx).groupby([iso.year.values, iso.week.values]).max().values
    last_of_week = pd.DatetimeIndex(sorted(last_of_week))
    keep = (last_of_week >= pd.Timestamp(start)) & (last_of_week <= pd.Timestamp(end))
    return set(pd.Series(np.arange(len(idx)), index=idx).reindex(last_of_week[keep]).dropna().astype(int))


# ════════════════════════════════════════════════════════════════════
# 决策日：固定锚点的「每 N 个交易日」
#
# 为什么必须用锚点而不是「上次运行是几天前」：漂移逻辑下，回测（从窗口起点一次跑完）和
# 实盘（每天只跑最后一根 bar）会数出不同的相位——同一段历史里回测第 3/6/9 天决策，
# 实盘第 4/7/10 天决策，于是回测=实盘这条命门直接失效。这里把序号定义成
#   ordinal(T) = T 在**全局交易日历**(meta/index_calendar.json) 里的下标 − 锚点下标
# 锚点固定 2015-01-05 = 0，ordinal 只跟日期有关，与运行次数、运行时刻、面板起点全部无关。
# 决策日 = (ordinal − decision_phase) % decide_every == 0。phase=0/1/2 就是三个相位：
# 同一条策略换一个相位，3y 年化能差 30pp 以上（见 --phases），所以相位必须写进配置、写死。
# ════════════════════════════════════════════════════════════════════
def full_calendar():
    """全局交易日历（含 2015 之前的历史，锚点定位用；与面板 idx 无关）。"""
    return pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))


def day_ordinal(idx, anchor=DEC_ANCHOR):
    """每个交易日相对锚点的序号（锚点当天 = 0；锚点前为负，取模用 floor-mod 保持非负）。"""
    cal = full_calendar()
    a = int(cal.searchsorted(pd.Timestamp(anchor)))
    if a >= len(cal) or cal[a] != pd.Timestamp(anchor):
        raise ValueError(f"决策锚点 {anchor} 不在交易日历里")
    return cal.searchsorted(pd.DatetimeIndex(idx)) - a


def is_decision_ord(o, every, phase=0):
    """序号 → 是否决策日（floor-mod：负数也安全）。"""
    return (int(o) - int(phase)) % int(every) == 0


def decision_days(idx, start, end, every=0, anchor=DEC_ANCHOR, phase=0):
    """决策日位置集合。every<=1 时退回旧周频口径（decide_every=0）。"""
    if not every or int(every) <= 1:
        return weekly_decisions(idx, start, end)
    o = day_ordinal(idx, anchor)
    ts = pd.DatetimeIndex(idx)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    sel = np.where(np.mod(o - int(phase), int(every)) == 0)[0]
    return {int(i) for i in sel if t0 <= ts[i] <= t1}


def day_info(idx, i, every=0, anchor=DEC_ANCHOR, phase=0):
    """某一天的决策日状态（实盘打印用：序号 / 取模 / 是否决策日 / 下一决策日日）。"""
    o = int(day_ordinal(idx[[i]], anchor)[0]) if every and int(every) > 1 else None
    out = dict(date=str(pd.Timestamp(idx[i]).date()), every=int(every or 0), phase=int(phase),
               anchor=str(anchor), ord=o, mod=(None if o is None else int(o % int(every))),
               is_decision=(True if not every or int(every) <= 1 else bool(is_decision_ord(o, every, phase))),
               next_decision=None)
    if every and int(every) > 1:
        # 下一决策日按**全局日历**往后找（面板最后一天之后也算得出来）
        cal = full_calendar()
        a = int(cal.searchsorted(pd.Timestamp(anchor)))
        for d in cal[cal > pd.Timestamp(idx[i])]:
            if is_decision_ord(int(cal.searchsorted(d)) - a, every, phase):
                out["next_decision"] = str(pd.Timestamp(d).date())
                break
        if not out["next_decision"]:
            # 日历还没到那一天（最后一个交易日就是今天）：按工作日往后推，标注"推算"
            o0 = int(day_ordinal(idx[[i]], anchor)[0])
            for k in range(1, every + 6):
                if is_decision_ord(o0 + k, every, phase):
                    out["next_decision"] = str(
                        np.busday_offset(np.datetime64(pd.Timestamp(idx[i]).date(), "D"), k,
                                         roll="forward")) + "（推算，待K线确认）"
                    break
    return out


def index_ma_series(P, D, prm):
    """空仓开关基准的收盘价 + MA（回测 run() 与实盘 latest_targets 共用这一条公式）。

    index_ma=60 直接取面板的 ma60（与原研究版逐位一致）；其它周期用同一滚动均值公式重算。
    返回 (列号, 收盘价数组, MA 数组)；基准不在面板里时列号为 None、数组全 NaN。
    """
    idx = P["idx"]
    hit = np.where(P["cols"] == BENCH)[0]
    if not len(hit):
        z = np.full(len(idx), np.nan, dtype=np.float32)
        return None, z, z
    bj = int(hit[0])
    close = P["C"][:, bj]
    if int(prm.index_ma) == 60:
        ma = D["ma60"][:, bj]
    else:
        _w = int(prm.index_ma)
        ma = pd.Series(close).rolling(_w, min_periods=max(10, int(_w * 2 / 3))) \
            .mean().to_numpy(np.float32)
    return bj, close, ma


def run(P, D, mask, pct, prm: Params, return_state=False):
    """日频撮合 + 决策日选股（周频或每 N 日）的组合模拟。返回净值/换手/成本/成交明细。

    return_state=True: 只给实盘用 —— 在最后一个交易日收盘后再跑一次决策，把"次日开盘
    该卖/该买/网格目标权重"连同持仓状态一起返回（不加 return_state 时逐位等价于研究版）。
    """
    idx = P["idx"]
    cols = P["cols"]
    names = P["names"]
    C, O, A_, RC = P["C"], P["O"], P["H"], P["RC"]
    atr, ma20 = D["atr"], D["ma20"]
    bj, close_b, ma60_b = index_ma_series(P, D, prm)
    limit = np.array([0.20 if LIMIT20.search(str(names.get(c, ""))) else 0.10 for c in cols],
                     dtype=np.float32)
    theme = np.array([theme_of(names.get(c, "")) for c in cols], dtype=object)
    ncol = len(cols)

    i0 = int(np.searchsorted(idx, pd.Timestamp(prm.start)))
    i1 = int(np.searchsorted(idx, pd.Timestamp(prm.end), side="right")) - 1
    # 决策日集合：decide_every>1 用固定锚点序号（回测/实盘同一组日子），否则旧周频
    wdec = decision_days(idx, prm.start, prm.end, prm.decide_every,
                         prm.decision_anchor, prm.decision_phase)

    cash = float(prm.cash)
    shares = {}
    basis = {}          # 每股成本（含网格加仓后的加权平均）
    realized = {}       # 累计已实现盈亏（网格部分卖出也计入，平仓时一次性结算）
    cost_total = {}     # 建仓以来累计买入成本，用于算持仓周期收益率
    qty_basis = {}      # 用于加权平均的已实现份额口径
    entry_i, anchor, anchor_atr, peak, mult = {}, {}, {}, {}, {}
    want_sell, want_buy, want_grid = set(), {}, []
    stop_set = set()
    trades = []
    eq_close, eq_open_r, gross_r = [], [], []
    turn_week, cost_day = {}, {}
    stopped = off_days = 0
    last_stop = {}
    tr = {"regime": 0.0, "rot": 0.0, "grid": 0.0, "stop": 0.0, "guard": 0.0}
    greed = {}                    # 网格上次触发价（棘轮）
    dbg = {"n_scan": 0, "n_changed": 0, "n_exec": 0}
    mult_prev = {}
    cause = "rot"
    cap_blocked = 0
    idx_blocked = idx_sells = 0      # 同指数去重：被拦截的建仓次数 / 因同指数超限卖出的次数
    corr_blocked = corr_sells = 0    # 相关性上限：被拦截的建仓次数 / 因同簇超限卖出的次数
    stop_pending = False
    sell_cause = {"stop": 0.0, "rot": 0.0, "regime": 0.0, "guard": 0.0}
    n_sells = n_stop_days = 0
    stop_log, sell_log = [], []
    # ── 只读诊断（不参与任何决策）：--trades 的逐笔明细来源 ──
    why, dec = {}, {}                # 卖单触发原因 / 产生该卖单的决策日位置
    off_log = []                     # 空仓开关判为 OFF 的决策日位置
    buys, buy_pct, buy_pct_dec = {}, {}, {}   # 建仓明细（可能多笔网格加仓）/ 买入日分位 / 决策日分位
    legs = []                        # 逐笔成交（含回测结束未平仓），与 trades 分离，不影响 stats()
    hold_n, theme_share = [], []     # 每日持仓只数 / 单一主题占持仓的最大比例（集中度实测）
    theme_wshare = []                # 单一主题占持仓的最大比例（市值口径，网格改权后更准）
    # ── 护栏状态（与回测同一段 run()；实盘 latest_targets 复用同一份状态）──
    guard_scale_cur = 1.0            # 当前目标仓位倍数（1.0 满仓 / guard_scale 降仓）
    guard = dict(day=0, theme=0, weak_sells=0, trims=0)   # 触发次数（单日熔断/赛道崩塌/卖出只数/降仓笔数）
    guard_set = set()                # 本次 decision 因单日熔断要清的标的
    guard_trim = set()               # 本次 decision 因赛道崩塌要降仓（按权重改写）的标的
    guard_log = []                   # (交易日位置, 类型, 标的或目标倍数)
    idx_share, idx_wshare, idx_cnt = [], [], []   # 单一指数占持仓的最大比例（只数/市值）+ 最大只数
    corr_share, corr_cnt = [], []    # 单一相关性簇占持仓的最大比例（只数）+ 最大只数
    lastpx = {}                      # 每只最后一个有效收盘价（缺 bar 时按最近价估值，不是按 0）
    pos_log, nan_close = [], []      # 每日收盘持仓明细 / 当日缺收盘价的持仓数（数据体检用）
    if "_idxid" not in P:            # 标的指数 id（按名称身份，时间不变）：集中度实测用
        _k = [index_of(P["names"].get(c, "")) for c in cols]
        _m = {k: n for n, k in enumerate(sorted({k for k in _k if k}))}
        P["_idxid"] = np.array([_m.get(k, -1) for k in _k], dtype=np.int32)
    idxid = P["_idxid"]
    gid_cur = None                   # 最近一次决策用的同指数分组（持仓集合只在决策日变，日频口径精确）
    cid_cur = None                   # 最近一次决策用的相关性聚类（同上）

    def po(i, j):
        v = O[i, j]
        return float(v) if np.isfinite(v) and v > 0 else np.nan

    def pcx(i, j):
        v = C[i, j]
        return float(v) if np.isfinite(v) and v > 0 else np.nan

    def fv(a, k):
        """只读：取数组第 k 个元素为 float（非有限值→NaN），诊断字段用。"""
        v = a[int(k)]
        return float(v) if np.isfinite(v) else float("nan")

    def rec_leg(j, i, p, q, pnl, ct, e_i):
        """只读诊断：记一笔已平仓成交（逐笔明细，stats()/撮合/选股都不读它）。"""
        _bl = buys.get(j, [])
        _dj = int(dec.get(j, i - 1))
        legs.append(dict(
            code=cols[j], name=names.get(cols[j], ""),
            entry_i=int(e_i), exit_i=int(i),
            entry_d=str(idx[int(e_i)].date()), exit_d=str(idx[int(i)].date()),
            dec_d=str(idx[_dj].date()),
            entry_p=float(_bl[0][1]) if _bl else float("nan"),
            entry_q=int(sum(b[2] for b in _bl)) if _bl else int(q),
            entry_amt=float(sum(b[3] for b in _bl)) if _bl else float(ct),
            exit_p=float(p), exit_q=int(q), exit_amt=float(q * p),
            cost=float(ct), pnl=float(pnl), ret=float(pnl / ct),
            reason=why.get(j, cause), dec_i=_dj, n_buy=len(_bl),
            entry_pct=float(buy_pct.get(j, np.nan)),
            entry_pct_dec=float(buy_pct_dec.get(j, np.nan)),
            entry_bdec_d=str(idx[int(e_i) - 1].date()),
            b_close=fv(close_b, i), b_ma=fv(ma60_b, i),
            b_close_dec=fv(close_b, _dj), b_ma_dec=fv(ma60_b, _dj),
            sell_pct=fv(pct[_dj], j), sell_pct_exec=fv(pct[i], j)))
        buys.pop(j, None); buy_pct.pop(j, None); buy_pct_dec.pop(j, None)
        why.pop(j, None); dec.pop(j, None)

    def val_at(i, j, q):
        p = po(i, j)
        if not np.isfinite(p):
            p = pcx(i, j)
        if not np.isfinite(p):
            p = lastpx.get(j, np.nan)
        return q * p if np.isfinite(p) else np.nan

    def decide(i, eq_c):
        """收盘决策：产出次日开盘的指令（原研究版 3a/3b/3c 逐行搬进来，未改一行逻辑）。

        返回 True=持仓模式(on)，False=空仓开关触发。只被 run() 内部调用，回测与实盘同一条路径。
        """
        nonlocal want_sell, want_buy, want_grid, stop_set, cause
        nonlocal off_days, stopped, n_stop_days, cap_blocked, mult_prev
        nonlocal idx_blocked, idx_sells, gid_cur
        nonlocal corr_blocked, corr_sells, cid_cur
        nonlocal guard_scale_cur, guard_set, guard_trim
        # ── 3) 收盘决策 ─────────────────────────────────────────────
        # 顺序（改这个顺序 = 改策略）：
        #   0) 空仓开关：每天收盘先看 510300 vs MA(index_ma)。跌破 → 次日开盘清仓，
        #      不管今天是不是决策日（日频、优先级最高，一票否决后面所有建仓/换仓）；
        #      站上 → 只是"允许持仓"，**必须等下一个决策日**才建仓（不是当天就买）。
        #   1) 止损（spec_stop=False 时不跑）
        #   2) 决策日：动量分位 + 缓冲带 + 去重/上限 → 目标持仓集合
        want_sell, want_buy, want_grid = set(), {}, []
        stop_set = set()
        guard_set = set()
        guard_trim = set()
        on = True
        if prm.regime and bj is not None and np.isfinite(close_b[i]) and np.isfinite(ma60_b[i]):
            on = bool(close_b[i] >= ma60_b[i])
        if not on:
            off_days += 1
            want_sell = set(shares)
            cause = "regime"
            off_log.append(i)                       # 只读诊断：空仓开关判 OFF 的日子
            for _j in want_sell:                    # 只读诊断：标注触发原因
                why[_j] = "regime"
                dec[_j] = i
            return False
        cause = "rot"

        # 3a) 出场：ATR 跟踪止损 / 破 MA20 清仓（消融对照）。spec_stop=False → 整段跳过
        for j in (list(shares) if prm.spec_stop else []):
            p = pcx(i, j)
            if not np.isfinite(p):
                continue
            peak[j] = max(peak.get(j, p), p)
            if prm.exit_mode == "atr" and prm.stop_k > 0:
                a = atr[i, j]
                if (prm.stop_freq == "daily" or i in wdec) and np.isfinite(a) and a > 0 \
                        and p < peak[j] - prm.stop_k * a:
                    want_sell.add(j)
                    stop_set.add(j)
                    last_stop[j] = i
                    stopped += 1
                    n_stop_days += 1
                    stop_log.append((i, j))
            elif prm.exit_mode == "ma20":
                m = ma20[i, j]
                if np.isfinite(m) and p < m:
                    want_sell.add(j)

        # 3b) 周频：动量分位 + 缓冲带 + 同指数去重 + 相关性聚类上限 + 主题上限 + 换手闸门
        if i in wdec:
            pct_i = pct[i]
            row_ok = mask[i]
            snap = idx_snapshot(P, D, i, prm) if prm.index_cap else None
            gid = snap["gid"] if snap else None
            lead = snap["lead"] if snap else {}
            gid_cur = gid
            csnap = corr_snapshot(P, D, i, prm, mask)   # 统计/上限共用；纯 T-1 数据
            cid = csnap["cid"]
            cid_cur = cid
            amt_p = D["amt20m"][max(i - 1, 0)]
            held = [j for j in shares if j not in want_sell]
            keep = []
            for j in held:
                age = i - entry_i.get(j, i)
                v = pct_i[j]
                if age < prm.min_hold or (np.isfinite(v) and v >= prm.hold_pct):
                    keep.append(j)
            keep.sort(key=lambda j: -(pct_i[j] if np.isfinite(pct_i[j]) else -1))
            _top_theme = None
            if prm.theme_mode == "top1":
                # 强制单吊（V3 口径）：把仓位全部压到"动量第一的赛道"= 当日动量分位最高那只所属主题，
                # 赛道内部仍然只收不同指数的（index_cap 照旧生效），不做任何跨赛道分散。
                _all = [j for j in range(ncol) if row_ok[j] and np.isfinite(pct_i[j])
                        and pct_i[j] >= 1.0 - prm.entry_pct
                        and (i - last_stop.get(j, -10 ** 9)) >= prm.cooldown]
                if _all:
                    _top_theme = theme[max(_all, key=lambda j: pct_i[j])]
                    _drop = [j for j in keep if theme[j] != _top_theme]
                    if _drop:
                        want_sell |= set(_drop)
                        keep = [j for j in keep if theme[j] == _top_theme]
            if prm.index_cap:                       # 同一标的指数超过上限：留 T-1 流动性最好的
                by_g, drop = {}, set()
                for j in keep:
                    by_g.setdefault(int(gid[j]), []).append(j)
                for g, js in by_g.items():
                    if len(js) > prm.index_cap:
                        js.sort(key=lambda j: -(float(amt_p[j])
                                                if np.isfinite(amt_p[j]) else -1.0))
                        drop.update(js[prm.index_cap:])
                if drop:
                    want_sell |= drop
                    keep = [j for j in keep if j not in drop]
                    idx_sells += len(drop)
            if prm.corr_cluster_cap:                # 同一相关性簇超过上限：留动量分位更高的
                by_c, drop = {}, set()
                for j in keep:                      # keep 已按分位从强到弱排序
                    by_c.setdefault(int(cid[j]), []).append(j)
                for c, js in by_c.items():
                    if len(js) > prm.corr_cluster_cap:
                        drop.update(js[prm.corr_cluster_cap:])
                if drop:
                    want_sell |= drop
                    keep = [j for j in keep if j not in drop]
                    corr_sells += len(drop)
            if prm.theme_cap:                       # 同主题超过上限：按分位砍尾部
                cnt, kk = {}, []
                for j in keep:
                    t = theme[j]
                    if cnt.get(t, 0) >= prm.theme_cap:
                        want_sell.add(j)
                        continue
                    cnt[t] = cnt.get(t, 0) + 1
                    kk.append(j)
                keep = kk
            if len(keep) > prm.n_hold:
                for j in keep[prm.n_hold:]:
                    want_sell.add(j)
                keep = keep[:prm.n_hold]
            cand = [j for j in range(ncol) if row_ok[j] and np.isfinite(pct_i[j])
                    and pct_i[j] >= 1.0 - prm.entry_pct
                    and j not in shares and j not in keep
                    and (i - last_stop.get(j, -10 ** 9)) >= prm.cooldown]
            if _top_theme is not None:              # 强制单吊：候选也只能来自这条赛道
                cand = [j for j in cand if theme[j] == _top_theme]
            if prm.index_cap:                       # 同指数只允许 "T-1 流动性代表" 建仓
                _c0 = len(cand)
                cand = [j for j in cand if j == lead.get(int(gid[j]), j)]
                idx_blocked += _c0 - len(cand)
            cand.sort(key=lambda j: -pct_i[j])
            tc, icnt, ccnt = {}, {}, {}
            for j in keep:
                tc[theme[j]] = tc.get(theme[j], 0) + 1
                _g = int(gid[j]) if prm.index_cap else -1
                icnt[_g] = icnt.get(_g, 0) + 1
                _c = int(cid[j]) if prm.corr_cluster_cap else -1
                ccnt[_c] = ccnt.get(_c, 0) + 1

            def fits(j):
                if prm.index_cap and icnt.get(int(gid[j]), 0) >= prm.index_cap:
                    return False
                if prm.corr_cluster_cap and ccnt.get(int(cid[j]), 0) >= prm.corr_cluster_cap:
                    return False
                return (not prm.theme_cap) or tc.get(theme[j], 0) < prm.theme_cap

            def take(j, sign=1):
                tc[theme[j]] = tc.get(theme[j], 0) + sign
                _g = int(gid[j]) if prm.index_cap else -1
                icnt[_g] = icnt.get(_g, 0) + sign
                _c = int(cid[j]) if prm.corr_cluster_cap else -1
                ccnt[_c] = ccnt.get(_c, 0) + sign

            add = []
            slots = prm.n_hold - len(keep)
            # (1) 空缺（被止损/清仓腾出来的位置）无闸门补满：空仓不是目标
            for j in list(cand):
                if slots <= 0:
                    break
                ok_j = fits(j)
                if not ok_j:
                    if prm.index_cap and icnt.get(int(gid[j]), 0) >= prm.index_cap:
                        idx_blocked += 1
                    elif prm.corr_cluster_cap and ccnt.get(int(cid[j]), 0) >= prm.corr_cluster_cap:
                        corr_blocked += 1
                    elif prm.theme_cap:
                        cap_blocked += 1
                else:
                    add.append(j)
                    take(j)
                    slots -= 1
                    cand.remove(j)
            # (2) 主动换仓：只用 max_swap 只额度，且要求分位改善超过 swap_margin
            nswap = 0
            for j in list(cand):
                if nswap >= prm.max_swap or not keep:
                    break
                weakest = min(keep, key=lambda x: pct_i[x] if np.isfinite(pct_i[x]) else -1)
                if pct_i[j] - (pct_i[weakest] if np.isfinite(pct_i[weakest]) else -1) <= prm.swap_margin:
                    break
                take(weakest, -1)
                if fits(j):
                    keep.remove(weakest)
                    want_sell.add(weakest)
                    add.append(j)
                    take(j)
                    nswap += 1
                else:
                    take(weakest, +1)
            target_set = keep + add
            for j in list(shares):
                if j not in target_set:
                    want_sell.add(j)
            for j in target_set:
                if j not in shares:
                    want_buy[j] = guard_scale_cur / prm.n_hold   # 降仓期新建只给半份

        # 3c) 权重：网格倍数在持仓集合内自筹资金归一化（加仓的钱来自同伴减仓，不用现金）
        live = [j for j in shares if j not in want_sell]
        newb = [j for j in want_buy if j not in live]
        base_gross = guard_scale_cur * (len(live) + len(newb)) / prm.n_hold
        if prm.grid and live:
            ms = {}
            for j in live:
                au, a0 = anchor.get(j), anchor_atr.get(j)
                p = pcx(i, j)
                m0 = mult.get(j, 1.0)
                if au and a0 and a0 > 0 and np.isfinite(p):
                    step = prm.grid_step * a0
                    mv = p - greed.get(j, au)
                    if mv <= -step and m0 < prm.grid_hi - 1e-9:
                        mult[j] = float(min(prm.grid_hi, m0 + 0.2))
                        greed[j] = p
                    elif mv >= step and m0 > prm.grid_lo + 1e-9:
                        mult[j] = float(max(prm.grid_lo, m0 - 0.2))
                        greed[j] = p
                ms[j] = mult.get(j, 1.0)
            tot = sum(ms.values())
            fired = [j for j in live if abs(mult.get(j, 1.0) - mult_prev.get(j, 1.0)) > 1e-9]
            if fired and tot > 0:      # 只有跳档那天才整组归一化，避免价格漂移带来的碎单
                dbg["n_changed"] += 1
                for j in live:
                    wtgt = base_gross * ms[j] / tot
                    cur = shares[j] * pcx(i, j) / max(eq_c, 1.0) if np.isfinite(pcx(i, j)) else np.nan
                    if np.isfinite(cur) and abs(wtgt - cur) > prm.grid_dead:
                        want_grid.append((j, wtgt))
            mult_prev = dict(mult)

        # 3d) 硬护栏（默认关）：只用 T 日收盘及更早的数据判定，T+1 开盘执行 —— 与主逻辑同一段 run()
        #  A 单日亏损熔断：T 日组合跌幅 <= -guard_day → T+1 开盘减仓
        #     weak1 = 清掉动量分位最弱的那只（并进冷却，cooldown 天内不许重建）
        #     half  = 整体减半（每只卖一半，卖出的钱留现金，不加回）
        if prm.guard_day > 0 and len(eq_close) >= 2 and shares:
            _dr = eq_close[-1] / max(eq_close[-2], 1e-9) - 1.0
            _live_g = [j for j in shares if j not in want_sell]
            if _dr <= -prm.guard_day and _live_g:
                guard["day"] += 1
                _pi = pct[i]
                if prm.guard_day_act == "half":
                    for j in _live_g:
                        _p = pcx(i, j)
                        if np.isfinite(_p) and _p > 0:
                            want_grid.append((j, 0.5 * shares[j] * _p / max(eq_c, 1.0)))
                            guard_trim.add(j)
                    guard_log.append((i, "day_half", round(_dr, 4)))
                else:
                    _w = min(_live_g, key=lambda j: _pi[j] if np.isfinite(_pi[j]) else -1)
                    want_sell.add(_w)
                    guard_set.add(_w)
                    guard["weak_sells"] += 1
                    last_stop[_w] = i                    # 冷却：护栏卖出后不许立刻重建
                    guard_log.append((i, "day_weak", int(_w)))

        #  B 赛道崩塌退出：持有赛道内跌破 MA20 的比例 > guard_theme → 整体降到 guard_scale 仓位
        #     状态式（不是每天砍一半）：破位比例回落 → 恢复满仓，恢复时按目标权重补回
        #     guard_theme_lead=True 时只看市值占比最大的那条赛道，且它必须 ≥ guard_theme_min
        #     （"单吊"口径：只有真的把半仓以上压在这一条赛道上，它崩了才降仓）
        if prm.guard_theme > 0 and shares:
            _bt, _bad = {}, False
            for j in shares:
                _bt.setdefault(theme[j], []).append(j)
            _scope = list(_bt)
            if prm.guard_theme_lead:
                _tv = {t: sum(shares[j] * pcx(i, j) for j in js
                              if np.isfinite(pcx(i, j))) for t, js in _bt.items()}
                _tot = sum(_tv.values())
                _lt = max(_tv, key=_tv.get) if _tv else None
                _scope = [_lt] if (_lt is not None and _tot > 0
                                   and _tv[_lt] / _tot >= prm.guard_theme_min) else []
            for _t in _scope:
                _js = _bt[_t]
                if len(_js) < 2:                         # 单只赛道没有"超过一半"可言
                    continue
                _nb = 0
                for j in _js:
                    _p, _m = pcx(i, j), ma20[i, j]
                    if np.isfinite(_p) and np.isfinite(_m) and _p < _m:
                        _nb += 1
                if _nb / len(_js) > prm.guard_theme:
                    _bad = True
                    break
            _scale_t = float(prm.guard_scale) if _bad else 1.0
            if abs(_scale_t - guard_scale_cur) > 1e-9:
                guard_scale_cur = _scale_t
                guard["theme"] += 1
                guard_log.append((i, "theme_scale", _scale_t))
                for j in shares:
                    if j in want_sell:
                        continue
                    want_grid.append((j, _scale_t / prm.n_hold))
                    guard_trim.add(j)
        # ── 只读诊断：给本轮卖单标注触发条件（只写字典，不参与任何决策/排序/撮合）──
        #   regime = 空仓开关（上面已标）；stop = ATR 止损；guard = 单日熔断；
        #   buffer = 决策日动量分位掉到缓冲带以下（< hold_pct）；
        #   rot = 分位仍在带内，但因去重/上限/持仓只数被挤掉（= 有更强标的）；
        #   data = 分位缺失（当日无有效动量，row_ok=False）
        for _j in want_sell:
            if _j in why:
                continue
            _v = pct[i][_j]
            if _j in stop_set:
                why[_j] = "stop"
            elif _j in guard_set:
                why[_j] = "guard"
            elif np.isfinite(_v):
                why[_j] = "buffer" if _v < prm.hold_pct else "rot"
            else:
                why[_j] = "data"
            dec[_j] = i
        return True

    for i in range(i0, i1 + 1):
        traded = 0.0
        eq_open = cash + sum(val_at(i, j, q) for j, q in shares.items()
                             if np.isfinite(val_at(i, j, q)))

        # ── 1) 开盘：执行上一交易日收盘的指令 ────────────────────────
        if i > i0:
            for j in sorted(want_sell):
                if j not in shares:
                    want_sell.discard(j)
                    continue
                p = po(i, j)
                if not np.isfinite(p):
                    continue                              # 停牌 → 顺延
                pc = C[i - 1, j]                          # 同一前复权序列内比，避免不复权口径错位
                if np.isfinite(pc) and p <= pc * (1 - limit[j]) * 1.0005:
                    continue                              # 开盘跌停 → 顺延
                q = shares.pop(j)
                sc = "stop" if j in stop_set else ("guard" if j in guard_set else cause)
                tr[sc] += q * p
                sell_cause[sc] = sell_cause.get(sc, 0.0) + q * p
                n_sells += 1
                sell_log.append((i, j))
                cash += q * p * (1 - prm.cost)
                traded += q * p
                realized[j] = realized.get(j, 0.0) + (p - basis.get(j, p)) * q
                e_i = entry_i.pop(j, np.nan)
                ct = cost_total.pop(j, np.nan)
                pnl = realized.pop(j, 0.0)
                if np.isfinite(e_i) and np.isfinite(ct) and ct > 0:
                    trades.append(dict(j=j, code=cols[j], name=names.get(cols[j], ""),
                                       entry_i=int(e_i), exit_i=i, pnl=pnl, ret=pnl / ct))
                    rec_leg(j, i, p, q, pnl, ct, e_i)     # 只读诊断：逐笔明细
                basis.pop(j, None)
                anchor.pop(j, None); anchor_atr.pop(j, None); peak.pop(j, None)
                mult.pop(j, None); greed.pop(j, None); want_sell.discard(j); want_buy.pop(j, None)
            for j in sorted(want_buy):
                if j in shares:
                    want_buy.pop(j, None)
                    continue
                p = po(i, j)
                if not np.isfinite(p):
                    continue
                pc = C[i - 1, j]
                if np.isfinite(pc) and p >= pc * (1 + limit[j]) * 0.9995:
                    continue                              # 开盘涨停 → 顺延
                budget = want_buy[j] * eq_open
                q = int(budget / p / LOT) * LOT
                if q < LOT:
                    continue
                need = q * p * (1 + prm.cost)
                if need > cash:
                    q = int(cash / (p * (1 + prm.cost)) / LOT) * LOT
                    if q < LOT:
                        continue
                    need = q * p * (1 + prm.cost)
                cash -= need
                shares[j] = q
                basis[j] = p
                realized[j] = 0.0
                cost_total[j] = q * p
                entry_i[j] = i
                anchor[j] = p
                a = atr[i - 1, j]
                anchor_atr[j] = float(a) if np.isfinite(a) and a > 0 else p * 0.02
                peak[j] = p
                mult[j] = 1.0
                greed[j] = p
                lastpx[j] = p
                traded += q * p
                tr["rot"] += q * p
                # ── 只读诊断：建仓明细（成交价/股数/金额/买入日分位），不参与决策 ──
                buys.setdefault(j, []).append((int(i), float(p), int(q), float(q * p)))
                buy_pct[j] = float(pct[i][j]) if np.isfinite(pct[i][j]) else float("nan")
                buy_pct_dec[j] = (float(pct[i - 1][j]) if np.isfinite(pct[i - 1][j])
                                  else float("nan"))
                want_buy.pop(j, None)
            # 网格权重微调（只动比例，不动集合）：先减后加，否则满仓时加仓永远没现金
            _g = []
            for j, wtgt in want_grid:
                _p = po(i, j)
                if np.isfinite(_p) and j in shares:
                    _g.append((j, wtgt, shares[j] * _p - wtgt * eq_open))
            _g.sort(key=lambda x: -x[2])          # 超配最多的先卖，腾出现金
            for j, wtgt in [(a, b) for a, b, c in _g]:
                if j not in shares:
                    continue
                p = po(i, j)
                if not np.isfinite(p):
                    continue
                cur_val = shares[j] * p
                tgt_val = wtgt * eq_open
                dv = tgt_val - cur_val
                if abs(dv) < prm.grid_dead * eq_open:
                    dbg["n_small"] = dbg.get("n_small", 0) + 1
                    continue
                dbg["n_exec"] += 1
                if dv > 0:
                    q = int(dv / p / LOT) * LOT
                    need = q * p * (1 + prm.cost)
                    dbg["n_add"] = dbg.get("n_add", 0) + 1
                    if q < LOT or need > cash:
                        dbg["n_add_cash"] = dbg.get("n_add_cash", 0) + 1
                        continue
                    dbg["n_add_ok"] = dbg.get("n_add_ok", 0) + 1
                    tr["guard" if j in guard_trim else "grid"] += q * p
                    cash -= need
                    basis[j] = (basis[j] * shares[j] + p * q) / (shares[j] + q)
                    shares[j] += q
                    cost_total[j] = cost_total.get(j, 0.0) + q * p
                    traded += q * p
                    buys.setdefault(j, []).append((int(i), float(p), int(q), float(q * p)))
                else:
                    q = min(shares[j], int(-dv / p / LOT) * LOT)
                    if q < LOT:
                        continue
                    realized[j] = realized.get(j, 0.0) + (p - basis.get(j, p)) * q
                    shares[j] -= q
                    cash += q * p * (1 - prm.cost)
                    traded += q * p
                    tr["guard" if j in guard_trim else "grid"] += q * p
                    if j in guard_trim:
                        guard["trims"] += 1
                    if shares[j] == 0:                     # 网格减到 0 才算平仓
                        e_i = entry_i.pop(j, np.nan)
                        ct = cost_total.pop(j, np.nan)
                        pnl = realized.pop(j, 0.0)
                        if np.isfinite(e_i) and np.isfinite(ct) and ct > 0:
                            trades.append(dict(j=j, code=cols[j], name=names.get(cols[j], ""),
                                               entry_i=int(e_i), exit_i=i, pnl=pnl, ret=pnl / ct))
                            rec_leg(j, i, p, q, pnl, ct, e_i)   # 只读诊断：逐笔明细
                        basis.pop(j, None)
                        anchor.pop(j, None); anchor_atr.pop(j, None); peak.pop(j, None)
                        mult.pop(j, None); greed.pop(j, None)

        # ── 2) 收盘估值 ─────────────────────────────────────────────
        # 持仓当日无 bar（停牌/数据缺口）按最近有效收盘价估值。原研究版直接跳过该持仓
        # （等价于记 0 元）→ 2025-09-11 造出假的 -12.97%，当天全部持仓其实是涨的。
        _posv = 0.0
        for j, q in shares.items():
            _p = pcx(i, j)
            if np.isfinite(_p):
                lastpx[j] = _p
            else:
                _p = lastpx.get(j)
            if _p is not None and np.isfinite(_p):
                _posv += q * _p
        eq_c = cash + _posv
        eq_close.append(eq_c)
        eq_open_r.append(eq_open)
        posv = eq_c - cash
        gross_r.append(max(posv, 0.0) / max(eq_c, 1.0))
        hold_n.append(len(shares))
        pos_log.append([(int(j), float(shares[j] * pcx(i, j)) if np.isfinite(pcx(i, j)) else np.nan)
                        for j in shares])
        nan_close.append(sum(1 for j in shares if not np.isfinite(pcx(i, j))))
        if shares:
            _tc, _tcv = {}, {}
            for j in shares:
                _tc[theme[j]] = _tc.get(theme[j], 0) + 1
                _pv = pcx(i, j) if np.isfinite(pcx(i, j)) else lastpx.get(j, np.nan)
                _tcv[theme[j]] = _tcv.get(theme[j], 0.0) + \
                    (shares[j] * _pv if np.isfinite(_pv) else 0.0)
            theme_share.append(max(_tc.values()) / len(shares))
            theme_wshare.append(max(_tcv.values()) / max(_posv, 1e-9))
            _ic, _icv = {}, {}
            _gi = gid_cur if gid_cur is not None else idxid
            for j, q in shares.items():
                k = int(_gi[j])
                _ic[k] = _ic.get(k, 0) + 1
                _p = pcx(i, j) if np.isfinite(pcx(i, j)) else lastpx.get(j, np.nan)
                _icv[k] = _icv.get(k, 0.0) + (q * _p if np.isfinite(_p) else 0.0)
            idx_share.append(max(_ic.values()) / len(shares))
            idx_wshare.append(max(_icv.values()) / max(_posv, 1e-9))
            idx_cnt.append(max(_ic.values()))
            _cc = {}
            for j in shares:                      # 相关性簇集中度（用最近一次决策的聚类）
                k = int(cid_cur[j]) if cid_cur is not None else int(j)
                _cc[k] = _cc.get(k, 0) + 1
            corr_share.append(max(_cc.values()) / len(shares))
            corr_cnt.append(max(_cc.values()))
        else:
            theme_share.append(0.0)
            theme_wshare.append(0.0)
            idx_share.append(0.0)
            idx_wshare.append(0.0)
            idx_cnt.append(0)
            corr_share.append(0.0)
            corr_cnt.append(0)
        wk = idx[i].isocalendar()
        key = f"{int(wk.year)}-W{int(wk.week):02d}"
        turn_week[key] = turn_week.get(key, 0.0) + 0.5 * traded / max(eq_c, 1.0)
        cost_day[i] = traded * prm.cost / max(eq_c, 1.0)

        if i >= i1:
            if return_state:      # 实盘：最后一日收盘也要决策，才有"次日开盘"的指令
                _on = decide(i, eq_c)
                state = dict(i=int(i), date=str(idx[i].date()), on=bool(_on),
                             eq=float(eq_c), cash=float(cash), shares=dict(shares),
                             basis=dict(basis), entry_i=dict(entry_i),
                             mult=dict(mult), want_sell=sorted(want_sell),
                             want_buy={int(k): float(v) for k, v in want_buy.items()},
                             want_grid=[(int(a), float(b)) for a, b in want_grid],
                             guard=dict(scale=float(guard_scale_cur), day_set=sorted(int(x) for x in guard_set),
                                        trim=sorted(int(x) for x in guard_trim),
                                        n_day=int(guard["day"]), n_theme=int(guard["theme"])),
                             cause=cause, pct={int(j): float(pct[i][j])
                                               for j in range(len(cols))
                                               if np.isfinite(pct[i][j])})
            break

        decide(i, eq_c)

    eq = pd.Series(eq_close, index=idx[i0:i0 + len(eq_close)])
    ret = eq.pct_change().fillna(0.0)
    # ── 只读诊断：回测结束时仍未平仓的仓位，按最后交易日收盘价估值（不是成交，单独标注 eod）──
    for j, q in shares.items():
        _ct = cost_total.get(j, float("nan"))
        _e_i = entry_i.get(j, np.nan)
        _p = C[i1, j] if np.isfinite(C[i1, j]) else lastpx.get(j, np.nan)
        if not (np.isfinite(_ct) and _ct > 0 and np.isfinite(_e_i) and np.isfinite(_p)):
            continue
        _pnl = (float(_p) - float(basis.get(j, _p))) * q + realized.get(j, 0.0)
        _bl = buys.get(j, [])
        legs.append(dict(
            code=cols[j], name=names.get(cols[j], ""),
            entry_i=int(_e_i), exit_i=int(i1),
            entry_d=str(idx[int(_e_i)].date()), exit_d=str(idx[int(i1)].date()),
            dec_d=str(idx[int(i1)].date()),
            entry_p=float(_bl[0][1]) if _bl else float("nan"),
            entry_q=int(sum(b[2] for b in _bl)) if _bl else int(q),
            entry_amt=float(sum(b[3] for b in _bl)) if _bl else float(_ct),
            exit_p=float(_p), exit_q=int(q), exit_amt=float(q * _p),
            cost=float(_ct), pnl=float(_pnl), ret=float(_pnl / _ct),
            reason="eod", dec_i=int(i1), n_buy=len(_bl),
            entry_pct=float(buy_pct.get(j, np.nan)),
            entry_pct_dec=float(buy_pct_dec.get(j, np.nan)),
            entry_bdec_d=str(idx[int(_e_i) - 1].date()),
            b_close=fv(close_b, i1), b_ma=fv(ma60_b, i1),
            b_close_dec=fv(close_b, i1), b_ma_dec=fv(ma60_b, i1),
            sell_pct=fv(pct[i1], j), sell_pct_exec=fv(pct[i1], j)))
    return dict(eq=eq, ret=ret,
                cost=pd.Series({idx[i]: v for i, v in cost_day.items()}),
                turn=pd.Series(turn_week), trades=pd.DataFrame(trades),
                tr_by=tr, cap_blocked=cap_blocked, sell_cause=sell_cause, dbg=dbg,
                stopped=stopped, off_days=off_days, n_sells=n_sells, n_stop_days=n_stop_days,
                stop_log=stop_log, sell_log=sell_log, n_weeks=len(turn_week),
                final_cash=cash, n_left=len(shares),
                idx_blocked=int(idx_blocked), idx_sells=int(idx_sells),
                corr_blocked=int(corr_blocked), corr_sells=int(corr_sells),
                corr_share=pd.Series(corr_share, index=idx[i0:i0 + len(corr_share)]),
                corr_cnt=pd.Series(corr_cnt, index=idx[i0:i0 + len(corr_cnt)]),
                eq_open=pd.Series(eq_open_r, index=idx[i0:i0 + len(eq_open_r)]),
                gross=pd.Series(gross_r, index=idx[i0:i0 + len(gross_r)]),
                hold_n=pd.Series(hold_n, index=idx[i0:i0 + len(hold_n)]),
                theme_share=pd.Series(theme_share, index=idx[i0:i0 + len(theme_share)]),
                theme_wshare=pd.Series(theme_wshare, index=idx[i0:i0 + len(theme_wshare)]),
                guard=dict(guard), guard_log=guard_log, guard_scale=float(guard_scale_cur),
                idx_share=pd.Series(idx_share, index=idx[i0:i0 + len(idx_share)]),
                idx_wshare=pd.Series(idx_wshare, index=idx[i0:i0 + len(idx_wshare)]),
                idx_cnt=pd.Series(idx_cnt, index=idx[i0:i0 + len(idx_cnt)]),
                pos_log=pos_log, nan_close=nan_close,
                legs=legs, off_log=off_log,
                off_dates=[str(idx[int(x)].date()) for x in off_log],
                decisions=sorted(int(i) for i in wdec),
                state=(state if return_state else None))


# ════════════════════════════════════════════════════════════════════
# 指标
# ════════════════════════════════════════════════════════════════════
def stats(res, prm, label="", rf=0.0):
    eq = res["eq"]
    if len(eq) < 30:
        return dict(label=label, cups=0, cagr=0, vol=0, sharpe=0, mdd=0, calmar=0,
                    turn_wk=0, cost_yr=0, win=0, pf=0, hold_d=0, years=0, n_trades=0, t=0)
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cups = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else 0.0
    r = res["ret"]
    vol = r.std(ddof=1) * np.sqrt(252)
    sharpe = (cagr - rf) / vol if vol > 0 else 0.0
    dd = eq / eq.cummax() - 1
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0
    turn_wk = float(res["turn"].iloc[4:].mean()) if len(res["turn"]) > 4 else float(res["turn"].mean())
    cost_yr = float(res["cost"].sum()) / yrs
    gross = float(res["gross"].mean()) if "gross" in res and len(res["gross"]) else float("nan")
    td = res["trades"]
    ntr = 0
    win = pf = hold = 0.0
    if len(td):
        pnl = td["pnl"].to_numpy()
        ntr = len(td)
        win = float((pnl > 0).mean())
        g = pnl[pnl > 0].sum()
        l = -pnl[pnl < 0].sum()
        pf = float(g / l) if l > 0 else float("inf")
        hold = float((td["exit_i"] - td["entry_i"]).mean())
    # 月度收益 t 值
    m = (1 + r).groupby([r.index.year, r.index.month]).prod() - 1
    t = float(m.mean() / (m.std(ddof=1) / np.sqrt(len(m)))) if len(m) > 3 and m.std(ddof=1) > 0 else 0.0
    return dict(label=label, cups=cups, cagr=cagr, vol=vol, sharpe=sharpe, mdd=mdd, calmar=calmar,
                turn_wk=turn_wk, cost_yr=cost_yr, win=win, pf=pf, hold_d=hold, years=yrs,
                n_trades=ntr, t=t, gross=gross)


def by_year(eq):
    y = eq.groupby(eq.index.year).apply(lambda s: s.iloc[-1] / s.iloc[0] - 1)
    return y.to_dict()


def fmt(s):
    return (f"{s['label']:<28} 年化{s['cagr']*100:7.2f}% 累计{s['cups']*100:8.2f}% "
            f"波动{s['vol']*100:5.1f}% 夏普{s['sharpe']:5.2f} 回撤{s['mdd']*100:6.1f}% "
            f"Calmar{s['calmar']:5.2f} 换手{s['turn_wk']*100:5.1f}%/周 成本{s['cost_yr']*100:4.2f}%/年 "
            f"仓位{s.get('gross', 0)*100:4.1f}% "
            f"胜率{s['win']*100:4.1f}% 盈亏比{s['pf']:4.2f} 持仓{s['hold_d']:5.1f}日 "
            f"t={s['t']:5.2f} 笔数{s['n_trades']}")


# ════════════════════════════════════════════════════════════════════
# 基准
# ════════════════════════════════════════════════════════════════════
def bench_ew(P, mask, idx, ok_extra=None, start=None, end=None):
    C = P["C"]
    ret = np.full_like(C, np.nan)
    ret[1:] = C[1:] / C[:-1] - 1.0
    ok = mask if ok_extra is None else (mask & ok_extra)
    eq, val = [], 1.0
    i0 = int(np.searchsorted(idx, pd.Timestamp(start)))
    i1 = int(np.searchsorted(idx, pd.Timestamp(end), side="right")) - 1
    for i in range(i0, i1 + 1):
        m = ok[i] & np.isfinite(ret[i])
        val *= (1.0 + (ret[i][m].mean() if m.any() else 0.0))
        if i >= i0:
            eq.append(val)
    return pd.Series(eq, index=idx[i0:i1 + 1])


def bench_hold(P, code, start, end):
    idx = P["idx"]
    j = int(np.where(P["cols"] == code)[0][0])
    C = P["C"][:, j]
    i0 = int(np.searchsorted(idx, pd.Timestamp(start)))
    i1 = int(np.searchsorted(idx, pd.Timestamp(end), side="right")) - 1
    s = pd.Series(C[i0:i1 + 1], index=idx[i0:i1 + 1]).ffill()
    return s / s.iloc[0]


# ════════════════════════════════════════════════════════════════════
def prep(force=False):
    P = build_panel(force=force)
    D = derivatives(P)
    mask = eligible_mask(P, D)
    pct = pctile(D["mom"], mask)
    return P, D, mask, pct


def pool_size(P, D, mask, on):
    i = int(np.searchsorted(P["idx"], pd.Timestamp(on)))
    return int(mask[i].sum())


def pool_of(P, D, mask, only_old=False):
    if not only_old:
        return mask
    old = np.array([P["first"].get(c) is not None and P["first"][c] <= pd.Timestamp("2022-12-31")
                    for c in P["cols"]])
    return mask & old[None, :]


def ic_by_quintile(P, mask, pct, s0, s1, step=5):
    C = P["C"]
    idx = P["idx"]
    fwd = np.full_like(C, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        fwd[:-20] = C[20:] / C[:-20] - 1.0
    i0 = int(np.searchsorted(idx, pd.Timestamp(s0)))
    i1 = int(np.searchsorted(idx, pd.Timestamp(s1)))
    rows = []
    for i in range(i0, i1, step):
        m = mask[i] & np.isfinite(fwd[i]) & np.isfinite(pct[i])
        if m.sum() < 50:
            continue
        q = pd.qcut(pct[i][m], 5, labels=False, duplicates="drop")
        f = fwd[i][m]
        for k in range(5):
            if (q == k).any():
                rows.append((k, float(np.nanmean(f[q == k]))))
    return pd.DataFrame(rows, columns=["q", "fwd20"]).groupby("q").fwd20.mean().mul(100).to_dict()


def fmt2(s):
    return (f"  {s['label']:<22} 年化{s['cagr']*100:6.2f}% 累计{s['cups']*100:7.2f}% "
            f"波动{s['vol']*100:5.1f}% 夏普{s['sharpe']:5.2f} 回撤{s['mdd']*100:6.1f}% "
            f"Calmar{s['calmar']:5.2f} 换手{s['turn_wk']*100:5.1f}% 成本{s['cost_yr']*100:4.2f}% "
            f"仓位{s.get('gross',0)*100:4.1f}% 胜率{s['win']*100:4.1f}% PF{s['pf']:4.2f} "
            f"持{s['hold_d']:4.1f}日 t={s['t']:5.2f} n={s['n_trades']}")


def bench_rows(P, mask, D, prm, tag):
    out = []
    same = bench_ew(P, mask, P["idx"], start=prm.start, end=prm.end)
    allm = (~np.isnan(P["C"])) & (D["hist"] >= MIN_HIST)
    allw = bench_ew(P, allm, P["idx"], start=prm.start, end=prm.end)
    hs = bench_hold(P, BENCH, prm.start, prm.end)
    for nm, series in (("同池等权", same), ("全市场等权", allw), ("510300 买入持有", hs)):
        bb = pd.Series(np.asarray(series), index=series.index)
        r = dict(eq=bb, ret=bb.pct_change().fillna(0.0), cost=pd.Series(dtype=float),
                 turn=pd.Series(dtype=float), trades=pd.DataFrame(columns=["pnl"]))
        st = stats(r, prm, f"{tag} {nm}")
        st["turn_wk"] = 0.0
        st["cost_yr"] = 0.0
        out.append(st)
    return out


def seg_stat(eq, nm, s0=None, s1=None):
    se = eq
    if s0:
        se = se[se.index >= pd.Timestamp(s0)]
    if s1:
        se = se[se.index <= pd.Timestamp(s1)]
    if len(se) < 20:
        return None
    yy = max((se.index[-1] - se.index[0]).days / 365.25, 1e-9)
    return dict(label=nm, cagr=(se.iloc[-1] / se.iloc[0]) ** (1 / yy) - 1,
                cups=se.iloc[-1] / se.iloc[0] - 1,
                mdd=float((se / se.cummax() - 1).min()), years=yy,
                s0=str(se.index[0].date()), s1=str(se.index[-1].date()))


def risk_extra(res, label=""):
    """集中度的真实代价：最坏单日 / 两日 / 五日 / 平均持仓只数 / 同主题、同指数最大占比。"""
    ret = res["ret"]
    hs = res["theme_share"]
    hv = res.get("theme_wshare")
    xs = res.get("idx_share")
    xv = res.get("idx_wshare")
    if len(ret) < 6:
        return dict(label=label, worst1=0.0, worst1_date="-", worst2=0.0, worst2_date="-",
                    worst5=0.0, worst5_date="-",
                    avg_n=0.0, share_p50=0.0, share_p90=0.0, share_max=0.0, sharew_max=0.0,
                    idx_p50=0.0, idx_p90=0.0, idx_max=0.0, idx_max2=0.0, idx_cnt_max=0,
                    idxw_max=0.0, n_multi_idx=0,
                    corr_p50=0.0, corr_p90=0.0, corr_max=0.0, corr_cnt_max=0)
    r = ret.to_numpy(dtype=float)
    two = r[1:] + r[:-1]
    k1 = int(np.nanargmin(r))
    k2 = int(np.nanargmin(two))
    five = (1.0 + ret).rolling(5).apply(np.prod, raw=True).to_numpy(dtype=float) - 1.0
    five = five[~np.isnan(five)]
    k5 = int(np.argmin(five))
    out = dict(label=label,
               worst1=float(r[k1]), worst1_date=str(ret.index[k1].date()),
               worst2=float(two[k2]), worst2_date=f"{ret.index[k2].date()}→{ret.index[k2 + 1].date()}",
               worst5=float(five[k5]),
               worst5_date=f"{ret.index[k5].date()}→{ret.index[k5 + 4].date()}" if len(five) else "-",
               avg_n=float(res["hold_n"].mean()),
               share_p50=float(np.nanpercentile(hs, 50)),
               share_p90=float(np.nanpercentile(hs, 90)),
               share_max=float(np.nanmax(hs)),
               sharew_max=float(np.nanmax(hv)) if hv is not None and len(hv) else 0.0)
    if xs is not None and len(xs):
        x = xs.to_numpy(dtype=float)
        hn = res["hold_n"].to_numpy(dtype=float)
        m2 = hn >= 2                                     # 只留 1 只时 100% 无意义，单列一个口径
        ic = res.get("idx_cnt")
        out.update(idx_p50=float(np.nanpercentile(x, 50)), idx_p90=float(np.nanpercentile(x, 90)),
                   idx_max=float(np.nanmax(x)),
                   idx_max2=float(np.nanmax(x[m2])) if m2.any() else 0.0,
                   idx_cnt_max=int(np.nanmax(ic.to_numpy(dtype=float))) if ic is not None else 0,
                   idxw_max=float(np.nanmax(xv.to_numpy(dtype=float)[m2])) if (xv is not None and m2.any()) else 0.0,
                   n_multi_idx=int(((x > 1.0 / 8 + 1e-9) & m2).sum()))  # 单一指数占比>1/8的天数
    else:
        out.update(idx_p50=0.0, idx_p90=0.0, idx_max=0.0, idx_max2=0.0, idx_cnt_max=0,
                   idxw_max=0.0, n_multi_idx=0)
    out["idx_blocked"] = int(res.get("idx_blocked", 0))
    out["idx_sells"] = int(res.get("idx_sells", 0))
    cs = res.get("corr_share")
    if cs is not None and len(cs):
        c = cs.to_numpy(dtype=float)
        hn2 = res["hold_n"].to_numpy(dtype=float) >= 2
        out.update(corr_p50=float(np.nanpercentile(c, 50)),
                   corr_p90=float(np.nanpercentile(c, 90)),
                   corr_max=float(np.nanmax(c[hn2])) if hn2.any() else 0.0,
                   corr_cnt_max=int(np.nanmax(res["corr_cnt"].to_numpy(dtype=float))))
    else:
        out.update(corr_p50=0.0, corr_p90=0.0, corr_max=0.0, corr_cnt_max=0)
    out["corr_blocked"] = int(res.get("corr_blocked", 0))
    out["corr_sells"] = int(res.get("corr_sells", 0))
    g = res.get("guard") or {}
    out.update(guard_day_trig=int(g.get("day", 0)), guard_theme_trig=int(g.get("theme", 0)),
               guard_weak_sells=int(g.get("weak_sells", 0)), guard_trims=int(g.get("trims", 0)),
               guard_turn=float((res.get("tr_by") or {}).get("guard", 0.0)))
    return out


# ════════════════════════════════════════════════════════════════════
# 同指数分组的审计（覆盖率 / 误并 / 漏并）—— 只读，不改任何交易逻辑
# ════════════════════════════════════════════════════════════════════
def index_map_report(cfg=None, win=120, quiet=False):
    """打印并落盘「标的指数 → ETF 清单」，同时用近 win 日收益率相关系数交叉验证。

    - 覆盖率：多少名称拿到指数键、多少只在多成员组里（即去重会归并掉多少只）
    - 误并：同一指数键内最低相关 < idx_corr_split 的组（名字一样但跟踪的指数不同）
    - 漏并：不同指数键但 ρ ≥ 0.97 的对（同一指数不同叫法，别名表没覆盖到）
    """
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    cols = P["cols"]
    names = P["names"]
    keys = [index_of(names.get(c, "")) for c in cols]
    grp = {}
    for i, k in enumerate(keys):
        if k:
            grp.setdefault(k, []).append(str(cols[i]))
    multi = {k: v for k, v in grp.items() if len(v) > 1}
    n_etf = sum(len(v) for v in grp.values())
    collapse = sum(len(v) - 1 for v in multi.values())
    print(f"[同指数] 名称 {len(cols)} 只 → 有指数键 {n_etf} 只 / {len(grp)} 个标的指数；"
          f"多成员组 {len(multi)} 个，去重最多归并 {collapse} 只（{collapse / max(n_etf, 1):.1%}）")

    C = P["C"]
    ret = pd.DataFrame(C[-win - 1:-1] / C[-win - 2:-2] - 1.0)
    good = ret.notna().mean() >= 0.8
    cpos = {str(c): j for j, c in enumerate(cols)}
    rc = [j for j, c in enumerate(cols) if bool(good.iloc[j])]
    col2row = {int(j): n for n, j in enumerate(rc)}
    M = ret.iloc[:, rc].corr(min_periods=int(win * 0.8))
    rows = []
    for k, v in sorted(multi.items(), key=lambda x: -len(x[1])):
        js = [col2row[cpos[c]] for c in v if cpos[c] in col2row]
        if len(js) < 2:
            rows.append(dict(index=k, n=len(v), min_rho=float("nan"), med_rho=float("nan"),
                             names=[names.get(c, "") for c in v]))
            continue
        sub = M.iloc[js, js].to_numpy()
        iu = np.triu_indices(len(js), 1)
        rows.append(dict(index=k, n=len(v), min_rho=float(np.nanmin(sub[iu])),
                         med_rho=float(np.nanmedian(sub[iu])),
                         names=[names.get(c, "") for c in v]))
    bad = [r for r in rows if np.isfinite(r["min_rho"]) and r["min_rho"] < cfg["idx_corr_split"]]
    print(f"[同指数] 组内相关：中位 ρ 的中位数 {np.nanmedian([r['med_rho'] for r in rows]):.4f}；"
          f"误并候选（组内最低 ρ < {cfg['idx_corr_split']}）{len(bad)}/{len(rows)} 组")
    for r in sorted(bad, key=lambda x: x["min_rho"])[:12]:
        print(f"    {r['index']:<10}{r['n']:3d}只 minρ={r['min_rho']:.3f} medρ={r['med_rho']:.3f}  "
              f"{', '.join(r['names'][:4])}")

    hi = M.to_numpy() >= 0.97
    np.fill_diagonal(hi, False)
    kk = [keys[j] for j in rc]
    pairs = {}
    for a, b in zip(*np.where(np.triu(hi, 1))):
        if kk[a] is None or kk[b] is None or kk[a] == kk[b]:
            continue
        key = tuple(sorted((kk[a], kk[b])))
        pairs.setdefault(key, []).append(float(M.iloc[a, b]))
    print(f"[同指数] 漏并候选（不同指数键但 ρ≥0.97）：{len(pairs)} 组键对")
    for (a, b), v in sorted(pairs.items(), key=lambda x: -len(x[1]))[:12]:
        print(f"    {a:<10} ~ {b:<10}{len(v):4d} 对  ρmax={max(v):.4f}  （不同指数，未合并）")
    json.dump(dict(n_etf=n_etf, n_index=len(grp), n_multi=len(multi), collapse=collapse,
                   groups={k: v for k, v in sorted(multi.items(), key=lambda x: -len(x[1]))},
                   within=[{k: v for k, v in r.items()} for r in rows],
                   cross_pairs=[dict(a=a, b=b, n=len(v), rho_max=max(v))
                                for (a, b), v in pairs.items()]),
              open(os.path.join(OUT, "index_map.json"), "w"), ensure_ascii=False, indent=1)
    print(f"  写完 {os.path.join(OUT, 'index_map.json')}")
    return dict(n_etf=n_etf, n_index=len(grp), n_multi=len(multi), collapse=collapse,
                bad=len(bad), cross=len(pairs))


# ════════════════════════════════════════════════════════════════════
# 任务一：主题上限取舍（5 / 6 / 8 / 不限 × 3y / 5y）
# ════════════════════════════════════════════════════════════════════
W3Y = ("2023-11-17", "2026-11-16")
W5Y = ("2021-11-17", "2026-11-16")


def caps_report(cfg=None, caps=(5, 6, 8, 0, 2, 3, 4), quiet=False):
    """主题上限四档 × 两窗口。返回 rows（含最坏单日/两日、平均持仓、主题占比、拦截次数）。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    rows = []
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        for cap in caps:
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), theme_cap=int(cap))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} 上限{cap or '不限'}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, cap=int(cap), label=st["label"],
                       cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"], calmar=st["calmar"],
                       avg_n=ex["avg_n"], share_p50=ex["share_p50"], share_p90=ex["share_p90"],
                       share_max=ex["share_max"], cap_blocked=int(res["cap_blocked"]),
                       worst1=ex["worst1"], worst1_date=ex["worst1_date"],
                       worst2=ex["worst2"], worst2_date=ex["worst2_date"],
                       n_trades=st["n_trades"], turn=st["turn_wk"])
            rows.append(row)
            if not quiet:
                print(f"{tag} 上限{cap or '不限':>2}  年化{row['cagr'] * 100:6.2f}% "
                      f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} "
                      f"Calmar{row['calmar']:5.2f} 持仓{row['avg_n']:4.1f}只 "
                      f"同主题占比中位{row['share_p50'] * 100:4.0f}%/90分位{row['share_p90'] * 100:4.0f}%"
                      f"/最大{row['share_max'] * 100:4.0f}% 拦截{row['cap_blocked']:4d}次 "
                      f"最坏单日{row['worst1'] * 100:6.2f}%({row['worst1_date']}) "
                      f"最坏两日{row['worst2'] * 100:6.2f}%({row['worst2_date']})",
                      flush=True)
    return rows


def pick_cap(rows, limit=-0.08):
    """推荐：在最坏单日不破 limit 的前提下年化最高（同等年化优先更小的集中度）。"""
    ok = [r for r in rows if r["worst1"] > limit]
    if not ok:
        return None
    return max(ok, key=lambda r: (round(r["cagr"], 6), -r["share_max"]))


# ════════════════════════════════════════════════════════════════════
# 相关性聚类的审计 / A-B-C 三档对照 / 阈值敏感性
# ════════════════════════════════════════════════════════════════════
def pair_corr(P, i, js, w=60):
    """近 w 个交易日（截止 T-1）指定标的的两两相关系数矩阵。返回 (矩阵, 最大ρ, 对应两只)。"""
    C = P["C"]
    w = max(20, int(w))
    lo = max(i - 1 - w, 1)
    R = C[lo:i]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = R[1:] / R[:-1] - 1.0
    codes = [str(P["cols"][j]) for j in js]
    if len(js) < 2:
        return pd.DataFrame(), float("nan"), (None, None)
    M = pd.DataFrame(ret[:, js], columns=codes).corr(min_periods=max(20, int(len(ret) * 0.8)))
    a = M.to_numpy(dtype=float)
    iu = np.triu_indices(len(js), 1)
    v = a[iu]
    if not np.isfinite(v).any():
        return M, float("nan"), (None, None)
    k = int(np.nanargmax(v))
    return M, float(v[k]), (codes[iu[0][k]], codes[iu[1][k]])


def corr_map_report(cfg=None, quiet=False):
    """打印：最新交易日的相关性聚类结构 + 决策日分布 + 新目标清单与其最大两两 ρ。只读。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    i = len(P["idx"]) - 1
    cols, names = P["cols"], P["names"]
    prm = params_from_cfg(cfg, start=cfg["live_start"], end=str(P["idx"][i].date()))
    snap = corr_snapshot(P, D, i, prm, mask)
    sizes = snap["sizes"]
    cid_all = snap["cid"]
    pool = np.where(mask[i])[0]
    n_pool_cl = len(np.unique(cid_all[pool]))
    n_big = sum(1 for c in np.unique(cid_all[pool]) if sizes[int(c)] >= 2)
    print(f"[相关性聚类] {P['idx'][i].date()} 池 {int(mask[i].sum())} 只 → {n_pool_cl} 簇"
          f"（≥2 只 {n_big} 簇；合并边 {snap['n_edges']}；窗口 {prm.idx_corr_window} 日 T-1；"
          f"阈值 ρ≥{prm.corr_threshold:.2f} 单链接）")
    for size, js in snap["big"]:
        ex = ", ".join(f"{_code6(cols[j])} {names.get(cols[j], '')}" for j in js[:5])
        print(f"    {size:4d} 只  {ex}")

    wd = sorted(weekly_decisions(P["idx"], cfg["live_start"], str(P["idx"][i].date())))
    ns, mx, pools = [], [], []
    for k in wd:
        s2 = corr_snapshot(P, D, k, prm, mask)
        pk = np.where(mask[k])[0]
        ns.append(len(np.unique(s2["cid"][pk])))
        mx.append(max(s2["sizes"][int(c)] for c in np.unique(s2["cid"][pk])))
        pools.append(len(pk))
    print(f"[相关性聚类] 决策日 {len(wd)} 个：簇数 中位{np.median(ns):.0f}/最少{min(ns)}；"
          f"最大簇 中位{np.median(mx):.0f}/最大{max(mx)} 只（池中位 {int(np.median(pools))} 只）")

    cid = snap["cid"]
    for lbl, cfgx in (("只做同指数去重(B)", dict(cfg, corr_cluster_cap=0)),
                      ("去重+相关簇上限(C)", dict(cfg))):
        tg = preview_targets(cfg=cfgx, quiet=True)
        js = [int(np.where(cols == sid)[0][0]) for sid in tg["targets"]]
        js.sort(key=lambda j: -(pct[i][j] if np.isfinite(pct[i][j]) else -1))
        M, rho, pr = pair_corr(P, i, js, cfg["idx_corr_window"])
        print(f"[{lbl}] {len(js)} 只，两两 ρ 最大 {rho:.4f}"
              + (f"（{_code6(pr[0])} ~ {_code6(pr[1])}）" if pr[0] else ""))
        for j in js:
            nm = names.get(cols[j], "")
            print(f"    {_code6(cols[j]):<7}{nm:<14}主题{str(theme_of(nm)):<10}"
                  f"指数{str(index_of(nm)):<10}分位{pct[i][j]:5.1%}  簇{cid[j]}({sizes[int(cid[j])]}只)")
    return dict(n_clusters=int(n_pool_cl), max_size=int(max(sizes[int(c)] for c in np.unique(cid_all[pool]))),
                decision_days=len(wd), cluster_med=float(np.median(ns)),
                max_size_med=float(np.median(mx)))


def worst_day_pos(res, P, date=None):
    """最坏单日（或指定日）的持仓清单 [(代码, 名称, 市值)]，核对「那天到底押了什么」。"""
    r = res["ret"].to_numpy(dtype=float)
    if date is None:
        k = int(np.nanargmin(r))
    else:
        k = int(np.where(res["ret"].index == pd.Timestamp(date))[0][0])
    cols, names = P["cols"], P["names"]
    pos = [(str(cols[j]), names.get(cols[j], ""), float(v))
           for j, v in res["pos_log"][k] if np.isfinite(v)]
    pos.sort(key=lambda x: -x[2])
    return str(res["ret"].index[k].date()), float(r[k]), pos


# ════════════════════════════════════════════════════════════════════
# 单吊赛道（theme_cap=0）专项报告 —— 四个版本 / 护栏消融 / 四窗口 / 最惨一段
# 只调 run() 的入参，不改一行策略；输出纯文本（QQ 直接用）
# ════════════════════════════════════════════════════════════════════
POD_VARIANTS = [
    ("V1 参照 theme6/index1", dict(theme_cap=6, index_cap=1)),
    ("V2 单吊 theme0/index1", dict(theme_cap=0, index_cap=1)),
    ("V3 强制单吊动量第一赛道", dict(theme_cap=0, index_cap=1, theme_mode="top1")),
    ("V4 单吊+集中4只", dict(theme_cap=0, index_cap=1, n_hold=4)),
]

GUARD_VARIANTS = [
    ("V2 无护栏", dict(theme_cap=0, index_cap=1)),
    ("V2+熔断6%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.06)),
    ("V2+熔断8%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.08)),
    ("V2+熔断10%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.10)),
    ("参考 熔断4%(清最弱)", dict(theme_cap=0, index_cap=1, guard_day=0.04)),
    ("V2+熔断6%(整体减半)", dict(theme_cap=0, index_cap=1, guard_day=0.06, guard_day_act="half")),
    ("V2+赛道破MA20过半→半仓", dict(theme_cap=0, index_cap=1, guard_theme=0.5)),
    ("V2+单吊赛道破MA20→半仓", dict(theme_cap=0, index_cap=1, guard_theme=0.5,
                                guard_theme_lead=True)),
    ("V2+熔断6%+单吊赛道半仓", dict(theme_cap=0, index_cap=1, guard_day=0.06, guard_theme=0.5,
                              guard_theme_lead=True)),
]


def _f1(name, st, ex):
    return (f"{name}\n  年化{st['cagr'] * 100:7.2f}% 累计{st['cups'] * 100:8.2f}% "
            f"波动{st['vol'] * 100:5.1f}% 夏普{st['sharpe']:5.2f} "
            f"回撤{st['mdd'] * 100:6.1f}% Calmar{st['calmar']:5.2f}")


def _f2(st, ex):
    return (f"  换手{st['turn_wk'] * 100:5.1f}%/周 成本{st['cost_yr'] * 100:5.2f}%/年 "
            f"平均持仓{ex['avg_n']:4.1f}只 最大单赛道占比 中位{ex['share_p50'] * 100:4.1f}%"
            f"/90分位{ex['share_p90'] * 100:5.1f}%/最大(市值){ex['sharew_max'] * 100:5.1f}% "
            f"交易{st['n_trades']}笔")


def _f3(ex):
    return (f"  最坏单日{ex['worst1'] * 100:6.2f}%({ex['worst1_date']}) "
            f"最坏两日{ex['worst2'] * 100:6.2f}%({ex['worst2_date']}) "
            f"最坏五日{ex['worst5'] * 100:6.2f}%({ex['worst5_date']})")


def _f4(eq):
    return "  分年 " + " ".join(f"{k}:{v * 100:+.1f}%" for k, v in by_year(eq).items())


def _f5(st, ex):
    """护栏口径：触发次数 + 护栏自己造成的换手/成本（相对无护栏的差在报告里另列）。"""
    return (f"  护栏触发 单日熔断{ex['guard_day_trig']}次(清{ex['guard_weak_sells']}只) "
            f"赛道崩塌{ex['guard_theme_trig']}次(改权{ex['guard_trims']}笔) "
            f"护栏换手¥{ex['guard_turn']:,.0f} 占总成交{ex['guard_turn'] / max(ex['_turn_amt'], 1e-9) * 100:4.1f}%")


def _report(cfg, variants, windows, title, with_guard=False):
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    out = []
    for tag, (s0, s1) in windows:
        print(f"\n=== {title} {tag} {s0} → {s1} ===")
        for nm, kw in variants:
            # 护栏默认关：四个版本/四窗口口径先看"策略本身"，护栏代价单独用 --guard 消融
            prm = replace(params_from_cfg(cfg, start=s0, end=s1),
                          **dict({"guard_day": 0.0, "guard_theme": 0.0}, **kw))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, nm)
            ex = risk_extra(res, nm)
            ex["_turn_amt"] = float(sum(res["tr_by"].values()))
            print(_f1(nm, st, ex))
            print(_f2(st, ex))
            print(_f3(ex))
            if with_guard:
                print(_f5(st, ex))
            print(_f4(res["eq"]), flush=True)
            out.append(dict(window=tag, name=nm, kw=kw, cagr=st["cagr"], cups=st["cups"],
                            vol=st["vol"], sharpe=st["sharpe"], mdd=st["mdd"],
                            calmar=st["calmar"], turn=st["turn_wk"], cost=st["cost_yr"],
                            avg_n=ex["avg_n"], sharew_max=ex["sharew_max"],
                            share_max=ex["share_max"], worst1=ex["worst1"],
                            worst1_date=ex["worst1_date"], worst2=ex["worst2"],
                            worst2_date=ex["worst2_date"], worst5=ex["worst5"],
                            worst5_date=ex["worst5_date"], n_trades=st["n_trades"],
                            years=st["years"], by_year=by_year(res["eq"]),
                            guard=dict(res["guard"])))
    return out


def pod_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """四个版本 × 两窗口：年化/累计/波动/夏普/回撤/Calmar/换手/成本/持仓/赛道占比/最坏1-2-5日/分年。"""
    return _report(cfg, POD_VARIANTS, windows, "四版本")


def guard_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """V2 单吊下的护栏消融：不加 vs 熔断 6/8/10% vs 赛道崩塌半仓 vs 组合。"""
    return _report(cfg, GUARD_VARIANTS, windows, "护栏消融", with_guard=True)


def windows_report(cfg=None, variants=None, ends=(("1y", "2025-11-17"), ("2y", "2024-11-17"))):
    """四个时间窗口（1y/2y/3y/5y）在同一版本上的年化对比。"""
    wins = list(ends) + [("3y", W3Y[0]), ("5y", W5Y[0])]
    return _report(cfg, variants or POD_VARIANTS[:2],
                   [(t, (s, W3Y[1])) for t, s in wins], "四窗口")


def dd_report(cfg=None, variants=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """历史上最惨的一段：峰值日 → 谷底日 → 回本日，谷底押的是什么赛道，亏多少，多久回本。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    cols, names = P["cols"], P["names"]
    out = []
    for tag, (s0, s1) in windows:
        for nm, kw in (variants or POD_VARIANTS[:2]):
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), **kw)
            res = run(P, D, mask, pct, prm)
            eq = res["eq"]
            dd = eq / eq.cummax() - 1
            k = int(np.argmin(dd.to_numpy(dtype=float)))
            pk = int(np.argmax(eq.to_numpy(dtype=float)[:k + 1]))
            depth = float(dd.iloc[k])
            rec_k, rec_days = None, None
            for m in range(k, len(eq)):
                if eq.iloc[m] >= eq.iloc[pk]:
                    rec_k = m
                    rec_days = int(np.busday_count(eq.index[k].date(), eq.index[m].date()))
                    break
            pos = [(str(cols[j]), names.get(cols[j], ""), float(v))
                   for j, v in res["pos_log"][k] if np.isfinite(v)]
            pos.sort(key=lambda x: -x[2])
            tot = sum(v for _, _, v in pos) or 1.0
            tc = {}
            for _, nmm, v in pos:
                tc[theme_of(nmm)] = tc.get(theme_of(nmm), 0.0) + v
            top_theme, top_share = max(tc.items(), key=lambda kv: kv[1])
            hold_days = int(np.busday_count(eq.index[pk].date(), eq.index[k].date()))
            print(f"\n=== {nm} {tag} 最惨一段 ===")
            print(f"  峰值 {eq.index[pk].date()} 净值{eq.iloc[pk] / eq.iloc[0]:.4f} → "
                  f"谷底 {eq.index[k].date()} 净值{eq.iloc[k] / eq.iloc[0]:.4f} "
                  f"回撤{depth * 100:6.2f}% 用时{hold_days}个交易日")
            print(f"  回本 {eq.index[rec_k].date() if rec_k else '至今未回本'} "
                  f"（自谷底{rec_days if rec_days is not None else '-'}个交易日，"
                  f"自峰值{int(np.busday_count(eq.index[pk].date(), eq.index[rec_k].date())) if rec_k else '-'}个交易日）")
            print(f"  谷底持仓 {len(pos)} 只，最大赛道 {top_theme} 占市值 {top_share / tot * 100:.1f}%")
            for c, nmm, v in pos:
                print(f"    {_code6(c)}  {nmm[:14]:<16}{theme_of(nmm):<10}{index_of(nmm):<12}"
                      f"市值¥{v:>10,.0f} {v / tot * 100:5.1f}%")
            out.append(dict(name=nm, window=tag, peak=str(eq.index[pk].date()),
                            trough=str(eq.index[k].date()), depth=depth,
                            hold_days=hold_days, rec=str(eq.index[rec_k].date()) if rec_k else None,
                            rec_days=rec_days, top_theme=top_theme,
                            top_share=float(top_share / tot), n_pos=len(pos)))
    return out


def abc_report(cfg=None, thrs=(0.85, 0.90, 0.95), quiet=False):
    """A/B/C 三档 × 3y/5y + 相关性阈值敏感性。返回 dict(rows, sens)。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    variants = [
        ("A 无去重(theme5)", dict(index_cap=0, corr_cluster_cap=0, theme_cap=5)),
        ("B 同指数去重", dict(index_cap=1, corr_cluster_cap=0, theme_cap=6)),
        ("C 去重+相关簇≤2", dict(index_cap=1, corr_cluster_cap=2, theme_cap=6,
                                 corr_threshold=0.90)),
    ]
    rows, keep_res = [], {}
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        print(f"\n=== {tag} {s0} → {s1} ===")
        for nm, kw in variants:
            # 护栏默认关：四个版本/四窗口口径先看"策略本身"，护栏代价单独用 --guard 消融
            prm = replace(params_from_cfg(cfg, start=s0, end=s1),
                          **dict({"guard_day": 0.0, "guard_theme": 0.0}, **kw))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} {nm}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, name=nm, cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
                       calmar=st["calmar"], turn=st["turn_wk"], cost=st["cost_yr"],
                       avg_n=ex["avg_n"], corr_p50=ex["corr_p50"], corr_p90=ex["corr_p90"],
                       corr_max=ex["corr_max"], corr_cnt_max=ex["corr_cnt_max"],
                       idx_max=ex["idx_max2"], worst1=ex["worst1"], worst1_date=ex["worst1_date"],
                       worst2=ex["worst2"], worst2_date=ex["worst2_date"],
                       corr_blocked=ex["corr_blocked"], corr_sells=ex["corr_sells"],
                       idx_blocked=ex["idx_blocked"], n_trades=st["n_trades"],
                       year=by_year(res["eq"]))
            rows.append(row)
            keep_res[(tag, nm)] = res
            print(f"{nm:<18} 年化{row['cagr'] * 100:6.2f}% 累计{st['cups'] * 100:7.2f}% "
                  f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} "
                  f"换手{row['turn'] * 100:5.1f}%/周 成本{row['cost'] * 100:4.2f}%/年 "
                  f"持仓{row['avg_n']:4.1f}只 相关簇占比 中位{row['corr_p50'] * 100:3.0f}%"
                  f"/90分位{row['corr_p90'] * 100:3.0f}%/最大{row['corr_max'] * 100:3.0f}%"
                  f"(最多{row['corr_cnt_max']}只) 拦截{row['corr_blocked']:4d} 卖出{row['corr_sells']:3d} "
                  f"最坏单日{row['worst1'] * 100:6.2f}%({row['worst1_date']}) "
                  f"最坏两日{row['worst2'] * 100:6.2f}%", flush=True)
            print(f"{'':18} 分年:" + " ".join(f"{y}:{v * 100:.2f}%" for y, v in row["year"].items()),
                  flush=True)

    sens = []
    for tag, (s0, s1) in (("3y", W3Y), ("5y", W5Y)):
        for thr in thrs:
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), index_cap=1, theme_cap=6,
                          corr_cluster_cap=2, corr_threshold=float(thr))
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm, f"{tag} ρ≥{thr}")
            ex = risk_extra(res, st["label"])
            row = dict(window=tag, thr=float(thr), cagr=st["cagr"], mdd=st["mdd"],
                       sharpe=st["sharpe"], calmar=st["calmar"], turn=st["turn_wk"],
                       cost=st["cost_yr"], avg_n=ex["avg_n"], corr_max=ex["corr_max"],
                       corr_cnt_max=ex["corr_cnt_max"], worst1=ex["worst1"],
                       worst2=ex["worst2"], n_trades=st["n_trades"],
                       corr_blocked=ex["corr_blocked"])
            sens.append(row)
            print(f"{tag} 阈值{thr:.2f}  年化{row['cagr'] * 100:6.2f}% 回撤{row['mdd'] * 100:6.1f}% "
                  f"夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} 换手{row['turn'] * 100:5.1f}% "
                  f"成本{row['cost'] * 100:4.2f}% 持仓{row['avg_n']:4.1f}只 "
                  f"相关簇最大占比{row['corr_max'] * 100:3.0f}%(最多{row['corr_cnt_max']}只) "
                  f"最坏单日{row['worst1'] * 100:6.2f}% 拦截{row['corr_blocked']:4d}", flush=True)

    dA, rA, posA = worst_day_pos(keep_res[("3y", variants[0][0])], P)
    print(f"\n[A 档最坏单日] {dA} {rA * 100:.2f}% 持仓 {len(posA)} 只：")
    print("    " + ", ".join(f"{c} {n}({v / 1e4:.0f}万)" for c, n, v in posA))
    for nm, _ in variants:
        d, r, pos = worst_day_pos(keep_res[("3y", nm)], P, date=dA)
        print(f"[{nm}] {d} 当日 {r * 100:6.2f}%，持仓 {len(pos)} 只："
              + (", ".join(f"{c} {n}" for c, n, v in pos) if pos else "空仓"))
    dC, rC, posC = worst_day_pos(keep_res[("3y", variants[2][0])], P)
    print(f"[C 档自身最坏单日] {dC} {rC * 100:.2f}% 持仓 {len(posC)} 只："
          + ", ".join(f"{c} {n}" for c, n, v in posC))
    return dict(rows=rows, sens=sens)


# ════════════════════════════════════════════════════════════════════
# 决策日序列校验 + 相位扫描
#
# 这一节的存在理由：decide_every 是「每 3 个交易日」，最容易出的错是回测从窗口起点开始数、
# 实盘从「今天」开始数，于是两边错开 1~2 天（看起来每天都有信号，实际不是同一批日子）。
# 校验方法：run() 内部用的决策日集合 vs 只读全局交易日历独立重算的集合，逐一比对，
# 并把 3y 窗口的前 10 / 后 10 个决策日连同 T+1 执行日打印出来（肉眼可核）。
# ════════════════════════════════════════════════════════════════════
def check_days(cfg=None, s0=None, s1=None):
    """决策日序列校验：回测口径 == 实盘口径，且每个决策日的次日开盘就是执行日。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    idx = P["idx"]
    every = int(cfg["decide_every"])
    ph = int(cfg["decision_phase"])
    anchor = cfg["decision_anchor"]
    s0, s1 = s0 or W3Y[0], s1 or W3Y[1]
    prm = params_from_cfg(cfg, start=s0, end=s1)
    print(f"[决策日] 锚点={anchor}（第 0 个交易日）  频率=每 {every} 个交易日  "
          f"相位={ph}（决策日 = (序号-相位) % {every} == 0）  窗口 {s0} → {s1}")
    back = [str(pd.Timestamp(idx[i]).date()) for i in run(P, D, mask, pct, prm)["decisions"]]
    # 独立重算：只读 meta/index_calendar.json，不碰面板、不碰 run()
    cal = full_calendar()
    a = int(cal.searchsorted(pd.Timestamp(anchor)))
    o = np.arange(len(cal)) - a
    sel = cal[(np.mod(o - ph, every) == 0) & (cal >= pd.Timestamp(s0)) & (cal <= pd.Timestamp(s1))]
    live = [str(d.date()) for d in sel]
    ok = back == live
    print(f"[决策日] 回测 run() 取 {len(back)} 个 ｜ 实盘按日历独立重算 {len(live)} 个 "
          f"｜ 一致={'是' if ok else '否'}")
    if not ok:
        miss = sorted(set(live) - set(back))
        extra = sorted(set(back) - set(live))
        print(f"    ✗ 不一致：日历有而回测没有 {miss[:5]}；回测有而日历没有 {extra[:5]}")
    # 序号/取模/T+1 执行日（前 10 + 后 10）
    pos = {str(pd.Timestamp(d).date()): k for k, d in enumerate(idx)}
    for lbl, part in (("前 10", back[:10]), ("后 10", back[-10:])):
        print(f"  {lbl}个决策日：")
        for dt in part:
            k = pos[dt]
            nxt = str(pd.Timestamp(idx[k + 1]).date()) if k + 1 < len(idx) else "-（窗口末日，不执行）"
            oo = int(day_ordinal(idx[[k]], anchor)[0])
            print(f"    {dt}  序号{oo:5d}  %{every}={oo % every}  →  T+1 开盘 {nxt}")
    today = day_info(idx, len(idx) - 1, every, anchor, ph)
    print(f"[决策日] 最新交易日 {today['date']}：序号={today['ord']} %{every}={today['mod']} → "
          f"{'决策日' if today['is_decision'] else '非决策日'}")
    return dict(ok=bool(ok), n=len(back), today=today)


def phases_report(cfg=None, windows=(("3y", W3Y), ("5y", W5Y))):
    """同一锚点的 N 个相位 × 两窗口：最好 / 中位 / 最差（相位 = 启动偏移，不是可调参数）。"""
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    every = int(cfg["decide_every"] or 3)
    out = {}
    for tag, (s0, s1) in windows:
        print(f"\n=== {tag} {s0} → {s1}：每 {every} 个交易日决策，锚点 {cfg['decision_anchor']} ===")
        rows = []
        for ph in range(every):
            prm = replace(params_from_cfg(cfg, start=s0, end=s1), decision_phase=ph)
            res = run(P, D, mask, pct, prm)
            st = stats(res, prm)
            ex = risk_extra(res)
            row = dict(window=tag, phase=ph, cagr=st["cagr"], cups=st["cups"], mdd=st["mdd"],
                       sharpe=st["sharpe"], calmar=st["calmar"], turn=st["turn_wk"],
                       cost=st["cost_yr"], avg_n=ex["avg_n"], n_trades=st["n_trades"],
                       worst1=ex["worst1"], worst2=ex["worst2"], worst5=ex["worst5"],
                       t=st["t"], year=by_year(res["eq"]))
            rows.append(row)
            mark = "  ←本档" if ph == int(cfg["decision_phase"]) else ""
            print(f"  相位{ph}: 年化{row['cagr'] * 100:7.2f}% 累计{row['cups'] * 100:8.2f}% "
                  f"回撤{row['mdd'] * 100:6.1f}% 夏普{row['sharpe']:5.2f} Calmar{row['calmar']:5.2f} "
                  f"换手{row['turn'] * 100:5.1f}%/周 成本{row['cost'] * 100:4.2f}%/年 "
                  f"持仓{row['avg_n']:4.2f}只 笔数{row['n_trades']:4d} "
                  f"最坏1{row['worst1'] * 100:6.2f}% 最坏2{row['worst2'] * 100:6.2f}% "
                  f"最坏5{row['worst5'] * 100:6.2f}% t={row['t']:5.2f}{mark}")
            print(f"          分年 " + " ".join(f"{y}:{v * 100:+.1f}%" for y, v in row["year"].items()))
        cg = np.array([r["cagr"] for r in rows])
        print(f"  → {tag} 相位：最好{np.nanmax(cg) * 100:.2f}% 中位{float(np.nanmedian(cg)) * 100:.2f}% "
              f"最差{np.nanmin(cg) * 100:.2f}%（相差{np.nanmax(cg) * 100 - np.nanmin(cg) * 100:.1f}pp）")
        out[tag] = rows
    return out


# ════════════════════════════════════════════════════════════════════
# 实盘：目标权重 → 下单清单（与 flow_strategy.build_plan 同一套口径）
# ════════════════════════════════════════════════════════════════════
def _lot(shares, lot=LOT):
    return int(shares // lot * lot)


def _code6(sid):
    """universe.csv 的 sh510300 → 510300（下单/持仓/行情都用 6 位）。"""
    return sid[2:] if str(sid)[:2] in ("sh", "sz") else str(sid)


def _pool_names():
    """6 位代码 → 中文名（universe.csv；缺名返回空串，绝不用代码冒充名字）。"""
    try:
        with open(os.path.join(META, "universe.csv")) as f:
            return {r["code"][2:]: r["name"] for r in csv.DictReader(f)}
    except Exception:                                        # noqa: BLE001
        return {}


def _load_price_cache():
    """不复权收盘价缓存（quant2/data/price），返回 (last_date, {6位代码: close})。"""
    px, last = {}, None
    for fn in os.listdir(RAWP):
        if not fn.endswith(".json"):
            continue
        try:
            bars = json.load(open(os.path.join(RAWP, fn))).get("bars") or []
        except Exception:                                    # noqa: BLE001
            continue
        if not bars:
            continue
        d0, close = bars[-1][0], float(bars[-1][2])
        px[_code6(fn[:-5])] = close
        if last is None or d0 > last:
            last = d0
    return last, px


def read_holdings(prefer_broker=True):
    """真实持仓 {6位代码: shares}。frida 优先，失败退回 portfolio.json（与 flow 同一口径）。"""
    if prefer_broker:
        try:
            from broker_frida import ThsBroker
            pos = ThsBroker().get_positions() or {}
            out = {_code6(c): int(v.get("shares") or 0) for c, v in pos.items()
                   if int(v.get("shares") or 0) > 0}
            if out:
                return out, "frida"
        except Exception as e:                               # noqa: BLE001
            print(f"[plan] frida 持仓读取失败: {e} → 退回 portfolio.json", file=sys.stderr)
    with open(os.path.join(HERE, "portfolio.json")) as f:
        pf = json.load(f)
    out = {_code6(c): int(v.get("shares") or 0)
           for c, v in pf.get("positions", {}).items() if int(v.get("shares") or 0) > 0}
    return out, "portfolio.json"


def read_account(prefer_broker=True):
    if prefer_broker:
        try:
            from broker_frida import ThsBroker
            a = ThsBroker().get_account() or {}
            ta, cash = float(a.get("total_asset") or 0), float(a.get("cash") or 0)
            if ta > 0:
                return dict(total_asset=ta, cash=cash,
                            market_value=float(a.get("market_value") or 0), source="frida")
        except Exception as e:                               # noqa: BLE001
            print(f"[plan] frida 账户读取失败: {e} → 退回 portfolio.json", file=sys.stderr)
    with open(os.path.join(HERE, "portfolio.json")) as f:
        acc = (json.load(f) or {}).get("account") or {}
    return dict(total_asset=float(acc.get("total_asset") or 0),
                cash=float(acc.get("cash") or 0),
                market_value=float(acc.get("market_value") or 0), source="portfolio.json")


def latest_targets(cfg=None, prm=None, quiet=False):
    """下一交易日的目标持仓（权重口径）。

    做法：用**回测同一个 run()** 跑完历史到最后一个已收盘交易日，取当日收盘决策：
      on=False（510300 < MA60）→ 目标为空（次日开盘清仓）
      否则 目标 = 持仓 - 次日卖出 + 次日买入，网格跳档的按网格目标权重，其余按当前市值权重
    """
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    end = str(P["idx"][-1].date())
    _stale = stale_warning(end)
    if _stale:
        print(f"⚠️ [chase] {_stale} —— 信号日落后，别当新数据用（先跑 scripts/chase_daily.sh）",
              file=sys.stderr, flush=True)
    prm = replace(prm or params_from_cfg(cfg, start=cfg["live_start"], end=end), end=end)
    res = run(P, D, mask, pct, prm, return_state=True)
    st = res["state"]
    i, eq = st["i"], max(st["eq"], 1.0)
    C, cols, names = P["C"], P["cols"], P["names"]
    sell = set(st["want_sell"])
    targets = {}
    if st["on"]:
        grid = {j: w for j, w in st["want_grid"]}
        for j, q in st["shares"].items():
            if j in sell:
                continue
            p = C[i, j]
            w = grid.get(j)
            if w is None:
                w = (q * float(p) / eq) if np.isfinite(p) else 0.0
            targets[j] = float(w)
        for j, w in st["want_buy"].items():
            if j not in sell:
                targets[j] = float(w)
    tg = {}
    _trim = set(st.get("guard", {}).get("trim") or [])
    _gs = st.get("guard", {})
    for j, w in targets.items():
        sid = str(cols[j])
        nm = names.get(sid, "")
        tg[sid] = dict(code=_code6(sid), sid=sid, name=nm, weight=float(w),
                       theme=theme_of(nm), index=index_of(nm),
                       pct=float(st["pct"].get(j, np.nan)), guard_trim=bool(j in _trim))
    dec = day_info(P["idx"], i, prm.decide_every, prm.decision_anchor, prm.decision_phase)
    _bj, _cb, _mb = index_ma_series(P, D, prm)
    _c = float(_cb[i]) if _bj is not None else float("nan")
    _m = float(_mb[i]) if _bj is not None else float("nan")
    sw = dict(code=_code6(BENCH), ma_n=int(prm.index_ma), on=bool(st["on"]), close=_c, ma=_m,
              diff=(float(_c / _m - 1.0) if (np.isfinite(_c) and np.isfinite(_m) and _m) else float("nan")))
    if not quiet:
        print(f"[chase] 目标日={end} 空仓开关={'空仓(指数<MA%s)' % prm.index_ma if not st['on'] else '持仓'}"
              f" 目标 {len(tg)} 只 可交易池 {int(mask[i].sum())} 只"
              f" 护栏目标仓位{float(_gs.get('scale', 1.0)):.0%}")
    return dict(as_of=end, price_date=end, on=bool(st["on"]), cause=st["cause"],
                eq=float(st["eq"]), cash=float(st["cash"]), n_targets=len(tg),
                n_pool=int(mask[i].sum()), targets=tg, guard=dict(_gs),
                dec=dec, switch=sw,
                held={_code6(cols[j]): int(q) for j, q in st["shares"].items()},
                next_sell=[_code6(cols[j]) for j in st["want_sell"]],
                cfg={k: cfg[k] for k in ("n_hold", "entry_pct", "hold_pct", "theme_cap",
                                         "index_cap", "corr_threshold", "corr_cluster_cap",
                                         "grid_step", "stop_k", "index_ma",
                                         "max_order_pct")},
                prm=dict(theme_cap=int(prm.theme_cap), index_cap=int(prm.index_cap),
                         n_hold=int(prm.n_hold), corr_threshold=float(prm.corr_threshold),
                         corr_cluster_cap=int(prm.corr_cluster_cap),
                         stop_k=float(prm.stop_k), grid_step=float(prm.grid_step),
                         hold_pct=float(prm.hold_pct), entry_pct=float(prm.entry_pct),
                         index_ma=int(prm.index_ma), decide_every=int(prm.decide_every),
                         decision_phase=int(prm.decision_phase),
                         spec_stop=bool(prm.spec_stop), grid=bool(prm.grid),
                         decision_anchor=str(prm.decision_anchor)))


def preview_targets(cfg=None, quiet=False):
    """假设「空仓开关=ON 且次日就是决策日」时的建仓名单（只用于核对，不是下单清单）。

    与 run() 的 3b 完全同一条规则（空仓起步：keep 为空、slots=n_hold、无换仓额度）：
    动量分位 >= 1-entry_pct 的候选按分位从强到弱，同指数（index_cap）与同主题（theme_cap）
    双重上限，取满 n_hold。同指数去重与 run() 共用 idx_snapshot()，T-1 口径完全一致。
    用途：开关关着的时候，让您先看到"一旦放行会买哪 8 只、各多少钱"。
    """
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    i = len(P["idx"]) - 1
    cols, names = P["cols"], P["names"]
    pi, ok = pct[i], mask[i]
    prm = params_from_cfg(cfg, start=cfg["live_start"], end=str(P["idx"][i].date()))
    snap = idx_snapshot(P, D, i, prm) if prm.index_cap else None
    gid = snap["gid"] if snap else None
    lead = snap["lead"] if snap else {}
    cid = corr_snapshot(P, D, i, prm, mask)["cid"]
    cand = [j for j in range(len(cols))
            if ok[j] and np.isfinite(pi[j]) and pi[j] >= 1.0 - float(cfg["entry_pct"])]
    if prm.index_cap:
        cand = [j for j in cand if j == lead.get(int(gid[j]), j)]
    cand.sort(key=lambda j: -pi[j])
    cap = int(cfg["theme_cap"] or 0)
    kcap = int(prm.corr_cluster_cap or 0)
    picked, tc, icnt, ccnt = [], {}, {}, {}
    for j in cand:
        t = theme_of(names.get(cols[j], ""))
        g = int(gid[j]) if prm.index_cap else -1
        c = int(cid[j]) if kcap else -1
        if cap and tc.get(t, 0) >= cap:               # 第三步 赛道主题上限
            continue
        if prm.index_cap and icnt.get(g, 0) >= prm.index_cap:   # 第一步 同指数去重
            continue
        if kcap and ccnt.get(c, 0) >= kcap:           # 第二步 相关性聚类上限
            continue
        picked.append(j)
        tc[t] = tc.get(t, 0) + 1
        icnt[g] = icnt.get(g, 0) + 1
        ccnt[c] = ccnt.get(c, 0) + 1
        if len(picked) >= int(cfg["n_hold"]):         # 第四步 取满 n_hold
            break
    w = 1.0 / int(cfg["n_hold"])
    tg = {}
    for j in picked:
        sid = str(cols[j])
        nm = names.get(sid, "")
        tg[sid] = dict(code=_code6(sid), sid=sid, name=nm, weight=w,
                       theme=theme_of(nm), index=index_of(nm), pct=float(pi[j]))
    if not quiet:
        print(f"[预览] 假设空仓开关=ON 且次日为决策日（{P['idx'][i].date()} 动量分位）："
              f"{len(tg)} 只候选，池 {int(ok.sum())} 只")
    return dict(as_of=str(P["idx"][i].date()), price_date=str(P["idx"][i].date()),
                on=True, cause="preview", eq=0.0, cash=0.0, n_targets=len(tg),
                n_pool=int(ok.sum()), targets=tg, held={}, next_sell=[],
                cfg={k: cfg[k] for k in ("n_hold", "entry_pct", "hold_pct", "theme_cap",
                                         "index_cap", "corr_threshold", "corr_cluster_cap",
                                         "grid_step", "stop_k", "index_ma",
                                         "max_order_pct")},
                prm=dict(theme_cap=cap, index_cap=int(prm.index_cap),
                         n_hold=int(cfg["n_hold"]), corr_threshold=float(prm.corr_threshold),
                         corr_cluster_cap=int(prm.corr_cluster_cap),
                         stop_k=float(cfg["stop_k"]),
                         grid_step=float(cfg["grid_step"]), hold_pct=float(cfg["hold_pct"]),
                         entry_pct=float(cfg["entry_pct"]), index_ma=int(cfg["index_ma"])))


def build_plan(cfg=None, totals=None, holdings=None, tg=None, quiet=False):
    """目标权重 → 每只目标股数/金额/当前持仓/差额/今日下单量（只算不下单）。

    口径与 flow_strategy.build_plan 一致：卖出先于买入（A 股卖出回款当日可用），
    按动量分位从强到弱分配资金；单只单次 ≤ 总资产 × chase.max_order_pct。
    100 股整手；不足 min_order_shares（5000）的差额不下单（trade.py 有 5000 股下限）。
    """
    cfg = dict(cfg or load_cfg())
    tg = tg or latest_targets(cfg=cfg, quiet=quiet)
    ta_info = totals or read_account()
    hold, hold_src = holdings if isinstance(holdings, tuple) else (holdings or read_holdings()[0], "arg")
    px_date, px = _load_price_cache()
    names = _pool_names()
    total_asset = float(ta_info["total_asset"])
    cash = float(ta_info["cash"])
    cap_amt = total_asset * float(cfg["max_order_pct"])
    min_sh = int(cfg["min_order_shares"])

    by_code = {v["code"]: v for v in tg["targets"].values()}
    codes = sorted(set(by_code) | {c for c, q in tg["held"].items() if q > 0}
                   | {c for c, q in hold.items() if q > 0})
    rows = []
    for code in codes:
        t = by_code.get(code)
        name = (t or {}).get("name") or names.get(code, "")
        weight = float((t or {}).get("weight") or 0.0)
        price = float(px.get(code) or 0.0)
        cur = int(hold.get(code, 0))
        target_amount = weight * total_asset
        tgt_sh = _lot(target_amount / price) if price > 0 else 0
        delta = tgt_sh - cur
        note = ""
        buy = sell = 0
        if not tg["on"]:
            sell = cur                                  # 空仓开关：全部清掉
            note = "空仓开关触发 → 清仓"
        elif delta > 0:
            if delta < min_sh and tgt_sh >= min_sh:
                buy = min_sh                            # trade.py 5000 股下限，补到可下单
                note = f"差额{delta}股 < 最小下单量，按{min_sh}股补"
            elif delta >= min_sh:
                buy = delta
            else:
                note = f"差额{delta}股 < 最小下单量，本次不买"
        elif delta < 0:
            if tgt_sh > 0 and -delta < min_sh:
                note = f"差额{delta}股 < 最小下单量，本次不动"
            else:
                sell = -delta
                if (t or {}).get("guard_trim"):
                    note = f"护栏降仓（单日熔断/赛道崩塌）→ 减到目标 {tgt_sh:,} 股"
        if buy > 0 and price > 0:
            buy = min(buy, _lot(cap_amt / price))
        rows.append(dict(code=code, sid=(t or {}).get("sid") or _sid(code), name=name,
                         weight=weight, price=price, target_amount=round(target_amount, 2),
                         target_shares=int(tgt_sh), cur_shares=cur,
                         delta_shares=int(delta), buy_shares=int(buy), sell_shares=int(sell),
                         buy_amount=round(buy * price, 2), sell_amount=round(sell * price, 2),
                         theme=(t or {}).get("theme"), pct=(t or {}).get("pct"),
                         guard_trim=bool((t or {}).get("guard_trim")),
                         in_target=bool(t) and tg["on"], note=note))

    for r in rows:                                   # 空仓开关下不买
        if not tg["on"]:
            r["buy_shares"] = 0
            r["buy_amount"] = 0.0

    sells = [r for r in rows if r["sell_shares"] > 0]
    sell_total = sum(r["sell_amount"] for r in sells)
    budget = cash + sell_total
    buys = [r for r in rows if r["buy_shares"] > 0]
    buys.sort(key=lambda x: (-(x["pct"] if x["pct"] == x["pct"] else -1), x["code"]))
    want_total, remaining = 0.0, budget
    for r in buys:
        want_amt = r["buy_amount"]
        want_total += want_amt
        afford = _lot(remaining / r["price"]) if r["price"] > 0 else 0
        r["buy_shares"] = int(max(0, min(r["buy_shares"], afford)))
        r["buy_amount"] = round(r["buy_shares"] * r["price"], 2)
        remaining -= r["buy_amount"]
    buy_total = round(budget - remaining, 2)
    plan = dict(as_of=tg["as_of"], price_date=px_date, total_asset=round(total_asset, 2),
                cash=round(cash, 2), account_source=ta_info.get("source"),
                holdings_source=hold_src, on=bool(tg["on"]), cause=tg["cause"],
                n_targets=tg["n_targets"], n_pool=tg["n_pool"], guard=tg.get("guard") or {},
                dec=tg.get("dec") or {}, switch=tg.get("switch") or {},
                buy_total=round(buy_total, 2), want_total=round(want_total, 2),
                sell_total=round(sell_total, 2), budget=round(budget, 2),
                cash_after=round(cash + sell_total - buy_total, 2),
                cash_scaled=bool(want_total > buy_total + 1e-6), rows=rows,
                cfg=tg["cfg"], prm=tg["prm"])
    # 执行后持仓市值/仓位：按逐只 (当前 - 卖 + 买) × 现价算，不用"总资产-现金"
    # （账户总资产含冻结/在途资金，与 现金+市值 对不上，会算出假的 1.3% 仓位）
    plan["pos_value_after"] = round(sum(
        max(0, r["cur_shares"] - r["sell_shares"] + r["buy_shares"]) * r["price"] for r in rows), 2)
    plan["pos_ratio_after"] = round(plan["pos_value_after"] / max(total_asset, 1.0), 4)
    return plan


def _sid(code6):
    return ("sh" if str(code6)[0] in ("5", "6") else "sz") + str(code6)


def fmt_dec(d):
    """决策日状态一行。"""
    if not d:
        return "  [决策日] -"
    if not d.get("every"):
        return f"  [决策日] {d['date']} 周频口径（decide_every=0）"
    mark = "决策日" if d.get("is_decision") else "非决策日"
    nxt = d.get("next_decision") or "-"
    return (f"  [决策日] {d['date']} 交易日序号={d.get('ord')} % {d['every']} = {d.get('mod')}"
            f"（相位{d['phase']}，锚点 {d['anchor']}）→ {mark}；下一决策日 {nxt}")


def fmt_switch(sw):
    """空仓开关状态一行：510300 收盘 vs MA，差多少。"""
    if not sw:
        return "  [空仓开关] -"
    diff = sw.get("diff")
    ds = f"{diff * 100:+.2f}%" if diff == diff else "-"
    rel = "<" if (diff == diff and diff < 0) else ">="
    return (f"  [空仓开关] 510300 收盘 {sw['close']:.4f} {rel} MA{sw['ma_n']} {sw['ma']:.4f}"
            f"（{ds}）→ {'持仓' if sw['on'] else '空仓（次日开盘清仓，不建仓）'}")


def print_plan(plan):
    print(f"[建仓清单] 信号日={plan['as_of']}  价格日={plan['price_date']}  "
          f"总资产=¥{plan['total_asset']:,.2f} 可用现金=¥{plan['cash']:,.2f} ({plan['account_source']})")
    print(fmt_dec(plan.get("dec")))
    print(fmt_switch(plan.get("switch")))
    print(f"  目标 {plan['n_targets']} 只（N={plan['cfg']['n_hold']} 主题上限{plan['cfg']['theme_cap']} "
          f"相关簇上限{plan['cfg'].get('corr_cluster_cap', 0)}@ρ≥{plan['cfg'].get('corr_threshold', 0):.2f}）"
          f"；可交易池 {plan['n_pool']} 只；空仓开关={'空仓' if not plan['on'] else '持仓'}")
    print(f"  {'代码':<8}{'名称':<12}{'主题':<10}{'现价':>7}{'目标金额':>11}{'目标股数':>9}"
          f"{'当前股数':>9}{'差额':>8}{'买/卖':>10}  动作")
    for r in sorted(plan["rows"], key=lambda x: (x["buy_shares"] == 0 and x["sell_shares"] == 0,
                                                 -(x["buy_amount"] + x["sell_amount"]))):
        act = ("清仓" if (not plan["on"] and r["cur_shares"] > 0) else
               "买入" if r["buy_shares"] > 0 else
               "卖出" if r["sell_shares"] > 0 else
               ("持有" if r["in_target"] else "空"))
        bs = (f"+{r['buy_shares']:,}" if r["buy_shares"] > 0 else
              (f"-{r['sell_shares']:,}" if r["sell_shares"] > 0 else "-"))
        print(f"  {r['code']:<8}{(r['name'] or '')[:11]:<12}{str(r['theme'] or '-')[:9]:<10}"
              f"{r['price']:>7.3f}{r['target_amount']:>11,.0f}{r['target_shares']:>9,}"
              f"{r['cur_shares']:>9,}{r['delta_shares']:>8,}{bs:>10}  {act}")
        if r["note"]:
            print(f"      ↳ {r['note']}")
    print(f"  可用资金 = 现金 ¥{plan['cash']:,.2f} + 当日卖出回款 ¥{plan['sell_total']:,.2f} "
          f"= ¥{plan['budget']:,.2f}")
    print(f"  合计: 买入 ¥{plan['buy_total']:,.2f}（需要 ¥{plan['want_total']:,.2f}）  "
          f"卖出 ¥{plan['sell_total']:,.2f}  执行后现金 ¥{plan['cash_after']:,.2f}  "
          f"持仓 ¥{plan['pos_value_after']:,.2f}（仓位 {plan['pos_ratio_after']:.1%}）"
          + (f"  ⚠️ 资金不足，已按动量分位从强到弱满足" if plan["cash_scaled"] else ""))
    _nt = sum(1 for r in plan["rows"] if r["in_target"])
    _nc = sum(1 for r in plan["rows"] if r["cur_shares"] > 0)
    _nb = sum(1 for r in plan["rows"] if r["buy_shares"] > 0)
    _ns = sum(1 for r in plan["rows"] if r["sell_shares"] > 0)
    _gap = sum(abs(r["delta_shares"]) for r in plan["rows"])
    act = ("一致（无需动作）" if (_nb == 0 and _ns == 0) else
           f"不一致：需买 {_nb} 笔 / 需卖 {_ns} 笔，股数差合计 {_gap:,}")
    print(f"  持仓一致性: 目标 {_nt} 只 / 实际 {_nc} 只 → {act}")


def trades_report(res, prm):
    """逐笔成交明细（只读导出）：打印 + 写 dumps/trades_{start}_{end}.json。

    数据全部来自 run() 的 legs（真实成交，非估算）；reason 是 decide() 当时的触发条件标注。
    """
    legs = res["legs"]
    eq = res["eq"]
    hn = res["hold_n"]
    offd = res["off_dates"]
    idx = eq.index

    def nat(a, b):
        return (pd.Timestamp(b) - pd.Timestamp(a)).days

    buys_by_day = {}
    for t in legs:
        buys_by_day.setdefault(t["entry_d"], []).append(t)

    RSN = {"regime": "空仓开关触发（510300 跌破 MA60）",
           "buffer": "动量掉出缓冲带（分位<0.70）",
           "rot": "决策日换仓（有更强的标的）",
           "eod": "回测结束平仓",
           "stop": "止损（ATR 跟踪止损触发）",
           "guard": "护栏卖出（单日熔断）",
           "data": "决策日换仓（当日动量分位缺失，按不达标处理）"}

    print("")
    print("=" * 78)
    print(f"逐笔成交明细  {prm.start} → {prm.end}   （run() 实际成交，非估算）")
    print("=" * 78)
    for n, t in enumerate(legs, 1):
        why = t["reason"]
        code6 = t["code"][2:] if t["code"][:2] in ("sh", "sz") else t["code"]
        print(f"第{n}笔 买入 {t['entry_d']} 开盘价 {t['entry_p']:.4f} 买 {t['entry_q']:,} 股 "
              f"金额 {t['entry_amt']:,.0f} 元（{code6} {t['name']}，动量分位 "
              f"{t['entry_pct'] * 100:.1f}%（{t['entry_d']}）/ "
              f"{t['entry_pct_dec'] * 100:.1f}%（决策日 {t['entry_bdec_d']}））")
        td = t["exit_i"] - t["entry_i"]
        nd = nat(t["entry_d"], t["exit_d"])
        if why == "eod":
            print(f"      至 {t['exit_d']} 回测结束仍持有，按当日收盘价 {t['exit_p']:.4f} 估值 "
                  f"持有 {td} 个交易日（自然日 {nd} 天） 浮动盈亏 "
                  f"{t['pnl']:+,.0f} 元 {t['ret'] * 100:+.2f}%")
        else:
            print(f"      卖出 {t['exit_d']} 开盘价 {t['exit_p']:.4f} 卖 {t['exit_q']:,} 股 "
                  f"金额 {t['exit_amt']:,.0f} 元 持有 {td} 个交易日（自然日 {nd} 天） "
                  f"盈亏 {t['pnl']:+,.0f} 元 {t['ret'] * 100:+.2f}%")
        det = []
        if why == "regime":
            det.append(f"决策日 {t['dec_d']} 510300 收 {t['b_close_dec']:.4f} < MA60 "
                       f"{t['b_ma_dec']:.4f}（低 {abs(t['b_close_dec'] / t['b_ma_dec'] - 1) * 100:.2f}%）")
        elif why == "buffer":
            det.append(f"决策日 {t['dec_d']} 动量分位 {t['sell_pct'] * 100:.1f}% < 70%")
        elif why == "rot":
            det.append(f"决策日 {t['dec_d']} 动量分位 {t['sell_pct'] * 100:.1f}% 仍在缓冲带内，"
                       f"被同指数去重/赛道上限/持仓只数挤出")
        elif why == "data":
            det.append(f"决策日 {t['dec_d']} 动量分位缺失")
        elif why == "eod":
            det.append(f"窗口最后一个交易日 {t['exit_d']} 收盘，非策略卖出")
        if why in ("rot", "buffer", "data"):
            _nb = [x for x in buys_by_day.get(t["exit_d"], []) if x is not t]
            if _nb:
                det.append("同日开盘换入 " + "、".join(
                    f"{x['code'][2:]} {x['name']}（分位 {x['entry_pct_dec'] * 100:.1f}%）"
                    for x in _nb))
        print(f"      卖出原因：{RSN.get(why, why)}" + ("；" + "；".join(det) if det else ""))
        if np.isfinite(t["b_close"]) and np.isfinite(t["b_ma"]) and t["b_ma"] > 0:
            _dd = (t["b_close"] / t["b_ma"] - 1) * 100
            print(f"      卖出日 510300 收 {t['b_close']:.4f} / MA60 {t['b_ma']:.4f}"
                  f"（{'高' if _dd >= 0 else '低'} {abs(_dd):.2f}%）")
        print("")

    eq0, eq1 = float(eq.iloc[0]), float(eq.iloc[-1])
    hold_days = int((hn > 0).sum())
    total_days = int(len(eq))
    s_pnl = sum(t["pnl"] for t in legs)
    closed = [t for t in legs if t["reason"] != "eod"]
    s_closed = sum(t["pnl"] for t in closed)
    print("-" * 78)
    print(f"总览  {prm.start} → {prm.end}")
    print(f"  交易日 {total_days} 天；起始资金 {eq0:,.0f} 元（窗口起点空仓起步，不继承窗口外持仓）")
    if legs:
        _last = max((t["exit_d"] for t in closed), default="—")
        print(f"  第一笔买入 {legs[0]['entry_d']}；最后一笔真实卖出 {_last}"
              + (f"；期末 {legs[-1]['exit_d']} 仍持有 {len(legs) - len(closed)} 只（按收盘价估值）"
                 if len(closed) < len(legs) else ""))
    print(f"  在仓 {hold_days} 天 / 总 {total_days} 天 = {hold_days / total_days * 100:.1f}%"
          f"（空仓 {total_days - hold_days} 天）")
    print(f"  成交 {len(legs)} 笔（已平仓 {len(closed)} 笔 + 期末仍持有 {len(legs) - len(closed)} 笔）")
    print(f"  已平仓 {len(closed)} 笔盈亏加总 {s_closed:+,.0f} 元（算术加总，不复利）")
    if len(closed) < len(legs):
        print(f"  含期末持仓浮盈的 {len(legs)} 笔加总 {s_pnl:+,.0f} 元（浮盈 "
              f"{s_pnl - s_closed:+,.0f} 元，按 {legs[-1]['exit_d']} 收盘价估值）")
    print(f"  复利累计 {eq1 / eq0 - 1:+.2%}（期末权益 {eq1:,.0f} 元，与 --backtest 输出一致）")
    eps = []
    for k, ds in enumerate(offd):
        i = int(idx.searchsorted(pd.Timestamp(ds)))
        if eps and i == eps[-1][-1][0] + 1:
            eps[-1].append((i, ds))
        else:
            eps.append([(i, ds)])
    if eps:
        print(f"  空仓开关触发 {len(eps)} 次（510300 收盘 < MA60 的连续区间，共 {len(offd)} 个交易日）：")
        for k, e in enumerate(eps, 1):
            _st, _ed = e[0][1], e[-1][1]
            sold = [t for t in legs if t["reason"] == "regime" and t["dec_d"] in [_x[1] for _x in e]]
            tail = ("，次日开盘清仓 " + "、".join(f"{t['code'][2:]} {t['name']}" for t in sold)
                    if sold else "，当时已空仓（无仓位可清）")
            print(f"    {k}) {_st} → {_ed} 共 {len(e)} 个交易日{tail}")
    else:
        print("  空仓开关触发 0 次（窗口内 510300 始终在 MA60 上方）")
    print("-" * 78)
    p = os.path.join(DUMPS, f"trades_{prm.start}_{prm.end}.json")
    json.dump(dict(start=prm.start, end=prm.end, eq0=eq0, eq1=eq1, cups=eq1 / eq0 - 1,
                   hold_days=hold_days, total_days=total_days,
                   pnl_sum=s_pnl, pnl_sum_closed=s_closed,
                   regime_episodes=[dict(start=e[0][1], end=e[-1][1], days=len(e)) for e in eps],
                   legs=[{k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                          for k, v in t.items()} for t in legs]),
              open(p, "w"), ensure_ascii=False, indent=1, default=str)
    print("明细已写", p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", "--run", dest="run", action="store_true")
    ap.add_argument("--suite", action="store_true")
    ap.add_argument("--caps", action="store_true", help="主题上限 5/6/8/不限 × 3y/5y 对照")
    ap.add_argument("--plan", action="store_true", help="下一交易日建仓清单（只算不下单）")
    ap.add_argument("--targets", action="store_true", help="只打印目标权重")
    ap.add_argument("--preview", action="store_true",
                    help="假设空仓开关=ON 且次日为决策日时的建仓名单（核对用，不是下单清单）")
    ap.add_argument("--themes", action="store_true")
    ap.add_argument("--index-map", action="store_true",
                    help="同指数分组审计（标的指数 → ETF 清单 + 组内相关系数验证）")
    ap.add_argument("--build-panel", action="store_true")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--theme-cap", type=int, default=None)
    ap.add_argument("--index-cap", type=int, default=None, help="同一标的指数最多几只（0=关）")
    ap.add_argument("--corr-cap", type=int, default=None, help="同一相关性簇最多几只（0=关）")
    ap.add_argument("--corr-thr", type=float, default=None, help="相关性聚类阈值（默认 0.90）")
    ap.add_argument("--corr-map", action="store_true",
                    help="相关性聚类审计（簇结构 + 新目标清单两两 ρ 最大值）")
    ap.add_argument("--abc", action="store_true", help="A/B/C 三档 × 3y/5y + 阈值敏感性")
    ap.add_argument("--check-days", action="store_true",
                    help="决策日序列校验（回测 run() vs 全局日历独立重算，前10/后10 + T+1）")
    ap.add_argument("--phases", action="store_true",
                    help="每 N 日决策的 N 个相位 × 3y/5y（最好/中位/最差）")
    ap.add_argument("--pod", action="store_true",
                    help="单吊赛道四个版本(V1参照/V2单吊/V3强制单吊/V4集中4只) × 3y/5y 全指标")
    ap.add_argument("--guard", action="store_true",
                    help="V2 单吊下的护栏消融（熔断 6/8/10% + 赛道崩塌半仓）")
    ap.add_argument("--win", action="store_true", help="1y/2y/3y/5y 四窗口年化")
    ap.add_argument("--dd", action="store_true", help="历史上最惨一段（峰值/谷底/回本/持有赛道）")
    ap.add_argument("--n-hold", type=int, default=None, help="目标持仓只数（默认 8）")
    ap.add_argument("--theme-mode", default=None, help="cap|top1（top1=强制全仓动量第一的赛道）")
    ap.add_argument("--guard-day", type=float, default=None, help="单日亏损熔断阈值（0=关）")
    ap.add_argument("--guard-theme", type=float, default=None, help="赛道崩塌阈值（0=关，0.5=过半破MA20）")
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trades", action="store_true",
                    help="逐笔成交明细（只读导出：打印 + 写 dumps/trades_START_END.json）")
    a = ap.parse_args()
    cfg = load_cfg()
    if a.theme_cap is not None:
        cfg["theme_cap"] = int(a.theme_cap)
    if a.index_cap is not None:
        cfg["index_cap"] = int(a.index_cap)
    if a.corr_cap is not None:
        cfg["corr_cluster_cap"] = int(a.corr_cap)
    if a.corr_thr is not None:
        cfg["corr_threshold"] = float(a.corr_thr)
    if a.n_hold is not None:
        cfg["n_hold"] = int(a.n_hold)
    if a.theme_mode is not None:
        cfg["theme_mode"] = str(a.theme_mode)
    if a.guard_day is not None:
        cfg["guard_day"] = float(a.guard_day)
    if a.guard_theme is not None:
        cfg["guard_theme"] = float(a.guard_theme)
    if a.cost is not None:
        cfg["cost"] = float(a.cost)
    os.makedirs(DUMPS, exist_ok=True)

    if a.pod or a.guard or a.win or a.dd:
        out = {}
        if a.dd:
            out["dd"] = dd_report(cfg)
        if a.pod:
            out["pod"] = pod_report(cfg)
        if a.guard:
            out["guard"] = guard_report(cfg)
        if a.win:
            v = POD_VARIANTS[:2] if not a.guard_day and not a.guard_theme else [
                ("V2+护栏", dict(theme_cap=0, index_cap=1,
                                 guard_day=float(cfg["guard_day"]),
                                 guard_theme=float(cfg["guard_theme"])))]
            out["win"] = windows_report(cfg, variants=v)
        p = os.path.join(DUMPS, f"pod_{datetime.now():%Y%m%d_%H%M}.json")
        json.dump(out, open(p, "w"), ensure_ascii=False, indent=1, default=float)
        print("\n写完", p)
        return 0

    if a.check_days:
        check_days(cfg, s0=a.start, s1=a.end)
        return 0

    if a.phases:
        out = phases_report(cfg)
        json.dump(out, open(os.path.join(DUMPS, "phases.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        print("\n写完", os.path.join(DUMPS, "phases.json"))
        return 0

    if a.themes:
        P = build_panel()
        m = {}
        for n in sorted(set(P["names"].values())):
            m.setdefault(theme_of(n), []).append(n)
        json.dump({str(k): sorted(v) for k, v in m.items()},
                  open(os.path.join(OUT, "theme_map.json"), "w"), ensure_ascii=False, indent=1)
        for t, v in sorted(m.items(), key=lambda x: -len(x[1])):
            print(f"{str(t):>12} {len(v):4d}  e.g. {', '.join(v[:4])}")
        return 0

    if a.index_map:
        index_map_report(cfg)
        return 0

    if a.corr_map:
        corr_map_report(cfg)
        return 0

    if a.abc:
        out = abc_report(cfg)
        json.dump(out, open(os.path.join(DUMPS, "abc.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        print("\n写完", os.path.join(DUMPS, "abc.json"))
        return 0

    if a.caps:
        rows = caps_report(cfg)
        best = pick_cap(rows)
        if best:
            print(f"\n[推荐] 上限={best['cap'] or '不限'}（{best['window']} 年化{best['cagr'] * 100:.2f}% "
                  f"最坏单日{best['worst1'] * 100:.2f}%）")
        json.dump(rows, open(os.path.join(DUMPS, "caps.json"), "w"),
                  ensure_ascii=False, indent=1, default=float)
        return 0

    if a.preview:
        tg = preview_targets(cfg=cfg, quiet=a.json)
        plan = build_plan(cfg=cfg, tg=tg, quiet=a.json)
        if a.json:
            print(json.dumps(plan, ensure_ascii=False, default=float, indent=1))
        else:
            print_plan(plan)
            print("  ↑ 预览口径：空仓开关=ON 且次日为决策日；实际下单清单以 signal_generator --mode=chase 为准")
        return 0

    if a.plan or a.targets:
        tg = latest_targets(cfg=cfg, quiet=a.json)
        if a.targets:
            for code, r in sorted(tg["targets"].items(), key=lambda kv: -kv[1]["weight"]):
                print(f"  {r['code']}  {(r['name'] or '')[:14]:<16}{r['weight']:.2%}  "
                      f"动量分位{r['pct']:.0%}  主题={r['theme']}")
            return 0
        plan = build_plan(cfg=cfg, tg=tg, quiet=a.json)
        if a.json:
            print(json.dumps(plan, ensure_ascii=False, default=float, indent=1))
        else:
            print_plan(plan)
        json.dump(plan, open(os.path.join(DUMPS, f"plan_{datetime.now():%Y%m%d}.json"), "w"),
                  ensure_ascii=False, default=float, indent=1)
        return 0

    P, D, mask, pct = prep(force=a.build_panel)
    print(f"面板 {P['C'].shape[0]} 交易日 × {P['C'].shape[1]} 只  "
          f"{P['idx'][0].date()} → {P['idx'][-1].date()}")
    if a.run:
        prm = replace(params_from_cfg(cfg, start=a.start or W3Y[0], end=a.end or str(P["idx"][-1].date())))
        res = run(P, D, mask, pct, prm)
        st = stats(res, prm, "chase")
        ex = risk_extra(res, "chase")
        print(fmt2(st))
        print(f"平均持仓 {ex['avg_n']:.1f} 只 | 最坏单日 {ex['worst1'] * 100:.2f}%({ex['worst1_date']}) "
              f"| 最坏两日 {ex['worst2'] * 100:.2f}%({ex['worst2_date']}) "
              f"| 最坏五日 {ex['worst5'] * 100:.2f}%({ex['worst5_date']}) "
              f"| 同主题占比 中位{ex['share_p50'] * 100:.0f}%/最大{ex['share_max'] * 100:.0f}%"
              f"(市值{ex['sharew_max'] * 100:.0f}%) "
              f"| 同一指数占比 中位{ex['idx_p50'] * 100:.0f}%/90分位{ex['idx_p90'] * 100:.0f}%"
              f"/最大{ex['idx_max2'] * 100:.0f}%(持仓≥2只口径,市值{ex['idxw_max'] * 100:.0f}%)"
              f"/最多{ex['idx_cnt_max']}只 "
              f"| 主题上限拦截 {res['cap_blocked']} 次 | 同指数拦截 {ex['idx_blocked']} 次"
              f"/卖出 {ex['idx_sells']} 次 | 相关簇最大占比 {ex['corr_max'] * 100:.0f}%"
              f"（中位{ex['corr_p50'] * 100:.0f}%，最多{ex['corr_cnt_max']}只）"
              f"相关簇拦截 {ex['corr_blocked']} 次/卖出 {ex['corr_sells']} 次 "
              f"| 护栏 单日熔断{ex['guard_day_trig']}次/赛道崩塌{ex['guard_theme_trig']}次 "
              f"| 空仓 {res['off_days']} 天")
        print("分年:", {k: round(v * 100, 2) for k, v in by_year(res["eq"]).items()})
        res["eq"].to_csv(os.path.join(DUMPS, f"eq_{prm.start}_{prm.end}.csv"))
        if a.trades:
            trades_report(res, prm)
        return 0
    if not a.suite:
        print("用 --suite / --caps / --plan / --backtest")
        return 0

    windows = [("3y", W3Y[0], W3Y[1]), ("5y", W5Y[0], W5Y[1])]
    variants = [
        ("基准", {}),
        ("空仓关", dict(regime=False)),
        ("网格关", dict(grid=False)),
        ("主题上限关", dict(theme_cap=0)),
        ("破MA20日频清仓", dict(exit_mode="ma20", stop_k=0.0)),
        ("无ATR止损", dict(stop_k=0.0)),
        ("持有带0.60", dict(hold_pct=0.60)),
        ("成本0.20%", dict(cost=0.002)),
        ("持仓6只", dict(n_hold=6)),
        ("持仓12只", dict(n_hold=12)),
    ]
    out = {}
    for tag, s0, s1 in windows:
        prm = replace(params_from_cfg(cfg, start=s0, end=s1))
        print(f"\n=== 窗口 {tag} {s0} → {s1} ===")
        base = run(P, D, mask, pct, prm)
        rows = [stats(base, prm, f"{tag} 基准")]
        print(fmt2(rows[0]), flush=True)
        for nm, kw in variants[1:]:
            r = run(P, D, mask, pct, replace(prm, **kw))
            st = stats(r, prm, f"{tag} {nm}")
            rows.append(st)
            print(fmt2(st), flush=True)
        for b in bench_rows(P, mask, D, prm, tag):
            print(fmt2(b), flush=True)
        print("  分年:", {k: round(v * 100, 2) for k, v in by_year(base["eq"]).items()})
        out[tag] = dict(rows=rows, eq={str(k.date()): float(v) for k, v in base["eq"].items()})
    json.dump(out, open(os.path.join(DUMPS, "suite.json"), "w"), ensure_ascii=False, indent=1,
              default=float)
    print("\n写完", os.path.join(DUMPS, "suite.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
