#!/usr/bin/env python3
"""strong_trend.py — 「只进强势 + 趋势跟随 + 空仓躲跌」ETF 轮动 + 网格（研究版）

客户口径：
    「回测只需要三年即可，五年加轮动加网格，再加上各种指标，只进强势etf，厌烦风险，追牛」

分三层，回测与将来的实盘共用同一份判定：
  1. signals()   指标：MA / MACD / RSI / ATR / 动量 / 相对 510300 强度 / 成交额放大
  2. entry/exit  决策：入场多指标共振、出场（破 MA20 或 MACD 转负）、大盘 MA60 空仓、
                 每 N 日按强势度轮动 + 缓冲带 + 最短持有
  3. Backtest    执行：T 收盘出信号 → T+1 开盘成交；涨跌停不成交、停牌不交易、100 股整手、
                 T+1 可卖、单边成本，禁止杠杆与做空（现金不能为负）

参数全部在 config.yaml 的 strong: 段。本文件不写死策略数值。
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "research", "strong"))


# ───────────────────────────────────────────────────────────── 配置
def load_config(path=None):
    path = path or os.path.join(REPO, "config.yaml")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    s = cfg.get("strong") or {}
    if not s:
        raise SystemExit("[strong_trend] config.yaml 缺少 strong: 段")
    s.setdefault("grid", {})
    s["_total_fund"] = float(cfg.get("total_fund", 220000))
    s["_min_lot"] = int(cfg.get("trade", {}).get("min_shares", 100))
    return s


def _p(cfg, key, default):
    v = cfg.get(key, default)
    return default if v is None else v


def with_overrides(cfg, **kw):
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for k, v in kw.items():
        if k.startswith("grid_"):
            c["grid"][k[5:]] = v
        else:
            c[k] = v
    return c


def audit(res, px, tag=""):
    """执行纪律自检：T+1 成交、100 股整手、成交价=当日开盘、不重复方向、无杠杆。"""
    f = res["fills"]
    bad = []
    if f is None or len(f) == 0:
        return bad
    if (f["shares"] % 100 != 0).any():
        bad.append("lot_not_100")
    if not (f["date"] > f["sig_date"]).all():
        bad.append("not_T1")
    op = px["open"].stack()
    px_map = {(d, c): v for (d, c), v in op.items()}
    mis = 0
    for r in f.itertuples():
        v = px_map.get((r.date, r.code), None)
        if v is None or abs(float(v) - float(r.price)) > 1e-9:
            mis += 1
    if mis:
        bad.append(f"price_not_open({mis})")
    dup = f.groupby(["date", "code"])["shares"].apply(lambda x: (x > 0).any() and (x < 0).any())
    if dup.any():
        bad.append("same_day_both_sides")
    if "exposure" in res and float(res["exposure"].max()) > 1.0001:
        bad.append("leverage")
    return bad


# ───────────────────────────────────────────────────────────── 指标
def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n=14):
    d = close.diff()
    au = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    ad = (-d).clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def atr_ratio(px, n=14):
    """ATR / 收盘价，无量纲，网格步长直接用。"""
    h, l, c = px["high"], px["low"], px["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).stack(), (h - pc).abs().stack(), (l - pc).abs().stack()],
                   axis=1).max(axis=1).unstack()
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / c


def signals(px, cfg):
    close, amt = px["close"], px["amt"]
    nf, ns = int(_p(cfg, "ma_fast", 20)), int(_p(cfg, "ma_slow", 60))
    md, rd = int(_p(cfg, "mom_days", 20)), int(_p(cfg, "rs_days", 20))
    f, s, g = cfg.get("macd", [12, 26, 9])
    ma_f = close.rolling(nf, min_periods=max(5, nf // 2)).mean()
    ma_s = close.rolling(ns, min_periods=max(10, ns // 2)).mean()
    macd = ema(close, int(f)) - ema(close, int(s))
    hist = macd - ema(macd, int(g))
    cross = ((hist > 0) & (hist.shift(1) <= 0))
    mom = close / close.shift(md) - 1.0
    amt5 = amt.rolling(5, min_periods=3).mean()
    amt20 = amt.rolling(20, min_periods=10).mean()
    reg_c = str(_p(cfg, "regime_code", "510300"))
    if reg_c in close.columns:
        rc = close[reg_c]
        regime = rc > rc.rolling(int(_p(cfg, "regime_ma", 60)), min_periods=20).mean()
        rs = mom.sub(mom[reg_c], axis=0)
    else:
        regime = pd.Series(True, index=close.index)
        rs = mom
    return dict(ma_f=ma_f, ma_s=ma_s, macd=macd, hist=hist, cross=cross, mom=mom, rs=rs,
                rsi=rsi(close, 14), apr=atr_ratio(px, int(cfg["grid"].get("atr_period", 14))),
                amt5=amt5, amt20=amt20, regime=regime, close=close)


def eligibility(px, cfg):
    """PIT 池：当日有 bar（未停牌）+ 有 >=min_history 根历史 + 20 日成交额中位数达标 + 非货币。"""
    close, amt = px["close"], px["amt"]
    have = close.notna()
    ok = have & (have.cumsum() >= int(_p(cfg, "min_history", 60)))
    ok &= (amt.rolling(20, min_periods=10).median() >= float(_p(cfg, "min_amount", 2e7)))
    ok &= (close > 0)
    if _p(cfg, "exclude_money", True) and "money" in px:
        ok &= ~px["money"].reindex(ok.columns).fillna(False).astype(bool)
    return ok


def entry_mask(sg, elig, cfg, mode="full"):
    """只进强势：多指标共振。mode='mom_only'/'exit_only' 只留动量排名（消融用）。"""
    close, mom = sg["close"], sg["mom"]
    rank = mom.where(elig).rank(axis=1, pct=True)
    top = float(_p(cfg, "mom_top_pct", 0.20))
    base = elig & (rank >= 1.0 - top)
    if mode in ("mom_only", "exit_only"):
        return base.fillna(False)
    cond = base & (close > sg["ma_f"]) & (sg["ma_f"] > sg["ma_s"])
    cond &= (sg["rs"] > float(_p(cfg, "rs_min", 0.0)))
    if str(_p(cfg, "macd_rule", "hist_or_cross")) == "hist":
        cond &= sg["hist"] > 0
    else:
        recent = sg["cross"].rolling(int(_p(cfg, "cross_days", 3)), min_periods=1).max()
        cond &= (sg["hist"] > 0) | recent.astype(bool)
    if bool(_p(cfg, "vol_confirm", True)):
        cond &= sg["amt5"] > sg["amt20"]
    lo, hi = _p(cfg, "rsi_range", [50, 75])
    cond &= (sg["rsi"] >= float(lo)) & (sg["rsi"] <= float(hi))
    return cond.fillna(False)


def strength(sg, elig):
    """强势度 = 动量 40% + 相对强度 30% + 多头排列 20% + MACD 柱 10%（组内百分位）"""
    def r(x):
        return x.where(elig).rank(axis=1, pct=True)
    return (0.40 * r(sg["mom"]) + 0.30 * r(sg["rs"]) +
            0.20 * r(sg["ma_f"] / sg["ma_s"] - 1.0) + 0.10 * r(sg["hist"] / sg["close"]))


def exit_mask(sg, cfg, mode="full"):
    """出场：破 MA20 或 MACD 柱转负。confirm>1 = 连续 N 日成立才算（滤掉单日噪音），
    buffer>0 = 跌破 MA20 还要再低 buffer 才认。"""
    if mode in ("mom_only", "entry_only"):
        return pd.DataFrame(False, index=sg["close"].index, columns=sg["close"].columns)
    cf = max(int(_p(cfg, "exit_confirm", 2)), 1)
    buf = float(_p(cfg, "exit_buffer", 0.01))
    below = sg["close"] < sg["ma_f"] * (1.0 - buf)
    if cf > 1:
        below = below.rolling(cf, min_periods=cf).sum() == cf
    out = below
    if bool(_p(cfg, "exit_macd", True)):
        neg = sg["hist"] < 0
        if cf > 1:
            neg = neg.rolling(cf, min_periods=cf).sum() == cf
        out = out | neg
    return out.fillna(False)


# ───────────────────────────────────────────────────────────── 执行
class Pos:
    __slots__ = ("shares", "base", "cost", "ref", "up", "dn", "open_i", "ep_buy",
                 "ep_sell", "ep_start", "grid_buys", "grid_sells")

    def __init__(self):
        self.shares = 0
        self.base = 0
        self.cost = 0.0
        self.ref = 0.0
        self.up = self.dn = 0
        self.open_i = -1
        self.ep_buy = 0.0
        self.ep_sell = 0.0
        self.ep_start = -1
        self.grid_buys = 0
        self.grid_sells = 0


def _limit_pct(code):
    try:
        from money_manager import _price_limit_pct
        return float(_price_limit_pct(code))
    except Exception:
        return 0.20 if (code[:3] in ("588", "589") or code[:4] == "1599") else 0.10


class Backtest:
    def __init__(self, px, cfg, mode="full", label=""):
        self.px, self.cfg, self.mode, self.label = px, cfg, mode, label
        self.lot = int(cfg.get("_min_lot", 100))
        self.cost = float(_p(cfg, "cost", 0.001))
        self.sg = signals(px, cfg)
        self.elig = eligibility(px, cfg)
        self.emask = entry_mask(self.sg, self.elig, cfg, mode)
        self.strg = strength(self.sg, self.elig)
        self.exits = exit_mask(self.sg, cfg, mode)
        self.grid_on = bool(cfg["grid"].get("enabled", True))
        self.regime_on = bool(_p(cfg, "empty_when_weak", True))

    # ---------- 成交（T 日开盘，订单来自 T-1 收盘）
    def _execute(self, i, orders, pos, cash, fills, eps):
        cal = self.px["cal"]
        d = cal[i]
        op = self.px["open"].iloc[i]
        prev = self.px["close"].iloc[i - 1]
        for code, action, qty, sig_i in orders:
            p_ = float(op.get(code, np.nan))
            if math.isnan(p_) or p_ <= 0:
                continue                                     # 停牌
            pc = float(prev.get(code, np.nan))
            lim = _limit_pct(code)
            if not math.isnan(pc) and pc > 0:
                if action in ("buy", "grid_buy") and p_ >= pc * (1 + lim) - 1e-6:
                    continue                                 # 开盘涨停，买不到
                if action in ("sell", "grid_sell") and p_ <= pc * (1 - lim) + 1e-6:
                    continue                                 # 开盘跌停，卖不掉
            po = pos.setdefault(code, Pos())
            if action in ("buy", "grid_buy"):
                q = int(qty)
                if q * p_ * (1 + self.cost) > cash:
                    q = int((cash / (p_ * (1 + self.cost))) // self.lot) * self.lot
                if q < self.lot:
                    continue
                amt = q * p_
                cash -= amt * (1 + self.cost)
                if po.shares == 0:
                    po.ep_start = i
                    po.ep_buy = po.ep_sell = 0.0
                    po.up = po.dn = 0
                    po.ref = p_
                    po.grid_buys = po.grid_sells = 0
                po.ep_buy += amt * (1 + self.cost)
                po.shares += q
                po.cost += amt * (1 + self.cost)
                if po.base < po.shares:
                    po.base = po.shares
                if action == "grid_buy":
                    po.grid_buys += 1
                fills.append(dict(date=d, code=code, side="grid_buy" if action == "grid_buy" else "buy",
                                  shares=q, price=p_, amount=amt, cost=amt * self.cost,
                                  sig_date=cal[sig_i]))
            else:
                q = min(int(qty) // self.lot * self.lot, po.shares)
                if q < self.lot:
                    continue
                amt = q * p_
                cash += amt * (1 - self.cost)
                po.ep_sell += amt * (1 - self.cost)
                basis = po.cost / po.shares
                po.shares -= q
                po.cost -= basis * q
                if action == "grid_sell":
                    po.grid_sells += 1
                fills.append(dict(date=d, code=code, side="grid_sell" if action == "grid_sell" else "sell",
                                  shares=-q, price=p_, amount=amt, cost=amt * self.cost,
                                  sig_date=cal[sig_i]))
                if po.shares == 0:
                    eps.append(dict(code=code, start=cal[po.ep_start], end=d,
                                    buy=po.ep_buy, sell=po.ep_sell,
                                    pnl=po.ep_sell - po.ep_buy,
                                    days=int(np.busday_count(cal[po.ep_start].date(), d.date())) + 1,
                                    grid_buys=po.grid_buys, grid_sells=po.grid_sells))
        return cash

    # ---------- 主循环
    def run(self, start, end, init=None):
        cal = self.px["cal"]
        win = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
        i0, i1 = int(cal.get_loc(win[0])), int(cal.get_loc(win[-1]))
        cfg = self.cfg
        rebal = int(_p(cfg, "rebalance_days", 5))
        top_n = int(_p(cfg, "top_n", 5))
        min_hold = int(_p(cfg, "min_hold_days", 5))
        top = float(_p(cfg, "mom_top_pct", 0.20))
        stop = float(_p(cfg, "stop_loss", 0.08))
        max_w = float(_p(cfg, "max_weight", 0.30))
        buf_pct = float(_p(cfg, "buffer_pct", 0.10))
        cool = int(_p(cfg, "reentry_cooldown", 0))
        tr = float(cfg["grid"].get("tranche", 0.10))
        hi = float(cfg["grid"].get("hi", 1.4))
        lo = float(cfg["grid"].get("lo", 0.6))
        step_atr = float(cfg["grid"].get("step_atr", 0.5))

        cash = float(init if init is not None else cfg.get("_total_fund", 220000))
        pos, fills, eps = {}, [], []
        eq_h, eq_d, exp_h = [], [], []
        pending, last_rebal = [], -10 ** 9
        sold_at = {}
        close_p = self.sg["close"]

        for i in range(i0, i1 + 1):
            d = cal[i]
            if pending:
                cash = self._execute(i, pending, pos, cash, fills, eps)
                for _c, _a, _q, _s in pending:
                    if str(_a).startswith("sell") or str(_a) == "grid_sell":
                        sold_at[_c] = i
                pending = []
            cl = close_p.iloc[i]
            mv = 0.0
            for c, p in pos.items():
                if p.shares:
                    v = float(cl.get(c, np.nan))
                    if not math.isnan(v):
                        mv += p.shares * v
            eq_h.append(cash + mv)
            eq_d.append(d)
            exp_h.append(mv / (cash + mv) if (cash + mv) > 0 else 0.0)
            if i >= i1:
                break

            eq_now = cash + mv
            regime = bool(self.sg["regime"].iloc[i]) if self.regime_on else True
            orders = {}
            held = [c for c, p in pos.items() if p.shares > 0]

            # ① 平仓：大盘空仓 / 破 MA20 或 MACD 转负 / 硬止损
            for c in held:
                p_x = float(cl.get(c, np.nan))
                if math.isnan(p_x):
                    continue
                p = pos[c]
                avg = p.cost / p.shares if p.shares else 0.0
                hit = (not regime) or bool(self.exits.iloc[i].get(c, False))
                if stop > 0 and avg > 0 and p_x <= avg * (1 - stop):
                    hit = True
                if hit:
                    orders[c] = ("sell", p.shares)

            # ② 网格：只作用于继续持有的头寸
            if self.grid_on and regime:
                for c, p in pos.items():
                    if p.shares <= 0 or c in orders or p.base <= 0:
                        continue
                    p_x = float(cl.get(c, np.nan))
                    if math.isnan(p_x) or p.ref <= 0:
                        continue
                    a = float(self.sg["apr"].iloc[i].get(c, np.nan))
                    if math.isnan(a):
                        continue
                    step = max(step_atr * a, 0.005)
                    if p_x >= p.ref * (1 + step) and p.shares > lo * p.base:
                        q = min(int(tr * p.base) // self.lot * self.lot,
                                max(p.shares - int(lo * p.base) // self.lot * self.lot, 0))
                        if q >= self.lot:
                            orders[c] = ("grid_sell", q)
                            p.ref = p_x
                            p.up += 1
                    elif p_x <= p.ref * (1 - step) and p.shares < hi * p.base:
                        q = int(tr * p.base) // self.lot * self.lot
                        if q >= self.lot and cash > q * p_x * (1 + self.cost) * 1.01:
                            orders[c] = ("grid_buy", q)
                            p.ref = p_x
                            p.dn += 1

            # ③ 轮动
            if i - last_rebal >= rebal:
                last_rebal = i
                er = self.emask.iloc[i]
                cand = [c for c in er.index[er.fillna(False).values]
                        if c not in orders and float(cl.get(c, np.nan)) > 0
                        and (cool <= 0 or i - sold_at.get(c, -10 ** 9) >= cool)]
                st = self.strg.iloc[i]
                cand.sort(key=lambda c: -float(st.get(c, -9e9)))
                mom_pct = self.sg["mom"].iloc[i].where(self.elig.iloc[i]).rank(pct=True)
                keep_pct = 1.0 - top - buf_pct          # 入场 top20% / 持有 top30% 的滞回带
                keep = []
                for c in held:
                    p = pos[c]
                    if c in orders:
                        continue
                    if (i - p.ep_start) < min_hold:
                        keep.append(c)
                    else:
                        mp = float(mom_pct.get(c, np.nan))
                        if (not math.isnan(mp)) and mp >= keep_pct \
                                and bool(self.elig.iloc[i].get(c, False)):
                            keep.append(c)
                want = (keep + [c for c in cand if c not in keep])[:top_n] if regime else []
                for c in held:
                    if c not in want and c not in orders:
                        orders[c] = ("sell", pos[c].shares)
                per = eq_now / max(top_n, 1)
                for c in want:
                    if c in orders:
                        continue
                    p_x = float(cl.get(c, np.nan))
                    if math.isnan(p_x) or p_x <= 0:
                        continue
                    p = pos.get(c)
                    cur = p.shares * p_x if p else 0.0
                    if cur <= 0:
                        q = int(per / (p_x * (1 + self.cost))) // self.lot * self.lot
                        if q >= self.lot and cash > q * p_x * (1 + self.cost):
                            orders[c] = ("buy", q)
                    elif cur > max_w * eq_now:
                        q = int((cur - max_w * eq_now) / p_x) // self.lot * self.lot
                        if q >= self.lot:
                            orders[c] = ("sell", q)

            pending = [(c, a, q, i) for c, (a, q) in orders.items()]

        eq = pd.Series(eq_h, index=pd.DatetimeIndex(eq_d), name="equity")
        fl = pd.DataFrame(fills)
        ep = pd.DataFrame(eps)
        return dict(equity=eq, fills=fl, episodes=ep, cash=cash, label=self.label,
                    exposure=pd.Series(exp_h, index=eq.index), grid_on=self.grid_on)


def audit(res, px, tag=""):
    """执行纪律自检：T+1 成交、100 股整手、成交价=当日开盘、不重复方向、无杠杆。"""
    f = res["fills"]
    bad = []
    if f is None or len(f) == 0:
        return bad
    if (f["shares"] % 100 != 0).any():
        bad.append("lot_not_100")
    if not (f["date"] > f["sig_date"]).all():
        bad.append("not_T1")
    op = px["open"].stack()
    px_map = {(d, c): v for (d, c), v in op.items()}
    mis = 0
    for r in f.itertuples():
        v = px_map.get((r.date, r.code), None)
        if v is None or abs(float(v) - float(r.price)) > 1e-9:
            mis += 1
    if mis:
        bad.append(f"price_not_open({mis})")
    dup = f.groupby(["date", "code"])["shares"].apply(lambda x: (x > 0).any() and (x < 0).any())
    if dup.any():
        bad.append("same_day_both_sides")
    if "exposure" in res and float(res["exposure"].max()) > 1.0001:
        bad.append("leverage")
    return bad


# ───────────────────────────────────────────────────────────── 指标
def perf(eq, rf=0.02):
    eq = eq.dropna()
    if len(eq) < 5:
        return None
    r = eq.pct_change().dropna()
    days = (eq.index[-1] - eq.index[0]).days
    yrs = max(days / 365.25, 1e-9)
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    ann = (1.0 + total) ** (1.0 / yrs) - 1.0
    vol = float(r.std(ddof=1) * math.sqrt(252))
    mdd = float((eq / eq.cummax() - 1.0).min())
    return dict(total=total, ann=ann, vol=vol,
                sharpe=(ann - rf) / vol if vol > 0 else np.nan,
                mdd=mdd, calmar=ann / abs(mdd) if mdd < 0 else np.nan,
                days=days, n=len(eq))


def yearly(eq):
    e = eq.dropna()
    out = {}
    prev = float(e.iloc[0])
    for y, g in e.groupby(e.index.year):
        out[int(y)] = float(g.iloc[-1] / prev - 1.0)
        prev = float(g.iloc[-1])
    return out


def turnover(fills, eq):
    """周换手（单边）= 每周成交额 / (2 × 周初权益)。"""
    if fills is None or len(fills) == 0:
        return 0.0
    f = fills.copy()
    wk = f["date"].dt.to_period("W")
    tot = f.groupby(wk)["amount"].sum()
    eqw = eq.resample("W-MON").first().dropna()
    idx = {}
    for p in tot.index:
        cand = eqw[eqw.index.to_period("W") == p]
        idx[p] = float(cand.iloc[0]) if len(cand) else float(eq.iloc[0])
    one = sum(tot[p] / (2.0 * idx[p]) for p in tot.index if idx[p] > 0)
    return float(one / max(len(tot), 1))


def trade_stats(fills, eps):
    if eps is None or len(eps) == 0:
        return dict(n_fills=0, n_eps=0, win=float("nan"), pl=float("nan"), hold=float("nan"))
    w = eps[eps.pnl > 0]
    l = eps[eps.pnl <= 0]
    return dict(n_fills=int(len(fills)) if fills is not None else 0,
                n_eps=int(len(eps)),
                win=float(len(w) / len(eps)),
                pl=float(w.pnl.mean() / abs(l.pnl.mean())) if len(l) and len(w) else float("nan"),
                hold=float(eps.days.mean()))


def summarize(res, rf=0.02, init=None):
    eq = res["equity"]
    s = perf(eq, rf)
    if s is None:
        return None
    s["yearly"] = yearly(eq)
    s["turnover_week"] = turnover(res["fills"], eq)
    f = res["fills"]
    s["cost"] = float(f["cost"].sum()) if f is not None and len(f) else 0.0
    s["cost_ann"] = s["cost"] / (eq.mean() * max(s["days"] / 365.25, 1e-9))
    s.update(trade_stats(f, res["episodes"]))
    s["final"] = float(eq.iloc[-1])
    s["cash_pct"] = float((res["cash"]) / eq.iloc[-1])
    if f is not None and len(f):
        s["grid_fills"] = int(f["side"].str.startswith("grid").sum())
    else:
        s["grid_fills"] = 0
    return s


# ───────────────────────────────────────────────────────────── 基准
def bench_510300(px, start, end, cost=0.001, init=220000.0):
    cal = px["cal"]
    win = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
    c = px["close"]["510300"].reindex(win).ffill()
    eq = c / c.iloc[0] * init * (1 - cost)
    return pd.Series(eq.values, index=win, name="510300_bh")


def bench_ew(px, cfg, start, end, pool="same", rebal=None, cost=0.001, init=220000.0):
    """等权基准。rebal=None → 买入持有（窗口首日等权买入，之后不动，退市按最后价清出）；
    rebal=5 → 每 5 个交易日再平衡（低换手参照）。"""
    cal = px["cal"]
    win = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
    i0 = int(cal.get_loc(win[0]))
    cols = list(px["close"].columns)
    if pool == "same":
        ok = eligibility(px, cfg)
        live = [c for c in cols if not bool(px["money"].get(c, False))]
        first_ok = ok.iloc[i0]
        sel = [c for c in live if bool(first_ok.get(c, False))]
    else:
        sel = [c for c in cols if not bool(px["money"].get(c, False))]
    sel = [c for c in sel if not math.isnan(float(px["open"].iloc[i0].get(c, np.nan)))]
    cash = float(init)
    w = 1.0 / max(len(sel), 1)
    sh = {}
    for c in sel:
        p = float(px["open"].iloc[i0].get(c, np.nan))
        if math.isnan(p):
            continue
        q = int((cash * w) / (p * (1 + cost))) // 100 * 100
        sh[c] = q
        cash -= q * p * (1 + cost)
    eq_h = []
    step = rebal or 10 ** 9
    for k, i in enumerate(range(i0, i0 + len(win))):
        d = cal[i]
        cl = px["close"].iloc[i]
        opn = px["open"].iloc[i]
        mv = sum(q * float(cl.get(c, np.nan)) for c, q in sh.items()
                 if not math.isnan(float(cl.get(c, np.nan))))
        eq = cash + mv
        eq_h.append((d, eq))
        if rebal and k % step == 0 and k > 0:
            # 等权再平衡（按开盘价，含成本）
            tgt = eq / max(len(sh), 1)
            for c in list(sh):
                p = float(opn.get(c, np.nan))
                if math.isnan(p):
                    continue
                cur = sh[c] * p
                if cur > tgt * 1.02:
                    q = int((cur - tgt) / p) // 100 * 100
                    if q >= 100 and sh[c] - q >= 0:
                        sh[c] -= q
                        cash += q * p * (1 - cost)
                elif cur < tgt * 0.98:
                    q = int((tgt - cur) / (p * (1 + cost))) // 100 * 100
                    if q >= 100 and cash > q * p * (1 + cost):
                        sh[c] += q
                        cash -= q * p * (1 + cost)
    s = pd.Series({d: v for d, v in eq_h})
    return s
