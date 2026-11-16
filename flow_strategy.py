#!/usr/bin/env python3
"""flow_strategy.py — E4 终局方案（等权宽组合 20 只 + 流量倾斜 + 溢价风控 + 低换手）.

一个实现，两条用法，参数全部来自 config.yaml -> flow: 段：
  * 回测: --backtest（同一份缓存、同一个引擎、同一套打分）
      引擎 = quant2/e4/engine2.step（E4 原样搬过来，没有第二份实现）
      信号 = quant2/e4/booklib.zr/smooth 的同一个表达式
  * 实盘: 被 signal_generator.py(mode=flow) 调用 latest_targets() 拿 20 只目标权重

信号（与 E4 报告 4.1 节逐条对应）
  池子    E3/E4 面板的 PIT keep 掩码（规模≥2亿、60日中位周成交额≥500万、连续20周）
  f       份额周变动 = shares_t / shares_{t-1} - 1，份额滞后 lag=1 个交易日（T-1 盘后已公布）
  排序    截面百分位 rank(f) 反向（买低申购/净赎回），再取过去 2 周均值平滑
  持有     等权 1/N；缓冲带 2N（跌出前 40 才卖）；新买入持有期下限 2 周；分批 0.5
  溢价     不复权收盘 / 净值(T-1) - 1 > 10% → 不建仓（已有持仓不强制卖，见 cfg.premium.block_new_only）
  波动目标 乘数 = min(1, target_vol / 近 vol_window_weeks 周实现波动)
           乘数只作用于每只的目标金额（等权 × 乘数），上限 1.0（只减仓、不加杠杆、不做空）
           研究结论（riskbudget@7604735）: 25% 目标 → 年化 8.79%/回撤 −27.6%/Sharpe 0.50，
           只有 9% 的周真的减仓，其余 91% 满仓

默认不杠杆、不做空、不择时（flow 模式不乘 timing 档位）。

CLI
  .venv/bin/python3 flow_strategy.py --backtest [--start 2015-01-01 --end 2024-12-31]
  .venv/bin/python3 flow_strategy.py --backtest --parity      # E4 原口径（对齐报告数字）
  .venv/bin/python3 flow_strategy.py --backtest --lag 0       # PIT 反证：份额滞后改 T
  .venv/bin/python3 flow_strategy.py --plan                   # 首日/每日建仓清单（只算不下单）
  .venv/bin/python3 flow_strategy.py --targets                # 只打印 20 只目标权重
"""
import argparse
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "quant2", "e3"))
sys.path.insert(0, os.path.join(_HERE, "quant2", "e4"))

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import yaml                                           # noqa: E402
import e3data                                         # noqa: E402
import e4data                                         # noqa: E402
import engine                                         # noqa: E402
import engine2                                        # noqa: E402
import booklib as B                                   # noqa: E402

CONFIG_PATH = os.path.join(_HERE, "config.yaml")
OUT_DIR = os.path.join(_HERE, "quant2", "out", "flow")

# ── 参数默认值（只作 config.yaml 缺省兜底；研究已冻结，别再按历史调）──
DEFAULTS = dict(
    n=20,                 # 持仓只数 N
    smooth_weeks=2,       # 信号平滑窗口（周）
    buffer_n=40,          # 缓冲带 = 2N
    min_hold_weeks=2,     # 持有期下限（周）
    partial=0.5,          # 分批换仓：每周只换需要换的一半
    premium_cap=0.10,     # 溢价阈值 10%
    premium_block_new_only=True,   # True=只拦新建仓（实盘）; False=E4 原口径（整列掩码）
    gross=1.0,            # 目标总仓位（flow 不乘择时档位）
    min_amount_daily=2.0e7,        # 实盘可交易闸门：日均成交额 > 2000 万
    flow_lag=1,           # 份额滞后交易日数（T-1 盘后已公布）
    max_order_pct=0.05,   # 单只单次下单上限 = 总资产 5%（分批建仓）
    cost=0.0015,          # 单边成本（E4 口径）
    cancel_pending_at='14:45',     # 尾盘撤未成交挂单时点
    vol_target=0.25,      # 年化波动目标（乘数 = min(1, 目标/实现波动)，>1 一律截到 1）
    vol_window_weeks=26,  # 实现波动回看周数
)

