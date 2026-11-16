#!/usr/bin/env python3
"""E2 extras 2: yearly stability + paired tests + sensitivity of the f-filter book.

Builds the weekly books once (vectorised selection), then reports:
  * yearly net return per book
  * paired t-test / bootstrap CI of (MOM - fQ5) vs MOM weekly nets
  * sensitivity to the exclusion threshold and to the universe size
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data
from final import spearman, nw_se, boot_ci

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
COST = 0.0012


def precompute(d):
    f, mom, scale = d["f"], d["mom"], d["scale"]
    ok = f.notna() & mom.notna()
    fr = f.where(ok).rank(axis=1, pct=True)
    mr = mom.where(ok).rank(axis=1, pct=True)
    return f, mom, scale, ok, fr, mr


def book_from_mask(mask, fwd1, cost=COST):
    """equal weight over selected names each week; returns weekly gross/net series + turnover"""
    gross, turn, dates = [], [], []
    prev = None
    for t in mask.index:
        if t not in fwd1.index:
            continue
        sel = mask.columns[mask.loc[t].fillna(False).values]
        if len(sel) < 5:
            continue
        rr = fwd1.loc[t].reindex(sel).dropna()
        if len(rr) < 5:
            continue
        w = pd.Series(1.0 / len(rr), index=rr.index)
        gross.append((w * rr).sum()); dates.append(t)
        if prev is not None:
            turn.append(float(w.sub(prev, fill_value=0.0).abs().sum() / 2.0))
        prev = w
    s = pd.Series(gross, index=pd.DatetimeIndex(dates))
    tn = float(np.mean(turn)) if turn else np.nan
    net = s - cost * tn
    return s, net, tn


def yearly(net):
    return {str(y): float((1 + g).prod() - 1) for y, g in net.groupby(net.index.year)}


def paired(a, b):
    """a - b weekly net diff: mean, NW t, bootstrap CI, annualised diff"""
    d = (a - b).dropna()
    mu = d.mean()
    se = nw_se(d, lag=1)
    lo, hi = boot_ci(d)
    return dict(diff_ann=float((1 + mu) ** 52 - 1), t=float(mu / se), ci_lo_ann=float((1 + lo) ** 52 - 1),
                ci_hi_ann=float((1 + hi) ** 52 - 1), n=int(len(d)), win_frac=float((d > 0).mean()))


d = e2data.build(lag=1, since="2015-01-01")
f, mom, scale, ok, fr, mr = precompute(d)
fwd1 = d["fwd"][1]
R = {}

books, masks = {}, {}

# --- reference books
masks["EW_all"] = ok
masks["MOM_top1/3"] = mr >= (2 / 3)
masks["MOM_top1/3_drop_fQ5"] = (mr >= (2 / 3)) & (fr < 0.8)
masks["MOM_top1/3_drop_fTop10%"] = (mr >= (2 / 3)) & (fr < 0.9)
masks["fQ1_lowest_flow"] = fr <= 0.2
masks["f_bottom10%"] = fr <= 0.1
# combined score: momentum percentile minus flow percentile
score = mr - fr
masks["MOM_minus_F_top10%"] = score.rank(axis=1, pct=True, ascending=False) <= 0.10
masks["MOM_minus_F_top20%"] = score.rank(axis=1, pct=True, ascending=False) <= 0.20

for k, m in masks.items():
    s, net, tn = book_from_mask(m, fwd1)
    books[k] = dict(series=s, net=net, turnover=tn,
                    ann_gross=float((1 + s.mean()) ** 52 - 1),
                    ann_net=float((1 + net.mean()) ** 52 - 1),
                    cost_ann=float(COST * tn * 52), n_weeks=int(len(s)),
                    avg_names=float(m.sum(axis=1).replace(0, np.nan).mean()),
                    yearly=yearly(net))
R["books"] = {k: {kk: vv for kk, vv in v.items() if kk not in ("series", "net")} for k, v in books.items()}
R["paired_vs_MOM"] = {k: paired(books[k]["net"], books["MOM_top1/3"]["net"])
                      for k in ["MOM_top1/3_drop_fQ5", "MOM_top1/3_drop_fTop10%",
                                "MOM_minus_F_top10%", "MOM_minus_F_top20%", "fQ1_lowest_flow"]}
R["paired_vs_EW"] = {k: paired(books[k]["net"], books["EW_all"]["net"])
                     for k in ["MOM_top1/3", "MOM_top1/3_drop_fQ5", "MOM_minus_F_top10%"]}

# --- sensitivity: exclusion threshold for the MOM filter
R["sens_excl"] = {}
for q in (0.6, 0.7, 0.8, 0.9):
    m = (mr >= (2 / 3)) & (fr < q)
    s, net, tn = book_from_mask(m, fwd1)
    R["sens_excl"][str(q)] = dict(ann_net=float((1 + net.mean()) ** 52 - 1), turnover=tn,
                                  avg_names=float(m.sum(axis=1).replace(0, np.nan).mean()),
                                  diff_vs_mom=paired(net, books["MOM_top1/3"]["net"])["diff_ann"])

# --- liquidity-restricted universe: top 100 by trailing amount
A = d["A"]
amt_rank = A.rolling(12, min_periods=6).median().rank(axis=1, pct=True, ascending=False)
liquid = amt_rank <= 0.3
R["sens_liquid"] = {}
for name, m in {"MOM_top1/3_liquid": (mr >= (2 / 3)) & liquid,
                "MOM_drop_fQ5_liquid": (mr >= (2 / 3)) & (fr < 0.8) & liquid,
                "MOM_minus_F_liquid": (score.rank(axis=1, pct=True, ascending=False) <= 0.10) & liquid}.items():
    s, net, tn = book_from_mask(m, fwd1)
    R["sens_liquid"][name] = dict(ann_net=float((1 + net.mean()) ** 52 - 1), turnover=tn,
                                  avg_names=float(m.sum(axis=1).replace(0, np.nan).mean()),
                                  yearly=yearly(net))
R["sens_liquid_paired"] = dict(
    dropQ5_vs_MOM=paired(book_from_mask((mr >= (2 / 3)) & (fr < 0.8) & liquid, fwd1)[1],
                         book_from_mask((mr >= (2 / 3)) & liquid, fwd1)[1]))

# --- exclude 2015
ws = f.index[f.index >= pd.Timestamp("2016-01-01")]
R["ex2015"] = {}
for k in ["EW_all", "MOM_top1/3", "MOM_top1/3_drop_fQ5", "MOM_minus_F_top10%"]:
    s, net, tn = book_from_mask(masks[k].loc[ws], fwd1)
    R["ex2015"][k] = dict(ann_net=float((1 + net.mean()) ** 52 - 1), turnover=tn)

json.dump(R, open(os.path.join(OUT, "E2_extra2.json"), "w"), indent=1, ensure_ascii=False)
pd.set_option("display.width", 240)
tb = pd.DataFrame({k: {"ann_gross": v["ann_gross"], "turnover": v["turnover"], "cost": v["cost_ann"],
                       "ann_net": v["ann_net"], "names": v["avg_names"]} for k, v in books.items()}).T
print(tb.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nyearly net:")
print(pd.DataFrame({k: v["yearly"] for k, v in books.items()}).to_string(float_format=lambda x: f"{x:+.1%}"))
print("\npaired vs MOM_top1/3 (weekly net, annualised diff):")
print(pd.DataFrame(R["paired_vs_MOM"]).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\npaired vs EW:")
print(pd.DataFrame(R["paired_vs_EW"]).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nsensitivity to exclusion threshold:")
print(pd.DataFrame(R["sens_excl"]).T.to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nliquid universe:")
print(pd.DataFrame({k: {kk: vv for kk, vv in v.items() if kk != "yearly"} for k, v in R["sens_liquid"].items()}).T
      .to_string(float_format=lambda x: f"{x:+.4f}"))
print("\nex2015:", json.dumps(R["ex2015"], indent=1))
