#!/usr/bin/env python3
"""E4 engine: byte-for-byte the E3 simulator, with ONE fix.

E3's `partial` (分批换仓) branch ends with

    keep_names = set(list(keep_names)[:N])

`keep_names` is a Python set of strings, so `list(...)` order depends on
PYTHONHASHSEED.  Whenever the set holds more than N names the truncation therefore keeps
an arbitrary subset, and the same config produces different books in different processes
(E4 measured 8.61% .. 9.66% annual net on the identical inputs, purely from the seed).

Fix: sort by (rank, name) before truncating, which is what the surrounding code intends.
A second tie exists in the exits/entries sorts (equal keys -> set iteration order), same fix.
Everything else (turnover accounting, cost, no-leverage, no-short) is unchanged, so the
non-partial books reproduce E3 exactly.
"""
import numpy as np
import pandas as pd


def _rk(rk, h):
    """Rank lookup that is total-ordered: NaN (name dropped out of the valid set) -> +inf.

    NaN is not comparable, so a plain sort key of NaN makes the order depend on the input
    permutation -- and the input here comes from a set, i.e. from PYTHONHASHSEED."""
    x = rk.get(h, np.inf)
    return np.inf if (x is None or not np.isfinite(x)) else float(x)


def simulate(score, ret1, valid, N=20, buffer_n=None, minhold=0, partial=1.0,
             trend=None, cost=0.0012, start=None, partial_mode="names"):
    weeks = score.index
    held = {}
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
            keep = sorted(keep, key=lambda h: (_rk(rk, h), h))[:N]
            slots = N - len(keep)
            cand = rk[(rk <= N) & (~rk.index.isin(keep)) & ok]
            add = list(cand.sort_values().index[:max(slots, 0)])
            sel = keep + add
            w = pd.Series(1.0 / N, index=sel)
        if partial < 1.0 and len(prev_w) and len(w):
            cur = set(prev_w.index)
            tgt = set(w.index)
            # sort by (rank, name): a bare rank key leaves ties to set-iteration order,
            # which is hash dependent -> the whole book drifts between processes
            exits = sorted(cur - tgt, key=lambda h: (_rk(rk, h), h))
            entries = sorted(tgt - cur, key=lambda h: (_rk(rk, h), h))
            n_move = int(np.ceil(partial * max(len(exits), len(entries))))
            if n_move < len(exits):
                keep_names = (cur - set(exits[:len(exits) - n_move])) | set(entries[:n_move])
            else:
                keep_names = tgt
            # deterministic truncation (E3 truncated an unordered set -> seed dependent)
            keep_names = sorted(keep_names, key=lambda h: (_rk(rk, h), h))[:N]
            if keep_names:
                w = pd.Series(1.0 / N, index=sorted(keep_names))
        all_idx = w.index.union(prev_w.index)
        turn = float((w.reindex(all_idx, fill_value=0.0)
                      - prev_w.reindex(all_idx, fill_value=0.0)).abs().sum() / 2.0)
        gross = float((w * ret1.loc[t].reindex(w.index).fillna(0.0)).sum())
        recs.append(dict(t=t, gross=gross, turn=turn, cost=turn * cost, net=gross - turn * cost,
                         n=len(w), w_sum=float(w.sum())))
        held = {n: (held[n] if n in held else i) for n in w.index}
        prev_w = w
    return pd.DataFrame(recs).set_index("t")
