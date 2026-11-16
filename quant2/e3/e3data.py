#!/usr/bin/env python3
"""E3 data layer: one weekly panel carrying BOTH signals on the same PIT grid.

Signals
  prem  close_t / NAV_{t-1} - 1        (E1 convention: NAV of the previous trading day is known
                                        before today's close; raw close, not qfq)
  f     shares_t / shares_{t-1} - 1    (E2 convention lag=1: shares published after the close of
                                        T-1, so at week-end t the last usable print is T-1)
  mom   qfq close_t / close_{t-12w} - 1 (60 trading days)

Forward returns are qfq close(t) -> close(t+h), h = 1/2/4 ISO weeks, so no same-bar leak.
Weekly grid = last real trading day of each ISO week from the exchange calendar.
Universe = E2's filter (>=2亿 scale, 60d median amount >= 500万, kept only after 20 qualifying
weeks), applied on trailing data only.
"""
import json, os, re
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("E3_DATA", os.path.join(ROOT, "data"))
META = os.path.join(DATA, "meta")

MONEY = r"货币|快线|现金|保证金|日利|添利|理财|理财金|短债|中证短融"
CROSS = r"纳指|纳斯达克|标普|美国|道琼斯|恒生|港股|中概|恒指|日经|东证|日本|德国|法国|欧洲|英国|东南亚|越南|印度|沙特|亚太|中韩|中日|H股|新兴市场|全球|国际"
CMD = r"原油|油气|豆粕|有色|白银|黄金|商品|大宗|能源化工|饲料|铜|铝|锌|镍"
WIDE = r"沪深300|中证500|中证1000|中证2000|上证50|上证180|上证380|创业板|科创50|科创100|科创200|中证A500|中证A50|中证100|中证800|中证全指|全指|MSCI|深证100|深证300|深证成指|中小板|等权|180|红利|价值|成长|低波|质量|基本面|自由现金流|A股|沪市|深市|创业板50|双创|大盘|小盘|小微盘"


def calendar():
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    today = pd.Timestamp.now().normalize()
    return cal[cal < today]


def load_shares():
    sh = {}
    d = os.path.join(DATA, "shares", "sse")
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            sh[f[:-5]] = json.load(open(os.path.join(d, f)))
    sh = pd.DataFrame(sh).T
    sh.index = pd.to_datetime(sh.index)
    sz = {}
    d = os.path.join(DATA, "shares", "szse")
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            for dt, vals in json.load(open(os.path.join(d, f))).items():
                sz.setdefault(dt, {}).update(vals)
    sz = pd.DataFrame(sz).T
    sz.index = pd.to_datetime(sz.index)
    return sh.sort_index(), sz.sort_index()


