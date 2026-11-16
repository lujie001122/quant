"""Stage 4: finalists, walk-forward, deflated Sharpe, cost sensitivity, capacity.

Schemes (structurally different):
  A 超短动量       cross-sectional 3/5d momentum, daily-ish rebalance, all pools
  B 趋势跟踪       QDII/commodity, MA/abs-momentum trend, 5-60d rebalance
  C 主题高波动动量  theme+industry, high-vol half, 5-20d momentum
  D 日内结构       intraday close/open strength or gap, hold 1-2d
  E 多策略拼装     inverse-vol weighted A+B+C
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_data as G
import gl_engine as E
import gl_schemes as S

OUT = G.DATA
START = "2019-01-01"
ZERO = 0.001
WF_YEARS = [2022, 2023, 2024, 2025, 2026]
SEGS = [("2019-21", "2019-01-01", "2021-12-31"),
        ("2022-23", "2022-01-01", "2023-12-31"),
        ("2024-26", "2024-01-01", "2026-11-13"),
        ("last9m", "2026-02-13", "2026-11-13")]

# ---------------------------------------------------------------- data
u = pd.read_csv(OUT / "universe_dedup.csv")
meta = dict(zip(u.symbol, u.category))
names = dict(zip(u.symbol, u.name))
data = G.load_all(u.symbol.tolist())
S.set_data(data)
close = pd.DataFrame({k: v["close"] for k, v in data.items()}).sort_index().loc[START:]
amount = pd.DataFrame({k: (v["volume"] * 100 * v["close"]) for k, v in data.items()}).reindex(close.index)
open_ = pd.DataFrame({k: v["open"] for k, v in data.items()}).reindex(close.index)
print(f"panel {close.shape}  {close.index[0].date()}..{close.index[-1].date()}", flush=True)

# ---------------------------------------------------------------- grids
def grid_A():
    for pool, topn, lb, rb in itertools.product(["all", "theme_ind"], [2, 3, 5], [3, 5], [1, 2, 5]):
        yield dict(pool=pool, topn=topn, lb=lb, rebal=rb, absmom=True)


def grid_B():
    for pool, topn, lb, rb in itertools.product(["qdii_comm", "commod", "qdii"], [1, 2, 3],
                                                [10, 20, 60], [5, 20, 60]):
        yield dict(pool=pool, topn=topn, lb=lb, rebal=rb, absmom=True)


def grid_C():
    for topn, lb, vq, ma in itertools.product([1, 2, 3], [5, 10, 20], [0.3, 0.5], [0, 60]):
        yield dict(topn=topn, lb=lb, vol_q=vq, ma=ma)


def grid_D():
    for topn, mode, rb in itertools.product([3, 5, 10], ["strong", "revert"], [1, 2]):
        yield dict(topn=topn, mode=mode, rebal=rb)


def run_A(c):
    return S.s_short_mom(close, amount, meta, **c, cost=ZERO)


def run_B(c):
    return S.s_short_mom(close, amount, meta, cost=ZERO, **c)


def run_C(c):
    return S.s_hivol_theme(close, amount, meta, cost=ZERO, **c)


def run_D(c):
    return S.s_intraday(close, amount, meta, cost=ZERO, ma=0, **c)


SCHEMES = {
    "A_超短动量": (grid_A, run_A),
    "B_趋势跟踪": (grid_B, run_B),
    "C_主题高波动": (grid_C, run_C),
    "D_日内结构": (grid_D, run_D),
}

t0 = time.time()
trial_rows = []
store = {}          # scheme -> list of (cfg, ret, turn, picks)
for name, (gf, rf) in SCHEMES.items():
    lst = []
    for cfg in gf():
        try:
            r = rf(cfg)
        except Exception as e:  # noqa: BLE001
            print("ERR", name, cfg, e)
            continue
        lst.append(dict(cfg=cfg, ret=r["ret"], turn=r["turnover"], picks=r["picks"]))
    for e in lst:
        mm = E.metrics(e["ret"])
        if mm:
            trial_rows.append(dict(scheme=name, cfg=str(e["cfg"]), ann=mm["ann"],
                                   mdd=mm["mdd"], sharpe=mm["sharpe"],
                                   sr_daily=e["ret"].mean()/e["ret"].std()))
    store[name] = lst
    print(f"{name}: {len(lst)} configs ({time.time()-t0:.0f}s)", flush=True)

total_cfgs = sum(len(v) for v in store.values())

# ---------------------------------------------------------------- walk-forward
def wf(scheme, cost=ZERO, ret_adj=True, k=1):
    """Walk-forward: train on Y-3..Y-1 (Sharpe), apply to Y. k>1 = ensemble of top-k."""
    lst = store[scheme]
    stitched = []
    chosen = {}
    for Y in WF_YEARS:
        tr = (pd.Timestamp(f"{Y-3}-01-01"), pd.Timestamp(f"{Y-1}-12-31"))
        te = (pd.Timestamp(f"{Y}-01-01"), pd.Timestamp(f"{Y}-12-31"))
        scored = []
        for e in lst:
            r = e["ret"].loc[tr[0]:tr[1]]
            if len(r) < 200:
                continue
            m = E.metrics(r)
            if not m or not np.isfinite(m["sharpe"]):
                continue
            scored.append((m["sharpe"], e))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        top = scored[:k]
        parts = []
        for _, e in top:
            r = e["ret"].loc[te[0]:te[1]]
            if cost != ZERO and ret_adj:
                r = r - e["turn"].loc[te[0]:te[1]] * (cost - ZERO)
            parts.append(r)
        stitched.append(pd.concat(parts, axis=1).mean(axis=1))
        chosen[Y] = [str(e["cfg"]) for _, e in top] if k > 1 else str(top[0][1]["cfg"])
    if not stitched:
        return pd.Series(dtype=float), chosen
    return pd.concat(stitched).sort_index(), chosen


def seg_table(ret):
    rows = []
    for nm, a, b in SEGS:
        r = ret.loc[a:b]
        m = E.metrics(r)
        if m:
            rows.append(dict(seg=nm, ann=m["ann"], mdd=m["mdd"], sharpe=m["sharpe"]))
    return pd.DataFrame(rows)


result = {}
for name in SCHEMES:
    for k in (1, 5):
        r, chosen = wf(name, k=k)
        m = E.metrics(r)
        if not m:
            print(f"\n== {name} k={k}: WF produced no trades")
            continue
        key = name if k == 1 else name + "_ens5"
        result[key] = dict(ret=r, m=m, chosen=chosen, seg=seg_table(r), yearly=E.yearly(r))
        print(f"\n== {key} WF: {E.fmt(m)}")
    print("   chosen(k1):", {k: v for k, v in result[name]["chosen"].items()})

# E: combination
combo = S.combine({k: result[k]["ret"] for k in ["A_超短动量_ens5", "B_趋势跟踪_ens5",
                                                "C_主题高波动_ens5"]},
                  mode="invvol", window=60)
# warm-up: first 60d weights unavailable -> use equal weight there already handled
mE = E.metrics(combo)
result["E_多策略拼装"] = dict(ret=combo, m=mE, chosen={}, seg=seg_table(combo), yearly=E.yearly(combo))
print(f"\n== E_多策略拼装: {E.fmt(mE)}")

# ---------------------------------------------------------------- report tables
rows = []
for name, d in result.items():
    m = d["m"]
    rows.append(dict(scheme=name, ann=m["ann"], mdd=m["mdd"], sharpe=m["sharpe"],
                     calmar=m["calmar"], vol=m["vol"], total=m["total"]))
tab = pd.DataFrame(rows)
tab.to_csv(OUT / "stage4_summary.csv", index=False)
print("\n=== WF summary ===")
print(tab.assign(ann=lambda d: (d.ann * 100).round(2), mdd=lambda d: (d.mdd * 100).round(1),
                 sharpe=lambda d: d.sharpe.round(2), calmar=lambda d: d.calmar.round(2),
                 vol=lambda d: (d.vol * 100).round(1), total=lambda d: (d.total * 100).round(1)
                 ).to_string(index=False))

print("\n=== segments (ann%) ===")
seg = pd.DataFrame({k: v["seg"].set_index("seg")["ann"] for k, v in result.items()}).mul(100).round(2)
print(seg.to_string())
seg.to_csv(OUT / "stage4_segments.csv")

print("\n=== yearly (%) ===")
yr = pd.DataFrame({k: v["yearly"] for k, v in result.items()}).mul(100).round(2)
print(yr.to_string())
yr.to_csv(OUT / "stage4_yearly.csv")

# cost sensitivity
costs = [0.0005, 0.001, 0.0015, 0.002, 0.003]
cs = {}
for name in SCHEMES:
    row = {}
    for c in costs:
        r, _ = wf(name, cost=c)
        row[c] = E.metrics(r)["ann"]
    row5 = {}
    for c in costs:
        r, _ = wf(name, cost=c, k=5)
        row5[c] = E.metrics(r)["ann"]
    cs[name] = row
    cs[name + "_ens5"] = row5
    continue
    cs[name] = row
csdf = pd.DataFrame(cs).mul(100).round(2)
print("\n=== cost sensitivity (WF ann %) ===")
print(csdf.to_string())
csdf.to_csv(OUT / "stage4_cost.csv")

# ---------------------------------------------------------------- DSR
dsr = {}
sr_pool = pd.DataFrame(trial_rows).sr_daily.dropna().values
for name, d in result.items():
    r = d["ret"].dropna()
    sr_d = r.mean() / r.std()
    skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
    kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4)
    dsr[name] = E.deflated_sharpe(sr_d, n_trials=total_cfgs, n_obs=len(r), skew=skew, kurt=kurt,
                                  sr_variance=float(np.var(sr_pool)))
    dsr[name].update(ann_sharpe=float(d["m"]["sharpe"]))
print("\n=== Deflated Sharpe (N=%d) ===" % total_cfgs)
print(pd.DataFrame(dsr).T.round(4).to_string())

# ---------------------------------------------------------------- capacity
cap = {}
for name, d in result.items():
    if name not in store:
        continue
    lst = store[name]
    picks_all = []
    for e in lst:
        for dt, sel in e["picks"].items():
            if sel:
                picks_all.append(np.mean([amount.at[dt, s] for s in sel if s in amount.columns]))
    if picks_all:
        cap[name] = dict(median_amt=float(np.median(picks_all)),
                         p10_amt=float(np.percentile(picks_all, 10)),
                         capacity_p10=float(np.percentile(picks_all, 10) * 0.10),
                         capacity_med=float(np.median(picks_all) * 0.10))
print("\n=== capacity (10% participation, 1 day) ===")
print(pd.DataFrame(cap).T.to_string())

# ---------------------------------------------------------------- fixed-rule reference
fix = [e for e in store["B_趋势跟踪"] if str(e["cfg"]) == str({'pool': 'commod', 'topn': 1, 'lb': 20, 'rebal': 60, 'absmom': True})]
if fix:
    rf_ = fix[0]
    mf = E.metrics(rf_["ret"])
    print("\n=== fixed rule: commodity top1 lb20 rebal60 (no per-year re-selection) ===")
    print(E.fmt(mf))
    print("yearly:", (E.yearly(rf_["ret"]) * 100).round(1).to_dict())
    fixout = dict(m=E.fmt(mf), yearly={str(k): float(v) for k, v in E.yearly(rf_["ret"]).items()},
                  seg=seg_table(rf_["ret"]).to_dict("records"))
else:
    fixout = {}

# ---------------------------------------------------------------- IS best per scheme
isb = {}
for name, lst in store.items():
    best = max(lst, key=lambda e: E.metrics(e["ret"])["ann"])
    mb = E.metrics(best["ret"])
    isb[name] = dict(cfg=str(best["cfg"]), ann=mb["ann"], mdd=mb["mdd"], sharpe=mb["sharpe"])
print("\n=== in-sample best per scheme (hindsight) ===")
print(pd.DataFrame(isb).T.to_string())
for name, lst in store.items():
    best = max(lst, key=lambda e: E.metrics(e["ret"])["ann"])
    print(f"\n-- IS best {name} {best['cfg']}")
    print("   yearly:", (E.yearly(best["ret"]) * 100).round(1).to_dict())
    print("   seg:", seg_table(best["ret"])[["seg", "ann"]].assign(ann=lambda d: (d.ann * 100).round(1)).to_dict("records"))

# ---------------------------------------------------------------- leverage
rf_cash = 0.02
lev = {}
for key in ["B_趋势跟踪_ens5", "B_趋势跟踪"]:
    m = result[key]["m"]
    for L in (1.0, 1.25, 1.5, 2.0):
        annL = rf_cash + L * (m["ann"] - rf_cash)
        lev[f"{key}_x{L}"] = dict(ann=annL, mdd=m["mdd"] * L, vol=m["vol"] * L)
        print(f"{key} x{L}: ann={annL*100:.1f}% mdd={m['mdd']*L*100:.1f}%")
need = (0.25 - rf_cash) / (result["B_趋势跟踪_ens5"]["m"]["ann"] - rf_cash)
print(f"leverage needed for 25% from WF ens5: {need:.2f}x")

# ---------------------------------------------------------------- save
pd.DataFrame(trial_rows).to_csv(OUT / "stage4_trials.csv", index=False)
out = dict(summary=tab.to_dict("records"), fixed_rule=fixout, is_best=isb,
           leverage={k: {kk: float(vv) for kk, vv in v.items()} for k, v in lev.items()},
           dsr={k: {kk: float(vv) for kk, vv in v.items()} for k, v in dsr.items()},
           capacity=cap, total_cfgs=total_cfgs,
           chosen={k: {str(a): str(b) for a, b in v["chosen"].items()} for k, v in result.items()})
(OUT / "stage4_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
pd.DataFrame({k: v["ret"] for k, v in result.items()}).to_csv(OUT / "stage4_curves.csv")
print(f"\ndone {time.time()-t0:.0f}s")
