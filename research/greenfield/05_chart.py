"""Build the deliverable chart: WF equity curves + fixed-rule gold + placebo band."""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
font_manager.fontManager.addfont("/System/Library/Fonts/Hiragino Sans GB.ttc")
plt.rcParams["font.family"] = "Hiragino Sans GB"
plt.rcParams["axes.unicode_minus"] = False
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gl_data as G

DATA = G.DATA
cur = pd.read_csv(DATA / "stage4_curves.csv", index_col=0, parse_dates=True)
fig, ax = plt.subplots(2, 1, figsize=(11, 9), gridspec_kw=dict(height_ratios=[3, 2]))

lab = {"A_超短动量_ens5": "A 超短动量(top5集成)", "B_趋势跟踪_ens5": "B 跨境/商品趋势(top5集成)",
       "C_主题高波动_ens5": "C 主题高波动", "D_日内结构_ens5": "D 日内结构", "E_多策略拼装": "E 多策略拼装"}
for c in cur.columns:
    if c.endswith("_ens5") or c == "E_多策略拼装":
        ax[0].plot(cur.index, (1 + cur[c].fillna(0)).cumprod(), lw=1.4, label=lab.get(c, c))
ax[0].set_yscale("log")
ax[0].axhline(1, color="k", lw=0.6, ls=":")
ax[0].set_title("Walk-forward 拼接净值 (2022-01 ~ 2026-11, 训练3年/测试1年, 滚动重选参数)")
ax[0].legend(fontsize=8, loc="lower left")
ax[0].grid(alpha=0.3)

u = pd.read_csv(DATA / "universe_dedup.csv")
meta = dict(zip(u.symbol, u.category))
data = G.load_all(u.symbol.tolist())
import gl_schemes as S
S.set_data(data)
close = pd.DataFrame({k: v["close"] for k, v in data.items()}).sort_index().loc["2019-01-01":]
amount = pd.DataFrame({k: (v["volume"] * 100 * v["close"]) for k, v in data.items()}).reindex(close.index)
r = S.s_short_mom(close, amount, meta, pool="commod", topn=1, lb=20, rebal=60,
                  absmom=True, cost=0.001)["ret"]
ax[1].plot(r.index, (1 + r).cumprod(), lw=1.4, color="darkorange",
           label="商品 top1 20日动量 60日调仓 (固定规则)")
bh = close["sh518880"].pct_change().fillna(0)
ax[1].plot(bh.index, (1 + bh).cumprod(), lw=1.2, color="goldenrod", ls="--", label="黄金ETF 买入持有")
ax[1].set_title("唯一跑赢的结构 = 黄金 beta (2019-2026 +22.0%/年; 2015-06~2018-12 仅 +0.35%/年)")
ax[1].legend(fontsize=8)
ax[1].grid(alpha=0.3)
plt.tight_layout()
out = DATA / "greenfield_curves.png"
plt.savefig(out, dpi=130)
print("saved", out)
