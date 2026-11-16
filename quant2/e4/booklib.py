#!/usr/bin/env python3
"""E4 shared pieces: score helpers, the candidate grid, and metric wrappers.

Conventions are copied verbatim from E3 (`engine`, `e3stats`) so numbers are comparable:
  * weekly long-only top-N, equal weight, name-level one-way turnover, cost 0.0015/side
  * excess is measured against the equal-weight universe book on the SAME weeks
  * t is Newey-West lag 4 on the weekly net-return difference
"""
import os, sys
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
sys.path.insert(0, _HERE)
import engine          # noqa: E402  (EW book only)
import engine2         # noqa: E402  (deterministic simulate)
import e3stats as S    # noqa: E402
import e4data          # noqa: E402

COST = 0.0015
FLOW = dict(N=20, buffer_n=40, minhold=2, partial=0.5)   # E3's best flow book


def zr(x):
    return x.rank(axis=1, pct=True) - 0.5


def smooth(x, k):
    return x.rolling(k, min_periods=1).mean()


def ctx(cutoff=None):
    d = e4data.build(cutoff)
    keep = d["keep"]
    f = d["f"]
    ret1 = d["fwd"][1].where(keep)
    valid = keep & ret1.notna()
    return dict(d=d, keep=keep, f=f, fwd={h: d["fwd"][h].where(keep) for h in (1, 2, 4)},
                ret1=ret1, valid=valid, ew=engine.ew_book(valid, ret1, cost=COST),
                weeks=d["wk"])


def ev(book, ew, lo=None, hi=None):
    """Metrics on a (optionally sub-sliced) book, excess vs EW on the same weeks."""
    net = book["net"]
    if lo is not None:
        net = net[net.index >= pd.Timestamp(lo)]
    if hi is not None:
        net = net[net.index < pd.Timestamp(hi)]
    s = S.perf(net)
    if s is None:
        return None
    turn = book["turn"].reindex(net.index)
    s["gross_ann"] = float(book["gross"].reindex(net.index).mean() * 52)
    s["turnover"] = float(turn.mean())
    s["cost_ann"] = float(turn.mean() * COST * 52)
    e = ew["net"].reindex(net.index)
    e2 = S.perf(e)
    d = (net - e).dropna()
    s["ew_ann"] = e2["ann"]
    s["ew_cagr"] = e2["cagr"]
    s["excess_ann"] = float(d.mean() * 52)
    se = S.nw_se(d, lag=4)
    s["excess_t"] = float(d.mean() / se) if se and se > 0 else np.nan
    s["excess_hit"] = float((d > 0).mean())
    s["avg_names"] = float(book["n"].mean()) if "n" in book else np.nan
    s["n_weeks"] = int(len(net))
    return s


def split_windows(book, ew, cut="2025-01-01"):
    """Same book, two evaluation windows -> honest 'with vs without 2025-26' decomposition.
    No truncation artifact: the book path is identical, only the scoring window changes."""
    cut = pd.Timestamp(cut)
    out = {}
    for tag, m in (("pre2025", book.index < cut), ("from2025", book.index >= cut)):
        net = book["net"][m]
        e = ew["net"].reindex(net.index)
        d = (net - e).dropna()
        se = S.nw_se(d, 4)
        s = S.perf(net)
        s["ew_ann"] = float(e.mean() * 52)
        s["excess_ann"] = float(d.mean() * 52)
        s["excess_t"] = float(d.mean() / se) if se and se > 0 else np.nan
        s["turnover"] = float(book["turn"][m].mean())
        s["gross_ann"] = float(book["gross"][m].mean() * 52)
        s["excess_total"] = float((1 + d).prod() - 1)
        s["net_total"] = float((1 + net).prod() - 1)
        s["ew_total"] = float((1 + e).prod() - 1)
        s["n_years"] = round(len(net) / 52.0, 2)
        out[tag] = s
    return out


