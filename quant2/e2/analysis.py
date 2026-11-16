#!/usr/bin/env python3
"""E2 phase 2: does weekly net subscription (f) predict forward ETF returns?

Everything is cross-sectional and weekly. Sign convention: f>0 = net creation (申购).
All forward returns are close(t) -> close(t+h) on the real trading calendar, so a signal at the
close of week t is followed by a return that only starts there.

Outputs: out/e2/E2_stats.json + console tables (report assembled by report.py).
"""
import argparse, json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data

def spearman(a, b):
    a = pd.Series(a, dtype=float); b = pd.Series(b, dtype=float)
    m = a.notna() & b.notna()
    if m.sum() < 5:
        return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")


def nw_t(x, lag=1):
    """Newey-West t-stat of the mean of x (overlapping-horizon correction)."""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 5:
        return np.nan, np.nan
    mu = x.mean()
    e = x - mu
    g0 = (e @ e) / n
    var = g0
    for L in range(1, lag + 1):
        gl = (e[L:] @ e[:-L]) / n
        w = 1.0 - L / (lag + 1.0)
        var += 2.0 * w * gl
    se = np.sqrt(max(var, 1e-12) / n)
    return mu / se, se


def block_boot_ci(x, block=13, n=2000, seed=7):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(x) < 20:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    means = []
    for _ in range(n):
        idx = rng.integers(0, len(x), nb)
        smp = np.concatenate([x[i:i + block] for i in idx])[:len(x)]
        means.append(smp.mean())
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def xs_demean(df):
    return df.sub(df.mean(axis=1), axis=0)


def rank_ic_series(f, r, min_n=20):
    """weekly Spearman rank IC, requires min_n names"""
    common = f.index.intersection(r.index)
    out = {}
    for t in common:
        a, b = f.loc[t], r.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        out[t] = spearman(a[m], b[m])
    return pd.Series(out).sort_index()


def ic_block(f, fwd, min_n=20):
    res = {}
    for h, r in fwd.items():
        ic = rank_ic_series(f, r, min_n)
        t, se = nw_t(ic, lag=max(h - 1, 1))
        lo, hi = block_boot_ci(ic)
        res[h] = dict(n=int(len(ic)), ic=float(ic.mean()), t=float(t), se=float(se),
                      ci_lo=lo, ci_hi=hi, pos_frac=float((ic > 0).mean()),
                      ic_sd=float(ic.std()), ic_series=ic)
    return res


def quintile_table(f, fwd, cat=None, cost_oneway=0.0012, groups=5, min_n=20):
    """Weekly sort into `groups` by f; hold h weeks via overlapping tranches.

    Portfolio value is chained in overlapping tranches: each week 1/h of the capital is
    rebalanced to the current ranking, so cost = one-way cost x (1/h) x turnover per week.
    """
    out = {}
    for h, r in fwd.items():
        rows = []
        cols = f.columns
        tranche_w = {}
        for t in f.index:
            a = f.loc[t].dropna()
            nxt = f.index[f.index > t]
            if len(nxt) == 0:
                break
            tn = nxt[0]
            if len(a) < min_n or tn not in r.index:
                continue
            q = pd.qcut(a.rank(method="first"), groups, labels=False)
            rows.append((t, tn, q, r.loc[tn]))
        if len(rows) < 30:
            out[h] = None
            continue
        # portfolio returns per group, h-week tranches -> weekly average of h-week returns
        grp_ret = {g: [] for g in range(groups)}
        uni_ret = []
        for t, tn, q, rr in rows:
            for g in range(groups):
                sel = q.index[q == g]
                v = rr.reindex(sel).dropna()
                if len(v):
                    grp_ret[g].append(v.mean())
            uni_ret.append(rr.dropna().mean())
        ann = {}
        for g in range(groups):
            s = pd.Series(grp_ret[g])
            wk = (1 + s).prod() ** (1 / len(s)) - 1
            ann[g] = (1 + wk) ** 52 - 1 if h == 1 else (1 + s.mean()) ** (52 / h) - 1
        uni = pd.Series(uni_ret)
        wk_u = (1 + uni).prod() ** (1 / len(uni)) - 1 if h == 1 else uni.mean()
        ann_u = (1 + wk_u) ** 52 - 1 if h == 1 else (1 + uni.mean()) ** (52 / h) - 1
        # exact turnover of the capped-equal-weight book (per rebalance of one tranche)
        turn = []
        prev = None
        for t in f.index:
            a = f.loc[t].dropna()
            if len(a) < min_n:
                continue
            g = pd.qcut(a.rank(method="first"), groups, labels=False)
            w = g.value_counts(normalize=True).reindex(range(groups)).fillna(0).values
            if prev is not None:
                turn.append(np.abs(w - prev).sum() / 2.0)
            prev = w
        turn = float(np.mean(turn)) if turn else np.nan
        cost_ann = cost_oneway * turn * (52.0 / h)
        spread = ann[groups - 1] - ann[0]
        mono = spearman(pd.Series(range(groups), dtype=float), pd.Series([ann[g] for g in range(groups)], dtype=float))
        out[h] = dict(ann={g: float(ann[g]) for g in range(groups)}, ann_uni=float(ann_u),
                      spread=float(spread), monotonicity=float(mono),
                      turnover_weekly_oneway=turn, cost_ann=float(cost_ann),
                      n_weeks=len(rows), groups=groups,
                      # Q1 = lowest f (redeemed) .. Q5 = highest f (created)
                      q_spread_t=float(spread))
    return out


