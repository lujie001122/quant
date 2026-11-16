#!/usr/bin/env python3
"""E3 step 3 (the key one): can turnover be pushed below 20%/week without losing the gross edge?

Four structural mechanisms, each measured on the same books:
  smooth    score = mean of the weekly percentile ranks over the last k weeks
  buffer    a held name is kept while its rank stays inside buffer_n (2N default)
  partial   move only half way from last week's weights to this week's target
  minhold   a new position cannot be sold for k weeks
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


def smooth(score, k):
    return score.rolling(k, min_periods=1).mean()


def main():
    d = e3data.build()
    keep = d["keep"]
    f = d["f"]; prem = d["prem_dev"].where(keep); mom = d["mom"]
    ret1 = d["fwd"][1].where(keep)
    valid = keep & ret1.notna()
    ew = engine.ew_book(valid, ret1, cost=COST)
    zm, zp, zf = zr(mom), zr(prem), zr(f)
    excl20 = (f.rank(axis=1, pct=True) > 0.8) | (prem.rank(axis=1, pct=True) > 0.8)
    excl10 = (f.rank(axis=1, pct=True) > 0.9) | (prem.rank(axis=1, pct=True) > 0.9)
    bases = {
        "filter20": zm.where(~excl20),
        "filter10": zm.where(~excl10),
        "lin_l1=0.1_l2=0.3": zm - 0.1 * zp - 0.3 * zf,
        "flow_only": -zf,
        "flow_prem_nomom": -0.5 * zp - 0.5 * zf,
    }
    res = {"meta": {"ew": engine.summarize(ew, None, COST, "EW")}, "runs": {}}

    def run(label, score, N=20, **kw):
        b = engine.simulate(score, ret1, valid, N=N, cost=COST, **kw)
        s = engine.summarize(b, ew, COST, label)
        s["N"] = N
        s.update({k: v for k, v in kw.items()})
        res["runs"][label] = s
        print(f"{label:52s} gross {s['gross_ann']:+.3f} turn {s['turnover']:.3f} "
              f"cost {s['cost_ann']:.3f} net {s['ann']:+.3f} cagr {s['cagr']:+.3f} "
              f"ex {s['excess_ann']:+.3f} t {s['excess_t']:+.2f} sh {s['sharpe']:.2f} mdd {s['mdd']:.2f}")
        return s

    for bname, base in bases.items():
        for N in (20, 30):
            tag = f"{bname}_N{N}"
            run(f"{tag}_base", base, N)
            for k in (2, 4):
                run(f"{tag}_smooth{k}", smooth(base, k), N)
            for mult in (1.5, 2, 3):
                run(f"{tag}_buffer{mult}N", base, N, buffer_n=int(mult * N))
            run(f"{tag}_partial50", base, N, partial=0.5)
            for k in (2, 4):
                run(f"{tag}_minhold{k}", base, N, minhold=k)
            run(f"{tag}_smooth2_buffer2N", smooth(base, 2), N, buffer_n=2 * N)
            run(f"{tag}_smooth4_buffer2N", smooth(base, 4), N, buffer_n=2 * N)
            run(f"{tag}_smooth2_buffer2N_minhold2", smooth(base, 2), N, buffer_n=2 * N, minhold=2)
            run(f"{tag}_smooth2_buffer2N_partial50", smooth(base, 2), N, buffer_n=2 * N, partial=0.5)
            run(f"{tag}_smooth4_buffer3N_minhold4", smooth(base, 4), N, buffer_n=3 * N, minhold=4)
            run(f"{tag}_smooth2_buffer2N_minhold2_partial50", smooth(base, 2), N,
                buffer_n=2 * N, minhold=2, partial=0.5)

    json.dump(res, open(os.path.join(OUT, "E3_turnover.json"), "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