# 波动目标最少观测周数（研究 min_obs=12：不足 12 周不下调乘数，按满仓）
MIN_VOL_OBS = 12


def load_cfg():
    """读取 config.yaml 的 flow: 段，缺键用 DEFAULTS 兜底。"""
    raw = {}
    try:
        with open(CONFIG_PATH) as f:
            raw = (yaml.safe_load(f) or {}).get("flow") or {}
    except Exception as e:
        print(f"[flow] config.yaml 读取失败({e})，全用默认参数", file=sys.stderr)
    cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    prem = raw.get("premium") or {}
    if "cap" in prem:
        cfg["premium_cap"] = float(prem["cap"])
    if "block_new_only" in prem:
        cfg["premium_block_new_only"] = bool(prem["block_new_only"])
    return cfg


def strategy_mode():
    """strategy.mode: flow | pyramid（默认 flow，一键切回 pyramid）。"""
    try:
        with open(CONFIG_PATH) as f:
            return str(((yaml.safe_load(f) or {}).get("strategy") or {}).get("mode", "flow")).lower()
    except Exception:
        return "flow"


# ════════════════════════════════════════════════════════════
# 〇、波动目标（riskbudget@7604735 唯一有增量的那一层）
# ════════════════════════════════════════════════════════════
def vol_multiplier(returns, target, window_weeks, min_obs=MIN_VOL_OBS):
    """乘数 = min(1, target / 近 window_weeks 周实现波动)，年化口径（周收益 × √52）。

    returns    : 已经实现的周净收益序列（PIT：只用本周之前的，绝不看未来）
    返回 (mult, realized_vol)；数据不足返回 (1.0, None)。

    上限硬编码 1.0：>1 就是杠杆，本策略不杠杆。下界 0 只用于防除零。
    研究口径标准差 ddof=1（样本标准差），与 rb.py 的 m 逐周对齐。
    """
    target = float(target or 0.0)
    if target <= 0:
        return 1.0, None
    if len(returns) < int(min_obs):
        return 1.0, None
    rv = float(pd.Series(list(returns)[-int(window_weeks):]).std(ddof=1) * np.sqrt(52))
    if not np.isfinite(rv) or rv <= 1e-6:
        return 1.0, rv
    return float(min(1.0, target / rv)), rv


def vol_note(cfg, rvol, mult):
    """一行人话：`[FLOW] 近26周波动=18.2% 目标=25% 乘数=1.00（满仓）`。"""
    w = int(cfg.get("vol_window_weeks") or 26)
    tgt = float(cfg.get("vol_target") or 0.0)
    if rvol is None:
        return "[FLOW] 波动数据不足，按满仓处理"
    tail = "（满仓）" if mult >= 0.9995 else f"（减仓{1.0 - mult:.0%}）"
    return f"[FLOW] 近{w}周波动={rvol:.1%} 目标={tgt:.0%} 乘数={mult:.2f}{tail}"


# ════════════════════════════════════════════════════════════
# 一、面板（与 E4 同一份缓存、同一套 PIT 口径）
# ════════════════════════════════════════════════════════════
def load_panel(lag=1, cutoff=None):
    """PIT 面板。lag=1 → 份额用 T-1（E4/E2 口径）；cutoff 截断样本并重算前向收益。

    每次都从 quant2/data 的原始缓存重建（不读 pickle），保证实盘当日追加的数据
    立刻进入面板，也保证回测与实盘看到的不是两份数据。
    """
    d = e3data.build(lag=int(lag))
    d = e4data.truncate(d, cutoff)
    return d


def context(d, cfg=None):
    """E4 booklib.ctx 的同一组对象：keep/f/ret1/valid/ew（等权基准）。"""
    cfg = cfg or load_cfg()
    keep = d["keep"]
    ret1 = d["fwd"][1].where(keep)
    valid = keep & ret1.notna()
    return dict(d=d, keep=keep, f=d["f"], fwd={h: d["fwd"][h].where(keep) for h in (1, 2, 4)},
                ret1=ret1, valid=valid,
                ew=engine.ew_book(valid, ret1, cost=float(cfg["cost"])),
                weeks=d["wk"])


