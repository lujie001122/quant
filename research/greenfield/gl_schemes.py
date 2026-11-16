"""Structurally different scheme families for the greenfield research."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_engine as E

POOL_DEFS = {
    "all": None,
    "qdii_comm": {"跨境QDII", "商品"},
    "theme_ind": {"主题", "行业"},
    "commod": {"商品"},
    "qdii": {"跨境QDII"},
    "broad": {"宽基"},
}


def pool_mask(close: pd.DataFrame, meta: dict, pool: str) -> pd.DataFrame:
    cats = POOL_DEFS[pool]
    m = pd.DataFrame(True, index=close.index, columns=close.columns)
    if cats is not None:
        keep = [c for c in close.columns if meta.get(c) in cats]
        m = pd.DataFrame(False, index=close.index, columns=close.columns)
        m[keep] = True
    return m


def elig_for(close, amount, meta, pool, min_amt=2e7, min_days=60):
    base = E.eligible_mask(close, amount, min_days=min_days, min_amt=min_amt)
    return base & pool_mask(close, meta, pool)


# ------------------------------------------------------------------ schemes
def s_short_mom(close, amount, meta, pool="all", topn=5, lb=3, rebal=1,
                cost=0.001, absmom=True, trend_ma=0):
    elig = elig_for(close, amount, meta, pool)
    px = close.ffill()
    score = E.momentum(px, lb)
    if trend_ma:
        above = px > px.rolling(trend_ma, min_periods=trend_ma // 2).mean()
        score = score.where(above)
    return E.run_rotation(close, elig, score, topn=topn, rebal=rebal, cost=cost,
                          min_score=0.0 if absmom else None)


def s_trend(close, amount, meta, pool="qdii_comm", topn=3, lb=20, rebal=5,
            cost=0.001, ma=60):
    elig = elig_for(close, amount, meta, pool)
    return E.run_trend(close, elig, topn=topn, rebal=rebal, cost=cost, ma=ma,
                       lookback=lb, use_abs_mom=True)


def s_hivol_theme(close, amount, meta, topn=3, lb=10, rebal=5, cost=0.001,
                  vol_q=0.5, ma=60):
    elig = elig_for(close, amount, meta, "theme_ind")
    px = close.ffill()
    vol = px.pct_change().rolling(60).std()
    rank = vol.rank(axis=1, pct=True)
    elig = elig & (rank >= 1 - vol_q)
    score = E.momentum(px, lb)
    if ma:
        score = score.where(px > px.rolling(ma, min_periods=ma // 2).mean())
    return E.run_rotation(close, elig, score, topn=topn, rebal=rebal, cost=cost,
                          min_score=0.0)


def s_intraday(close, amount, meta, topn=5, rebal=1, cost=0.001, mode="strong",
               pool="all", ma=20):
    """Signal from the *intraday* part of the bar: close/open-1, held 1+ days.

    mode='strong'  : chase intraday strength (buy close, hold k days)
    mode='revert'  : buy intraday weakness (contrarian)
    """
    op = pd.DataFrame({c: d["open"] for c, d in _DATA.items()}).reindex(close.index)
    intraday = close / op - 1.0
    elig = elig_for(close, amount, meta, pool)
    px = close.ffill()
    if ma:
        elig = elig & (px > px.rolling(ma, min_periods=ma // 2).mean())
    sc = intraday if mode == "strong" else -intraday
    return E.run_rotation(close, elig, sc, topn=topn, rebal=rebal, cost=cost,
                          min_score=0.0)


_DATA: dict[str, pd.DataFrame] = {}


def set_data(data):
    global _DATA
    _DATA = data


def combine(curves: dict[str, pd.Series], mode="invvol", window=60):
    """Combine return streams: inverse-vol weights (trailing), daily rebalance."""
    df = pd.DataFrame(curves).fillna(0.0)
    if mode == "eq":
        return df.mean(axis=1)
    vol = df.rolling(window).std()
    iv = 1.0 / vol.replace(0, np.nan)
    w = iv.div(iv.sum(axis=1), axis=0)
    return (df * w.fillna(1 / len(df.columns))).sum(axis=1)
