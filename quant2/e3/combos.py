#!/usr/bin/env python3
"""E3 step 2: three ways of combining premium with fund flow, plus single-signal references.

(a) linear      score = zrank(mom) - l1*zrank(prem_dev) - l2*zrank(flow)
(b) filter      drop the top 20% flow names and the top 20% premium names, then rank momentum
(c) intersect   only names in the bottom 20% of BOTH premium and flow (small sample)
Benchmark: equal-weight all-universe book on the same weeks, same cost accounting.
"""
import json, os, sys, itertools
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e3data, engine, e3stats as S

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e3")
COST = 0.0015


def zr(x):
    return x.rank(axis=1, pct=True) - 0.5


def top_pct(x, q=0.8):
    """True where the value is in the top (1-q) fraction of that week's cross-section."""
    r = x.rank(axis=1, pct=True)
    return r > q


def main():
    os.makedirs(OUT, exist_ok=True)
    d = e3data.build()
    keep = d["keep"]
    f = d["f"]
    prem = d["prem_dev"].where(keep)
    prem_lvl = d["prem"].where(keep)
    mom = d["mom"]
    ret1 = d["fwd"][1].where(keep)
    valid = keep & ret1.notna()
    weeks = d["wk"]
    ew = engine.ew_book(valid, ret1, cost=COST)
    ew_s = engine.summarize(ew, None, COST, "EW")
    print("EW", round(ew_s["gross_ann"], 4), round(ew_s["ann"], 4), round(ew_s["cagr"], 4))

    zm, zp, zf = zr(mom), zr(prem), zr(f)
    zp_lvl = zr(prem_lvl)

    res = {"meta": {"weeks": len(weeks), "avg_names": float(valid.sum(axis=1).mean()),
                    "cost": COST,
                    "ew": ew_s}, "runs": {}}

    def run(label, score, N=20, **kw):
        b = engine.simulate(score, ret1, valid, N=N, cost=COST, **kw)
        s = engine.summarize(b, ew, COST, label)
        res["runs"][label] = s
        print(f"{label:38s} N{N:<2d} gross {s['gross_ann']:+.3f} net {s['ann']:+.3f} "
              f"cagr {s['cagr']:+.3f} turn {s['turnover']:.3f} cost {s['cost_ann']:.3f} "
              f"ex {s['excess_ann']:+.3f} t {s['excess_t']:+.2f} sh {s['sharpe']:.2f} "
              f"mdd {s['mdd']:.2f} n {s['avg_names']:.1f}")
        return s

    # ---------------- references
    for N in (10, 20, 30, 50):
        run(f"ref_mom_N{N}", zm, N)
        run(f"ref_flow_N{N}", -zf, N)
        run(f"ref_prem_N{N}", -zp, N)
        run(f"ref_prem_lvl_N{N}", -zp_lvl, N)
    run("ref_flow_only_filter_N20", (-zf).where(~top_pct(f, 0.8)), 20)

    # ---------------- (a) linear
    for l1, l2 in itertools.product((0.0, 0.1, 0.2, 0.3), (0.0, 0.1, 0.2, 0.3)):
        if l1 == 0 and l2 == 0:
            continue
        run(f"a_lin_l1={l1}_l2={l2}_N20", zm - l1 * zp - l2 * zf, 20)
    for w in (0.2, 0.3, 0.5, 0.7):
        for N in (20, 30):
            run(f"a2_nomom_w={w}_N{N}", -w * zp - (1 - w) * zf, N)
    for l1, l2 in [(0.1, 0.2), (0.2, 0.2), (0.2, 0.3), (0.3, 0.3)]:
        for N in (10, 30):
            run(f"a_lin_l1={l1}_l2={l2}_N{N}", zm - l1 * zp - l2 * zf, N)

    # ---------------- (b) filter
    f = f.reindex(columns=mom.columns); prem = prem.reindex(columns=mom.columns)
    excl_flow = top_pct(f, 0.8)
    excl_prem = top_pct(prem, 0.8)
    run("b_filter_flow20_N20", zm.where(~excl_flow), 20)
    run("b_filter_flow20_prem20_N20", zm.where(~excl_flow & ~excl_prem), 20)
    run("b_filter_flow10_prem10_N20", zm.where(~top_pct(f, 0.9) & ~top_pct(prem, 0.9)), 20)
    run("b_filter_flow30_prem30_N20", zm.where(~top_pct(f, 0.7) & ~top_pct(prem, 0.7)), 20)
    for N in (10, 30):
        run(f"b_filter_flow20_prem20_N{N}", zm.where(~excl_flow & ~excl_prem), N)

    # ---------------- (c) intersection
    low_flow = f.rank(axis=1, pct=True) <= 0.2
    low_prem = prem.rank(axis=1, pct=True) <= 0.2
    inter = low_flow & low_prem
    for N in (5, 10, 20):
        run(f"c_inter_q20_N{N}", zm.where(inter), N)
    run("c_inter_q20_mom0_N10", (0.0 * zm).where(inter), 10)     # equal weight the intersection
    # looser intersection
    inter2 = (f.rank(axis=1, pct=True) <= 0.3) & (prem.rank(axis=1, pct=True) <= 0.3)
    run("c_inter_q30_N20", zm.where(inter2), 20)
    # intersection coverage
    res["coverage"] = {"q20_avg": float(inter.sum(axis=1).mean()),
                       "q20_median": float(inter.sum(axis=1).median()),
                       "q20_p10": float(inter.sum(axis=1).quantile(0.1)),
                       "q30_avg": float(inter2.sum(axis=1).mean())}

    json.dump(res, open(os.path.join(OUT, "E3_combos.json"), "w"), indent=1, ensure_ascii=False)
    print("coverage", res["coverage"])


if __name__ == "__main__":
    main()
