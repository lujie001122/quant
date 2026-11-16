"""Stage 1: build the ETF universe (list -> qfq daily cache -> filter stats)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_data as G

t0 = time.time()
lst = G.get_etf_list()
print(f"ETF list: {len(lst)} rows  ({time.time()-t0:.1f}s)")
print(lst.head(3).to_string())

syms = lst["symbol"].tolist()
res = G.download_daily(syms, workers=6)
ok = sum(1 for n, s in res.values() if s in ("ok", "cached") and n > 0)
fail = [k for k, (n, s) in res.items() if n == 0]
print(f"daily cached: {ok}/{len(syms)}  failed={len(fail)}  ({time.time()-t0:.1f}s)")
if fail:
    (G.DATA / "failed_daily.txt").write_text("\n".join(fail), encoding="utf-8")

# ---------------------------------------------------------------- stats
rows = []
for _, r in lst.iterrows():
    sym, code, name = r["symbol"], str(r["code"]), r["name"]
    df = G.load_daily(sym)
    if df.empty:
        rows.append(dict(symbol=sym, code=code, name=name, category=G.classify(name),
                         bars=0, first=None, last=None))
        continue
    amt = (df["volume"] * 100.0 * df["close"])
    amt250 = amt.tail(250).mean()
    amt60 = amt.tail(60).mean()
    rows.append(dict(
        symbol=sym, code=code, name=name, category=G.classify(name),
        bars=len(df), first=df.index[0].date(), last=df.index[-1].date(),
        amt250=float(amt250), amt60=float(amt60),
        px=float(df["close"].iloc[-1]),
    ))

u = pd.DataFrame(rows).sort_values(["category", "amt250"], ascending=[True, False])
u["listed_years"] = u["first"].apply(
    lambda d: np.nan if pd.isna(d) else (pd.Timestamp("2026-11-16") - pd.Timestamp(d)).days / 365.25)
u.to_csv(G.DATA / "universe_stats.csv", index=False, encoding="utf-8")

print("\n=== counts by category (all listed) ===")
print(u.groupby("category").size().to_string())
print("\n=== with bars>0 ===")
v = u[u.bars > 0]
print(v.groupby("category").size().to_string())

f = v[(v.listed_years >= 2) & (v.amt250 > 2e7) & (~v.category.isin(["货币"]))]
print(f"\n=== PASS filters (>=2y listed, amt250>20M, no money ETF) : {len(f)} ===")
print(f.groupby("category").size().to_string())
f.to_csv(G.DATA / "universe.csv", index=False, encoding="utf-8")
print(f"\nfirst-date distribution:\n{v['first'].astype(str).str[:4].value_counts().sort_index().to_string()}")
print(f"\ntotal {time.time()-t0:.1f}s")