def load_bars(subdir, codes):
    px, amt = {}, {}
    for code in codes:
        p = os.path.join(DATA, subdir, f"{code}.json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p))
        bars = d.get("bars") or []
        if len(bars) < 60:
            continue
        px[code[2:]] = {b[0]: b[2] for b in bars}
        amt[code[2:]] = {b[0]: (b[6] if len(b) > 6 else np.nan) for b in bars}
    p = pd.DataFrame(px).T
    p.columns = pd.to_datetime(p.columns)
    p = p.sort_index(axis=1)
    a = pd.DataFrame(amt).T
    a.columns = pd.to_datetime(a.columns)
    a = a.sort_index(axis=1)
    return p.T, a.T


def unit_nav(bare):
    p = os.path.join(DATA, "nav", f"{bare}.json")
    if not os.path.exists(p):
        return None
    rows = [(r[0], float(r[1])) for r in json.load(open(p)).get("rows", []) if r[1]]
    if len(rows) < 30:
        return None
    s = pd.Series(dict(rows))
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def repair_splits(shares, navs):
    adj = shares.copy()
    ev = {}
    for c in adj.columns:
        s = adj[c].dropna()
        if len(s) < 30:
            continue
        nav = navs.get(c)
        if nav is None:
            continue
        nav = nav.reindex(s.index).ffill(limit=5)
        dls = np.log(s / s.shift(1))
        dln = np.log(nav / nav.shift(1))
        mask = (dls.abs() > 0.20) & ((dls + dln).abs() < 0.08) & dln.notna()
        n_ev = int(mask.sum())
        if n_ev:
            k = np.exp(dls.where(mask, 0.0)).cumprod()
            adj[c] = s / k
        ev[c] = n_ev
    return adj, ev


def classify(name):
    if re.search(MONEY, str(name)):
        return None
    if re.search(CROSS, str(name)):
        return "跨境"
    if re.search(CMD, str(name)):
        return "商品"
    if re.search(r"债|国债|信用债|城投|政金|公司债|短融|转债|可转债", str(name)):
        return "债券"
    if re.search(WIDE, str(name)):
        return "宽基"
    return "行业"


def weekly_grid(cal):
    idx = pd.DatetimeIndex(cal)
    key = idx.isocalendar()
    df = pd.DataFrame({"d": idx, "y": key.year.values, "w": key.week.values})
    return pd.DatetimeIndex(df.groupby(["y", "w"])["d"].max().values).sort_values()


def build(lag=1, min_amount=5e6, min_scale=2e8, since="2015-01-01", min_week_amt=20, prem_lag=1):
    cal = calendar()
    sh, sz = load_shares()
    shares = pd.concat([sh, sz]).sort_index()
    if shares.index.duplicated().any():
        shares = shares.groupby(level=0).first()
    uni = pd.read_csv(os.path.join(META, "universe.csv"), dtype={"code": str})
    names = dict(zip(uni["code"].str[2:], uni["name"]))
    codes = [c for c in shares.columns if c in names]
    exch = ["sh" + c if c.startswith("5") else "sz" + c for c in codes]
    px, amt = load_bars("pxq", exch)                      # qfq (total-return proxy)
    raw, _ = load_bars("price", exch)                     # unadjusted close for the premium
    codes = [c for c in codes if c in px.columns and c in raw.columns]
    navs = {c: unit_nav(c) for c in codes}
    navs = {c: v for c, v in navs.items() if v is not None}
    shares = shares[[c for c in codes if shares[c].notna().sum() > 100]]
    shares = shares.loc[shares.index >= pd.Timestamp("2014-01-01")]
    adj, ev = repair_splits(shares, navs)
    codes = [c for c in adj.columns if c in px.columns and c in raw.columns]

    wk = weekly_grid(cal)
    wk = wk[(wk >= pd.Timestamp(since)) & (wk <= cal[-1])]
    lookup = pd.Series(np.arange(len(cal)), index=cal)
    wk_pos = lookup.reindex(wk).values
    # shares source = lag trading days before the week-end close
    pos = np.clip(wk_pos - lag, 0, len(cal) - 1)
    wk_src = cal[pos]

    def to_week(df, src_index=None, limit=8):
        idx = src_index if src_index is not None else wk
        out = df.reindex(df.index.union(idx)).ffill(limit=limit).reindex(idx)
        out.index = wk
        return out

    S = to_week(adj.reindex(adj.index.union(wk_src)).ffill(limit=8).reindex(wk_src))
    S.index = wk
    P = to_week(px)
    RAW = to_week(raw)
    A = to_week(amt)
    cols = [c for c in codes if c in S.columns and c in P.columns]
    S, P, RAW, A = S[cols], P[cols], RAW[cols], A[cols]

    amt_med = A.rolling(12, min_periods=6).median()
    scale = S * pd.DataFrame({c: navs[c].reindex(S.index).ffill(limit=8) if c in navs else np.nan
                              for c in cols})
    good = (amt_med >= min_amount) & (scale >= min_scale)
    keep = good.fillna(False).cumsum() >= min_week_amt
    keep = keep & S.notna()
    S, P, RAW, A = S.where(keep), P.where(keep), RAW.where(keep), A.where(keep)

    f = S.pct_change().replace([np.inf, -np.inf], np.nan).clip(-0.95, 3.0)

    # ---- premium: weekly grid, NAV of prem_lag trading days before the week-end close -------
    nav_d = pd.DataFrame({c: navs[c] for c in cols if c in navs})
    nav_d = nav_d.reindex(nav_d.index.union(cal)).sort_index().reindex(cal).ffill(limit=7)
    raw_d = raw.reindex(raw.index.union(cal)).sort_index().reindex(cal)[[c for c in cols if c in raw.columns]]
    prem_d = raw_d / nav_d.shift(prem_lag) - 1.0
    prem_d = prem_d.replace([np.inf, -np.inf], np.nan)
    prem_d = prem_d.where(prem_d.abs() < 3.0)
    prem = prem_d.reindex(prem_d.index.union(wk)).ffill(limit=2).reindex(wk)
    prem.index = wk
    # relative richness vs own 60-trading-day norm (E1's dev60: comparable across categories)
    prem_dev_d = prem_d - prem_d.rolling(60, min_periods=30).median()
    prem_dev = prem_dev_d.reindex(prem_dev_d.index.union(wk)).ffill(limit=2).reindex(wk)
    prem_dev.index = wk
    # one common column universe: pandas alignment inside score arithmetic silently drops
    # columns when the signal frames differ, which corrupts rank combinations
    prem = prem.reindex(columns=cols)
    prem_dev = prem_dev.reindex(columns=cols)

    ret = P.pct_change()
    mom = P / P.shift(12) - 1.0
    fwd = {h: P.shift(-h) / P - 1.0 for h in (1, 2, 4)}

    # 20-day trend at the week-end close (daily qfq), for the live-shape test
    px_d = px.reindex(px.index.union(cal)).sort_index().reindex(cal)
    ma20 = px_d.rolling(20, min_periods=10).mean()
    ma60 = px_d.rolling(60, min_periods=20).mean()
    trend20 = (px_d > ma20)
    trend60 = (px_d > ma60)
    T20 = trend20.reindex(trend20.index.union(wk)).ffill(limit=2).reindex(wk)
    T60 = trend60.reindex(trend60.index.union(wk)).ffill(limit=2).reindex(wk)
    T20.index, T60.index = wk, wk
    T20 = T20.reindex(columns=cols)
    T60 = T60.reindex(columns=cols)

    cat = pd.Series({c: classify(names[c]) for c in cols})
    return dict(f=f, prem=prem, prem_dev=prem_dev, mom=mom, ret=ret, fwd=fwd, S=S, P=P, RAW=RAW, A=A,
                scale=scale, cat=cat, names=names, ev=ev, wk=wk, cal=cal, keep=keep,
                T20=T20, T60=T60)


if __name__ == "__main__":
    d = build()
    for k in ("prem", "prem_dev", "f", "mom"):
        print(k, d[k].shape, "non-null", int(d[k].notna().sum().sum()),
              "codes", int((d[k].notna().sum() > 100).sum()))
    print("weeks", len(d["wk"]), d["wk"][0].date(), d["wk"][-1].date())
    print("cats", d["cat"].value_counts().to_dict())
