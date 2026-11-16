#!/usr/bin/env python3
"""E4 final: the one book we hand over, its year-by-year record, and the 25% arithmetic."""
import json, os, sys
import numpy as np, pandas as pd
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE); sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B, engine2, e3stats as S

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")
CFG = dict(N=20, buffer_n=40, minhold=2, partial=0.5)

def score(c):
    prem = c["d"]["prem"].where(c["keep"])
    return B.smooth(-B.zr(c["f"].where(c["keep"])), 2).where(prem <= 0.10)

res = {}
for tag, cutoff in (("cut2024", "2024-12-31"), ("full", None)):
    c = B.ctx(cutoff)
    b = engine2.simulate(score(c), c["ret1"], c["valid"], cost=B.COST, **CFG)
    res[tag] = {"book": B.ev(b, c["ew"]), "by_year": B.by_year(b, c["ew"]),
                "new_high_prem_weeks": float((c["d"]["prem"].where(c["keep"]) > 0.10).sum(axis=1).gt(0).mean())}
    print(f"[{tag}]")
    for y, v in res[tag]["by_year"].items():
        print(f"   {y} net {v['net']:+.2%} EW {v['ew']:+.2%} ex {v['net']-v['ew']:+.2%} turn {v['turn']:.1%}")
    m = res[tag]["book"]
    print(f"   net {m['ann']:+.3f} cagr {m['cagr']:+.3f} vol {m['vol']:.3f} sharpe {m['sharpe']:.2f} "
          f"mdd {m['mdd']:+.3f} turn {m['turnover']:.3f} cost {m['cost_ann']:+.3f} ex {m['excess_ann']:+.3f} t {m['excess_t']:+.2f}")
    yrs = m["n_weeks"] / 52
    if tag == "cut2024":
        ic_h2 = 0.0275                     # measured first-order flow IC(h=2), data <= 2024
        sl = (m["excess_ann"] * 100) / (ic_h2 * 100)     # excess pp per 0.01 of IC
        res["math"] = dict(years=round(yrs, 2),
                           ew_ann=S.perf(c["ew"]["net"])["ann"],
                           ew_cagr=S.perf(c["ew"]["net"])["cagr"],
                           ew_mdd=S.perf(c["ew"]["net"])["mdd"], ic_h2=ic_h2,
                           pp_per_ic_unit=sl / 1000,
                           ic_needed_20pp=20.0 / sl * 0.01,
                           t_of_mean=float(m["ann"] / (m["vol"] / np.sqrt(yrs))),
                           t_needed_for_25pct=float(0.25 / (m["vol"] / np.sqrt(yrs))),
                           leverage_for_25=0.25 / m["ann"],
                           mdd_at_leverage=m["mdd"] * 0.25 / m["ann"])
res["config"] = CFG
json.dump(res, open(os.path.join(OUT, "E4_final.json"), "w"), indent=1, ensure_ascii=False, default=float)
print("\nMATH:", {k: round(v, 4) if isinstance(v, float) else v for k, v in res["math"].items()})
