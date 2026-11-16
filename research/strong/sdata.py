#!/usr/bin/env python3
"""Research data layer for the strong-trend book.

Source of truth: quant2/data (十亿级腾讯前复权日 K 缓存，主工作区同一份，不重复抓取).

Hard rules enforced here, not in the engine:
  * trading calendar = exchange calendar (quant2/data/meta/index_calendar.json).
    EVERY bar date of EVERY code must be a member of that calendar; any bar on a
    non-trading day makes the process sys.exit(2). This is the defence against the
    "节假日假 K 线" that made rotation/grid look good in the old backtest.
  * PIT universe: a code is eligible on day d only if first_bar <= d <= last_bar
    (from its own bars) AND the bar for d actually exists (suspension -> not tradable).
  * Prices are 前复权 (qfq) for signals/returns; limit-up/down detection uses the
    same series ratio vs the previous close (multiplicative adjustment preserves it).

Panels are (date x code) float frames. Amount is in CNY.
"""
import json
import os
import pickle
import re
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(ROOT))
MAIN = os.environ.get("QUANT_MAIN", "/Users/lujie/Documents/code/quant")
DATA = os.environ.get("STRONG_DATA", os.path.join(MAIN, "quant2", "data"))
META = os.path.join(DATA, "meta")
CACHE = os.path.join(ROOT, "cache_panel.pkl")

# 类现金标的没有趋势可言（且交易所不收 ETF 印花税，货币 ETF 走的是另一套定价），剔除
MONEY = re.compile(r"货币|快线|现金|保证金|日利|添利|理财|理财金|短债|中证短融")

FIRST = pd.Timestamp("2019-01-01")   # 指标预热起点（MA60 / 250 日分位都要历史）
LAST = pd.Timestamp("2026-11-16")


def _die(msg):
    print(f"[sdata] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(2)


def calendar():
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    cal = cal[(cal >= FIRST) & (cal <= LAST)].sort_values()
    if len(cal) == 0:
        _die("empty exchange calendar")
    return cal


def _bars(path):
    d = json.load(open(path))
    rows = d.get("bars") or []
    if not rows:
        return None
    a = np.array([r[:7] for r in rows], dtype=object)
    dates = pd.to_datetime(a[:, 0])
    o = a[:, 1].astype(float)
    c = a[:, 2].astype(float)
    h = a[:, 3].astype(float)
    lo = a[:, 4].astype(float)
    v = np.nan_to_num(a[:, 5].astype(float))
    amt = np.nan_to_num(a[:, 6].astype(float)) if a.shape[1] > 6 else np.full(len(a), np.nan)
    return dates, o, c, h, lo, v, amt


def build():
    cal = calendar()
    calset = set(cal)
    uni = pd.read_csv(os.path.join(META, "universe.csv"), dtype={"code": str})
    names = dict(zip(uni["code"], uni["name"]))

    frames = {k: {} for k in ("open", "close", "high", "low", "vol", "amt")}
    firsts, lasts = {}, {}
    bad_dates = []
    for code in uni["code"]:
        p = os.path.join(DATA, "pxq", f"{code}.json")
        if not os.path.exists(p):
            continue
        b = _bars(p)
        if b is None:
            continue
        dates, o, c, h, lo, v, amt = b
        keep = (dates >= FIRST) & (dates <= LAST)
        if keep.sum() < 120:
            continue
        dates = pd.DatetimeIndex(dates[keep])
        # ---- 非交易日 bar 硬校验（历史假 K 线事故的防线）----
        off = [d for d in dates if d not in calset]
        if off:
            bad_dates.append((code, len(off), str(off[0].date())))
            continue
        if dates.duplicated().any():
            _die(f"{code} duplicate bar dates")
        bare = code[2:]
        ser = {
            "open": o[keep], "close": c[keep], "high": h[keep], "low": lo[keep],
            "vol": v[keep], "amt": amt[keep],
        }
        # OHLC 自洽性（错价的 bar 会污染 ATR/网格）
        ok = (ser["high"] + 1e-9 >= np.maximum(ser["open"], ser["close"])) & \
             (ser["low"] - 1e-9 <= np.minimum(ser["open"], ser["close"])) & \
             (ser["close"] > 0) & (ser["open"] > 0)
        if (~ok).any():
            ser = {k: pd.Series(vv[ok], index=dates[ok]) for k, vv in ser.items()}
            dates = dates[ok]
        for k in frames:
            frames[k][bare] = pd.Series(ser[k], index=dates)
        firsts[bare], lasts[bare] = dates[0], dates[-1]

    out = {}
    for k, dd in frames.items():
        f = pd.DataFrame(dd).reindex(cal).sort_index()
        out[k] = f.astype("float64")
    out["first"] = pd.Series(firsts, dtype="datetime64[ns]")
    out["last"] = pd.Series(lasts, dtype="datetime64[ns]")
    out["name"] = pd.Series({c: names.get(("sh" if c.startswith("5") else "sz") + c, c)
                             for c in out["close"].columns})
    out["cal"] = cal
    out["bad_dates"] = bad_dates
    out["money"] = pd.Series({c: bool(MONEY.search(str(out["name"][c])))
                              for c in out["close"].columns})
    print(f"[sdata] codes={out['close'].shape[1]} days={len(cal)} "
          f"{cal[0].date()}..{cal[-1].date()} rejected_non_trading_bars={len(bad_dates)}",
          flush=True)
    return out


def load(rebuild=False):
    if (not rebuild) and os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    d = build()
    pickle.dump(d, open(CACHE, "wb"))
    return d


if __name__ == "__main__":
    d = load(rebuild="--rebuild" in sys.argv)
    c = d["close"]
    for w in (("2021-11-17", "2026-11-16"), ("2023-11-17", "2026-11-16")):
        m = (c.index >= w[0]) & (c.index <= w[1])
        sub = c.loc[m]
        live = sub.columns[(sub.notna().sum() > 0)]
        late = sub.columns[(sub.notna() & (sub.notna().cumsum() == 0)).any()]
        print(w, "days", int(m.sum()), "codes_with_data", len(live))
    print("money-like", int(d["money"].sum()))
    if d["bad_dates"]:
        print("REJECTED:", d["bad_dates"][:10])
