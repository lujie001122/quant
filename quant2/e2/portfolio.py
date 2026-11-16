#!/usr/bin/env python3
"""E2 practical layer: extreme-flow buckets, and a top-3 concentrated book that mirrors the live
system's shape (top-n=3, weekly rotation) with / without the flow penalty."""
import json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data
from final import spearman, nw_se, boot_ci

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
COST = 0.0012

d = e2data.build(lag=1, since="2015-01-01")
f, mom, fwd1, fwd4 = d["f"], d["mom"], d["fwd"][1], d["fwd"][4]
R = {}

# ---- extreme flow buckets: forward 4w excess return by f bucket
bins = [-9, -0.30, -0.10, -0.02, 0.02, 0.10, 0.30, 9]
lbl = ["<-30%", "-30~-10%", "-10~-2%", "-2~2%", "2~10%", "10~30%", ">30%"]
rows = []
for t in f.index:
    a = f.loc[t]
    if t not in fwd4.index:
        continue
    b = pd.cut(a, bins=bins, labels=lbl)
    r = fwd4.loc[t]
    for k in lbl:
        v = r.reindex(a.index[b == k]).dropna()
        if len(v) >= 3:
            rows.append((k, v.mean(), len(v)))
bt = pd.DataFrame(rows, columns=["bucket", "r", "n"]).groupby("bucket", observed=True).agg(
    r=("r", "mean"), n_obs=("r", "count"), avg_names=("n", "mean"))
bt["ann"] = (1 + bt["r"]) ** (52 / 4) - 1
R["extreme_buckets_h4"] = bt.reset_index().to_dict("records")
_v = f.values.ravel(); _v = _v[~np.isnan(_v)]
R["freq_share"] = {"n_obs": int(len(_v)),
                   "p_gt10": float((_v > 0.10).mean()), "p_gt30": float((_v > 0.30).mean()),
                   "p_lt_-10": float((_v < -0.10).mean()), "p_lt_-30": float((_v < -0.30).mean()),
                   "median": float(np.median(_v)), "p05": float(np.percentile(_v, 5)),
                   "p95": float(np.percentile(_v, 95)), "p01": float(np.percentile(_v, 1)),
                   "p99": float(np.percentile(_v, 99))}

# ---- top-3 concentrated book (mirrors live shape)
def topn_book(score_fn, n=3):
    rets, dates, turn = [], [], []
    prev = None
    for t in f.index:
        if t not in fwd1.index:
            continue
        s = score_fn(t)
        if s is None or s.notna().sum() < 10:
            continue
        sel = list(s.dropna().nlargest(n).index)
        rr = fwd1.loc[t].reindex(sel).dropna()
        if len(rr) == 0:
            continue
        w = pd.Series(1.0 / len(rr), index=rr.index)
        rets.append((w * rr).sum()); dates.append(t)
        if prev is not None:
            turn.append(float(pd.Series(1.0, index=sel).reindex(prev.union(sel)).fillna(0)
                              .sub(pd.Series(1.0, index=prev).reindex(prev.union(sel)).fillna(0))
                              .abs().sum() / 2))
        prev = pd.Index(sel)
    s = pd.Series(rets, index=pd.DatetimeIndex(dates))
    tn = float(np.mean(turn)) if turn else np.nan
    return s, tn

def mom_score(t):
    a, m = f.loc[t], mom.loc[t]
    ok = a.notna() & m.notna()
    return m.where(ok)

def mix_score(lam):
    def fn(t):
        a, m = f.loc[t], mom.loc[t]
        ok = a.notna() & m.notna()
        if ok.sum() < 10:
            return None
        return (m[ok].rank(pct=True) - lam * a[ok].rank(pct=True))
    return fn

books = {}
for name, fn in [("MOM_top3", mom_score), ("MOM-F(0.1)_top3", mix_score(0.1)),
                 ("MOM-F(0.3)_top3", mix_score(0.3)), ("MOM-F(0.5)_top3", mix_score(0.5))]:
    s, tn = topn_book(fn, 3)
    books[name] = dict(ann_gross=float((1 + s.mean()) ** 52 - 1), turnover=tn,
                       cost=float(COST * tn * 52),
                       ann_net=float((1 + s.mean()) ** 52 - 1 - COST * tn * 52), n=int(len(s)))
    R[name] = books[name]
R["top3_books"] = books

# paired diff of the best lambda vs mom_only
base, _ = topn_book(mom_score, 3)
for lam in (0.1, 0.3, 0.5):
    alt, _ = topn_book(mix_score(lam), 3)
    diff = (alt - base).dropna()
    R[f"top3_diff_lam{lam}"] = dict(diff_ann=float((1 + diff.mean()) ** 52 - 1),
                                    t=float(diff.mean() / nw_se(diff, 1)), n=int(len(diff)),
                                    win=float((diff > 0).mean()))

json.dump(R, open(os.path.join(OUT, "E2_portfolio.json"), "w"), indent=1, ensure_ascii=False)
pd.set_option("display.width", 200)
print(bt.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nextreme share:", {k: f"{v:.2%}" for k, v in R["freq_share"].items()})
print("\ntop3 books:")
print(pd.DataFrame(books).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\npaired vs MOM_top3:")
print(pd.DataFrame({k: v for k, v in R.items() if k.startswith("top3_diff")}).T
      .to_string(float_format=lambda x: f"{x:+.4f}"))