def score_and_block(c, cfg):
    """打分 + 溢价掩码。返回 (score, entry_block)。

    打分与 E4 final.py 完全一致：
        smooth(-zr(f), k).where(prem <= cap)
    parity 口径（E4 原样）：溢价整列掩码 → 超阈值连持仓一起出局。
    实盘口径（cfg.premium_block_new_only=True）：打分不掩码，溢价只写进 entry_block，
        已有持仓不因溢价被强制卖出。
    """
    k = int(cfg["smooth_weeks"])
    cap = float(cfg["premium_cap"])
    prem = c["d"]["prem"].where(c["keep"])
    raw = B.smooth(-B.zr(c["f"].where(c["keep"])), k)
    # NaN 溢价 = 当天没有可信溢价（缺净值） → 不许新建仓，不用旧值硬填
    entry_block = (prem <= cap)
    if cfg["premium_block_new_only"]:
        return raw, entry_block
    return raw.where(prem <= cap), entry_block


def tradable_overlay(c, cfg):
    """实盘可交易闸门：日均成交额 > min_amount_daily（0/None 关闭 → 与 E4 同池）。"""
    floor = float(cfg.get("min_amount_daily") or 0)
    if floor <= 0:
        return None
    amt = c["d"]["A"] / 5.0                      # 周成交额 ≈ 5 个交易日
    return amt.rolling(2, min_periods=1).mean() > floor


# ════════════════════════════════════════════════════════════
# 二、回测（与 E4 同一套 engine2.step；波动目标就长在这一个循环里）
# ════════════════════════════════════════════════════════════
def simulate_book(c, cfg, overlay=None, valid=None, return_state=False):
    """逐周决策 + 波动目标。回测与实盘（latest_targets）**共用这一个函数**。

    决策体仍来自 engine2.step（E4 原样），这里只做三件事：
      1) 每周决策后按 PIT 实现波动算乘数 m，把目标权重整列乘 m（现金腿不投）
      2) 换手 = 0.5×Σ|Δw|（engine2 同一口径，乘数只是让 w 变小，没有第二套记账）
      3) 乘数序列写进 book["mult"]，供回测打印分布 / 实盘算目标金额

    cfg.vol_target <= 0 → 不启用，逐周结果与 engine2.simulate 逐位相同（m≡1）。
    valid   : 覆盖 c["valid"]（实盘把最后一周放宽为"今天可交易"用的就是它）
    返回 book（含 mult/rvol 两列）；return_state=True 时另给最后一行的 prev_w（已乘 m）。
    """
    score, entry_block = score_and_block(c, cfg)
    valid = c["valid"] if valid is None else valid
    if overlay is not None:
        valid = valid & overlay.reindex(valid.index).fillna(False)
    eb = entry_block if cfg["premium_block_new_only"] else None
    target = float(cfg.get("vol_target") or 0.0)
    window = int(cfg.get("vol_window_weeks") or 26)
    cost = float(cfg["cost"])
    held, prev_w, nets = {}, pd.Series(dtype=float), []
    recs = []
    m, rvol = 1.0, None
    for i, t in enumerate(score.index):
        w = engine2.step(score.loc[t], valid.loc[t].fillna(False), held, prev_w, i,
                         N=int(cfg["n"]), buffer_n=int(cfg["buffer_n"]),
                         minhold=int(cfg["min_hold_weeks"]), partial=float(cfg["partial"]),
                         entry_block=(None if eb is None else eb.loc[t]))
        m, rvol = 1.0, None
        if target > 0 and len(w):
            m, rvol = vol_multiplier(nets, target, window)
        w_eff = w * m
        all_idx = w_eff.index.union(prev_w.index)
        turn = float((w_eff.reindex(all_idx, fill_value=0.0)
                      - prev_w.reindex(all_idx, fill_value=0.0)).abs().sum() / 2.0)
        gross = float((w_eff * c["ret1"].loc[t].reindex(w_eff.index).fillna(0.0)).sum())
        net = gross - turn * cost
        nets.append(net)
        recs.append(dict(t=t, gross=gross, turn=turn, cost=turn * cost, net=net,
                         n=int((w_eff > 0).sum()), w_sum=float(w_eff.sum()),
                         mult=float(m), rvol=(np.nan if rvol is None else float(rvol))))
        held = {n: (held[n] if n in held else i) for n in w.index}
        prev_w = w_eff
    book = pd.DataFrame(recs).set_index("t")
    if return_state:
        return book, dict(held=dict(held), prev_w=prev_w, last_i=len(score.index) - 1,
                          weeks=score.index, mult=float(m), rvol=rvol,
                          cash=float(max(0.0, 1.0 - float(prev_w.sum()))))
    return book


