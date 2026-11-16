#!/usr/bin/env python3
"""E2 phase 2 final tables: net-subscription (f) predictiveness for ETF returns.

Design choices (all PIT-safe):
  * weekly grid = last trading day of each ISO week (real calendar)
  * base case lag=1: the share number used at week-end t is the one from the previous trading
    day, i.e. already published. lag=0 / lag=2 are the optimistic / conservative variants.
  * forward return = close(t) -> close(t+h); no same-bar information
  * cross-sectional excess = return minus equal-weight universe mean that week
  * rank IC (Spearman), weekly; t-stat Newey-West with lag=h; 95% CI by block bootstrap
  * quintiles on f, equal weight within group; name-level turnover, 0.12% one-way per side
"""
import argparse, json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
COST = 0.0012


# ---------------------------------------------------------------- stats helpers
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


def boot_ci(x, block=13, n=2000, seed=7):
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


def ic_stats(ic, h):
    ic = pd.Series(ic).dropna()
    se = nw_se(ic, lag=max(h - 1, 1))
    lo, hi = boot_ci(ic)
    return dict(n=int(len(ic)), ic=float(ic.mean()), se=float(se),
                t=float(ic.mean() / se) if se and se > 0 else np.nan,
                ci_lo=lo, ci_hi=hi, pos_frac=float((ic > 0).mean()),
                sd=float(ic.std()))


def xs_demean(df):
    return df.sub(df.mean(axis=1), axis=0)


def rank_ic(f, r, weeks=None, min_n=20):
    weeks = f.index if weeks is None else weeks
    out = {}
    for t in weeks:
        if t not in f.index or t not in r.index:
            continue
        a, b = f.loc[t], r.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        out[t] = spearman(a[m], b[m])
    return pd.Series(out).sort_index()


def xs_reg_resid(f, mom, min_n=20):
    res = pd.DataFrame(np.nan, index=f.index, columns=f.columns)
    for t in f.index:
        a, b = f.loc[t], mom.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < min_n:
            continue
        x = b[m].values.astype(float)
        y = a[m].values.astype(float)
        X = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        res.loc[t, m[m].index] = y - X @ beta
    return res


def pooled_reg(f, mom, r, min_n=30):
    recs = []
    for t in f.index:
        if t not in r.index:
            continue
        y, a, b = r.loc[t], f.loc[t], mom.loc[t]
        m = a.notna() & b.notna() & y.notna()
        if m.sum() < min_n:
            continue
        X = np.column_stack([a[m].values, b[m].values]).astype(float)
        X = (X - X.mean(0)) / (X.std(0) + 1e-12)
        recs.append((X, y[m].values - y[m].mean()))
    if not recs:
        return None
    X = np.vstack([x for x, _ in recs])
    Y = np.concatenate([y for _, y in recs])
    beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
    XtX = np.linalg.inv(X.T @ X)
    meat = np.zeros((2, 2))
    for x, y in recs:
        u = y - x @ beta
        g = x.T @ u
        meat += np.outer(g, g)
    se = np.sqrt(np.diag(XtX @ meat @ XtX))
    return dict(b_f=float(beta[0]), t_f=float(beta[0] / se[0]),
                b_mom=float(beta[1]), t_mom=float(beta[1] / se[1]), n_obs=int(len(Y)))


