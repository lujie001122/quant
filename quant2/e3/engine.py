#!/usr/bin/env python3
"""E3 portfolio simulator: weekly long-only top-N book with the turnover-control mechanisms.

Contract
  score   weeks x names, higher = more attractive (already combined/smoothed)
  ret1    weeks x names, forward 1-week total return (close t -> close t+1)
  valid   weeks x names, tradable & has a realised forward return
  trend   weeks x names bool, optional entry/hold filter (price > MA20)

Turnover is name-level one-way: sum|w_t - w_{t-1}|/2 (cash = 0 weight).
Cost is charged in the rebalance week: turnover * cost_one_way.
No leverage, no shorting; weights sum to <= 1.
"""
import numpy as np
import pandas as pd

from e3stats import perf, nw_se


def simulate(score, ret1, valid, N=20, buffer_n=None, minhold=0, partial=1.0,
             trend=None, cost=0.0012, start=None, partial_mode="names"):
    weeks = score.index
    held = {}          # name -> week index of entry
    prev_w = pd.Series(dtype=float)
    recs = []
    for i, t in enumerate(weeks):
        s = score.loc[t]
        ok = valid.loc[t].fillna(False) & s.notna()
        if trend is not None:
            ok &= trend.loc[t].fillna(False)
        if start is not None and t < start:
            prev_w = pd.Series(dtype=float); held = {}
            continue
        if ok.sum() == 0:
            w = pd.Series(dtype=float)
        else:
            rk = s.where(ok).rank(ascending=False, method="first")
            keep = []
            for h in list(held):
                if not ok.get(h, False):
                    continue
                age = i - held[h]
                lim = buffer_n if buffer_n is not None else N
                if age < minhold or rk.get(h, np.inf) <= lim:
                    keep.append(h)
            keep = sorted(keep, key=lambda h: rk.get(h, np.inf))[:N]
            slots = N - len(keep)
            cand = rk[(rk <= N) & (~rk.index.isin(keep)) & ok]
            add = list(cand.sort_values().index[:max(slots, 0)])
            sel = keep + add
            w = pd.Series(1.0 / N, index=sel)
        if partial < 1.0 and len(prev_w) and len(w):
            # rotate only a fraction of the names that have to change, keep equal weights
            cur = set(prev_w.index)
            tgt = set(w.index)
            exits = sorted(cur - tgt, key=lambda h: rk.get(h, np.inf))          # worst rank first
            entries = sorted(tgt - cur, key=lambda h: rk.get(h, np.inf))        # best rank first
            n_move = int(np.ceil(partial * max(len(exits), len(entries))))
            if n_move < len(exits):
                keep_names = (cur - set(exits[:len(exits) - n_move])) | set(entries[:n_move])
            else:
                keep_names = tgt
            keep_names = set(list(keep_names)[:N])
            if keep_names:
                w = pd.Series(1.0 / N, index=sorted(keep_names))
            else:
                w = w
        all_idx = w.index.union(prev_w.index)
        turn = float((w.reindex(all_idx, fill_value=0.0)
                      - prev_w.reindex(all_idx, fill_value=0.0)).abs().sum() / 2.0)
        gross = float((w * ret1.loc[t].reindex(w.index).fillna(0.0)).sum())
        c = turn * cost
        recs.append(dict(t=t, gross=gross, turn=turn, cost=c, net=gross - c,
                         n=len(w), w_sum=float(w.sum())))
        held = {n: (held[n] if n in held else i) for n in w.index}
        prev_w = w
    df = pd.DataFrame(recs).set_index("t")
    return df


def ew_book(valid, ret1, cost=0.0012):
    """Equal-weight all-universe book, same turnover accounting (the benchmark)."""
    weeks = valid.index
    prev = pd.Series(dtype=float)
    recs = []
    for t in weeks:
        ok = valid.loc[t].fillna(False) & ret1.loc[t].notna()
        idx = ok.index[ok]
        w = pd.Series(1.0 / len(idx), index=idx) if len(idx) else pd.Series(dtype=float)
        all_idx = w.index.union(prev.index)
        turn = float((w.reindex(all_idx, fill_value=0.0)
                      - prev.reindex(all_idx, fill_value=0.0)).abs().sum() / 2.0)
        gross = float((w * ret1.loc[t].reindex(w.index).fillna(0.0)).sum())
        recs.append(dict(t=t, gross=gross, turn=turn, cost=turn * cost, net=gross - turn * cost))
        prev = w
    return pd.DataFrame(recs).set_index("t")


def summarize(book, ew=None, cost=0.0012, label=""):
    net = book["net"]
    out = {"label": label, **perf(net), "turnover": float(book["turn"].mean()),
           "cost_ann": float(book["cost"].mean() * 52), "gross_ann": None,
           "avg_names": float(book["n"].mean()) if "n" in book else np.nan}
    out["gross_ann"] = float(book["gross"].mean() * 52)
    out["gross_cagr"] = float((1 + book["gross"]).prod() ** (52 / len(book)) - 1)
    if ew is not None:
        d = (net - ew["net"]).dropna()
        out["excess_ann"] = float(d.mean() * 52)
        se = nw_se(d, lag=4)
        out["excess_t"] = float(d.mean() / se) if se and se > 0 else np.nan
        out["excess_hit"] = float((d > 0).mean())
        out["ew_ann"] = float(ew["net"].mean() * 52)
        out["ew_cagr"] = float((1 + ew["net"]).prod() ** (52 / len(ew)) - 1)
    return out
