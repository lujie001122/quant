#!/usr/bin/env python3
"""E2 figures."""
import json, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2")
plt.rcParams["font.size"] = 9
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.25

st = json.load(open(os.path.join(OUT, "E2_stats_lag1.json")))
ck = json.load(open(os.path.join(OUT, "E2_checks.json")))

# 1. IC decay with CI
fig, ax = plt.subplots(1, 2, figsize=(9.2, 3.4))
hs = [int(h) for h in ck["decay"]]
ic = [ck["decay"][str(h)]["ic"] for h in hs]
lo = [ck["decay"][str(h)]["ci_lo"] for h in hs]
hi = [ck["decay"][str(h)]["ci_hi"] for h in hs]
ax[0].errorbar(hs, ic, yerr=[np.array(ic) - lo, np.array(hi) - ic], fmt="o-", color="#1f4e79", capsize=3)
ax[0].axhline(0, color="k", lw=0.8)
ax[0].set_xlabel("horizon (weeks)"); ax[0].set_ylabel("rank IC (f -> forward excess return)")
ax[0].set_title("A. signal decay (lag=1, 95% block-bootstrap CI)")

yr = st["ic_by_year"]["1"]
yy = [int(y) for y in yr]
vals = [yr[str(y)]["ic"] for y in yy]
ax[1].bar(yy, vals, color=["#c00000" if v < 0 else "#2e7d32" for v in vals])
ax[1].axhline(st["ic"]["1"]["ic"], color="#1f4e79", ls="--", lw=1, label=f"full-sample {st['ic']['1']['ic']:+.4f}")
ax[1].axhline(0, color="k", lw=0.8)
ax[1].set_title("B. h=1 IC by year"); ax[1].set_xlabel("year"); ax[1].legend(fontsize=7)
ax[1].tick_params(axis="x", rotation=45)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "e2_ic.png"), dpi=140); plt.close(fig)

# 2. quintiles gross/net for h=1 and h=4
fig, ax = plt.subplots(1, 2, figsize=(9.2, 3.4), sharey=True)
for k, h in enumerate(("1", "4")):
    q = st["quint"][h]
    xs = np.arange(5)
    g = [q["ann"][str(i)] for i in range(5)]
    n = [q["ann_net"][str(i)] for i in range(5)]
    ax[k].bar(xs - 0.2, g, 0.4, label="gross", color="#1f4e79")
    ax[k].bar(xs + 0.2, n, 0.4, label="net of 0.12%/side", color="#c00000")
    ax[k].axhline(q["ann_uni"], color="k", ls=":", lw=1, label=f"EW {q['ann_uni']:+.2%}")
    ax[k].set_xticks(xs); ax[k].set_xticklabels([f"Q{i+1}" for i in range(5)])
    ax[k].set_title(f"C. quintiles by f, hold {h}w (turnover {q['turnover']['0']:.0%}/wk)")
    ax[k].axhline(0, color="k", lw=0.8)
    if k == 0:
        ax[k].set_ylabel("annualised return")
        ax[k].legend(fontsize=7)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "e2_quintiles.png"), dpi=140); plt.close(fig)
print("figures written")
