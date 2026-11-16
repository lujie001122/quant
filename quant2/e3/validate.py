#!/usr/bin/env python3
"""E3 step 4: strict validation of the surviving books.

  * period splits (2015-19 / 2020-24 / 2025-26), each restarted flat
  * walk-forward: train 3 calendar years -> test the next year, rolling, config chosen on train
  * cost tiers 0.10% / 0.15% / 0.20% one-way
  * parameter neighbourhood around the two best structures
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


def smooth(x, k):
    return x.rolling(k, min_periods=1).mean()


def main():
    d = e3data.build()
    keep = d["keep"]; f = d["f"]; prem = d["prem_dev"].where(keep); mom = d["mom"]
    ret1 = d["fwd"][1].where(keep); valid = keep & ret1.notna()
    weeks = d["wk"]
    ew = engine.ew_book(valid, ret1, cost=COST)
    zm, zp, zf = zr(mom), zr(prem), zr(f)
    excl20 = (f.rank(axis=1, pct=True) > 0.8) | (prem.rank(axis=1, pct=True) > 0.8)

    # named candidate books
    cand = {
        "ew": (None, {}),
        "mom_N20": (zm, dict(N=20)),
        "flow_base_N20": (-zf, dict(N=20)),
        "flow_sm2_buf2_hold2": (smooth(-zf, 2), dict(N=20, buffer_n=40, minhold=2)),
        "flow_sm2_buf2_hold2_p50": (smooth(-zf, 2), dict(N=20, buffer_n=40, minhold=2, partial=0.5)),
        "flow_sm4_buf2": (smooth(-zf, 4), dict(N=20, buffer_n=40)),
        "flow_sm4_buf3_hold4": (smooth(-zf, 4), dict(N=20, buffer_n=60, minhold=4)),
        "joint_lin_buf2": (zm - 0.1 * zp - 0.3 * zf, dict(N=20, buffer_n=40)),
        "joint_lin_sm2_buf2": (smooth(zm - 0.1 * zp - 0.3 * zf, 2), dict(N=20, buffer_n=40)),
        "filter20_hold2": (zm.where(~excl20), dict(N=20, minhold=2)),
    }

    def book(score, kw, cost=COST, start=None):
        if score is None:
            return engine.ew_book(valid, ret1, cost=cost)
        return engine.simulate(score, ret1, valid, cost=cost, start=start, **kw)

    def ev(b, lo=None, hi=None, cost=COST):
        net = b["net"]
        if lo is not None:
            net = net[net.index >= pd.Timestamp(lo)]
        if hi is not None:
            net = net[net.index < pd.Timestamp(hi)]
        gross = b["gross"].reindex(net.index)
        turn = b["turn"].reindex(net.index)
        s = S.perf(net)
        s["gross_ann"] = float(gross.mean() * 52)
        s["turnover"] = float(turn.mean())
        s["cost_ann"] = float(turn.mean() * cost)
        return s

    res = {"periods": {}, "cost_tiers": {}, "neighbourhood": {}, "wf": {}}

    # ---------------- 1. period splits, each book restarted flat at the period start
    periods = [("2015-19", "2015-01-01", "2020-01-01"),
               ("2020-24", "2020-01-01", "2025-01-01"),
               ("2025-26", "2025-01-01", "2026-12-31")]
    for name, (score, kw) in cand.items():
        res["periods"][name] = {}
        for tag, lo, hi in periods:
            b = book(score, kw, start=None if score is None else pd.Timestamp(lo))
            s = ev(b, lo, hi)
            ew_s = ev(ew, lo, hi)
            s["excess_ann"] = s["ann"] - ew_s["ann"]
            s["ew_ann"] = ew_s["ann"]
            res["periods"][name][tag] = s
    # print
    print("=" * 100)
    print("PERIODS (net arith / turnover / excess vs EW)")
    for name in cand:
        row = res["periods"][name]
        print(f"{name:22s} " + "  ".join(
            f"{tag}: {row[tag]['ann']:+.3f} (t{row[tag]['turnover']:.2f}, ex{row[tag]['excess_ann']:+.3f})"
            for tag, _, _ in periods))

    # ---------------- 2. cost tiers
    for name, (score, kw) in cand.items():
        res["cost_tiers"][name] = {}
        for c in (0.0010, 0.0015, 0.0020):
            b = book(score, kw, cost=c)
            s = ev(b, cost=c)
            res["cost_tiers"][name][str(c)] = s
    print("-" * 100)
    print("COST TIERS (net arith)")
    for name in cand:
        print(f"{name:22s} " + "  ".join(f"{k}: {v['ann']:+.3f}" for k, v in res["cost_tiers"][name].items()))

    # ---------------- 3. walk-forward grid
    grid = {}
    for l1, l2 in itertools.product((0.0, 0.1, 0.2, 0.3), repeat=2):
        if l1 == 0 and l2 == 0:
            continue
        for k in (1, 2, 4):
            for buf in (None, 40):
                for hold in (0, 2):
                    grid[f"lin_{l1}_{l2}_sm{k}_buf{buf}_h{hold}"] = (
                        smooth(zm - l1 * zp - l2 * zf, k), dict(N=20, buffer_n=buf, minhold=hold))
    for k in (1, 2, 4):
        for buf in (None, 40):
            for hold in (0, 2):
                grid[f"flow_sm{k}_buf{buf}_h{hold}"] = (smooth(-zf, k), dict(N=20, buffer_n=buf, minhold=hold))
                grid[f"filter_sm{k}_buf{buf}_h{hold}"] = (smooth(zm.where(~excl20), k),
                                                          dict(N=20, buffer_n=buf, minhold=hold))
    books = {}
    for k, (score, kw) in grid.items():
        books[k] = engine.simulate(score, ret1, valid, cost=COST, **kw)

    folds = []
    for y in range(2018, 2027):
        tr_lo, tr_hi = pd.Timestamp(f"{y-3}-01-01"), pd.Timestamp(f"{y}-01-01")
        te_lo, te_hi = tr_hi, pd.Timestamp(f"{y+1}-01-01")
        best, best_net = None, -9
        for k, b in books.items():
            n = b["net"][(b.index >= tr_lo) & (b.index < tr_hi)]
            if len(n) < 100:
                continue
            score_ = n.mean() * 52
            if score_ > best_net:
                best_net, best = score_, k
        n = books[best]["net"][(books[best].index >= te_lo) & (books[best].index < te_hi)]
        ew_n = ew["net"][(ew.index >= te_lo) & (ew.index < te_hi)]
        folds.append(dict(year=y, chosen=best, train_net=float(best_net),
                          test_net=float(n.mean() * 52), test_turn=float(books[best]["turn"][n.index].mean()),
                          ew_net=float(ew_n.mean() * 52), n_weeks=int(len(n))))
    oos = pd.concat([books[f["chosen"]]["net"][(books[f["chosen"]].index >= pd.Timestamp(f"{f['year']}-01-01"))
                                                & (books[f["chosen"]].index < pd.Timestamp(f"{f['year']+1}-01-01"))]
                     for f in folds])
    oos_ew = ew["net"].reindex(oos.index)
    dd = (oos - oos_ew)
    res["wf"] = {"folds": folds, "oos_net_ann": float(oos.mean() * 52),
                 "oos_ew_ann": float(oos_ew.mean() * 52),
                 "oos_excess_ann": float(dd.mean() * 52),
                 "oos_excess_t": float(dd.mean() / S.nw_se(dd, 4)),
                 "oos_sharpe": float(oos.mean() / oos.std() * np.sqrt(52)),
                 "oos_mdd": float(((1 + oos).cumprod() / (1 + oos).cumprod().cummax() - 1).min()),
                 "n_weeks": int(len(oos)),
                 "chosen_counts": pd.Series([f["chosen"] for f in folds]).value_counts().to_dict()}
    print("-" * 100)
    print("WALK-FORWARD")
    for f in folds:
        print(f"  test {f['year']}  {f['chosen']:32s} train {f['train_net']:+.3f} "
              f"test {f['test_net']:+.3f} ew {f['ew_net']:+.3f} turn {f['test_turn']:.2f}")
    print(f"  OOS net {res['wf']['oos_net_ann']:+.3f} vs EW {res['wf']['oos_ew_ann']:+.3f} "
          f"(ex {res['wf']['oos_excess_ann']:+.3f}, t {res['wf']['oos_excess_t']:+.2f})")

    # ---------------- 4. neighbourhood
    neigh = {}
    for l1 in (0.0, 0.05, 0.1, 0.2, 0.3):
        for l2 in (0.1, 0.2, 0.3, 0.4):
            for buf in (None, 30, 40, 60):
                s = smooth(zm - l1 * zp - l2 * zf, 2)
                b = engine.simulate(s, ret1, valid, N=20, buffer_n=buf, cost=COST)
                o = ev(b)
                neigh[f"l1={l1}_l2={l2}_buf{buf}"] = {"net": o["ann"], "turn": o["turnover"],
                                                      "gross": o["gross_ann"], "sharpe": o["sharpe"]}
    res["neighbourhood"] = neigh
    for nm, sub in (("N", (10, 15, 20, 25, 30, 40)), ("smooth", (1, 2, 3, 4, 6)),
                    ("buffer", (None, 20, 30, 40, 50, 60)), ("minhold", (0, 1, 2, 4, 8))):
        res.setdefault("one_dim", {})[nm] = {}
        for v in sub:
            kw = dict(N=20, buffer_n=40, minhold=2)
            sc = smooth(-zf, 2)
            if nm == "N":
                kw["N"] = v
            elif nm == "smooth":
                sc = smooth(-zf, v)
            elif nm == "buffer":
                kw["buffer_n"] = v
            else:
                kw["minhold"] = v
            b = engine.simulate(sc, ret1, valid, cost=COST, **kw)
            o = ev(b)
            res["one_dim"][nm][str(v)] = {"net": o["ann"], "turn": o["turnover"], "sharpe": o["sharpe"]}

    json.dump(res, open(os.path.join(OUT, "E3_validate.json"), "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