def vol_stats(book, lo=None, cfg=None):
    """乘数分布：多少周满仓 / 多少周减仓 / 最少最多多少（验收第 2 条）。"""
    cfg = cfg or load_cfg()
    m = book["mult"].astype(float)
    if lo is not None:
        m = m[m.index >= pd.Timestamp(lo)]
    return dict(target=float(cfg.get("vol_target") or 0.0),
                window_weeks=int(cfg.get("vol_window_weeks") or 26),
                n_weeks=int(len(m)),
                n_full=int((m >= 0.9995).sum()),
                n_trim=int((m < 0.9995).sum()),
                share_trim=float((m < 0.9995).mean()),
                m_min=float(m.min()), m_max=float(m.max()), m_mean=float(m.mean()))


def backtest(start="2015-01-01", end="2024-12-31", lag=1, parity=False, overlay=False,
             cfg=None, quiet=False):
    cfg = dict(cfg or load_cfg())
    if parity:
        cfg["premium_block_new_only"] = False
        cfg["min_amount_daily"] = 0.0
    cutoff = None
    if end:
        cutoff = str((pd.Timestamp(end) + pd.Timedelta(days=1)).date())
    d = load_panel(lag=lag, cutoff=cutoff)
    c = context(d, cfg)
    ov = tradable_overlay(c, cfg) if overlay else None
    book = simulate_book(c, cfg, overlay=ov)
    m = B.ev(book, c["ew"], lo=start, hi=None)
    yrs = B.by_year(book, c["ew"])
    vs = vol_stats(book, lo=start, cfg=cfg)
    out = dict(config=cfg, window=dict(start=start, end=end, lag=int(lag),
                                       parity=bool(parity), overlay=bool(overlay)),
               metrics=m, by_year={int(k): v for k, v in yrs.items()},
               vol=vs,
               prem_block_share=float((c["d"]["prem"].where(c["keep"]) >
                                       float(cfg["premium_cap"])).sum(axis=1).gt(0).mean()))
    if not quiet:
        vt = (f"波动目标={float(cfg['vol_target']):.0%}/{int(cfg['vol_window_weeks'])}周"
              if float(cfg.get("vol_target") or 0) > 0 else "波动目标=关")
        print("=" * 78)
        print(f"[回测] {start} → {end}  lag={lag}  parity={parity}  overlay={overlay}  "
              f"N={cfg['n']} 平滑={cfg['smooth_weeks']}周 缓冲={cfg['buffer_n']} "
              f"持有>={cfg['min_hold_weeks']}周 分批={cfg['partial']} 成本={cfg['cost']:.4f} {vt}")
        print(f"  净收益(周均×52) {m['ann']:+.2%}  CAGR {m['cagr']:+.2%}  波动 {m['vol']:.1%}  "
              f"Sharpe {m['sharpe']:.2f}")
        print(f"  最大回撤 {m['mdd']:+.1%}  周换手 {m['turnover']:.1%}  年成本 {m['cost_ann']:.2%}  "
              f"周数 {m['n_weeks']}  平均持仓 {m['avg_names']:.2f}")
        print(f"  等权基准 净 {m['ew_ann']:+.2%} CAGR {m['ew_cagr']:+.2%}  "
              f"超额 {m['excess_ann']:+.2%} (t {m['excess_t']:.2f})  胜率 {m['excess_hit']:.1%}")
        if vs["target"] > 0:
            print(f"  乘数分布: 满仓 {vs['n_full']} 周 / 减仓 {vs['n_trim']} 周"
                  f"（{vs['share_trim']:.1%}）  最少 {vs['m_min']:.3f}  最多 {vs['m_max']:.3f}  "
                  f"均值 {vs['m_mean']:.3f}")
        print("  分年(净/等权/超额): " + " ".join(
            f"{y}:{v['net']:+.1%}/{v['ew']:+.1%}/{(v['net'] - v['ew']):+.1%}"
            for y, v in out["by_year"].items()))
    return out


