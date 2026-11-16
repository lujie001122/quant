"""Stage 3 exploration: search structurally different schemes, keep every trial on record."""
from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_data as G
import gl_engine as E
import gl_schemes as S

START = "2019-01-01"
u = pd.read_csv(G.DATA / "universe.csv")
meta = dict(zip(u.symbol, u.category))
syms = u.symbol.tolist()
data = G.load_all(syms)
S.set_data(data)
close = pd.DataFrame({k: v["close"] for k, v in data.items()}).sort_index().loc[START:]
amount = pd.DataFrame({k: (v["volume"] * 100 * v["close"]) for k, v in data.items()}).reindex(close.index)
print(f"panel {close.shape}", flush=True)

trials = []


def rec(family, cfg, res):
    m = E.metrics(res["ret"])
    if not m:
        return
    tr = res["turnover"]
    years = len(res["ret"]) / E.TRADING_DAYS
    ann_turn = float(tr.sum() / years)
    trials.append(dict(family=family, cfg=cfg, ann=m["ann"], mdd=m["mdd"],
                       sharpe=m["sharpe"], vol=m["vol"], calmar=m["calmar"],
                       turn=ann_turn))
    return m


t0 = time.time()
# ---- family A: short-term momentum on different pools
for pool, topn, lb, rb in itertools.product(
        ["all", "qdii_comm", "theme_ind", "commod", "qdii"], [1, 2, 3, 5], [5, 10, 20], [5, 20]):
    r = S.s_short_mom(close, amount, meta, pool=pool, topn=topn, lb=lb, rebal=rb)
    rec("A_mom", f"pool={pool},top{topn},lb{lb},r{rb}", r)

# ---- family B: trend following on cross-border / commodity
for pool, topn, lb, ma in itertools.product(
        ["qdii_comm", "commod", "qdii"], [2, 3], [10, 20], [20, 60]):
    r = S.s_trend(close, amount, meta, pool=pool, topn=topn, lb=lb, rebal=5, ma=ma)
    rec("B_trend", f"pool={pool},top{topn},lb{lb},ma{ma}", r)

# ---- family C: high-vol theme momentum
for topn, lb, vq in itertools.product([1, 2, 3], [5, 10, 20], [0.3, 0.5]):
    r = S.s_hivol_theme(close, amount, meta, topn=topn, lb=lb, rebal=5, vol_q=vq)
    rec("C_hivol", f"top{topn},lb{lb},vq{vq}", r)

# ---- family D: intraday structure
for topn, mode, rb in itertools.product([3, 5], ["strong", "revert"], [1, 2]):
    r = S.s_intraday(close, amount, meta, topn=topn, rebal=rb, mode=mode, ma=0)
    rec("D_intraday", f"top{topn},{mode},r{rb}", r)

t = pd.DataFrame(trials)
t.to_csv(G.DATA / "stage3_trials.csv", index=False)
print(f"{len(t)} trials in {time.time()-t0:.0f}s")
t["annp"] = (t.ann * 100).round(2)
t["mddp"] = (t.mdd * 100).round(1)
t["sh"] = t.sharpe.round(2)
t["tp"] = t.turn.round(1)
print("\n=== top 25 by ann ===")
print(t.nlargest(25, "ann")[["family", "cfg", "annp", "mddp", "sh", "tp"]].to_string(index=False))
print("\n=== top 10 by calmar ===")
print(t.nlargest(10, "calmar")[["family", "cfg", "annp", "mddp", "sh", "tp"]].to_string(index=False))
print("\n=== best per family ===")
print(t.loc[t.groupby("family").ann.idxmax()][["family", "cfg", "annp", "mddp", "sh"]].to_string(index=False))
