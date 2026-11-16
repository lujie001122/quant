#!/usr/bin/env python3
"""E2 extras: is f additive to 60-day momentum, and what would a portfolio actually earn?

  1. decile profile of f (monotonicity at the extremes)
  2. double sort: momentum tercile x f quintile
  3. actual weekly-rebalanced books: EW / momentum / momentum minus high-f / f-only,
     name-level turnover and 0.12%-per-side cost
  4. flow variants: raw f, surprise vs own 8w mean, own-history z-score, |f|
  5. feedback: does f chase past returns? (corr(f_t, mom_t) and corr(f_t, past 1w return))
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data
from final import spearman, nw_se, boot_ci, ic_stats, rank_ic, xs_demean

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
COST = 0.0012

d = e2data.build(lag=1, since="2015-01-01")
f, mom, cat, scale = d["f"], d["mom"], d["cat"], d["scale"]
fwd_raw, ret = d["fwd"], d["ret"]
fwd = {h: xs_demean(r) for h, r in fwd_raw.items()}
R = {}

# ---------------- 1. deciles
rows = []
for t in f.index:
    a = f.loc[t].dropna()
    if len(a) < 40 or t not in fwd_raw[1].index:
        continue
    dec = pd.qcut(a.rank(method="first"), 10, labels=False)
    rr = fwd_raw[1].loc[t]
    for k in range(10):
        sel = dec.index[dec == k]
        v = rr.reindex(sel).dropna()
        if len(v):
            rows.append((k, v.mean(), len(v)))
dec = pd.DataFrame(rows, columns=["dec", "r", "n"]).groupby("dec").agg(
    r=("r", "mean"), n=("n", "mean"), weeks=("r", "count"))
dec["ann"] = (1 + dec["r"]) ** 52 - 1
R["deciles_h1"] = dec.reset_index().to_dict("records")

# ---------------- 2. double sort: momentum terciles x f quintiles
cells = []
for t in f.index:
    a, m = f.loc[t], mom.loc[t]
    if t not in fwd_raw[4].index:
        continue
    ok = a.notna() & m.notna()
    if ok.sum() < 60:
        continue
    aa, mm = a[ok], m[ok]
    mt = pd.qcut(mm.rank(method="first"), 3, labels=False)
    fq = pd.qcut(aa.rank(method="first"), 5, labels=False)
    rr = fwd_raw[4].loc[t]
    for mi in range(3):
        for fi in range(5):
            sel = aa.index[(mt == mi) & (fq == fi)]
            v = rr.reindex(sel).dropna()
            if len(v):
                cells.append((mi, fi, v.mean(), len(v)))
cell = pd.DataFrame(cells, columns=["mom", "fq", "r", "n"]).groupby(["mom", "fq"]).mean()
R["double_sort_h4"] = {f"mom{int(m)}_{int(q)}": float(r["r"]) for (m, q), r in cell.iterrows()}
# f-spread inside each momentum bucket (Q1-Q5, 4w)
R["f_spread_within_mom_h4"] = {}
for mi in range(3):
    sub = cell.loc[mi]
    R["f_spread_within_mom_h4"][f"mom{mi}"] = float(sub.loc[0, "r"] - sub.loc[4, "r"])

# ---------------- 3. portfolio books
def book(pick, h=1, cost=COST, weeks=None):
    """pick(df_of_signals_at_t) -> list of codes; equal weight; weekly rebalance."""
    ws = [t for t in (weeks if weeks is not None else f.index) if t in fwd_raw[h].index]
    rets, turns = [], []
    prev = None
    for t in ws:
        sel = pick(t)
        if not sel:
            continue
        w = pd.Series(1.0 / len(sel), index=pd.Index(sel))
        rr = fwd_raw[h].loc[t].reindex(sel)
        if rr.notna().sum() == 0:
            continue
        ww = w[rr.notna()]
        ww = ww / ww.sum()
        rets.append(float((ww * rr[rr.notna()]).sum()))
        if prev is not None:
            d = ww.sub(prev, fill_value=0.0).abs().sum() / 2.0
            turns.append(float(d))
        prev = ww
    if len(rets) < 30:
        return None
    s = pd.Series(rets)
    turn = float(np.mean(turns)) if turns else np.nan
    ann_g = float((1 + s.mean()) ** (52 / h) - 1)
    c = cost * turn * (52.0 / h)
    return dict(ann_gross=ann_g, turnover=turn, cost_ann=float(c), ann_net=ann_g - c,
                n_weeks=len(s), avg_names=float(np.mean([len(pick(t)) for t in ws if pick(t)])))

def top_by(score_fn, n_top):
    def pick(t):
        s = score_fn(t)
        if s is None or s.notna().sum() < 20:
            return []
        return list(s.dropna().nlargest(n_top).index)
    return pick

def mom_score(t):
    return mom.loc[t]

books = {}
books["EW_all"] = book(lambda t: list(f.loc[t].dropna().index) if f.loc[t].notna().sum() >= 20 else [])
# momentum top 1/3 of the tradable universe each week
def mom_third(t, exclude=None, min_n=20):
    m = mom.loc[t].dropna()
    avail = f.loc[t].dropna().index
    m = m[m.index.isin(avail)]
    if len(m) < 30:
        return []
    k = int(np.ceil(len(m) / 3))
    sel = list(m.nlargest(k).index)
    if exclude is not None:
        ex = exclude(t)
        sel = [c for c in sel if c not in ex]
    return sel
books["MOM_top1/3"] = book(lambda t: mom_third(t))
books["MOM_top1/3_drop_fQ5"] = book(lambda t: mom_third(t, exclude=lambda tt: [
    c for c in f.loc[tt].dropna().index
    if f.loc[tt, c] > f.loc[tt].quantile(0.8)]))
books["MOM_top1/3_drop_fTop10%"] = book(lambda t: mom_third(t, exclude=lambda tt: [
    c for c in f.loc[tt].dropna().index
    if f.loc[tt, c] > f.loc[tt].quantile(0.9)]))
books["fQ1_lowest_flow"] = book(lambda t: list(f.loc[t].dropna().nsmallest(
    max(20, int(np.ceil(f.loc[t].notna().sum() / 5)))).index))
books["fQ1_bottom10%"] = book(lambda t: list(f.loc[t].dropna().nsmallest(
    max(10, int(np.ceil(f.loc[t].notna().sum() / 10)))).index))
# combined: cross-sectional percentile of mom minus percentile of f, top 10%
def comb(t):
    m, ff = mom.loc[t], f.loc[t]
    ok = m.notna() & ff.notna()
    if ok.sum() < 40:
        return []
    score = m[ok].rank(pct=True) - ff[ok].rank(pct=True)
    k = max(10, int(np.ceil(len(score) * 0.10)))
    return list(score.nlargest(k).index)
books["MOM_minus_F_top10%"] = book(comb)
R["books_h1"] = books

# h=4 version for the best two
books4 = {k: book(books_pick, h=4) for k, books_pick in {
    "MOM_top1/3": lambda t: mom_third(t),
    "MOM_top1/3_drop_fQ5": lambda t: mom_third(t, exclude=lambda tt: [
        c for c in f.loc[tt].dropna().index if f.loc[tt, c] > f.loc[tt].quantile(0.8)]),
    "fQ1_lowest_flow": lambda t: list(f.loc[t].dropna().nsmallest(
        max(20, int(np.ceil(f.loc[t].notna().sum() / 5)))).index),
    "EW_all": lambda t: list(f.loc[t].dropna().index),
}.items()}
R["books_h4"] = books4

# sub-period stability of the momentum filter
for name, (a0, a1) in {"2017_2021": ("2017-01-01", "2021-12-31"),
                       "2022_2026": ("2022-01-01", "2026-12-31")}.items():
    ws = f.index[(f.index >= pd.Timestamp(a0)) & (f.index <= pd.Timestamp(a1))]
    R[f"books_{name}"] = {
        "MOM_top1/3": book(lambda t: mom_third(t), weeks=ws),
        "MOM_top1/3_drop_fQ5": book(lambda t: mom_third(t, exclude=lambda tt: [
            c for c in f.loc[tt].dropna().index if f.loc[tt, c] > f.loc[tt].quantile(0.8)]),
            weeks=ws),
    }

# ---------------- 4. flow variants
fsurp = f - f.rolling(8, min_periods=4).mean()
fz = (f - f.rolling(12, min_periods=6).mean()) / (f.rolling(12, min_periods=6).std() + 1e-9)
R["ic_variants"] = {}
for name, sig in [("raw", f), ("surprise_8w", fsurp), ("zscore_12w", fz), ("abs", f.abs())]:
    R["ic_variants"][name] = {f"h{h}": ic_stats(rank_ic(sig, fwd[h]), h) for h in (1, 2, 4)}

# ---------------- 5. feedback: does flow chase returns?
rows = []
for t in f.index:
    a, m = f.loc[t], mom.loc[t]
    ok = a.notna() & m.notna()
    if ok.sum() >= 30:
        rows.append((spearman(a[ok], m[ok]), spearman(a[ok], ret.loc[t][ok])))
fb = pd.DataFrame(rows, columns=["rho_mom", "rho_ret"]).dropna()
R["feedback"] = dict(rho_mom_mean=float(fb.rho_mom.mean()), rho_mom_t=float(fb.rho_mom.mean() / nw_se(fb.rho_mom)),
                     rho_lastweek_mean=float(fb.rho_ret.mean()),
                     rho_lastweek_t=float(fb.rho_ret.mean() / nw_se(fb.rho_ret)), n=len(fb))

# autocorrelation of f (persistence)
ac = []
for lag in (1, 2, 4):
    vals = []
    for t in f.index:
        t2 = f.index[f.index.get_loc(t) + lag] if f.index.get_loc(t) + lag < len(f.index) else None
        if t2 is None:
            continue
        a, b = f.loc[t], f.loc[t2]
        ok = a.notna() & b.notna()
        if ok.sum() >= 20:
            vals.append(spearman(a[ok], b[ok]))
    ac.append((lag, float(np.mean(vals))))
R["f_autocorr"] = {str(l): v for l, v in ac}

json.dump(R, open(os.path.join(OUT, "E2_extra.json"), "w"), indent=1, ensure_ascii=False)
pd.set_option("display.width", 200)
print("deciles (h=1):")
print(dec.to_string(float_format=lambda x: f"{x:.4f}"))
print("\ndouble sort h=4 (row=mom tercile, col=f quintile), mean 4w excess return:")
print(cell["r"].unstack().to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nbooks h=1:")
print(pd.DataFrame(books).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nbooks h=4:")
print(pd.DataFrame(books4).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nic variants:")
for k, v in R["ic_variants"].items():
    print(" ", k, " ".join(f"h{h}:{v['h'+h]['ic']:+.4f}(t{v['h'+h]['t']:+.1f})" for h in ("1", "2", "4")))
print("\nfeedback:", R["feedback"])
print("f autocorr:", R["f_autocorr"])
