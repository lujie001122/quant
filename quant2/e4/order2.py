#!/usr/bin/env python3
"""E4 task 2: second-order share-flow signals vs the first-order one.

Variants (all PIT, built only from `f` up to week t):
  f      shares_t/shares_{t-1} - 1              (E2/E3 first-order baseline)
  run    signed length of the current run of weekly f>0 (or f<0)
  acc    f_t - f_{t-1}                          (acceleration)
  pct52  percentile rank of f_t in its own trailing 52 weeks

Criterion for "adds something": |IC(h=2)| >= 0.03 AND |t| >= 2.5 AND mean cross-sectional
|rho| vs f < 0.6.  Reported on the truncated (<=2024) panel and on the full panel, because
a variant that only works with 2025-26 in the sample is not a signal, it's the regime.
"""
import json, os, sys
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B          # noqa: E402
import engine2               # noqa: E402
import e4data                # noqa: E402
import e3stats as S          # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")
MINN = 10


def cs_corr(a, b, minn=10):
    """Weekly cross-sectional Spearman between two signals (same week), averaged."""
    rhos = []
    for t in a.index:
        if t not in b.index:
            continue
        x, y = a.loc[t], b.loc[t]
        m = x.notna() & y.notna()
        if m.sum() < minn:
            continue
        r = S.spearman(x[m], y[m])
        if not np.isnan(r):
            rhos.append(r)
    r = pd.Series(rhos).dropna()
    return dict(mean=float(r.mean()), mean_abs=float(r.abs().mean()),
                p90_abs=float(r.abs().quantile(0.9)) if len(r) else np.nan, n=int(len(r)))


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    for tag, cutoff in (("cut2024", "2024-12-31"), ("full", None)):
        c = B.ctx(cutoff)
        keep, f = c["keep"], c["f"]
        sig = {"f": f,
               "run": e4data.signed_run(f),
               "acc": e4data.accel(f),
               "pct52": e4data.pct52(f)}
        block = {"weeks": [str(c["weeks"][0].date()), str(c["weeks"][-1].date())],
                 "n_weeks": int(len(c["weeks"])), "ic": {}, "corr_vs_f": {}, "resid_ic": {},
                 "book": {}}
        print("=" * 104)
        print(f"[{tag}] {block['weeks'][0]} ~ {block['weeks'][1]}  ({block['n_weeks']} weeks)")

        print("  RAW RANK IC (tradable pool = names with a realised forward return)")
        for name, s in sig.items():
            block["ic"][name] = {}
            cells = []
            for h in (1, 2, 4):
                st = S.ic_stats(S.rank_ic(s, c["fwd"][h], min_n=MINN), h)
                block["ic"][name][f"h{h}"] = st
                cells.append(f"h{h} {st['ic']:+.4f}(t{st['t']:+.2f})")
            print(f"    {name:6s} " + "  ".join(cells) + f"   n={block['ic'][name]['h2']['n']}")

        print("  CROSS-SECTIONAL CORRELATION WITH f (must be |rho| < 0.6 to count as incremental)")
        for name, s in sig.items():
            if name == "f":
                continue
            cc = cs_corr(s, f)
            block["corr_vs_f"][name] = cc
            print(f"    {name:6s} mean {cc['mean']:+.3f}  mean|rho| {cc['mean_abs']:.3f}  "
                  f"p90|rho| {cc['p90_abs']:.3f}")

        print("  INCREMENTAL: rank IC after removing rank-linear dependence on f")
        for name, s in sig.items():
            if name == "f":
                continue
            block["resid_ic"][name] = {}
            cells = []
            for h in (1, 2, 4):
                st = S.ic_stats(S.residual_ic(s, c["fwd"][h], [f], min_n=MINN), h)
                block["resid_ic"][name][f"h{h}"] = st
                cells.append(f"h{h} {st['ic']:+.4f}(t{st['t']:+.2f})")
            print(f"    {name:6s} " + "  ".join(cells))

        print("  BOOK CHECK (sm2 + buffer2N + hold2, N=20, cost 0.15%/side)")
        for name, s in sig.items():
            sc = B.smooth(-B.zr(s.where(keep)), 2)
            b = engine2.simulate(sc, c["ret1"], c["valid"], N=20, buffer_n=40, minhold=2,
                                 cost=B.COST)
            m = B.ev(b, c["ew"])
            block["book"][name] = m
            print(f"    {name:6s} gross {m['gross_ann']:+.3f} turn {m['turnover']:.3f} "
                  f"net {m['ann']:+.3f} ex {m['excess_ann']:+.3f} t {m['excess_t']:+.2f} "
                  f"mdd {m['mdd']:+.3f}")
        res[tag] = block

    # ---------------- verdict
    print("=" * 104)
    print("VERDICT (criterion: |IC h2| >= 0.03, |t| >= 2.5, mean|rho| vs f < 0.6)")
    verdict = {}
    for tag in ("cut2024", "full"):
        blk = res[tag]
        for name in ("run", "acc", "pct52"):
            h2 = blk["ic"][name]["h2"]
            rho = blk["corr_vs_f"][name]["mean_abs"]
            rh2 = blk["resid_ic"][name]["h2"]
            ok = (abs(h2["ic"]) >= 0.03) and (abs(h2["t"]) >= 2.5) and (rho < 0.6)
            verdict[f"{tag}:{name}"] = dict(ic_h2=h2["ic"], t_h2=h2["t"], rho=rho,
                                            resid_ic_h2=rh2["ic"], resid_t=rh2["t"],
                                            passes=bool(ok))
            print(f"  [{tag:7s}] {name:6s} IC {h2['ic']:+.4f} (t{h2['t']:+.2f})  "
                  f"rho {rho:.3f}  resid {rh2['ic']:+.4f} (t{rh2['t']:+.2f})  -> "
                  f"{'PASS' if ok else 'FAIL'}")
    res["verdict"] = verdict

    json.dump(res, open(os.path.join(OUT, "E4_order2.json"), "w"), indent=1,
              ensure_ascii=False, default=float)
    print("\nsaved", os.path.join(OUT, "E4_order2.json"))


if __name__ == "__main__":
    main()