# ════════════════════════════════════════════════════════════
# 三、实盘目标权重（回测最后一行的同一个 step）
# ════════════════════════════════════════════════════════════
def latest_targets(cfg=None, lag=None, relax_last_week=True, quiet=False):
    """算出"下一交易日该持有哪 20 只、各多少权重"。

    做法：整段历史跑 simulate_book（与回测同一个函数），最后一周的 valid 放宽为
    "今天可交易"（不要求已有的前向收益），最后一周的目标权重 = 输出 prev_w（已乘波动乘数）。
    最后一周 = 最近一个已收盘交易周的周五（PIT：份额 T-1、净值 T-1、收盘 T）。
    """
    cfg = dict(cfg or load_cfg())
    lag = int(cfg["flow_lag"] if lag is None else lag)
    d = load_panel(lag=lag, cutoff=None)
    c = context(d, cfg)
    score, entry_block = score_and_block(c, cfg)
    valid = c["valid"]
    last = c["weeks"][-1]
    if relax_last_week:
        valid = valid.copy()
        valid.loc[last] = c["keep"].loc[last].fillna(False)
    ov = tradable_overlay(c, cfg)
    if ov is not None:
        valid = valid & ov.reindex(valid.index).fillna(False)
    book, st = simulate_book(c, cfg, valid=valid, return_state=True)
    w = st["prev_w"]
    prem = c["d"]["prem"].loc[last]
    amt = c["d"]["A"].loc[last] / 5.0
    names = c["d"]["names"]
    # 波动乘数（PIT：只用最后一周之前已实现的周收益；上限 1.0）
    vt_on = float(cfg.get("vol_target") or 0) > 0
    mult = float(st["mult"]) if vt_on else 1.0
    rvol = st["rvol"] if vt_on else None
    if vt_on and not quiet:
        print(vol_note(cfg, rvol, mult))
    # 打分排名（1 = 最该买；建仓顺序按它来，钱不够时先满足排名靠前的）
    s_last = score.loc[last].where(c["keep"].loc[last].fillna(False)).dropna()
    rank = {code: int(i) + 1 for i, code in enumerate(s_last.sort_values(ascending=False).index)}
    rows = {}
    for code in w.index:
        rows[code] = dict(code=code, name=names.get(code, code), weight=float(w[code]),
                          rank=rank.get(code, 0),
                          prem=float(prem.get(code, np.nan)),
                          amount_daily=float(amt.get(code, np.nan)),
                          cat=(c["d"]["cat"].get(code) if "cat" in c["d"] else None))
    held_state = {k: int(v) for k, v in st["held"].items()}
    return dict(as_of=str(pd.Timestamp(last).date()), weeks=int(len(c["weeks"])),
                lag=lag, gross=float(cfg["gross"]), targets=rows, held_since=held_state,
                n_names=len(rows), pool_last=int(c["keep"].loc[last].fillna(False).sum()),
                vol=dict(target=float(cfg.get("vol_target") or 0),
                         window_weeks=int(cfg.get("vol_window_weeks") or 26),
                         realized_vol=rvol, mult=mult),
                cfg=cfg)