def residualized(f, mom, min_n=20):
    """cross-sectional OLS residual of f on mom each week (f = a + b*mom + e), e is the
    part of the flow signal that is orthogonal to momentum."""
    res = pd.DataFrame(np.nan, index=f.index, columns=f.columns)
    for t in f.index:
        a, b = f.loc[t], mom.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        x = b[m].values
        y = a[m].values
        X = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        res.loc[t, m[m].index] = y - X @ beta
    return res


def orthogonality(f, mom, min_n=20):
    rhos = []
    for t in f.index:
        a, b = f.loc[t], mom.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        rhos.append(spearman(a[m], b[m]))
    rhos = pd.Series(rhos)
    return dict(mean_rho=float(rhos.mean()), median_abs=float(rhos.abs().median()),
                p90_abs=float(rhos.abs().quantile(0.9)), pct_abs_gt_05=float((rhos.abs() > 0.5).mean()))


def panel_reg(f, mom, r, min_n=30):
    """pooled cross-sectional regression r = a + b1*f + b2*mom, week fixed effects removed by
    demeaning r within week; reports b1, its t-stat (clustered by week)."""
    recs = []
    for t in f.index:
        a, b = f.loc[t], mom.loc[t]
        if t not in r.index:
            continue
        y = r.loc[t]
        m = a.notna() & b.notna() & y.notna()
        if m.sum() < min_n:
            continue
        X = np.column_stack([a[m].values, b[m].values])
        X = (X - X.mean(0)) / (X.std(0) + 1e-12)
        yy = y[m].values - y[m].mean()
        recs.append((t, X, yy))
    if not recs:
        return None
    X = np.vstack([x for _, x, _ in recs])
    Y = np.concatenate([y for _, _, y in recs])
    beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
    resid = Y - X @ beta
    # cluster-robust by week
    XtX_inv = np.linalg.inv(X.T @ X)
    meat = np.zeros((2, 2))
    for _, x, y in recs:
        u = y - x @ beta
        g = x.T @ u
        meat += np.outer(g, g)
    V = XtX_inv @ meat @ XtX_inv
    se = np.sqrt(np.diag(V))
    return dict(b_f=float(beta[0]), t_f=float(beta[0] / se[0]),
                b_mom=float(beta[1]), t_mom=float(beta[1] / se[1]), n_obs=int(len(Y)))


def profile(f, cat, scale):
    rows = []
    for c, s in f.items():
        s = s.dropna()
        if len(s) < 50:
            continue
        rows.append(dict(code=c, cat=cat.get(c), n=len(s), mean=s.mean(), sd=s.std(),
                         p05=s.quantile(0.05), p95=s.quantile(0.95),
                         gt5=(s > 0.05).mean(), ren5=(s < -0.05).mean(),
                         gt20=(s > 0.20).mean(), ren20=(s < -0.20).mean(),
                         scale_med=float(np.nanmedian(scale[c])) if c in scale else np.nan))
    p = pd.DataFrame(rows)
    g = p.groupby("cat").agg(n=("code", "count"), f_mean=("mean", "mean"), f_sd=("sd", "median"),
                             p05=("p05", "mean"), p95=("p95", "mean"),
                             freq_gt5=("gt5", "mean"), freq_ren5=("ren5", "mean"),
                             freq_gt20=("gt20", "mean"), freq_ren20=("ren20", "mean"))
    return p, g


def run(lag=0, start="2017-01-01"):
    d = e2data.build(lag=lag, since=start)
    f, mom, cat, scale = d["f"], d["mom"], d["cat"], d["scale"]
    fwd = {h: xs_demean(r) for h, r in d["fwd"].items()}   # excess over cross-sectional mean
    fwd_raw = d["fwd"]
    res = {"lag": lag, "start": start, "weeks": [str(d["wk"][0].date()), str(d["wk"][-1].date())],
           "n_codes": int(f.shape[1])}
    res["ic"] = {str(h): {k: v for k, v in ic_block(f, fwd)[h].items() if k != "ic_series"}
                 for h in (1, 2, 4)}
    res["quint"] = {str(h): v for h, v in quintile_table(f, fwd_raw).items()}
    res["orth"] = orthogonality(f, mom)
    fres = residualized(f, mom)
    res["ic_resid"] = {str(h): {k: v for k, v in ic_block(fres, fwd)[h].items() if k != "ic_series"}
                       for h in (1, 2, 4)}
    res["reg"] = {str(h): panel_reg(f, mom, fwd[h]) for h in (1, 2, 4)}
    # decile IC on f (extreme flows)
    p, g = profile(f, cat, scale)
    res["profile"] = g.reset_index().to_dict("records")
    res["profile_n"] = int(len(p))
    return res, d


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lag", type=int, default=0)
    ap.add_argument("--start", default="2017-01-01")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    res, d = run(a.lag, a.start)
    print(json.dumps({k: v for k, v in res.items() if k != "profile"}, indent=1, ensure_ascii=False))
    print(pd.DataFrame(res["profile"]).to_string(index=False))