# ---------------------------------------------------------------- quintiles
def quintile_books(f, fwd_raw, groups=5, min_n=20, cost=COST):
    """Return weekly group returns, name-level turnover and cost-adjusted annual returns.

    Annualisation: h-week overlapping horizon -> (1+mean_h)^(52/h)-1.
    Turnover: one-way, per side, name-level: sum|w_t - w_{t-1}|/2 for the tranche rotating in;
    book-level cost per week = cost * turnover * (1/h) ... h tranches each of size 1/h rotate.
    """
    out = {}
    weeks = [t for t in f.index if t in f.index]
    for h, r in fwd_raw.items():
        rets = {g: [] for g in range(groups)}
        uni = []
        turns = {g: [] for g in range(groups)}
        prev_w = {g: None for g in range(groups)}
        n_wk = 0
        for t in weeks:
            a = f.loc[t].dropna()
            if len(a) < min_n or t not in r.index:
                continue
            q = pd.qcut(a.rank(method="first"), groups, labels=False)
            rr = r.loc[t]
            if rr.notna().sum() < min_n:
                continue
            n_wk += 1
            uni.append(rr.dropna().mean())
            for g in range(groups):
                sel = q.index[q == g]
                v = rr.reindex(sel).dropna()
                rets[g].append(v.mean() if len(v) else np.nan)
                w = pd.Series(1.0 / len(sel), index=sel)
                if prev_w[g] is not None:
                    d = w.sub(prev_w[g], fill_value=0.0).abs().sum() / 2.0
                    turns[g].append(float(d))
                prev_w[g] = w
        if n_wk < 30:
            out[h] = None
            continue
        ann, turn_m, cost_ann = {}, {}, {}
        for g in range(groups):
            s = pd.Series(rets[g]).dropna()
            ann[g] = float((1 + s.mean()) ** (52 / h) - 1)
            turn_m[g] = float(np.mean(turns[g])) if turns[g] else np.nan
            cost_ann[g] = float(cost * turn_m[g] * (52.0 / h))
        u = pd.Series(uni).dropna()
        ann_u = float((1 + u.mean()) ** (52 / h) - 1)
        # Q5 - Q1 long/short decomposition is NOT traded (no shorting) but the spread tells
        # us whether the signal is monotone and how much is left after costs.
        spread = ann[groups - 1] - ann[0]
        spread_cost = cost_ann[groups - 1] + cost_ann[0]
        mono = spearman(pd.Series(range(groups), dtype=float),
                        pd.Series([ann[g] for g in range(groups)], dtype=float))
        out[h] = dict(n_weeks=n_wk, ann={str(g): ann[g] for g in range(groups)},
                      ann_uni=ann_u, spread=float(spread), spread_cost=float(spread_cost),
                      spread_net=float(spread - spread_cost),
                      monotonicity=float(mono),
                      turnover={str(g): turn_m[g] for g in range(groups)},
                      cost_ann={str(g): cost_ann[g] for g in range(groups)},
                      ann_net={str(g): ann[g] - cost_ann[g] for g in range(groups)})
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--lag", type=int, default=1)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    d = e2data.build(lag=a.lag, since=a.start)
    f, mom, cat, scale = d["f"], d["mom"], d["cat"], d["scale"]
    fwd_raw = d["fwd"]
    fwd = {h: xs_demean(r) for h, r in fwd_raw.items()}
    weeks = f.index
    cov = f.notna().sum()

    res = {"meta": {"lag": a.lag, "start": a.start,
                    "weeks": [str(weeks[0].date()), str(weeks[-1].date())],
                    "n_weeks": int(len(weeks)), "n_codes": int(f.shape[1]),
                    "codes_200w": int((cov >= 200).sum()), "codes_250w": int((cov >= 250).sum()),
                    "median_obs": float(cov.median())},
           "cat_counts": cat.value_counts().to_dict()}

    # ---- profile
    rows = []
    for c in f.columns:
        s = f[c].dropna()
        if len(s) < 50:
            continue
        rows.append(dict(code=c, cat=cat.get(c), n=len(s), mean=s.mean(), sd=s.std(),
                         p05=s.quantile(0.05), p95=s.quantile(0.95),
                         gt5=float((s > 0.05).mean()), ren5=float((s < -0.05).mean()),
                         gt20=float((s > 0.20).mean()), ren20=float((s < -0.20).mean()),
                         scale_med=float(np.nanmedian(scale[c])) if c in scale.columns else np.nan))
    prof = pd.DataFrame(rows)
    prof.to_csv(os.path.join(OUT, "f_profile_by_etf.csv"), index=False)
    g = prof.groupby("cat").agg(n_etf=("code", "count"), n_obs=("n", "sum"),
                                f_mean=("mean", "mean"), f_sd=("sd", "median"),
                                p05=("p05", "mean"), p95=("p95", "mean"),
                                gt5=("gt5", "mean"), ren5=("ren5", "mean"),
                                gt20=("gt20", "mean"), ren20=("ren20", "mean"),
                                scale_med_yi=("scale_med", "median"))
    g["scale_med_yi"] = g["scale_med_yi"] / 1e8
    res["profile"] = g.reset_index().to_dict("records")

    # ---- main IC
    ic = {h: rank_ic(f, fwd[h]) for h in (1, 2, 4)}
    res["ic"] = {str(h): ic_stats(ic[h], h) for h in (1, 2, 4)}
    res["ic_by_year"] = {}
    for h in (1, 2, 4):
        s = ic[h]
        by = s.groupby(s.index.year).agg(["mean", "count"])
        res["ic_by_year"][str(h)] = {str(y): {"ic": float(r["mean"]), "n": int(r["count"])}
                                     for y, r in by.iterrows()}

    # ---- by category (cross-section within category)
    res["ic_by_cat"] = {}
    for h in (1, 2, 4):
        out = {}
        for c in ["宽基", "行业", "跨境", "债券", "商品"]:
            cols = cat.index[cat == c]
            if len(cols) < 10:
                continue
            sub = rank_ic(f[cols], fwd[h][cols], min_n=8)
            if len(sub) >= 20:
                st = ic_stats(sub, h)
                st["avg_names"] = float(f[cols].notna().sum(axis=1).mean())
                out[c] = st
        res["ic_by_cat"][str(h)] = out

    # ---- by size tercile
    res["ic_by_size"] = {}
    q = scale.rank(axis=1, pct=True)
    for h in (1, 2, 4):
        out = {}
        for name, lo, hi in [("小", 0.0, 1 / 3), ("中", 1 / 3, 2 / 3), ("大", 2 / 3, 1.01)]:
            mask = (q >= lo) & (q < hi)
            fw = f.where(mask)
            sub = rank_ic(fw, fwd[h], min_n=8)
            if len(sub) >= 20:
                out[name] = ic_stats(sub, h)
        res["ic_by_size"][str(h)] = out

    # ---- quintiles
    res["quint"] = {str(h): v for h, v in quintile_books(f, fwd_raw).items()}

    # ---- momentum orthogonality + increment
    res["orth"] = {}
    rhos = []
    for t in f.index:
        a1, b1 = f.loc[t], mom.loc[t]
        m = a1.notna() & b1.notna()
        if m.sum() >= 20:
            rhos.append(spearman(a1[m], b1[m]))
    rhos = pd.Series(rhos)
    res["orth"] = dict(mean_rho=float(rhos.mean()), mean_abs=float(rhos.abs().mean()),
                       p90_abs=float(rhos.abs().quantile(0.9)),
                       pct_abs_gt_05=float((rhos.abs() > 0.5).mean()), n=len(rhos))

    fres = xs_reg_resid(f, mom)
    res["ic_resid"] = {}
    for h in (1, 2, 4):
        st = ic_stats(rank_ic(fres, fwd[h]), h)
        st["delta_vs_raw"] = st["ic"] - res["ic"][str(h)]["ic"]
        res["ic_resid"][str(h)] = st
    res["reg"] = {str(h): pooled_reg(f, mom, fwd[h]) for h in (1, 2, 4)}

    # ---- robustness: winsorised f, drop first 26 weeks after entering universe, no 2015
    fw_ = f.clip(f.quantile(0.01, axis=1), f.quantile(0.99, axis=1), axis=0)
    res["ic_winsor"] = {str(h): ic_stats(rank_ic(fw_, fwd[h]), h) for h in (1, 2, 4)}
    first = f.apply(lambda s: s.first_valid_index())
    age = pd.DataFrame({c: [(t - first[c]).days / 7 if pd.notna(first[c]) else -1 for t in f.index]
                        for c in f.columns}, index=f.index)
    f_age = f.where(age > 26)
    res["ic_after_26w"] = {str(h): ic_stats(rank_ic(f_age, fwd[h]), h) for h in (1, 2, 4)}
    keep_y = weeks[~weeks.year.isin([2015])]
    res["ic_ex2015"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=keep_y), h) for h in (1, 2, 4)}
    # only the recent half of the sample
    half = weeks[weeks >= pd.Timestamp("2021-01-01")]
    res["ic_2021on"] = {str(h): ic_stats(rank_ic(f, fwd[h], weeks=half), h) for h in (1, 2, 4)}

    json.dump(res, open(os.path.join(OUT, f"E2_stats_lag{a.lag}.json"), "w"), indent=1, ensure_ascii=False)
    pd.DataFrame(ic).to_csv(os.path.join(OUT, f"ic_series_lag{a.lag}.csv"))

    # ---- console summary
    def p(*x):
        print(*x)
    p("=" * 78)
    p(f"lag={a.lag}  weeks {res['meta']['weeks'][0]}..{res['meta']['weeks'][1]} "
      f"({res['meta']['n_weeks']})  codes={res['meta']['n_codes']}  >=200w={res['meta']['codes_200w']}")
    p("-" * 78)
    p("IC (f -> forward excess return)")
    for h in ("1", "2", "4"):
        s = res["ic"][h]
        p(f"  h={h}w  IC={s['ic']:+.4f}  t={s['t']:+.2f}  n={s['n']}  "
          f"95%CI[{s['ci_lo']:+.4f},{s['ci_hi']:+.4f}]  pos={s['pos_frac']:.1%}")
    p("-" * 78)
    p("residual IC (f orthogonalised on 60d momentum)")
    for h in ("1", "2", "4"):
        s = res["ic_resid"][h]
        p(f"  h={h}w  IC={s['ic']:+.4f}  t={s['t']:+.2f}  delta={s['delta_vs_raw']:+.4f}")
    p("-" * 78)
    p(f"orthogonality f vs mom: mean rho={res['orth']['mean_rho']:+.3f}  mean|rho|={res['orth']['mean_abs']:.3f}  "
      f"p90|rho|={res['orth']['p90_abs']:.3f}  share|rho|>0.5={res['orth']['pct_abs_gt_05']:.1%}")
    p("-" * 78)
    p("quintiles (Q1=most redeemed .. Q5=most created), annualised, cost 0.12%/side")
    for h in ("1", "2", "4"):
        qd = res["quint"][h]
        if not qd:
            continue
        p(f"  h={h}w  n_weeks={qd['n_weeks']}")
        p("    gross: " + "  ".join(f"Q{g}={qd['ann'][str(g)]:+.2%}" for g in range(5)) +
          f"  EW={qd['ann_uni']:+.2%}")
        p("    net  : " + "  ".join(f"Q{g}={qd['ann_net'][str(g)]:+.2%}" for g in range(5)))
        p(f"    spread Q5-Q1={qd['spread']:+.2%}  cost={qd['spread_cost']:.2%}  net={qd['spread_net']:+.2%}"
          f"  mono={qd['monotonicity']:+.2f}")
    p("-" * 78)
    p("IC by year (h=1)")
    p("  " + "  ".join(f"{y}:{v['ic']:+.3f}" for y, v in res["ic_by_year"]["1"].items()))
    p("robustness h=1: winsor %.4f | after26w %.4f | ex2015 %.4f | 2021on %.4f" % (
        res["ic_winsor"]["1"]["ic"], res["ic_after_26w"]["1"]["ic"],
        res["ic_ex2015"]["1"]["ic"], res["ic_2021on"]["1"]["ic"]))
    p("=" * 78)


if __name__ == "__main__":
    main()