# ════════════════════════════════════════════════════════════
# 四、建仓/调仓清单（只算不下单）
# ════════════════════════════════════════════════════════════
def _lot(shares, lot=100):
    return int(shares // lot * lot)


def _pool_names():
    """代码 → 中文名（universe.csv，缺名返回空串，绝不用代码冒充名字）。"""
    import csv
    p = os.path.join(_HERE, "quant2", "data", "meta", "universe.csv")
    try:
        with open(p) as f:
            return {r["code"][2:]: r["name"] for r in csv.DictReader(f)}
    except Exception:
        return {}


def read_holdings(prefer_broker=True):
    """真实持仓 {code: shares}。frida 优先，失败退回 portfolio.json。"""
    if prefer_broker:
        try:
            from broker_frida import ThsBroker
            pos = ThsBroker().get_positions() or {}
            out = {c: int(v.get("shares") or 0) for c, v in pos.items() if int(v.get("shares") or 0) > 0}
            if out:
                return out, "frida"
        except Exception as e:
            print(f"[plan] frida 持仓读取失败: {e} → 退回 portfolio.json", file=sys.stderr)
    p = os.path.join(_HERE, "portfolio.json")
    with open(p) as f:
        pf = json.load(f)
    out = {c: int(v.get("shares") or 0) for c, v in pf.get("positions", {}).items()
           if int(v.get("shares") or 0) > 0}
    return out, "portfolio.json"


def read_account(prefer_broker=True):
    if prefer_broker:
        try:
            from broker_frida import ThsBroker
            a = ThsBroker().get_account() or {}
            ta = float(a.get("total_asset") or 0)
            cash = float(a.get("cash") or 0)
            if ta > 0:
                return dict(total_asset=ta, cash=cash,
                            market_value=float(a.get("market_value") or 0), source="frida")
        except Exception as e:
            print(f"[plan] frida 账户读取失败: {e} → 退回 portfolio.json", file=sys.stderr)
    with open(os.path.join(_HERE, "portfolio.json")) as f:
        acc = (json.load(f) or {}).get("account") or {}
    return dict(total_asset=float(acc.get("total_asset") or 0),
                cash=float(acc.get("cash") or 0),
                market_value=float(acc.get("market_value") or 0), source="portfolio.json")


def _load_price_cache():
    """不复权收盘价缓存（quant2/data/price），返回 (last_date, {code: close})。"""
    root = os.path.join(_HERE, "quant2", "data", "price")
    px, last = {}, None
    for fn in os.listdir(root):
        if not fn.endswith(".json"):
            continue
        code = fn[2:-5] if fn[:2] in ("sh", "sz") else fn[:-5]
        try:
            bars = json.load(open(os.path.join(root, fn))).get("bars") or []
        except Exception:
            continue
        if not bars:
            continue
        d0, close = bars[-1][0], float(bars[-1][2])
        px[code] = close
        if last is None or d0 > last:
            last = d0
    return last, px


def build_plan(totals=None, holdings=None, asof_price=None, cfg=None, targets=None,
               quiet=False):
    """给定目标权重 → 每只的目标股数/金额、当前持仓、差额、今日可下单量。

    分批约束：单只单次买入 ≤ 总资产 × max_order_pct（默认 5%）；超出的部分留给下一交易日。
    现金约束：可动用资金 = 可用现金 + 当日卖出回款（A 股卖出当日可用于买入）。
             不足时按比例缩小下单并在清单里标明。
    """
    cfg = dict(cfg or load_cfg())
    tg = targets or latest_targets(cfg=cfg, quiet=quiet)
    ta_info = totals or read_account()
    hold, hold_src = holdings if isinstance(holdings, tuple) else (holdings or read_holdings()[0], "arg")
    px_date, px = _load_price_cache()
    if asof_price:
        px.update(asof_price)
    names = _pool_names()

    total_asset = float(ta_info["total_asset"])
    cash = float(ta_info["cash"])
    gross = float(cfg["gross"])
    vol = dict(tg.get("vol") or dict(target=0.0,
                                     window_weeks=int(cfg.get("vol_window_weeks") or 26),
                                     realized_vol=None, mult=1.0))
    mult = float(vol.get("mult") or 1.0)          # 目标金额 = 等权 × 乘数（权重里已经乘过）
    cap_amt = total_asset * float(cfg["max_order_pct"])
    rows = []
    codes = sorted(set(tg["targets"].keys()) | set(hold.keys()))
    for code in codes:
        t = tg["targets"].get(code)
        weight = float(t["weight"]) if t else 0.0
        target_amount = gross * total_asset * weight
        price = float(px.get(code) or 0.0)
        cur = int(hold.get(code, 0))
        tgt_sh = _lot(target_amount / price) if price > 0 else 0
        delta = tgt_sh - cur
        # 1 手都买不起（价格 > 5%×总资产/100）→ 这只票在 20 万账户上放不进 1/N 的仓位，
        # 直接跳过（不拿 100 股硬凑、不放大权重），钱留到下次再平衡
        skip_reason = None
        if weight > 0 and tgt_sh < 100:
            skip_reason = f"1手需¥{price * 100:,.0f} > 目标仓位¥{target_amount:,.0f}，跳过"
            tgt_sh = 0
            delta = -cur
        buy_today = sell_today = 0
        if delta > 0:
            buy_today = min(delta, _lot(cap_amt / price) if price > 0 else delta)
        elif delta < 0:
            sell_today = -delta
        rows.append(dict(code=code, name=(t or {}).get("name") or names.get(code, ""),
                         weight=weight, rank=int((t or {}).get("rank") or 0),
                         price=price, target_amount=round(target_amount, 2),
                         target_shares=int(tgt_sh), cur_shares=cur,
                         delta_shares=int(delta), buy_shares=int(buy_today),
                         sell_shares=int(sell_today),
                         buy_amount=round(buy_today * price, 2),
                         sell_amount=round(sell_today * price, 2),
                         prem=(t or {}).get("prem"), in_target=bool(t),
                         skip_reason=skip_reason))
    buys = [r for r in rows if r["delta_shares"] > 0]
    sells = [r for r in rows if r["sell_shares"] > 0]
    sell_total = sum(r["sell_amount"] for r in sells)
    budget = cash + sell_total if cfg.get("use_sell_proceeds", True) else cash
    # 按打分排名顺序分配资金：钱不够时先满足排名靠前的，**不**把所有票等比削薄
    # （等比削薄会留下 20 个各差几百股的灰尘仓位，下一交易日又要重新凑）
    want_total = 0.0
    remaining = budget
    for r in sorted(buys, key=lambda x: (x["rank"] or 10 ** 9, x["code"])):
        want = min(r["delta_shares"], _lot(cap_amt / r["price"]) if r["price"] > 0 else r["delta_shares"])
        want_amt = round(want * r["price"], 2)
        want_total += want_amt
        afford = _lot(remaining / r["price"]) if r["price"] > 0 else 0
        r["buy_shares"] = int(max(0, min(want, afford)))
        r["buy_amount"] = round(r["buy_shares"] * r["price"], 2)
        remaining -= r["buy_amount"]
    need_cash = round(want_total, 2)
    got_cash = round(budget - remaining, 2)
    scale = (got_cash / need_cash) if need_cash > 0 else 1.0
    plan = dict(as_of=tg["as_of"], price_date=px_date, total_asset=round(total_asset, 2),
                cash=round(cash, 2), account_source=ta_info.get("source"),
                holdings_source=hold_src, gross=gross,
                vol=vol, vol_mult=round(mult, 6),
                target_value=round(gross * total_asset * mult, 2),
                n_targets=tg["n_names"], pool=tg["pool_last"],
                buy_total=round(got_cash, 2),
                want_total=round(need_cash, 2),
                sell_total=round(sell_total, 2),
                budget=round(budget, 2),
                cash_scaled=bool(scale < 1.0), scale=round(scale, 4),
                rows=rows, cfg={k: cfg[k] for k in ("n", "smooth_weeks", "buffer_n",
                                                    "min_hold_weeks", "partial",
                                                    "premium_cap", "max_order_pct",
                                                    "vol_target", "vol_window_weeks")})
    plan["cash_after"] = round(plan["cash"] + plan["sell_total"] - plan["buy_total"], 2)
    return plan


def print_plan(plan):
    print(f"[建仓清单] 信号周={plan['as_of']}  价格日={plan['price_date']}  "
          f"总资产=¥{plan['total_asset']:,.2f} 可用现金=¥{plan['cash']:,.2f} "
          f"({plan['account_source']})")
    print(f"  目标持仓 {plan['n_targets']} 只 × 等权 = 目标市值 ¥{plan['target_value']:,.2f}；"
          f"可交易池 {plan['pool']} 只")
    vol = plan.get("vol") or {}
    if float(plan.get("vol_mult") or 1.0) < 0.9995:
        rv = vol.get("realized_vol")
        rvs = f"{rv:.1%}" if rv is not None else "-"
        released = plan["gross"] * plan["total_asset"] - plan["target_value"]
        print(f"  ⚠️ 波动乘数 {plan['vol_mult']:.2f}（近{vol.get('window_weeks')}周波动 {rvs} > "
              f"目标 {float(vol.get('target') or 0):.0%}）→ 目标市值 ×{plan['vol_mult']:.2f}，"
              f"释放现金 ¥{released:,.0f} 不投")
    print(f"  {'代码':<8}{'名称':<12}{'现价':>7}{'权重':>6}{'目标金额':>11}{'目标股数':>9}"
          f"{'当前股数':>9}{'差额':>8}{'买/卖':>10}  {'溢价':>7} 动作")
    for r in plan["rows"]:
        act = ("跳过" if r.get("skip_reason") else
               "买入" if r["buy_shares"] > 0 else
               "卖出" if r["sell_shares"] > 0 else
               ("持有" if r["in_target"] else "空"))
        prems = f"{r['prem']:+.1%}" if r["prem"] is not None and np.isfinite(r["prem"]) else "-"
        bs = (f"{r['buy_shares']:,}" if r["buy_shares"] > 0 else
              (f"-{r['sell_shares']:,}" if r["sell_shares"] > 0 else "-"))
        print(f"  {r['code']:<8}{r['name'][:11]:<12}{r['price']:>7.3f}{r['weight']:>6.0%}"
              f"{r['target_amount']:>11,.0f}{r['target_shares']:>9,}{r['cur_shares']:>9,}"
              f"{r['delta_shares']:>8,}{bs:>10}  {prems:>7} {act}")
    for r in plan["rows"]:
        if r.get("skip_reason"):
            print(f"  ⚠️ {r['code']} {r['name']}: {r['skip_reason']}")
    print(f"  可用资金 = 现金 ¥{plan['cash']:,.2f} + 当日卖出回款 ¥{plan['sell_total']:,.2f} "
          f"= ¥{plan['budget']:,.2f}")
    print(f"  合计: 买入 ¥{plan['buy_total']:,.2f}（需要 ¥{plan['want_total']:,.2f}）  "
          f"卖出 ¥{plan['sell_total']:,.2f}  执行后现金 ¥{plan['cash_after']:,.2f}"
          + (f"  ⚠️ 资金缺口，已按排名顺序满足前若干只（成交率 {plan['scale']:.1%}）"
             if plan["cash_scaled"] else ""))
    if plan["cash_after"] > plan["total_asset"] * 0.10:
        print(f"  ⚠️ 执行后现金占比 {plan['cash_after'] / plan['total_asset']:.1%} > 10%："
              f"分批/现金约束导致未建满，余额下一交易日继续")


# ════════════════════════════════════════════════════════════
# 五、CLI
# ════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--lag", type=int, default=None, help="份额滞后交易日；1=T-1(默认)，0=PIT反证")
    ap.add_argument("--parity", action="store_true", help="E4 原口径（溢价整列掩码、无流动性覆盖）")
    ap.add_argument("--overlay", action="store_true", help="叠加实盘可交易闸门（日均成交额）")
    ap.add_argument("--plan", action="store_true", help="输出建仓/调仓清单（不下单）")
    ap.add_argument("--targets", action="store_true", help="输出目标权重")
    ap.add_argument("--json", action="store_true", help="结果以 JSON 打印到 stdout")
    ap.add_argument("--vol-target", type=float, default=None,
                    help="年化波动目标（默认 config flow.vol_target=0.25；0=关掉这一层）")
    ap.add_argument("--vol-window", type=int, default=None, help="实现波动回看周数（默认 26）")
    a = ap.parse_args()

    cfg = load_cfg()
    if a.vol_target is not None:
        cfg["vol_target"] = float(a.vol_target)
    if a.vol_window is not None:
        cfg["vol_window_weeks"] = int(a.vol_window)
    if a.backtest:
        out = backtest(start=a.start, end=a.end,
                       lag=int(cfg["flow_lag"] if a.lag is None else a.lag),
                       parity=a.parity, overlay=a.overlay, cfg=cfg, quiet=a.json)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, default=float, indent=1))
        return 0
    if a.plan:
        plan = build_plan(cfg=cfg, quiet=a.json)
        if a.json:
            print(json.dumps(plan, ensure_ascii=False, default=float, indent=1))
        else:
            print_plan(plan)
        os.makedirs(OUT_DIR, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d")
        with open(os.path.join(OUT_DIR, f"plan_{tag}.json"), "w") as f:
            json.dump(plan, f, ensure_ascii=False, default=float, indent=1)
        return 0
    tg = latest_targets(cfg=cfg, quiet=a.json)
    if a.json:
        print(json.dumps(tg, ensure_ascii=False, default=float, indent=1))
        return 0
    print(f"[目标权重] 信号周={tg['as_of']}  lag={tg['lag']}  持仓 {tg['n_names']} 只  "
          f"总仓位={tg['gross']:.0%}  可交易池={tg['pool_last']} 只"
          f"  波动乘数={float((tg.get('vol') or {}).get('mult') or 1.0):.2f}")
    for code, r in sorted(tg["targets"].items(), key=lambda kv: -kv[1]["weight"]):
        prem = r["prem"]
        ps = f"{prem:+.1%}" if np.isfinite(prem) else "-"
        print(f"  {code}  {r['name'][:14]:<16}{r['weight']:.2%}  溢价={ps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
