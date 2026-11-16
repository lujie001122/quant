#!/usr/bin/env python3
"""E2 data layer: weekly net-subscription panel + forward returns on the real trading calendar.

Inputs (all local caches):
  data/shares/{sse,szse}/*.json  daily 份额 from the two exchanges (份)
  data/pxq/{code}.json           daily qfq close (total return) from Tencent
  data/nav/{bare}.json           unit NAV, used ONLY to detect 份额折算/拆分 (share splits)
  data/meta/{index_calendar.json,universe.csv}

Weekly grid = last trading day of each ISO week. Signals at week-end t, forward return from the
close of t to the close of t+h (h=1,2,4 weeks), so no same-bar look-ahead.

PIT: 份额 is published after the close of T. Using shares dated t for a trade that starts at the
close of t is the optimistic case; `--lag L` shifts the share observation back L trading days
(default 0 = same-day close, tested against 1/2).
"""
import argparse, json, os, re
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "meta")

MONEY = r"货币|快线|现金|保证金|日利|添利|理财|理财金|保证金|短债|中证短融"
CROSS = r"纳指|纳斯达克|标普|美国|道琼斯|恒生|港股|中概|恒指|日经|东证|日本|德国|法国|欧洲|英国|东南亚|越南|印度|沙特|亚太|中韩|中日|H股|沙特|新兴市场|全球|国际"
CMD = r"原油|油气|豆粕|有色|白银|黄金|商品|大宗|能源化工|饲料|铜|铝|锌|镍"
WIDE = r"沪深300|中证500|中证1000|中证2000|上证50|上证180|上证380|创业板|科创50|科创100|科创200|中证A500|中证A50|中证100|中证800|中证全指|全指|MSCI|深证100|深证300|深证成指|中小板|等权|180|红利|价值|成长|低波|质量|基本面|自由现金流|A股|沪市|深市|创业板50|双创|大盘|小盘|小微盘"


def calendar():
    cal = pd.DatetimeIndex(pd.to_datetime(json.load(open(os.path.join(META, "index_calendar.json")))))
    today = pd.Timestamp.now().normalize()
    return cal[cal < today]


def load_shares():
    sh = {}
    d = os.path.join(ROOT, "data", "shares", "sse")
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            sh[f[:-5]] = json.load(open(os.path.join(d, f)))
    sh = pd.DataFrame(sh).T
    sh.index = pd.to_datetime(sh.index)
    sz = {}
    d = os.path.join(ROOT, "data", "shares", "szse")
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            for dt, vals in json.load(open(os.path.join(d, f))).items():
                sz.setdefault(dt, {}).update(vals)
    sz = pd.DataFrame(sz).T
    sz.index = pd.to_datetime(sz.index)
    return sh.sort_index(), sz.sort_index()


def load_prices(codes):
    px, amt = {}, {}
    for code in codes:
        p = os.path.join(ROOT, "data", "pxq", f"{code}.json")
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
    p = os.path.join(ROOT, "data", "nav", f"{bare}.json")
    if not os.path.exists(p):
        return None
    rows = [(r[0], float(r[1])) for r in json.load(open(p)).get("rows", []) if r[1]]
    if len(rows) < 30:
        return None
    s = pd.Series(dict(rows))
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def repair_splits(shares: pd.DataFrame, navs: dict):
    """Remove 份额折算/拆分 jumps: shares jump by k while NAV jumps by 1/k (scale unchanged).

    Returns adjusted share panel plus the count of events per code.
    """
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
    """last trading day of each ISO week"""
    idx = pd.DatetimeIndex(cal)
    key = idx.isocalendar()
    df = pd.DataFrame({"d": idx, "y": key.year.values, "w": key.week.values})
    return pd.DatetimeIndex(df.groupby(["y", "w"])["d"].max().values).sort_values()


def build(lag=0, min_amount=5e6, min_scale=2e8, since="2015-01-01", min_week_amt=20):
    cal = calendar()
    sh, sz = load_shares()
    shares = pd.concat([sh, sz]).sort_index()
    shares = shares.groupby(level=0).first() if shares.index.duplicated().any() else shares
    uni = pd.read_csv(os.path.join(META, "universe.csv"), dtype={"code": str})
    names = dict(zip(uni["code"].str[2:], uni["name"]))
    codes = [c for c in shares.columns if c in names]
    px, amt = load_prices(["sh" + c if c.startswith("5") else "sz" + c for c in codes])
    codes = [c for c in codes if c in px.columns]
    navs = {c: unit_nav(c) for c in codes}
    shares = shares[[c for c in codes if shares[c].notna().sum() > 100]]
    shares = shares.loc[shares.index >= pd.Timestamp("2014-01-01")]
    adj, ev = repair_splits(shares, navs)

    wk = weekly_grid(cal)
    wk = wk[(wk >= pd.Timestamp(since)) & (wk <= cal[-1])]
    # last actual observation on/before each week end, shares shifted by lag trading days
    lookup = pd.Series(np.arange(len(cal)), index=cal)
    wk_pos = lookup.reindex(wk).values
    pos = np.clip(wk_pos - lag, 0, len(cal) - 1)
    wk_src = cal[pos]

    S = adj.reindex(adj.index.union(wk_src)).ffill(limit=8).reindex(wk_src)
    S.index = wk
    P = px.reindex(px.index.union(wk)).ffill(limit=8).reindex(wk)
    P.index = wk
    A = amt.reindex(amt.index.union(wk)).ffill(limit=8).reindex(wk)
    A.index = wk

    cols = [c for c in S.columns if c in P.columns]
    S, P, A = S[cols], P[cols], A[cols]

    # liquidity / size filters, trailing so no look-ahead for the *universe* choice
    amt_med = A.rolling(12, min_periods=6).median()
    scale = (S * pd.DataFrame({c: navs[c].reindex(S.index).ffill() if navs.get(c) is not None else np.nan
                               for c in cols}))
    good = (amt_med >= min_amount) & (scale >= min_scale)
    # keep a code once it has been tradable for at least min_week_amt weeks (expanding, no future info)
    keep = good.fillna(False).cumsum() >= min_week_amt
    S = S.where(keep)
    P = P.where(keep)
    A = A.where(keep)

    f = S.pct_change()
    f = f.replace([np.inf, -np.inf], np.nan)
    f = f.clip(-0.95, 3.0)  # a single week of >300% net creation is a data artefact / 折算
    ret = P.pct_change()
    mom = P / P.shift(12) - 1.0  # 60 trading days ~ 12 weeks
    fwd = {h: P.shift(-h) / P - 1.0 for h in (1, 2, 4)}
    cat = pd.Series({c: classify(names[c]) for c in cols})
    return dict(f=f, ret=ret, mom=mom, fwd=fwd, S=S, P=P, A=A, scale=scale,
                cat=cat, names=names, ev=ev, wk=wk, cal=cal, keep=keep)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lag", type=int, default=0)
    a = ap.parse_args()
    d = build(lag=a.lag)
    f, m = d["f"], d["mom"]
    n = f.notna().sum()
    print("weeks:", len(d["wk"]), d["wk"][0].date(), "->", d["wk"][-1].date())
    print("codes with f:", f.shape[1])
    print("codes with >=200 weekly f obs:", int((n >= 200).sum()))
    print("codes with >=250 weekly f obs:", int((n >= 250).sum()))
    print("per-category counts (>=200 obs):")
    print(d["cat"].loc[n[n >= 200].index].value_counts().to_string())
    print("median weekly obs per code:", int(n.median()))
    print("split events repaired:", int(sum(d["ev"].values())), "codes affected:", sum(1 for v in d["ev"].values() if v))
