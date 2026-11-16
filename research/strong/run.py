#!/usr/bin/env python3
"""research/strong/run.py — 全套回测：两窗口 + 消融 + 参数邻域 + 样本内/外 + 成本敏感 + 基准。

用法:  .venv/bin/python3 research/strong/run.py
输出:  research/strong/results.json  +  stdout 报表
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import sdata          # noqa: E402
import strong_trend as st   # noqa: E402

WINDOWS = {"3y": ("2023-11-17", "2026-11-16"), "5y": ("2021-11-17", "2026-11-16")}
SPLITS = {"3y": (("2023-11-17", "2025-11-16"), ("2025-11-17", "2026-11-16")),
          "5y": (("2021-11-17", "2024-11-16"), ("2024-11-17", "2026-11-16"))}
OUT = {}


def nw_se(x, lag=4):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    xc = x - x.mean()
    s = xc @ xc / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1 - L / (lag + 1)
        s += 2 * w * (xc[L:] @ xc[:-L]) / n
    return math.sqrt(max(s, 0) / n)


def weekly(eq):
    return eq.resample("W-FRI").last().pct_change().dropna()


def run_one(px, cfg, start, end, mode="full", label="", init=None):
    bt = st.Backtest(px, cfg, mode=mode, label=label)
    res = bt.run(start, end, init=init)
    s = st.summarize(res, rf=float(cfg.get("rf", 0.02)))
    s["bad"] = st.audit(res, px)
    s["exposure"] = float(res["exposure"].mean())
    s["cash_days"] = float((res["exposure"] <= 1e-9).mean())
    s["fills"] = int(len(res["fills"])) if res["fills"] is not None else 0
    return s, res


def diag(px, res, bench_eq=None):
    """运气诊断：卖出择时质量 + 收益集中度 + 周超额 t 值。"""
    eq = res["equity"]
    f = res["fills"]
    out = {}
    close = px["close"]
    cal = list(px["cal"])
    idx = {d: i for i, d in enumerate(cal)}
    wi = wj = n = 0
    if f is not None and len(f):
        s = f[f.side == "sell"]
        for r in s.itertuples():
            i = idx.get(r.date)
            if i is None or r.code not in close.columns:
                continue
            px0 = float(r.price)
            for h in (10, 20):
                if i + h < len(cal):
                    v = float(close[r.code].iloc[i + h])
                    if not math.isnan(v):
                        if h == 10:
                            n += 1
                            wi += v > px0 * 1.02
                        else:
                            wj += v > px0 * 1.02
    out["exit_whipsaw_10d"] = (wi / n) if n else float("nan")
    out["exit_whipsaw_20d"] = (wj / n) if n else float("nan")
    w = weekly(eq)
    if len(w) > 12:
        tot = float((1 + w).prod() - 1)
        top = w.nlargest(10).sum()
        out["top10wk_share"] = float(top / tot) if abs(tot) > 1e-9 else float("nan")
        out["n_weeks"] = int(len(w))
    if bench_eq is not None:
        b = weekly(bench_eq).reindex(w.index).dropna()
        d = (w.reindex(b.index) - b).dropna()
        out["excess_ann"] = float(d.mean() * 52)
        se = nw_se(d.values, 4)
        out["excess_t"] = float(d.mean() / se) if se and se > 0 else float("nan")
        out["excess_hit"] = float((d > 0).mean())
    return out


def fmt(s, name):
    if s is None:
        return f"{name}: n/a"
    y = " ".join(f"{k}:{v*100:+.1f}%" for k, v in s["yearly"].items())
    return (f"{name:26s} ann={s['ann']*100:+7.2f}% tot={s['total']*100:+7.1f}% "
            f"vol={s['vol']*100:5.1f}% sh={s['sharpe']:5.2f} mdd={s['mdd']*100:7.2f}% "
            f"calmar={s['calmar']:5.2f} turn/wk={s['turnover_week']*100:5.1f}% "
            f"cost={s['cost_ann']*100:4.1f}%/y fills={s['fills']:4d} eps={s['n_eps']:4d} "
            f"win={s['win']*100:4.1f}% pl={s['pl']:4.2f} hold={s['hold']:4.1f}d "
            f"exp={s['exposure']*100:4.1f}% | {y}")


def main():
    px = sdata.load()
    cfg = st.load_config()
    results = {}
    for tag, (s0, s1) in WINDOWS.items():
        R = results[tag] = {}
        print(f"\n{'='*150}\n### {tag}  {s0} → {s1}")
        grid_off = st.with_overrides(cfg, grid={"enabled": False})
        variants = [
            ("base", cfg, "full"),
            ("no_grid", grid_off, "full"),
            ("regime_off", st.with_overrides(cfg, empty_when_weak=False), "full"),
            ("mom_only", cfg, "mom_only"),
            ("mom_only_no_grid", grid_off, "mom_only"),
            ("entry_only", cfg, "entry_only"),
            ("exit_only", cfg, "exit_only"),
            ("no_stop", st.with_overrides(cfg, stop_loss=0), "full"),
            ("cost_0.05%", st.with_overrides(cfg, cost=0.0005), "full"),
            ("cost_0.2%", st.with_overrides(cfg, cost=0.002), "full"),
            ("cost_0.3%", st.with_overrides(cfg, cost=0.003), "full"),
        ]
        for name, c, mode in variants:
            s, res = run_one(px, c, s0, s1, mode=mode, label=name)
            R[name] = s
            if s["bad"]:
                print(f"  !! AUDIT FAIL {name}: {s['bad']}")
            print(" ", fmt(s, name))
            if name == "base":
                base_res = res

        # 基准
        print("  --- benchmarks")
        b300 = st.bench_510300(px, s0, s1, cost=float(cfg["cost"]))
        B = {"510300_bh": b300,
             "same_pool_ew_bh": st.bench_ew(px, cfg, s0, s1, pool="same"),
             "all_ew_bh": st.bench_ew(px, cfg, s0, s1, pool="all"),
             "same_pool_ew_wk": st.bench_ew(px, cfg, s0, s1, pool="same", rebal=5)}
        R["bench"] = {}
        for k, eq in B.items():
            p = st.perf(eq, float(cfg["rf"]))
            p["yearly"] = st.yearly(eq)
            R["bench"][k] = p
            print(f"  {k:20s} ann={p['ann']*100:+7.2f}% tot={p['total']*100:+7.1f}% "
                  f"vol={p['vol']*100:5.1f}% sh={p['sharpe']:5.2f} mdd={p['mdd']*100:7.2f}% "
                  f"calmar={p['calmar']:5.2f}")
        R["diag"] = diag(px, base_res, B["same_pool_ew_wk"])
        R["diag_510300"] = diag(px, base_res, b300)
        print(f"  diag: whipsaw@10d={R['diag']['exit_whipsaw_10d']*100:.1f}% "
              f"@20d={R['diag']['exit_whipsaw_20d']*100:.1f}% "
              f"top10wk/share={R['diag']['top10wk_share']:.2f} "
              f"excess vs EWwk={R['diag']['excess_ann']*100:+.2f}%/y t={R['diag']['excess_t']:.2f} "
              f"hit={R['diag']['excess_hit']*100:.0f}%")

        # 样本内 / 样本外：同一本书按日期切片（无重启伪影）+ 冷启动重跑（对照）
        print("  --- IS / OOS")
        R["split"] = {}
        beq = base_res["equity"]
        for nm, (a, b) in (("IS", SPLITS[tag][0]), ("OOS", SPLITS[tag][1])):
            sub = beq[(beq.index >= pd.Timestamp(a)) & (beq.index <= pd.Timestamp(b))]
            p_ = st.perf(sub / sub.iloc[0] * float(cfg["_total_fund"]), float(cfg["rf"]))
            p_["yearly"] = st.yearly(sub / sub.iloc[0])
            p_["turnover_week"] = float("nan")
            p_["cost_ann"] = float("nan")
            p_["exposure"] = float(base_res["exposure"].reindex(sub.index).mean())
            p_["cash_days"] = float((base_res["exposure"].reindex(sub.index) <= 1e-9).mean())
            p_["n_eps"] = int(((base_res["episodes"]["start"] <= pd.Timestamp(b))
                               & (base_res["episodes"]["start"] >= pd.Timestamp(a))).sum())
            p_["win"] = float((base_res["episodes"]["pnl"] > 0)[
                (base_res["episodes"]["start"] >= pd.Timestamp(a))
                & (base_res["episodes"]["start"] <= pd.Timestamp(b))].mean())
            p_["fills"] = int(((base_res["fills"]["date"] >= pd.Timestamp(a))
                               & (base_res["fills"]["date"] <= pd.Timestamp(b))).sum())
            R["split"][nm] = dict(p_, window=[a, b], kind="sliced")
            print(f"  sliced {nm} {a}~{b}  ann={p_['ann']*100:+.2f}% tot={p_['total']*100:+.1f}% "
                  f"mdd={p_['mdd']*100:+.2f}% exp={p_['exposure']*100:.0f}% fills={p_['fills']}")
        for nm, (a, b) in (("IS_cold", SPLITS[tag][0]), ("OOS_cold", SPLITS[tag][1])):
            s, _ = run_one(px, cfg, a, b, label=f"{tag}_{nm}")
            R["split"][nm] = dict(s, window=[a, b], kind="cold")
            print(f"  cold   {nm} {a}~{b}  ann={s['ann']*100:+.2f}% tot={s['total']*100:+.1f}% "
                  f"mdd={s['mdd']*100:+.2f}% exp={s['exposure']*100:.0f}% fills={s['fills']}")

        # 参数邻域（±20%）
        print("  --- neighborhood (±20%)")
        R["hood"] = {}
        hood = {
            "top_pct_0.16": st.with_overrides(cfg, mom_top_pct=0.16),
            "top_pct_0.20": st.with_overrides(cfg, mom_top_pct=0.20),
            "top_pct_0.24": st.with_overrides(cfg, mom_top_pct=0.24),
            "ma_fast_16": st.with_overrides(cfg, ma_fast=16),
            "ma_fast_20": st.with_overrides(cfg, ma_fast=20),
            "ma_fast_24": st.with_overrides(cfg, ma_fast=24),
            "ma_slow_48": st.with_overrides(cfg, ma_slow=48),
            "ma_slow_60": st.with_overrides(cfg, ma_slow=60),
            "ma_slow_72": st.with_overrides(cfg, ma_slow=72),
            "grid_step_0.40": st.with_overrides(cfg, grid_step_atr=0.40),
            "grid_step_0.50": st.with_overrides(cfg, grid_step_atr=0.50),
            "grid_step_0.60": st.with_overrides(cfg, grid_step_atr=0.60),
            "top_n_4": st.with_overrides(cfg, top_n=4),
            "top_n_5": st.with_overrides(cfg, top_n=5),
            "top_n_6": st.with_overrides(cfg, top_n=6),
            "exit_confirm_1": st.with_overrides(cfg, exit_confirm=1),
            "exit_confirm_2": st.with_overrides(cfg, exit_confirm=2),
            "exit_confirm_3": st.with_overrides(cfg, exit_confirm=3),
        }
        for nm, c in hood.items():
            s, _ = run_one(px, c, s0, s1, label=nm)
            R["hood"][nm] = s
            print("  ", fmt(s, nm))
    json.dump({k: {kk: (vv if not isinstance(vv, dict) else vv) for kk, vv in v.items()}
               for k, v in results.items()}, open(os.path.join(HERE, "results.json"), "w"),
              ensure_ascii=False, indent=1, default=float)
    print("\nwrote", os.path.join(HERE, "results.json"))


if __name__ == "__main__":
    main()
