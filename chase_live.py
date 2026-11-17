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
  轮动   每周最后一个交易日收盘决策，次日开盘成交；缓冲带 = 已持有只要动量分位 >= hold_pct
         （默认 0.60）就留，掉出去才卖；最短持有 min_hold 个交易日（默认 5）
  换手   每周最多替换 max_swap 只（默认 1），把单边换手压到 15%/周以下
  网格   每只持仓按 ATR 网格只调权重 0.6~1.4 倍（锚=买入价，档距 grid_step×ATR，
         越跌越加、越涨越减），不改持仓集合；权重归一化到总仓位 1.0（不加杠杆）
  出场   **不做**日频破 MA20 清仓；改 ATR 跟踪止损（峰值 - k*ATR14，k=2.5，日频检查）
         周频只做动量分位检查
  主题   同一主题最多 theme_cap 只（主题表由名称关键词构造，落盘 theme_map.json 可审计）

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
# 数据层：日频面板 + 磁盘缓存
# ════════════════════════════════════════════════════════════════════
def calendar():
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    return cal[cal < pd.Timestamp.now().normalize()]


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
    hist = pd.DataFrame(np.cumsum(~np.isnan(C), axis=0))
    traded = pd.DataFrame(V) > 0
    return dict(mom=mom.to_numpy(np.float32), atr=atr.to_numpy(np.float32),
                ma20=ma20.to_numpy(np.float32), ma60=ma60.to_numpy(np.float32),
                amt20=amt20.to_numpy(np.float32), hist=hist.to_numpy(np.float32),
                traded=traded.to_numpy(bool))


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
    cost: float = 0.001            # 单边成本
    cash: float = 1_000_000.0
    warmup: int = 0


# ── 实盘参数默认值（config.yaml chase: 段缺键时的兜底；研究已冻结，别按历史调）──
DEFAULTS = dict(
    n_hold=8, entry_pct=0.20, hold_pct=0.70, min_hold=5, max_swap=0, swap_margin=0.0,
    cooldown=10, grid=True, grid_step=1.0, grid_lo=0.6, grid_hi=1.4, grid_dead=0.01,
    stop_k=3.0, stop_freq="weekly", exit_mode="atr", regime=True, theme_cap=2,
    cost=0.001, index_code="510300", index_ma=60, live_start="2023-11-17",
    max_order_pct=0.15, limit_buffer_pct=0.01, min_order_shares=5000,
)

