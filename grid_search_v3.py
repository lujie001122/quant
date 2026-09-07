#!/usr/bin/env python3
"""第2轮优化: 基于最优参数微调 + 新特征组合"""
import subprocess
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

# 基于第一轮最优: simple, e1, ri=5, sl=0.08, top=5, lb=15-20, init=0.5, trail=0.1
# 探索方向:
# 1. 趋势入场 (--trend-entry) 能否提升
# 2. 动态仓位 (--dynamic-pct) 能否提升
# 3. 更激进的 init_pct (0.3 低价, 留空间加仓)
# 4. 板块分散 (--sector-diversify)
# 5. 更短轮动间隔 ri=3
# 6. confirm_days=2 (减少换仓摩擦)
# 7. rsi_entry_max 更宽松 (60, 65)

COMBOS = []

# 基线: 最优参数组合 (已验证 ~30%)
BASE = {"momentum": "simple", "trail": "e1", "rotation_interval": 5, "stop_loss": 0.08, "top_n": 5, "lookback": 20}

# 方向1: trend-entry
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10, 0.12]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": True, "dynamic_pct": False, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})

# 方向2: dynamic-pct
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10, 0.12]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": False, "dynamic_pct": True, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})

# 方向3: trend-entry + dynamic-pct
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": True, "dynamic_pct": True, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})

# 方向4: sector-diversify
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": False, "dynamic_pct": False, "sector_diversify": True, "confirm_days": 0, "rsi_entry_max": 55})

# 方向5: confirm_days=2
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": False, "dynamic_pct": False, "sector_diversify": False, "confirm_days": 2, "rsi_entry_max": 55})

# 方向6: rsi_entry_max 更宽松
for rsi_max in [60, 65, 70]:
    COMBOS.append({**BASE, "init_pct": 0.50, "trail_pct": 0.10, "trend_entry": False, "dynamic_pct": False, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": rsi_max})

# 方向7: ri=3 更短轮动
BASE3 = {**BASE, "rotation_interval": 3}
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10]:
        COMBOS.append({**BASE3, "init_pct": init, "trail_pct": trail, "trend_entry": False, "dynamic_pct": False, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})
        COMBOS.append({**BASE3, "init_pct": init, "trail_pct": trail, "trend_entry": True, "dynamic_pct": True, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})

# 方向8: 更短lookback (5,8) + trend-entry
BASES = [{**BASE, "lookback": lb} for lb in [5, 8, 10]]
for b in BASES:
    for init in [0.30, 0.50]:
        COMBOS.append({**b, "init_pct": init, "trail_pct": 0.10, "trend_entry": True, "dynamic_pct": True, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55})

# 方向9: ma60-filter
for init in [0.30, 0.50]:
    for trail in [0.07, 0.10]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "trend_entry": False, "dynamic_pct": False, "sector_diversify": False, "confirm_days": 0, "rsi_entry_max": 55, "ma60_filter": True})

def run_one(combo):
    args = ["python3", "backtest_bt.py", "--concentrated", "--weekly-rotation"]
    bool_flags = {"trend_entry": "--trend-entry", "dynamic_pct": "--dynamic-pct", "sector_diversify": "--sector-diversify", "ma60_filter": "--ma60-filter"}
    
    for k, v in combo.items():
        if k in bool_flags:
            if v:
                args.append(bool_flags[k])
        elif k == "momentum":
            args.append(f"--momentum={v}")
        elif k == "trail":
            args.append(f"--trail={v}")
        elif k == "rsi_entry_max":
            args.append(f"--rsi-entry-max={v}")
        elif k == "confirm_days":
            args.append(f"--confirm-days={v}")
        else:
            args.append(f"--{k.replace('_', '-')}={v}")
    
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90,
                          cwd="/Users/lujie/Documents/code/quant")
        out = r.stdout + r.stderr
        annual = max_dd = sharpe = calmar = trades = win_rate = pf = None
        for line in out.split("\n"):
            if "年化收益:" in line:
                try: annual = float(line.split("年化收益:")[-1].replace("%","").replace("+","").strip())
                except: pass
            elif "最大回撤:" in line:
                try: max_dd = float(line.split("最大回撤:")[-1].replace("%","").strip())
                except: pass
            elif "夏普比率:" in line:
                try: sharpe = float(line.split("夏普比率:")[-1].strip())
                except: pass
            elif "Calmar" in line:
                try: calmar = float(line.split(":")[-1].strip())
                except: pass
            elif "交易笔数:" in line:
                try: trades = int(line.split(":")[-1].strip())
                except: pass
            elif "胜率:" in line and "总交易" not in line:
                try: win_rate = float(line.split(":")[-1].replace("%","").strip())
                except: pass
            elif "盈亏比:" in line:
                try: pf = float(line.split(":")[-1].strip())
                except: pass
        return {**combo, "annual": annual, "max_dd": max_dd, "sharpe": sharpe, "calmar": calmar, "trades": trades, "win_rate": win_rate, "pf": pf}
    except:
        return {**combo, "annual": None, "max_dd": None}

def main():
    print(f"═══ 第2轮优化: {len(COMBOS)} 组 ═══")
    results = []
    
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, c): i for i, c in enumerate(COMBOS)}
        for f in as_completed(futures):
            r = f.result()
            idx = futures[f]
            if r["annual"] is not None:
                extras = []
                if r.get("trend_entry"): extras.append("TE")
                if r.get("dynamic_pct"): extras.append("DP")
                if r.get("sector_diversify"): extras.append("SD")
                if r.get("confirm_days", 0) > 0: extras.append(f"CD={r['confirm_days']}")
                if r.get("ma60_filter"): extras.append("MA60")
                extra_str = " ".join(extras) if extras else ""
                print(f"[{idx+1}/{len(COMBOS)}] 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% | init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} ri={r['rotation_interval']} {extra_str}")
            else:
                print(f"[{idx+1}/{len(COMBOS)}] FAILED")
            results.append(r)
    
    valid = [r for r in results if r["annual"] is not None and r["annual"] > 0]
    valid.sort(key=lambda x: x["annual"], reverse=True)
    
    print(f"\n{'='*80}")
    print("  TOP 15 按年化收益排序")
    print(f"{'='*80}")
    for i, r in enumerate(valid[:15]):
        extras = []
        if r.get("trend_entry"): extras.append("TE")
        if r.get("dynamic_pct"): extras.append("DP")
        if r.get("sector_diversify"): extras.append("SD")
        if r.get("confirm_days", 0) > 0: extras.append(f"CD={r['confirm_days']}")
        if r.get("ma60_filter"): extras.append("MA60")
        if r.get("rsi_entry_max", 55) != 55: extras.append(f"RSI={r.get('rsi_entry_max',55)}")
        extra_str = " ".join(extras) if extras else "BASE"
        print(f"  #{i+1}: 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} 夏普={r.get('sharpe',0):.2f} 胜率={r.get('win_rate',0):.1f}% 盈亏比={r.get('pf',0):.2f}")
        print(f"       init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} ri={r['rotation_interval']} [{extra_str}]")

    with open("/Users/lujie/Documents/code/quant/grid_results_v3.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
