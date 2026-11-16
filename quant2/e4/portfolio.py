#!/usr/bin/env python3
"""E4 task 3: the deliverable book + capacity + the arithmetic behind the 25% question.

Long-only, no leverage, A-share ETFs, 200k CNY.  Every candidate runs on the same PIT panel
and the same cost/turnover accounting as E1/E2/E3, on BOTH the truncated (<=2024) sample and
the full sample, so the client number is not a 2025-26 artifact.
"""
import json, os, sys
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import booklib as B          # noqa: E402
import engine2               # noqa: E402
import e3stats as S          # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e4")
CAPITAL = 200_000.0


def cap_mask(c):
    """E1 risk rule: no NEW position while the ETF trades above a 10% premium."""
    return c["d"]["prem"].where(c["keep"]) <= 0.10


def candidates(c):
    keep = c["keep"]
    zf_s = B.smooth(-B.zr(c["f"].where(keep)), 2)
    cap = cap_mask(c)
    trend = c["d"]["T20"]
    return {
        "EW_all": (None, {}),
        "EW_cap10": (None, {}),                                    # benchmark w/ premium filter
        "flow_N20_lowturn": (zf_s, dict(N=20, buffer_n=40, minhold=2)),
        "flow_N30_lowturn": (zf_s, dict(N=30, buffer_n=60, minhold=2)),
        "flow_N30_lowturn_p50": (zf_s, dict(N=30, buffer_n=60, minhold=2, partial=0.5)),
        "flow_N30_cap10_p50": (zf_s.where(cap), dict(N=30, buffer_n=60, minhold=2, partial=0.5)),
        "flow_N30_cap10": (zf_s.where(cap), dict(N=30, buffer_n=60, minhold=2)),
        "flow_N30_cap10_trend": (zf_s.where(cap), dict(N=30, buffer_n=60, minhold=2, trend=trend)),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"candidates": {}, "capacity": {}, "ceiling": {}}
    for tag, cutoff in (("cut2024", "2024-12-31"), ("full", None)):
        c = B.ctx(cutoff)
        keep = c["keep"]
        ew = c["ew"]
        print("=" * 116)
        print(f"[{tag}] candidate books ({c['weeks'][0].date()} ~ {c['weeks'][-1].date()})")
        print(f"{'book':24s} {'net':>7s} {'cagr':>7s} {'vol':>6s} {'sharpe':>6s} {'mdd':>7s} "
              f"{'turn':>6s} {'excess':>7s} {'t':>6s} {'names':>6s}")
        for name, (sc, kw) in candidates(c).items():
            if sc is None:
                b = ew
            else:
                b = engine2.simulate(sc, c["ret1"], c["valid"], cost=B.COST, **kw)
            m = B.ev(b, ew)
            res.setdefault(tag, {})[name] = m
            print(f"{name:24s} {m['ann']:+7.3f} {m['cagr']:+7.3f} {m['vol']:6.3f} "
                  f"{m['sharpe']:6.2f} {m['mdd']:+7.3f} {m['turnover']:6.3f} "
                  f"{m['excess_ann']:+7.3f} {m['excess_t']:+6.2f} {m['avg_names']:6.1f}")

        # ---------------- capacity: participation in the held names' own traded amount
        A = c["d"]["A"]                                    # weekly traded amount (CNY)
        sc = B.smooth(-B.zr(c["f"].where(keep)), 2).where(cap_mask(c))
        b = engine2.simulate(sc, c["ret1"], c["valid"], N=30, buffer_n=60, minhold=2,
                             partial=0.5, cost=B.COST)
        med = []
        for t in sc.index:
            ok = c["valid"].loc[t].fillna(False) & sc.loc[t].notna()
            if ok.sum() == 0 or t not in A.index:
                continue
            rk = sc.loc[t].where(ok).rank(ascending=False, method="first")
            sel = list(rk[rk <= 30].index)
            a = A.loc[t].reindex(sel).dropna()
            if len(a):
                med.append(float(a.median()))
        weekly_amt = float(np.median(med))
        tau = float(b["turn"].mean())
        part = 0.10
        aum_cap = part * weekly_amt * 30 / max(tau, 1e-9)
        res["capacity"][tag] = dict(median_held_weekly_amount=weekly_amt, weekly_turnover=tau,
                                    per_name_weekly_notional_at_200k=tau * CAPITAL / 30,
                                    participation_at_200k=tau * CAPITAL / 30 / weekly_amt,
                                    aum_cap_cny=aum_cap)
        print(f"   capacity: held names trade {weekly_amt/1e4:,.0f} 万/周 (median); "
              f"weekly turnover {tau:.1%}; at 20万 the per-name weekly ticket is "
              f"{tau*CAPITAL/30:,.0f} 元 = {tau*CAPITAL/30/weekly_amt:.5%} of that name's week; "
              f"AUM cap @10% participation ≈ {aum_cap/1e8:,.1f} 亿")

        # ---------------- why 25% is out of reach: arithmetic from the MEASURED IC
        fwd2 = c["fwd"][2]
        ok = fwd2.notna() & keep
        xs_sd = float(fwd2.where(ok).stack().groupby(level=0).std().median())  # 2w cross-sec SD
        ic2 = S.ic_stats(S.rank_ic(c["f"], fwd2, min_n=10), 2)["ic"]
        n_avg = float(ok.sum(axis=1).median())
        q = 30.0 / n_avg
        M = 1_000_000
        z = np.sort(np.random.default_rng(0).normal(size=M))[::-1]
        zbar = float(z[:int(q * M)].mean())
        g2w = ic2 * xs_sd * zbar
        cost_ann = float(b["cost"].mean() * 52)
        res["ceiling"][tag] = dict(
            ic_h2=ic2, xs_sd_2w=xs_sd, n_names_median=n_avg, z_top30=zbar,
            implied_gross_ann=g2w * 26, implied_net_ann=g2w * 26 - cost_ann,
            ic_needed_20pp=0.20 / (xs_sd * zbar * 26),
            realized_sharpe=float(b["net"].mean() / b["net"].std() * np.sqrt(52)),
            sharpe_needed_25pct=0.25 / float(b["net"].std() * np.sqrt(52)),
            vol_ann=float(b["net"].std() * np.sqrt(52)))
        cl = res["ceiling"][tag]
        print(f"   ceiling: IC(h2) {ic2:+.4f} x xs_sd(2w) {xs_sd:.3f} x z(top30|{n_avg:.0f}) "
              f"{zbar:.2f} = {g2w:+.4f}/2w -> {cl['implied_gross_ann']:+.1%}/yr gross, "
              f"{cl['implied_net_ann']:+.1%}/yr net vs EW; IC needed for +20pp "
              f"{cl['ic_needed_20pp']:.3f}; realised Sharpe {cl['realized_sharpe']:.2f} vs "
              f"25% needing {cl['sharpe_needed_25pct']:.2f}")

    # ---------------- the recommended book, year by year, truncated sample
    c = B.ctx("2024-12-31")
    sc = B.smooth(-B.zr(c["f"].where(c["keep"])), 2).where(cap_mask(c))
    b = engine2.simulate(sc, c["ret1"], c["valid"], N=30, buffer_n=60, minhold=2,
                         partial=0.5, cost=B.COST)
    res["recommended_by_year"] = B.by_year(b, c["ew"])
    res["recommended"] = B.ev(b, c["ew"])
    res["recommended_full"] = B.ev(
        engine2.simulate(B.smooth(-B.zr(B.ctx(None)["f"].where(B.ctx(None)["keep"])), 2)
                         .where(cap_mask(B.ctx(None))), B.ctx(None)["ret1"],
                         B.ctx(None)["valid"], N=30, buffer_n=60, minhold=2,
                         partial=0.5, cost=B.COST), B.ctx(None)["ew"])
    print("=" * 116)
    print("RECOMMENDED = flow N30, smooth2, buffer2N, hold2, 分批50%, premium cap 10%")
    for y, v in res["recommended_by_year"].items():
        print(f"   {y}: net {v['net']:+.2%}  EW {v['ew']:+.2%}  excess {v['net']-v['ew']:+.2%}  "
              f"turn {v['turn']:.1%}")
    for k in ("recommended", "recommended_full"):
        m = res[k]
        print(f"   [{k}] net {m['ann']:+.3f} cagr {m['cagr']:+.3f} vol {m['vol']:.3f} "
              f"mdd {m['mdd']:+.3f} sharpe {m['sharpe']:.2f} turn {m['turnover']:.3f} "
              f"ex {m['excess_ann']:+.3f} t {m['excess_t']:+.2f}")

    json.dump(res, open(os.path.join(OUT, "E4_portfolio.json"), "w"), indent=1,
              ensure_ascii=False, default=float)
    print("\nsaved", os.path.join(OUT, "E4_portfolio.json"))


if __name__ == "__main__":
    main()
