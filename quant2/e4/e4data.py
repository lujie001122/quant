#!/usr/bin/env python3
"""E4 data layer.

Reuses the E3 panel (same PIT grid, same cache, same universe filter) so every number is
directly comparable with E1/E2/E3.  Two jobs:

1. `build(cutoff=...)` -- hard truncation of the sample at a date.  Forward returns are
   RE-COMPUTED on the truncated price panel, so a book that ends 2024-12-31 never sees a
   2025 print through fwd[1] (the last week gets NaN and drops out of `valid`).
2. second-order fund-flow signals (all PIT, uses only f up to week t):
      run    signed run length of weeks with f>0 (or f<0)
      acc    f_t - f_{t-1}
      pct52  percentile rank of f_t inside its own trailing 52 weeks
"""
import os, sys, time, pickle
import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "e3"))
import e3data  # noqa: E402

CACHE = os.path.join(os.path.dirname(_HERE), "data", "e4_panel.pkl")
FRAMES = ("f", "prem", "prem_dev", "mom", "ret", "S", "P", "RAW", "A", "scale", "keep",
          "T20", "T60")


def _raw():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    t0 = time.time()
    d = e3data.build()
    pickle.dump(d, open(CACHE, "wb"))
    print(f"[e4data] built panel in {time.time()-t0:.1f}s -> {CACHE}", flush=True)
    return d


def truncate(d, cutoff):
    """Cut every frame at `cutoff` (a week-end date) and rebuild forward returns on the
    truncated price panel so nothing after the cutoff can leak in."""
    if cutoff is None:
        return d
    cut = pd.Timestamp(cutoff)
    out = dict(d)
    for k in FRAMES:
        x = d[k]
        out[k] = x.loc[x.index <= cut]
    P = out["P"]
    out["fwd"] = {h: P.shift(-h) / P - 1.0 for h in (1, 2, 4)}
    wk = out["f"].index
    out["wk"] = wk
    out["cal"] = d["cal"][d["cal"] <= cut]
    return out


def build(cutoff=None):
    d = truncate(_raw(), cutoff)
    d["cats"] = d["cat"]
    return d


# ---------------------------------------------------------------- second-order flow
def signed_run(f):
    """Signed length of the current run of same-sign weekly flows (>0 buying, <0 selling)."""
    a = f.values.astype(float)
    out = np.full(a.shape, np.nan)
    s = np.sign(np.nan_to_num(a, nan=0.0))
    s[np.isnan(a)] = 0
    run = np.zeros(a.shape[1])
    for i in range(a.shape[0]):
        prev = s[i - 1] if i > 0 else np.zeros(a.shape[1])
        same = (s[i] != 0) & (s[i] == prev)
        run = np.where(same, run + 1, np.where(s[i] != 0, 1.0, 0.0))
        out[i] = np.where(s[i] != 0, run * s[i], 0.0)
    out[np.isnan(a)] = np.nan
    return pd.DataFrame(out, index=f.index, columns=f.columns)


def accel(f):
    return f - f.shift(1)


def pct52(f, win=52, minp=26):
    """Percentile rank of f_t among its own previous `win` weeks (PIT, current excluded)."""
    a = f.values.astype(float)
    n, m = a.shape
    out = np.full((n, m), np.nan)
    step = 64
    for lo in range(0, m, step):
        hi = min(lo + step, m)
        blk = a[:, lo:hi]
        # (rows, cols, win) sliding windows over the time axis
        for i in range(win, n):
            w = blk[i - win:i, :]
            cur = blk[i, :]
            ok = ~np.isnan(w)
            cnt = ok.sum(0)
            less = ((w < cur) & ok).sum(0)
            with np.errstate(invalid="ignore", divide="ignore"):
                p = np.where(cnt >= minp, less / cnt, np.nan)
            p[np.isnan(cur)] = np.nan
            out[i, lo:hi] = p
    return pd.DataFrame(out, index=f.index, columns=f.columns)


if __name__ == "__main__":
    t0 = time.time()
    d = build()
    f = d["f"]
    print("full weeks", d["wk"][0].date(), d["wk"][-1].date(), d["f"].shape)
    d24 = build("2024-12-31")
    print("cut  weeks", d24["wk"][0].date(), d24["wk"][-1].date(), d24["f"].shape)
    r = signed_run(f); ac = accel(f); p = pct52(f)
    print("run  ", r.shape, int(r.notna().sum().sum()))
    print("acc  ", ac.shape, int(ac.notna().sum().sum()))
    print("pct52", p.shape, int(p.notna().sum().sum()), f"({time.time()-t0:.1f}s)")
    print("pct52 last-week sample", p.iloc[-1].dropna().head(3).to_dict())