_MODE_TO_PRM = dict(                      # config 键 → Params 字段
    n_hold="n_hold", entry_pct="entry_pct", hold_pct="hold_pct", min_hold="min_hold",
    max_swap="max_swap", swap_margin="swap_margin", cooldown="cooldown", grid="grid",
    grid_step="grid_step", grid_lo="grid_lo", grid_hi="grid_hi", grid_dead="grid_dead",
    stop_k="stop_k", stop_freq="stop_freq", exit_mode="exit_mode", regime="regime",
    index_ma="index_ma", theme_cap="theme_cap", cost="cost",
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
    for k in ("n_hold", "min_hold", "max_swap", "cooldown", "index_ma", "theme_cap",
              "min_order_shares"):
        cfg[k] = int(cfg[k])
    for k in ("entry_pct", "hold_pct", "swap_margin", "grid_step", "grid_lo", "grid_hi",
              "grid_dead", "stop_k", "cost", "max_order_pct", "limit_buffer_pct"):
        cfg[k] = float(cfg[k])
    cfg["grid"] = bool(cfg["grid"])
    cfg["regime"] = bool(cfg["regime"])
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


def run(P, D, mask, pct, prm: Params, return_state=False):
    """日频撮合 + 周频选股的组合模拟。返回净值/换手/成本/成交明细。

    return_state=True: 只给实盘用 —— 在最后一个交易日收盘后再跑一次决策，把"次日开盘
    该卖/该买/网格目标权重"连同持仓状态一起返回（不加 return_state 时逐位等价于研究版）。
    """
    idx = P["idx"]
    cols = P["cols"]
    names = P["names"]
    C, O, A_, RC = P["C"], P["O"], P["H"], P["RC"]
    atr, ma20 = D["atr"], D["ma20"]
    n = len(idx)
    cidx = {c: j for j, c in enumerate(cols)}
    bj = cidx.get(BENCH)
    close_b = C[:, bj] if bj is not None else np.full(n, np.nan)
    if bj is not None and int(prm.index_ma) != 60:
        # 非默认周期：同一公式（收盘价滚动均值）重算，60 走原 ma60 通道保证逐位不变
        _w = int(prm.index_ma)
        ma60_b = pd.Series(close_b).rolling(_w, min_periods=max(10, int(_w * 2 / 3))) \
            .mean().to_numpy(np.float32)
    else:
        ma60_b = D["ma60"][:, bj] if bj is not None else np.full(n, np.nan)
    limit = np.array([0.20 if LIMIT20.search(str(names.get(c, ""))) else 0.10 for c in cols],
                     dtype=np.float32)
    theme = np.array([theme_of(names.get(c, "")) for c in cols], dtype=object)
    ncol = len(cols)

    i0 = int(np.searchsorted(idx, pd.Timestamp(prm.start)))
    i1 = int(np.searchsorted(idx, pd.Timestamp(prm.end), side="right")) - 1
    wdec = weekly_decisions(idx, prm.start, prm.end)

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
    tr = {"regime": 0.0, "rot": 0.0, "grid": 0.0, "stop": 0.0}
    greed = {}                    # 网格上次触发价（棘轮）
    dbg = {"n_scan": 0, "n_changed": 0, "n_exec": 0}
    mult_prev = {}
    cause = "rot"
    cap_blocked = 0
    stop_pending = False
    sell_cause = {"stop": 0.0, "rot": 0.0, "regime": 0.0}
    n_sells = n_stop_days = 0
    stop_log, sell_log = [], []
    hold_n, theme_share = [], []     # 每日持仓只数 / 单一主题占持仓的最大比例（集中度实测）
    lastpx = {}                      # 每只最后一个有效收盘价（缺 bar 时按最近价估值，不是按 0）
    pos_log, nan_close = [], []      # 每日收盘持仓明细 / 当日缺收盘价的持仓数（数据体检用）

    def po(i, j):
        v = O[i, j]
        return float(v) if np.isfinite(v) and v > 0 else np.nan

    def pcx(i, j):
        v = C[i, j]
        return float(v) if np.isfinite(v) and v > 0 else np.nan

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
        # ── 3) 收盘决策 ─────────────────────────────────────────────
        want_sell, want_buy, want_grid = set(), {}, []
        stop_set = set()
        on = True
        if prm.regime and bj is not None and np.isfinite(close_b[i]) and np.isfinite(ma60_b[i]):
            on = bool(close_b[i] >= ma60_b[i])
        if not on:
            off_days += 1
            want_sell = set(shares)
            cause = "regime"
            return False
        cause = "rot"

        # 3a) 日频出场：ATR 跟踪止损 / 破 MA20 清仓（消融对照）
        for j in list(shares):
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

        # 3b) 周频：动量分位 + 缓冲带 + 主题上限 + 换手闸门
        if i in wdec:
            pct_i = pct[i]
            row_ok = mask[i]
            held = [j for j in shares if j not in want_sell]
            keep = []
            for j in held:
                age = i - entry_i.get(j, i)
                v = pct_i[j]
                if age < prm.min_hold or (np.isfinite(v) and v >= prm.hold_pct):
                    keep.append(j)
            keep.sort(key=lambda j: -(pct_i[j] if np.isfinite(pct_i[j]) else -1))
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
            cand.sort(key=lambda j: -pct_i[j])
            tc = {}
            for j in keep:
                tc[theme[j]] = tc.get(theme[j], 0) + 1

            def fits(j):
                return (not prm.theme_cap) or tc.get(theme[j], 0) < prm.theme_cap

            add = []
            slots = prm.n_hold - len(keep)
            # (1) 空缺（被止损/清仓腾出来的位置）无闸门补满：空仓不是目标
            for j in list(cand):
                if slots <= 0:
                    break
                if not fits(j) and prm.theme_cap:
                    cap_blocked += 1
                if fits(j):
                    add.append(j)
                    tc[theme[j]] = tc.get(theme[j], 0) + 1
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
                t = theme[weakest]
                tc[t] = tc.get(t, 0) - 1
                if fits(j):
                    keep.remove(weakest)
                    want_sell.add(weakest)
                    add.append(j)
                    tc[theme[j]] = tc.get(theme[j], 0) + 1
                    nswap += 1
                else:
                    tc[t] = tc.get(t, 0) + 1
            target_set = keep + add
            for j in list(shares):
                if j not in target_set:
                    want_sell.add(j)
            for j in target_set:
                if j not in shares:
                    want_buy[j] = 1.0 / prm.n_hold

        # 3c) 权重：网格倍数在持仓集合内自筹资金归一化（加仓的钱来自同伴减仓，不用现金）
        live = [j for j in shares if j not in want_sell]
        newb = [j for j in want_buy if j not in live]
        base_gross = (len(live) + len(newb)) / prm.n_hold
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
                sc = "stop" if j in stop_set else cause
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
                    tr["grid"] += q * p
                    cash -= need
                    basis[j] = (basis[j] * shares[j] + p * q) / (shares[j] + q)
                    shares[j] += q
                    cost_total[j] = cost_total.get(j, 0.0) + q * p
                    traded += q * p
                else:
                    q = min(shares[j], int(-dv / p / LOT) * LOT)
                    if q < LOT:
                        continue
                    realized[j] = realized.get(j, 0.0) + (p - basis.get(j, p)) * q
                    shares[j] -= q
                    cash += q * p * (1 - prm.cost)
                    traded += q * p
                    tr["grid"] += q * p
                    if shares[j] == 0:                     # 网格减到 0 才算平仓
                        e_i = entry_i.pop(j, np.nan)
                        ct = cost_total.pop(j, np.nan)
                        pnl = realized.pop(j, 0.0)
                        if np.isfinite(e_i) and np.isfinite(ct) and ct > 0:
                            trades.append(dict(j=j, code=cols[j], name=names.get(cols[j], ""),
                                               entry_i=int(e_i), exit_i=i, pnl=pnl, ret=pnl / ct))
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
            _tc = {}
            for j in shares:
                _tc[theme[j]] = _tc.get(theme[j], 0) + 1
            theme_share.append(max(_tc.values()) / len(shares))
        else:
            theme_share.append(0.0)
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
                             cause=cause, pct={int(j): float(pct[i][j])
                                               for j in range(len(cols))
                                               if np.isfinite(pct[i][j])})
            break

        decide(i, eq_c)

    eq = pd.Series(eq_close, index=idx[i0:i0 + len(eq_close)])
    ret = eq.pct_change().fillna(0.0)
    return dict(eq=eq, ret=ret,
                cost=pd.Series({idx[i]: v for i, v in cost_day.items()}),
                turn=pd.Series(turn_week), trades=pd.DataFrame(trades),
                tr_by=tr, cap_blocked=cap_blocked, sell_cause=sell_cause, dbg=dbg,
                stopped=stopped, off_days=off_days, n_sells=n_sells, n_stop_days=n_stop_days,
                stop_log=stop_log, sell_log=sell_log, n_weeks=len(turn_week),
                final_cash=cash, n_left=len(shares),
                eq_open=pd.Series(eq_open_r, index=idx[i0:i0 + len(eq_open_r)]),
                gross=pd.Series(gross_r, index=idx[i0:i0 + len(gross_r)]),
                hold_n=pd.Series(hold_n, index=idx[i0:i0 + len(hold_n)]),
                theme_share=pd.Series(theme_share, index=idx[i0:i0 + len(theme_share)]),
                pos_log=pos_log, nan_close=nan_close,
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
    """集中度的真实代价：最坏单日 / 最坏两日 / 平均持仓只数 / 同主题最大占比分布。"""
    ret = res["ret"]
    hs = res["theme_share"]
    if len(ret) < 3:
        return dict(label=label, worst1=0.0, worst1_date="-", worst2=0.0, worst2_date="-",
                    avg_n=0.0, share_p50=0.0, share_p90=0.0, share_max=0.0)
    r = ret.to_numpy(dtype=float)
    two = r[1:] + r[:-1]
    k1 = int(np.nanargmin(r))
    k2 = int(np.nanargmin(two))
    return dict(label=label,
                worst1=float(r[k1]), worst1_date=str(ret.index[k1].date()),
                worst2=float(two[k2]), worst2_date=f"{ret.index[k2].date()}→{ret.index[k2 + 1].date()}",
                avg_n=float(res["hold_n"].mean()),
                share_p50=float(np.nanpercentile(hs, 50)),
                share_p90=float(np.nanpercentile(hs, 90)),
                share_max=float(np.nanmax(hs)))


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
    for j, w in targets.items():
        sid = str(cols[j])
        nm = names.get(sid, "")
        tg[sid] = dict(code=_code6(sid), sid=sid, name=nm, weight=float(w),
                       theme=theme_of(nm), pct=float(st["pct"].get(j, np.nan)))
    if not quiet:
        print(f"[chase] 目标日={end} 空仓开关={'空仓(指数<MA%s)' % prm.index_ma if not st['on'] else '持仓'}"
              f" 目标 {len(tg)} 只 可交易池 {int(mask[i].sum())} 只")
    return dict(as_of=end, price_date=end, on=bool(st["on"]), cause=st["cause"],
                eq=float(st["eq"]), cash=float(st["cash"]), n_targets=len(tg),
                n_pool=int(mask[i].sum()), targets=tg,
                held={_code6(cols[j]): int(q) for j, q in st["shares"].items()},
                next_sell=[_code6(cols[j]) for j in st["want_sell"]],
                cfg={k: cfg[k] for k in ("n_hold", "entry_pct", "hold_pct", "theme_cap",
                                         "grid_step", "stop_k", "index_ma", "max_order_pct")},
                prm=dict(theme_cap=int(prm.theme_cap), n_hold=int(prm.n_hold),
                         stop_k=float(prm.stop_k), grid_step=float(prm.grid_step),
                         hold_pct=float(prm.hold_pct), entry_pct=float(prm.entry_pct),
                         index_ma=int(prm.index_ma)))


def preview_targets(cfg=None, quiet=False):
    """假设「空仓开关=ON 且次日就是决策日」时的建仓名单（只用于核对，不是下单清单）。

    与 run() 的 3b 完全同一条规则（空仓起步：keep 为空、slots=n_hold、无换仓额度）：
    动量分位 >= 1-entry_pct 的候选按分位从强到弱，同主题最多 theme_cap 只，取满 n_hold。
    用途：开关关着的时候，让您先看到"一旦放行会买哪 8 只、各多少钱"。
    """
    cfg = dict(cfg or load_cfg())
    P, D, mask, pct = prep()
    i = len(P["idx"]) - 1
    cols, names = P["cols"], P["names"]
    pi, ok = pct[i], mask[i]
    cand = [j for j in range(len(cols))
            if ok[j] and np.isfinite(pi[j]) and pi[j] >= 1.0 - float(cfg["entry_pct"])]
    cand.sort(key=lambda j: -pi[j])
    cap = int(cfg["theme_cap"] or 0)
    picked, tc = [], {}
    for j in cand:
        t = theme_of(names.get(cols[j], ""))
        if cap and tc.get(t, 0) >= cap:
            continue
        picked.append(j)
        tc[t] = tc.get(t, 0) + 1
        if len(picked) >= int(cfg["n_hold"]):
            break
    w = 1.0 / int(cfg["n_hold"])
    tg = {}
    for j in picked:
        sid = str(cols[j])
        nm = names.get(sid, "")
        tg[sid] = dict(code=_code6(sid), sid=sid, name=nm, weight=w,
                       theme=theme_of(nm), pct=float(pi[j]))
    if not quiet:
        print(f"[预览] 假设空仓开关=ON 且次日为决策日（{P['idx'][i].date()} 动量分位）："
              f"{len(tg)} 只候选，池 {int(ok.sum())} 只")
    return dict(as_of=str(P["idx"][i].date()), price_date=str(P["idx"][i].date()),
                on=True, cause="preview", eq=0.0, cash=0.0, n_targets=len(tg),
                n_pool=int(ok.sum()), targets=tg, held={}, next_sell=[],
                cfg={k: cfg[k] for k in ("n_hold", "entry_pct", "hold_pct", "theme_cap",
                                         "grid_step", "stop_k", "index_ma", "max_order_pct")},
                prm=dict(theme_cap=cap, n_hold=int(cfg["n_hold"]), stop_k=float(cfg["stop_k"]),
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
        if buy > 0 and price > 0:
            buy = min(buy, _lot(cap_amt / price))
        rows.append(dict(code=code, sid=(t or {}).get("sid") or _sid(code), name=name,
                         weight=weight, price=price, target_amount=round(target_amount, 2),
                         target_shares=int(tgt_sh), cur_shares=cur,
                         delta_shares=int(delta), buy_shares=int(buy), sell_shares=int(sell),
                         buy_amount=round(buy * price, 2), sell_amount=round(sell * price, 2),
                         theme=(t or {}).get("theme"), pct=(t or {}).get("pct"),
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
                n_targets=tg["n_targets"], n_pool=tg["n_pool"],
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


def print_plan(plan):
    print(f"[建仓清单] 信号日={plan['as_of']}  价格日={plan['price_date']}  "
          f"总资产=¥{plan['total_asset']:,.2f} 可用现金=¥{plan['cash']:,.2f} ({plan['account_source']})")
    print(f"  目标 {plan['n_targets']} 只（N={plan['cfg']['n_hold']} 主题上限{plan['cfg']['theme_cap']}）"
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
    ap.add_argument("--build-panel", action="store_true")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--theme-cap", type=int, default=None)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg()
    if a.theme_cap is not None:
        cfg["theme_cap"] = int(a.theme_cap)
    if a.cost is not None:
        cfg["cost"] = float(a.cost)
    os.makedirs(DUMPS, exist_ok=True)

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
              f"| 同主题占比 中位{ex['share_p50'] * 100:.0f}%/最大{ex['share_max'] * 100:.0f}% "
              f"| 主题上限拦截 {res['cap_blocked']} 次 | 空仓 {res['off_days']} 天")
        print("分年:", {k: round(v * 100, 2) for k, v in by_year(res["eq"]).items()})
        res["eq"].to_csv(os.path.join(DUMPS, f"eq_{prm.start}_{prm.end}.csv"))
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
