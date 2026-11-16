#!/usr/bin/env python3
"""E3 shared stats helpers (identical conventions to E1/E2 so numbers are comparable)."""
import numpy as np
import pandas as pd


def spearman(a, b):
    a = pd.Series(a, dtype=float)
    b = pd.Series(b, dtype=float)
    m = a.notna() & b.notna()
    if m.sum() < 5:
        return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def nw_se(x, lag=1):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 5:
        return np.nan
    e = x - x.mean()
    var = (e @ e) / n
    for L in range(1, min(lag, n - 2) + 1):
        gl = (e[L:] @ e[:-L]) / n
        var += 2.0 * (1.0 - L / (lag + 1.0)) * gl
    return np.sqrt(max(var, 1e-12) / n)


def boot_ci(x, block=13, n=1000, seed=7):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(x) < 20:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    means = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, len(x), nb)
        means[i] = np.concatenate([x[j:j + block] for j in idx])[:len(x)].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def ic_stats(s, h=1):
    s = pd.Series(s).dropna()
    se = nw_se(s, lag=max(h - 1, 1))
    lo, hi = boot_ci(s)
    return dict(n=int(len(s)), ic=float(s.mean()), se=float(se),
                t=float(s.mean() / se) if se and se > 0 else np.nan,
                ci_lo=lo, ci_hi=hi, pos_frac=float((s > 0).mean()), sd=float(s.std()))


def rank_ic(sig, fwd, weeks=None, min_n=10):
    weeks = sig.index if weeks is None else weeks
    out = {}
    for t in weeks:
        if t not in sig.index or t not in fwd.index:
            continue
        a, b = sig.loc[t], fwd.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        out[t] = spearman(a[m], b[m])
    return pd.Series(out).sort_index()


def residual_ic(sig, fwd, ctrls, min_n=10):
    """Rank IC of sig after removing within-week rank-linear dependence on the controls."""
    out = {}
    for t in sig.index:
        if t not in fwd.index:
            continue
        a, b = sig.loc[t], fwd.loc[t]
        m = a.notna() & b.notna()
        for c in ctrls:
            m &= c.loc[t].notna()
        if m.sum() < min_n:
            continue
        y = a[m].rank().values
        X = np.column_stack([np.ones(m.sum())] + [c.loc[t][m].rank().values for c in ctrls])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = pd.Series(y - X @ beta, index=a[m].index)
        out[t] = resid.rank().corr(b[m].rank())
    return pd.Series(out).sort_index()


def xs_reg(sig, fwd, ctrls, min_n=20):
    """Pooled weekly cross-sectional regression: fwd ~ sig + ctrls (standardised, cluster by week)."""
    recs = []
    for t in sig.index:
        if t not in fwd.index:
            continue
        y = fwd.loc[t]
        cols = [sig.loc[t]] + [c.loc[t] for c in ctrls]
        m = y.notna()
        for c in cols:
            m &= c.notna()
        if m.sum() < min_n:
            continue
        X = np.column_stack([c[m].values for c in cols]).astype(float)
        X = (X - X.mean(0)) / (X.std(0) + 1e-12)
        recs.append((X, (y[m].values - y[m].mean())))
    if not recs:
        return None
    X = np.vstack([x for x, _ in recs]); Y = np.concatenate([y for _, y in recs])
    beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
    XtX = np.linalg.inv(X.T @ X)
    meat = np.zeros((X.shape[1], X.shape[1]))
    for x, y in recs:
        g = x.T @ (y - x @ beta)
        meat += np.outer(g, g)
    se = np.sqrt(np.diag(XtX @ meat @ XtX))
    names = ["sig"] + [f"ctrl{i}" for i in range(len(ctrls))]
    return {"b": {n: float(b) for n, b in zip(names, beta)},
            "t": {n: float(b / s) for n, b, s in zip(names, beta, se)},
            "n_obs": int(len(Y))}


def perf(weekly_net, weeks_per_year=52):
    """Weekly net series -> arith-annualised (E1/E2 convention) and compounded CAGR."""
    r = pd.Series(weekly_net).dropna()
    if len(r) < 20:
        return None
    nav = (1 + r).cumprod()
    ann_a = float(r.mean() * weeks_per_year)
    ann_c = float(nav.iloc[-1] ** (weeks_per_year / len(r)) - 1)
    vol = float(r.std(ddof=1) * np.sqrt(weeks_per_year))
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(weeks_per_year)) if r.std(ddof=1) > 0 else np.nan
    mdd = float((nav / nav.cummax() - 1).min())
    return dict(ann=ann_a, cagr=ann_c, vol=vol, sharpe=sharpe, mdd=mdd,
                n_weeks=int(len(r)), total=float(nav.iloc[-1] - 1))
