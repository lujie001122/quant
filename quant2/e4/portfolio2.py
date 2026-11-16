#!/usr/bin/env python3
"""E4 task 3b: pin down the deliverable book (N, cap level) and the warm-up artefact."""
import json, os, sys
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B          # noqa: E402
import engine2               # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")
CAPITAL = 200_000.0


def main():
    res = {}
    for tag, cutoff in (("cut2024", "2024-12-31"), ("full", None)):
        c = B.ctx(cutoff)
        keep = c["keep"]
        prem = c["d"]["prem"].where(keep)
        zf_s = B.smooth(-B.zr(c["f"].where(keep)), 2)
        print("=" * 108)
        print(f"[{tag}]")
        # how often is the premium cap even binding inside the tradable pool?
        frac = (prem > 0.10).sum(axis=1) / prem.notna().sum(axis=1)
        res.setdefault("prem_share", {})[tag] = dict(
            mean=float(frac.mean()), p90=float(frac.quantile(0.9)),
            max=float(frac.max()))
        print(f"  names above 10% premium: mean {frac.mean():.2%} of the pool, "
              f"p90 week {frac.quantile(0.9):.2%}, max {frac.max():.1%}")

        print(f"  {'book':30s} {'net':>7s} {'cagr':>7s} {'mdd':>7s} {'turn':>6s} "
              f"{'excess':>7s} {'t':>6s} {'names':>6s}")
        row = {}
        for cap in (None, 0.10, 0.15, 0.20):
            base = zf_s if cap is None else zf_s.where(prem <= cap)
            for N, buf in ((20, 40), (30, 60)):
                for p50 in (1.0, 0.5):
                    lab = f"N{N}_cap{cap if cap else 'off'}_p{int(p50*100)}"
                    b = engine2.simulate(base, c["ret1"], c["valid"], N=N, buffer_n=buf,
                                         minhold=2, partial=p50, cost=B.COST)
                    m = B.ev(b, c["ew"])
                    row[lab] = m
                    print(f"  {lab:30s} {m['ann']:+7.3f} {m['cagr']:+7.3f} {m['mdd']:+7.3f} "
                          f"{m['turnover']:6.3f} {m['excess_ann']:+7.3f} {m['excess_t']:+6.2f} "
                          f"{m['avg_names']:6.1f}")
        res.setdefault("books", {})[tag] = row

        # warm-up: how many names are tradable per year
        ny = keep.sum(axis=1).groupby(keep.index.year).median()
        res.setdefault("names_by_year", {})[tag] = {int(y): float(v) for y, v in ny.items()}
        print("  tradable names (median per year): " +
              " ".join(f"{y}:{v:.0f}" for y, v in ny.items()))

    json.dump(res, open(os.path.join(OUT, "E4_portfolio2.json"), "w"), indent=1,
              ensure_ascii=False, default=float)
    print("\nsaved", os.path.join(OUT, "E4_portfolio2.json"))


if __name__ == "__main__":
    main()
