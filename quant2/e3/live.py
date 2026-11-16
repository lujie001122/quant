#!/usr/bin/env python3
"""E3 step 5: put the best combos into the live shape (top3/top5 + trend filter, weekly).

The live book is concentrated (top3), enters only when price > MA20, and rotates weekly.
Everything here is the same PIT panel/cost accounting; nothing is levered or shorted.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e3data, engine, e3stats as S

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e3")
COST = 0.0015


def zr(x):
    return x.rank(axis=1, pct=True) - 0.5


def smooth(x, k):
    return x.rolling(k, min_periods=1).mean()


def main():
    d = e3data.build()
    keep = d["keep"]; f = d["f"]; prem = d["prem_dev"].where(keep); prem_lvl = d["prem"].where(keep)
    mom = d["mom"]; ret1 = d["fwd"][1].where(keep); valid = keep & ret1.notna()
    trend = d["T20"]
    ew = engine.ew_book(valid, ret1, cost=COST)
    ew_s = engine.summarize(ew, None, COST)
    zm, zp, zf = zr(mom), zr(prem), zr(f)
    joint = zm - 0.1 * zp - 0.3 * zf
    joint_nm = -0.5 * zp - 0.5 * zf
    filter20 = zm.where(~((f.rank(axis=1, pct=True) > 0.8) | (prem.rank(axis=1, pct=True) > 0.8)))
    cap = prem_lvl <= 0.10                      # E1 risk rule: no new money above 10% premium

    res = {"ew": ew_s, "runs": {}}

    def run(label, score, N, trend_on=False, **kw):
        b = engine.simulate(score, ret1, valid, N=N, cost=COST,
                            trend=(trend if trend_on else None), **kw)
        s = engine.summarize(b, ew, COST, label)
        s["trend"] = trend_on
        res["runs"][label] = s
        print(f"{label:36s} gross {s['gross_ann']:+.3f} net {s['ann']:+.3f} cagr {s['cagr']:+.3f} "
              f"turn {s['turnover']:.2f} cost {s['cost_ann']:.3f} ex {s['excess_ann']:+.3f} "
              f"t {s['excess_t']:+.2f} sh {s['sharpe']:.2f} mdd {s['mdd']:.2f} n {s['avg_names']:.1f}")
        return s

    base = {"mom": zm, "flow": -zf, "joint": joint, "filter20": filter20}
    for nm, sc in base.items():
        for N in (3, 5):
            run(f"{nm}_top{N}", sc, N)
            run(f"{nm}_top{N}_trend", sc, N, trend_on=True)
    # best turnover-controlled variants in live shape
    for nm, sc in (("flow", smooth(-zf, 2)), ("joint", smooth(joint, 2))):
        for N in (3, 5):
            run(f"{nm}_sm2_buf_top{N}_trend", sc, N, trend_on=True, buffer_n=2 * N, minhold=2)
    # premium risk cap (no new position above 10% premium)
    for nm, sc in (("mom", zm), ("joint", joint), ("flow", -zf)):
        run(f"{nm}_cap10_top3_trend", sc.where(cap), 3, trend_on=True)
        run(f"{nm}_cap10_top5", sc.where(cap), 5)

    # yearly breakdown of the flagship live candidates
    years = {}
    for nm, sc, N in (("mom_top3_trend", zm, 3), ("flow_top3_trend", -zf, 3),
                      ("joint_top3_trend", joint, 3), ("mom_top5_trend", zm, 5),
                      ("flow_top5_trend", -zf, 5), ("joint_top5_trend", joint, 5),
                      ("joint_sm2_buf_top3_trend", smooth(joint, 2), 3)):
        kw = dict(N=N, cost=COST, trend=trend)
        if "buf" in nm:
            kw.update(buffer_n=2 * N, minhold=2)
        b = engine.simulate(sc, ret1, valid, **kw)
        g = b["net"].groupby(b.index.year).apply(lambda s: float((1 + s).prod() - 1))
        years[nm] = {int(y): round(v, 4) for y, v in g.items()}
    res["by_year"] = years

    # paired difference vs the momentum-only live proxy (same weeks)
    diffs = {}
    for nm in ("flow", "joint", "filter20"):
        for N in (3, 5):
            a = engine.simulate({"flow": -zf, "joint": joint, "filter20": filter20}[nm], ret1, valid,
                                N=N, cost=COST, trend=trend)["net"]
            m = engine.simulate(zm, ret1, valid, N=N, cost=COST, trend=trend)["net"]
            dd = (a - m).dropna()
            diffs[f"{nm}_top{N}_trend_minus_mom"] = {
                "ann": float(dd.mean() * 52), "t": float(dd.mean() / S.nw_se(dd, 4)),
                "hit": float((dd > 0).mean())}
    res["vs_mom"] = diffs

    json.dump(res, open(os.path.join(OUT, "E3_live.json"), "w"), indent=1, ensure_ascii=False)
    print("-" * 100)
    print("vs mom-only live proxy:", json.dumps({k: {kk: round(vv, 3) for kk, vv in v.items()}
                                                 for k, v in diffs.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
