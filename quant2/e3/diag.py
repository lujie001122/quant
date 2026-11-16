#!/usr/bin/env python3
"""E3 step 1: correlation / complementarity diagnostics between premium and fund-flow.

Answers:
  * how correlated are the two signals in the cross-section (same week, same pool)?
  * is either one just momentum?
  * does each survive controlling for the other (bivariate cross-sectional regression)?
  * do they agree on the same names (2-D sort), and how big is the intersection?
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e3data
import e3stats as S

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "e3")


def zr(x):
    return x.rank(axis=1, pct=True) - 0.5


def corr_block(sig_a, sig_b, minn=20):
    rhos = []
    for t in sig_a.index:
        if t not in sig_b.index:
            continue
        a, b = sig_a.loc[t], sig_b.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < minn:
            continue
        rhos.append(S.spearman(a[m], b[m]))
    r = pd.Series(rhos).dropna()
    return dict(mean=float(r.mean()), mean_abs=float(r.abs().mean()),
                p90_abs=float(r.abs().quantile(0.9)),
                pct_gt_05=float((r.abs() > 0.5).mean()), n=int(len(r)))


def main():
    os.makedirs(OUT, exist_ok=True)
    d = e3data.build()
    keep = d["keep"]
    f = d["f"]
    prem_lvl = d["prem"].where(keep)
    prem = d["prem_dev"].where(keep)
    mom = d["mom"]
    fwd = {h: d["fwd"][h].where(keep) for h in (1, 2, 4)}
    cat = d["cat"]
    weeks = d["wk"]
    cb = cat.index[cat == "跨境"]

    res = {"meta": {"weeks": [str(weeks[0].date()), str(weeks[-1].date())], "n_weeks": len(weeks),
                    "n_codes": int(f.shape[1]), "n_cross": int(len(cb)),
                    "median_names_all": float(keep.sum(axis=1).median()),
                    "median_names_cross": float(keep[cb].sum(axis=1).median()),
                    "prem_names": int((prem.notna().sum() > 100).sum())}}

    # ---------------- 1. cross-sectional correlation
    res["corr"] = {
        "all": {"prem_f": corr_block(prem, f), "prem_mom": corr_block(prem, mom),
                "f_mom": corr_block(f, mom)},
        "cross": {"prem_f": corr_block(prem[cb], f[cb], minn=8),
                  "prem_mom": corr_block(prem[cb], mom[cb], minn=8),
                  "f_mom": corr_block(f[cb], mom[cb], minn=8)},
    }

    # ---------------- 2. IC of each signal, and residual ICs
    def ic_all(sig, h, minn=20):
        return S.ic_stats(S.rank_ic(sig, fwd[h], min_n=minn), h)

    def ic_cross(sig, h):
        return S.ic_stats(S.rank_ic(sig[cb], fwd[h][cb], min_n=8), h)

    res["ic"] = {"all": {k: {str(h): ic_all(v, h) for h in (1, 2, 4)}
                         for k, v in (("prem", prem), ("prem_lvl", prem_lvl), ("flow", f), ("mom", mom))},
                 "cross": {k: {str(h): ic_cross(v, h) for h in (1, 2, 4)}
                           for k, v in (("prem", prem), ("prem_lvl", prem_lvl), ("flow", f), ("mom", mom))}}
    # E1 reported per-category (HK / US); pooled cross-border mixes very different premium
    # baselines, so keep the within-category numbers side by side.
    import re
    CATS = [("US", r"纳指|纳斯达克|标普500|标普消费|美国50|道琼斯|纳指科技|标普生物|标普油气|纳指生物"),
            ("HK", r"恒生|港股|中概|恒指"), ("JP", r"日经|东证|日本"), ("EU", r"德国|法国|欧洲|英国"),
            ("OTHER", r"东南亚|越南|印度|沙特|亚太|中韩|中日")]
    nm = d["names"]
    by_cat = {}
    for tag, pat in CATS:
        cols = [c for c in f.columns if re.search(pat, str(nm.get(c, "")))]
        if len(cols) >= 5:
            by_cat[tag] = {"n_names": len(cols),
                        "prem_dev_h1": S.ic_stats(S.rank_ic(prem[cols], fwd[1][cols], min_n=5)),
                        "prem_lvl_h1": S.ic_stats(S.rank_ic(prem_lvl[cols], fwd[1][cols], min_n=5)),
                        "flow_h2": S.ic_stats(S.rank_ic(f[cols], fwd[2][cols], min_n=5))}
    res["by_cat"] = by_cat

    res["resid"] = {}
    for h in (1, 2, 4):
        res["resid"][str(h)] = {
            "prem|flow": S.ic_stats(S.residual_ic(prem, fwd[h], [f])),
            "flow|prem": S.ic_stats(S.residual_ic(f, fwd[h], [prem])),
            "prem|mom": S.ic_stats(S.residual_ic(prem, fwd[h], [mom])),
            "flow|mom": S.ic_stats(S.residual_ic(f, fwd[h], [mom])),
            "prem|flow+mom": S.ic_stats(S.residual_ic(prem, fwd[h], [f, mom])),
            "flow|prem+mom": S.ic_stats(S.residual_ic(f, fwd[h], [prem, mom])),
        }

    res["reg"] = {str(h): S.xs_reg(f, fwd[h], [prem, mom]) for h in (1, 2, 4)}
    res["reg_prem"] = {str(h): S.xs_reg(prem, fwd[h], [f, mom]) for h in (1, 2, 4)}

    # ---------------- 3. double sort: premium tercile x flow tercile, fwd 4w excess
    ds = {}
    for h in (2, 4):
        cells = {}
        cnt = {}
        for t in weeks:
            a, b = prem.loc[t], f.loc[t]
            r = fwd[h].loc[t]
            m = a.notna() & b.notna() & r.notna()
            if m.sum() < 30:
                continue
            aa, bb, rr = a[m], b[m], r[m] - r[m].mean()
            qa = pd.qcut(aa.rank(method="first"), 3, labels=False, duplicates="drop")
            qb = pd.qcut(bb.rank(method="first"), 3, labels=False, duplicates="drop")
            for i in (0, 1, 2):
                for j in (0, 1, 2):
                    sel = (qa == i) & (qb == j)
                    if sel.sum() == 0:
                        continue
                    cells.setdefault((i, j), []).append(float(rr[sel].mean()))
                    cnt.setdefault((i, j), []).append(int(sel.sum()))
        ds[str(h)] = {f"prem{i+1}_flow{j+1}": {"ret_ann": float(np.mean(v) * 52 / h),
                                               "weeks": len(v), "avg_names": float(np.mean(cnt[(i, j)]))}
                      for (i, j), v in cells.items()}
    res["double_sort"] = ds

    # ---------------- 4. intersection coverage
    inter, inter_ret, lo_prem, hi_prem = [], [], [], []
    for t in weeks:
        a, b = prem.loc[t], f.loc[t]
        r = fwd[4].loc[t]
        m = a.notna() & b.notna() & r.notna()
        if m.sum() < 30:
            continue
        aa, bb = a[m], b[m]
        p20, p80 = aa.quantile(0.2), aa.quantile(0.8)
        q20, q80 = bb.quantile(0.2), bb.quantile(0.8)
        sel = (aa <= p20) & (bb <= q20)
        inter.append(int(sel.sum()))
        if sel.sum():
            inter_ret.append(float((r[m] - r[m].mean())[sel].mean()))
        hi_prem.append(int(((aa > 0.10) & (bb > q80)).sum()))
    res["intersection"] = {
        "avg_names_lowprem_lowflow": float(np.mean(inter)),
        "median_names": float(np.median(inter)),
        "weeks_with_ge3": float(np.mean([x >= 3 for x in inter])),
        "fwd4w_excess_ann": float(np.mean(inter_ret) * 13) if inter_ret else None,
        "fwd4w_weeks": len(inter_ret),
        "avg_names_hi_prem_gt10_and_hi_flow": float(np.mean(hi_prem)),
    }

    # ---------------- 4b. IC of the combined scores (does the blend beat each leg?)
    def zr_(x):
        return x.rank(axis=1, pct=True) - 0.5
    zm_, zp_, zf_ = zr_(mom), zr_(prem), zr_(f)
    comb = {"flow_only": -zf_, "prem_only": -zp_, "mom_only": zm_,
            "flow+prem_50_50": -0.5 * zf_ - 0.5 * zp_,
            "flow+prem_30_70": -0.3 * zf_ - 0.7 * zp_,
            "lin_l1.1_l2.3": zm_ - 0.1 * zp_ - 0.3 * zf_}
    res["combined_ic"] = {k: {str(h): S.ic_stats(S.rank_ic(v, fwd[h], min_n=30), h)
                              for h in (1, 2, 4)} for k, v in comb.items()}

    # ---------------- 5. signal persistence (why turnover differs)
    pers = {}
    for nm, sig in (("prem", prem), ("flow", f), ("mom", mom)):
        ac = []
        for k in (1, 2, 4, 8):
            rhos = []
            for i in range(k, len(weeks)):
                t, t0 = weeks[i], weeks[i - k]
                a, b = sig.loc[t], sig.loc[t0]
                m = a.notna() & b.notna()
                if m.sum() < 20:
                    continue
                rhos.append(S.spearman(a[m], b[m]))
            ac.append(float(pd.Series(rhos).mean()))
        pers[nm] = {str(k): v for k, v in zip((1, 2, 4, 8), ac)}
    res["persistence"] = pers

    # ---------------- 6. premium only in cross-border: is the tradeable subset weaker?
    amt = d["A"].rolling(12, min_periods=6).median()
    res["prem_liquidity"] = {}
    for tag, mask in (("all_names", prem.notna()), ("amt>=500w", amt >= 5e6),
                      ("amt>=5000w", amt >= 5e7)):
        sub = prem[cb].where(mask[cb])
        res["prem_liquidity"][tag] = S.ic_stats(S.rank_ic(sub, fwd[1][cb], min_n=8))
        res["prem_liquidity"][tag]["avg_names"] = float(sub.notna().sum(axis=1).mean())

    json.dump(res, open(os.path.join(OUT, "E3_diag.json"), "w"), indent=1, ensure_ascii=False)

    # ---------------- console
    print("=" * 90)
    print(f"panel {res['meta']}")
    print("-" * 90)
    for pool in ("all", "cross"):
        c = res["corr"][pool]
        print(f"[corr {pool}] prem~flow {c['prem_f']['mean']:+.3f} (|r| {c['prem_f']['mean_abs']:.3f}) "
              f"prem~mom {c['prem_mom']['mean']:+.3f} ({c['prem_mom']['mean_abs']:.3f}) "
              f"flow~mom {c['f_mom']['mean']:+.3f} ({c['f_mom']['mean_abs']:.3f})")
    for pool in ("all", "cross"):
        for nm in ("prem", "prem_lvl", "flow", "mom"):
            for h in ("1", "2", "4"):
                s = res["ic"][pool][nm][h]
                print(f"IC {pool:5s} {nm:5s} h{h}  {s['ic']:+.4f} t {s['t']:+.2f} n {s['n']}")
    print("by category:", {k: (v["n_names"], round(v["prem_dev_h1"]["ic"], 4),
                               round(v["prem_dev_h1"]["t"], 2)) for k, v in by_cat.items()})
    print("-" * 90)
    for h in ("1", "2", "4"):
        print(f"resid h{h}: " + "  ".join(f"{k} {v['ic']:+.4f}(t{v['t']:+.2f})"
                                          for k, v in res["resid"][h].items()))
    print("-" * 90)
    print("bivariate reg (fwd ~ flow + prem + mom):")
    for h in ("1", "2", "4"):
        r = res["reg"][h]
        print(f"  h{h}", {k: (round(v, 4), round(r['t'][k], 2)) for k, v in r["b"].items()})
    print("intersection:", {k: (round(v, 3) if isinstance(v, float) else v)
                            for k, v in res["intersection"].items()})
    print("combined IC h2:", {k: (round(v["2"]["ic"], 4), round(v["2"]["t"], 2))
                              for k, v in res["combined_ic"].items()})
    print("persistence:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in pers.items()})
    print("prem liquidity:", {k: (round(v["ic"], 4), round(v["t"], 2)) for k, v in res["prem_liquidity"].items()})
    print("double sort h4 (prem rows low->high, flow cols low->high):")
    for i in (0, 1, 2):
        print("  ", [round(res["double_sort"]["4"][f"prem{i+1}_flow{j+1}"]["ret_ann"], 4) for j in (0, 1, 2)])


if __name__ == "__main__":
    main()
