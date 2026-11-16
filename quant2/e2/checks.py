#!/usr/bin/env python3
"""E2 checks: signal decay by horizon, extreme-decile long/short t-stat, sample-cut robustness,
second-source cross-check of the 份额 panel against jisilu's daily 份额 snapshot."""
import json, os, sys
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data
from final import spearman, nw_se, boot_ci, ic_stats, rank_ic, xs_demean

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
R = {}

d = e2data.build(lag=1, since="2015-01-01")
f, P, mom = d["f"], d["P"], d["mom"]
wk = f.index

# ---- 1. decay: IC at 1..12 weeks
fwd = {}
for h in (1, 2, 3, 4, 6, 8, 12):
    r = P.shift(-h) / P - 1.0
    fwd[h] = xs_demean(r)
R["decay"] = {str(h): ic_stats(rank_ic(f, fwd[h]), h) for h in fwd}

# ---- 2. decile long/short (Q1 most redeemed - Q10 most created), weekly, gross
rows = []
for t in f.index:
    a = f.loc[t].dropna()
    if len(a) < 40 or t not in fwd[1].index:
        continue
    dec = pd.qcut(a.rank(method="first"), 10, labels=False)
    r = fwd[1].loc[t]
    lo = r.reindex(dec.index[dec == 0]).dropna()
    hi = r.reindex(dec.index[dec == 9]).dropna()
    if len(lo) and len(hi):
        rows.append(lo.mean() - hi.mean())
ls = pd.Series(rows)
se = nw_se(ls, lag=1)
lo, hi = boot_ci(ls)
R["decile_ls_h1"] = dict(mean_weekly=float(ls.mean()), ann=float((1 + ls.mean()) ** 52 - 1),
                         t=float(ls.mean() / se), ci_lo=lo * 52, ci_hi=hi * 52,
                         n=int(len(ls)), win=float((ls > 0).mean()))
# same for quintile Q1-Q5
rows5 = []
for t in f.index:
    a = f.loc[t].dropna()
    if len(a) < 40 or t not in fwd[1].index:
        continue
    q = pd.qcut(a.rank(method="first"), 5, labels=False)
    r = fwd[1].loc[t]
    lo5 = r.reindex(q.index[q == 0]).dropna()
    hi5 = r.reindex(q.index[q == 4]).dropna()
    if len(lo5) and len(hi5):
        rows5.append(lo5.mean() - hi5.mean())
s5 = pd.Series(rows5)
R["quintile_ls_h1"] = dict(mean_weekly=float(s5.mean()), ann=float((1 + s5.mean()) ** 52 - 1),
                           t=float(s5.mean() / nw_se(s5, lag=1)), n=int(len(s5)),
                           win=float((s5 > 0).mean()))

# ---- 3. sample cuts
cuts = {"ex2015": "2016-01-01", "ex2025on": "2015-01-01|2025-01-01-",
        "2015_2019": "2015-01-01|2020-01-01-", "2020_2026": "2020-01-01"}
m2000 = f.index[f.index >= pd.Timestamp("2016-01-01")]
R["cuts"] = {}
R["cuts"]["ex2015"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=m2000), h) for h in (1, 2, 4)}
m_no2526 = f.index[f.index < pd.Timestamp("2025-01-01")]
R["cuts"]["ex2025_26"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=m_no2526), h) for h in (1, 2, 4)}
m_no2026 = f.index[f.index < pd.Timestamp("2026-01-01")]
R["cuts"]["ex2026"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=m_no2026), h) for h in (1, 2, 4)}
m_pre = f.index[f.index < pd.Timestamp("2020-01-01")]
R["cuts"]["2015_2019"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=m_pre), h) for h in (1, 2, 4)}
m_post = f.index[f.index >= pd.Timestamp("2020-01-01")]
R["cuts"]["2020_2026"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=m_post), h) for h in (1, 2, 4)}

# ---- 4. second source: jisilu daily 份额 vs our panel (same day, all pages)
try:
    H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.jisilu.cn/data/etf/"}
    jsl = {}
    r = requests.get("https://www.jisilu.cn/data/etf/etf_list/?___jsl=LST___t=1", headers=H, timeout=20)
    for row in (r.json().get("rows") or []):
        if True:
            c = row["cell"]
            try:
                jsl[c["fund_id"]] = float(c["amount"]) * 1e4  # 万份 -> 份
            except Exception:
                pass
    S = d["S"]
    last = S.index[-1]
    ours = S.loc[last].dropna()
    pairs = [(c, float(ours[c]), jsl[c]) for c in ours.index if c in jsl]
    if pairs:
        df = pd.DataFrame(pairs, columns=["code", "ours", "jsl"])
        df["ratio"] = df["ours"] / df["jsl"]
        R["jsl_check"] = dict(n=len(df), median_ratio=float(df.ratio.median()),
                              p10=float(df.ratio.quantile(0.1)), p90=float(df.ratio.quantile(0.9)),
                              within_1pct=float((df.ratio.sub(1).abs() < 0.01).mean()),
                              within_5pct=float((df.ratio.sub(1).abs() < 0.05).mean()),
                              date=str(last.date()))
        df.to_csv(os.path.join(OUT, "jsl_check.csv"), index=False)
    else:
        R["jsl_check"] = {"error": "no overlap", "n_jsl": len(jsl)}
except Exception as e:
    R["jsl_check"] = {"error": str(e)}

json.dump(R, open(os.path.join(OUT, "E2_checks.json"), "w"), indent=1, ensure_ascii=False)
print("decay:")
for h, v in R["decay"].items():
    print(f"  h={h:>2}w IC={v['ic']:+.4f} t={v['t']:+.2f} n={v['n']} CI[{v['ci_lo']:+.3f},{v['ci_hi']:+.3f}]")
print("\ndecile_LS h1:", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in R["decile_ls_h1"].items()})
print("quint_LS h1:", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in R["quintile_ls_h1"].items()})
print("\ncuts h1/h2:")
for k, v in R["cuts"].items():
    print(f"  {k:>10}: " + "  ".join(f"h{h}:{v[h]['ic']:+.4f}(t{v[h]['t']:+.1f})" for h in ("1", "2", "4")))
print("\njsl:", R["jsl_check"])