def by_year(book, ew):
    out = {}
    y = book.index.year
    for yr in sorted(set(y)):
        m = y == yr
        net = book["net"][m]
        e = ew["net"].reindex(net.index)
        if len(net) < 10:
            continue
        out[int(yr)] = dict(net=float((1 + net).prod() - 1),
                            ew=float((1 + e).prod() - 1),
                            gross=float(book["gross"][m].mean() * 52),
                            turn=float(book["turn"][m].mean()),
                            weeks=int(len(net)))
    return out


def grid_books(c, grid="full"):
    """Candidate books simulated on the context's own sample (used by walk-forward)."""
    f, keep, ret1, valid = c["f"], c["keep"], c["ret1"], c["valid"]
    prem = c["d"]["prem_dev"].where(keep)
    mom = c["d"]["mom"]
    zf, zp, zm = zr(f), zr(prem), zr(mom)
    out = {}
    for k in (1, 2, 4):
        for buf in (None, 40):
            for hold in (0, 2):
                out[f"flow_sm{k}_buf{buf}_h{hold}"] = (
                    smooth(-zf, k), dict(N=20, buffer_n=buf, minhold=hold))
    if grid == "full":
        for l1, l2 in ((0.0, 0.3), (0.1, 0.3), (0.0, 0.4), (0.3, 0.3)):
            for k in (1, 2):
                out[f"lin_{l1}_{l2}_sm{k}_buf40_h2"] = (
                    smooth(zm - l1 * zp - l2 * zf, k), dict(N=20, buffer_n=40, minhold=2))
    books = {k: engine2.simulate(sc, ret1, valid, cost=COST, **kw) for k, (sc, kw) in out.items()}
    return books, out


def walk_forward(c, books, test_years, min_train_weeks=100):
    ew = c["ew"]
    folds = []
    for y in test_years:
        tr_lo, tr_hi = pd.Timestamp(f"{y-3}-01-01"), pd.Timestamp(f"{y}-01-01")
        te_lo, te_hi = tr_hi, pd.Timestamp(f"{y+1}-01-01")
        best, best_net = None, -9.0
        for k, b in books.items():
            n = b["net"][(b.index >= tr_lo) & (b.index < tr_hi)]
            if len(n) < min_train_weeks:
                continue
            if n.mean() * 52 > best_net:
                best_net, best = n.mean() * 52, k
        if best is None:
            continue
        n = books[best]["net"][(books[best].index >= te_lo) & (books[best].index < te_hi)]
        e = ew["net"].reindex(n.index)
        folds.append(dict(year=y, chosen=best, train_net=float(best_net),
                          test_net=float(n.mean() * 52), ew_net=float(e.mean() * 52),
                          test_turn=float(books[best]["turn"][n.index].mean()),
                          n_weeks=int(len(n))))
    if not folds:
        return None
    oos = pd.concat([books[f["chosen"]]["net"][
        (books[f["chosen"]].index >= pd.Timestamp(f"{f['year']}-01-01"))
        & (books[f["chosen"]].index < pd.Timestamp(f"{f['year']+1}-01-01"))] for f in folds])
    e = ew["net"].reindex(oos.index)
    d = (oos - e).dropna()
    se = S.nw_se(d, 4)
    return dict(folds=folds, oos_net_ann=float(oos.mean() * 52),
                oos_ew_ann=float(e.mean() * 52),
                oos_excess_ann=float(d.mean() * 52),
                oos_excess_t=float(d.mean() / se) if se and se > 0 else np.nan,
                oos_cagr=float((1 + oos).prod() ** (52 / len(oos)) - 1),
                oos_sharpe=float(oos.mean() / oos.std() * np.sqrt(52)),
                oos_mdd=float(((1 + oos).cumprod() / (1 + oos).cumprod().cummax() - 1).min()),
                oos_hit=float((d > 0).mean()), n_weeks=int(len(oos)),
                wins=int((d.groupby(d.index.year).mean() > 0).sum()),
                losses=int((d.groupby(d.index.year).mean() <= 0).sum()),
                chosen_counts={k: int(v) for k, v in
                               pd.Series([f["chosen"] for f in folds]).value_counts().items()},
                by_year={int(y): float((1 + g).prod() - 1) for y, g in oos.groupby(oos.index.year)})
