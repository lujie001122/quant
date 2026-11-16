"""Stage 2: which ETF category has actually produced 25%+ (2019-2026)?

For each category independently: equal-weight buy&hold, momentum rotation
(3/5/10/20d), trend-following. No cross-category mixing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_data as G
import gl_engine as E

pd.set_option("display.width", 200)
START = "2019-01-01"
SEGS = [("2019-21", "2019-01-01", "2021-12-31"),
        ("2022-23", "2022-01-01", "2023-12-31"),
        ("2024-26", "2024-01-01", "2026-11-13"),
        ("last9m", "2026-02-13", "2026-11-13")]

u = pd.read_csv(G.DATA / "universe_dedup.csv")
meta = dict(zip(u.symbol, u.category))
names = dict(zip(u.symbol, u.name))
syms = u.symbol.tolist()

print(f"loading {len(syms)} ETFs ...", flush=True)
data = G.load_all(syms)
close = pd.DataFrame({k: v["close"] for k, v in data.items()}).sort_index()
amount = pd.DataFrame({k: (v["volume"] * 100 * v["close"]) for k, v in data.items()}).sort_index()
close = close.loc[close.index >= START]
amount = amount.reindex(close.index)
print(f"panel {close.shape} {close.index[0].date()}..{close.index[-1].date()}", flush=True)

# liquidity as of each day (rolling, no look-ahead), listed>=60 bars
elig_base = E.eligible_mask(close, amount, min_days=60, min_amt=2e7)
print("avg eligible/day:", float(elig_base.sum(axis=1).mean()), flush=True)

CONFIGS = []
for lb in (3, 5, 10, 20):
    for rb in (1, 5, 20):
        CONFIGS.append((f"M{lb}/r{rb}", lb, rb))
TREND = [("T60/20", 60, 20), ("T120/20", 120, 20), ("T20/5", 20, 5)]

rows = []
curves = {}
for cat in ["宽基", "行业", "主题", "跨境QDII", "商品", "债券"]:
    elig = elig_base.copy()
    keep = [c for c in close.columns if meta.get(c) == cat]
    mask = pd.DataFrame(False, index=close.index, columns=close.columns)
    mask[keep] = True
    elig = elig & mask
    n_etf = int((elig.sum(axis=1) > 0).sum())
    if n_etf < 20:
        print(f"\n## {cat}: too few ETFs ({n_etf} days with >=1 eligible)")
        continue
    print(f"\n## {cat}  ({len(keep)} ETFs in pool, {n_etf} tradable days)", flush=True)

    # equal weight buy&hold
    res = E.run_equal_weight(close, elig, cost=0.001, rebal=21)
    m = E.metrics(res["ret"].loc[START:])
    rows.append(dict(cat=cat, strat="EQ-BH", **{k: m[k] for k in ("ann", "mdd", "sharpe")},
                     n=len(keep)))
    curves[(cat, "EQ-BH")] = res["ret"]

    for label, lb, rb in CONFIGS:
        sc = E.momentum(close, lb)
        r = E.run_rotation(close, elig, sc, topn=5, rebal=rb, cost=0.001)
        m = E.metrics(r["ret"])
        rows.append(dict(cat=cat, strat=label, **{k: m[k] for k in ("ann", "mdd", "sharpe")},
                         n=len(keep)))
        curves[(cat, label)] = r["ret"]

    for label, ma, lb in TREND:
        r = E.run_trend(close, elig, topn=5, rebal=5, cost=0.001, ma=ma, lookback=lb)
        m = E.metrics(r["ret"])
        rows.append(dict(cat=cat, strat=label, **{k: m[k] for k in ("ann", "mdd", "sharpe")},
                         n=len(keep)))
        curves[(cat, label)] = r["ret"]

t = pd.DataFrame(rows)
t["annp"] = (t.ann * 100).round(2)
t["mddp"] = (t.mdd * 100).round(1)
t["sharpe"] = t.sharpe.round(2)
piv = t.pivot_table(index="strat", columns="cat", values="annp")
t.to_csv(G.DATA / "stage2_category_dedup.csv", index=False)

print("\n================ 年化 % (2019-2026, cost 0.1%/side) ================")
print(piv.to_string())
print("\n================ 最大回撤 % ================")
print(t.pivot_table(index="strat", columns="cat", values="mddp").to_string())
print("\n================ 夏普 ================")
print(t.pivot_table(index="strat", columns="cat", values="sharpe").to_string())

best = t.sort_values("ann", ascending=False).head(12)
print("\n================ top12 by ann ================")
print(best[["cat", "strat", "annp", "mddp", "sharpe"]].to_string(index=False))

# save curves for later
pd.DataFrame(curves).to_csv(G.DATA / "stage2_curves_dedup.csv", index=False)
