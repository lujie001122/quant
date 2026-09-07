#!/usr/bin/env python3
"""第3轮优化: 围绕最优参数深度搜索"""
import subprocess
import json
from concurrent.futures import ProcessPoolExecutor, as_completed

# 最优: 年化34.57%, init=0.5 trail=0.07 lb=20 top=5 ri=3 TE DP simple e1 sl=0.08
# 探索方向:
# 1. 更短 ri (1,2) + TE
# 2. init_pct 微调 (0.4, 0.6, 0.7) + TE
# 3. trail_pct 微调 (0.05, 0.06, 0.08) + TE
# 4. lookback 微调 (18, 22, 25) + TE
# 5. top_n 微调 (4, 6, 7) + TE
# 6. rsi_entry_max 放宽 (60, 65) + TE
# 7. stop_loss 微调 (0.06, 0.10)
# 8. e2 trail mode vs e1
# 9. confirm_days=1

COMBOS = []
BASE = {"momentum": "simple", "trail": "e1", "rotation_interval": 3, "stop_loss": 0.08, "top_n": 5, "lookback": 20, "trend_entry": True}

# ri 微调
for ri in [1, 2, 3]:
    COMBOS.append({**BASE, "rotation_interval": ri, "init_pct": 0.5, "trail_pct": 0.07})

# init 微调
for init in [0.4, 0.5, 0.6, 0.7]:
    for trail in [0.05, 0.06, 0.07, 0.08]:
        COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail})

# lookback 微调
for lb in [15, 18, 20, 22, 25]:
    COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "lookback": lb})

# top_n 微调
for top in [3, 4, 5, 6, 7]:
    COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "top_n": top})

# rsi_entry_max
for rsi in [55, 60, 65, 70]:
    COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "rsi_entry_max": rsi})

# stop_loss 微调
for sl in [0.05, 0.06, 0.08, 0.10]:
    COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "stop_loss": sl})

# e2 vs e1
COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "trail": "e2"})
COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.10, "trail": "e2"})

# confirm_days
for cd in [1, 2]:
    COMBOS.append({**BASE, "init_pct": 0.5, "trail_pct": 0.07, "confirm_days": cd})

# 组合: 最佳参数探索
for init in [0.5, 0.6]:
    for trail in [0.05, 0.07]:
        for lb in [15, 20]:
            for top in [4, 5, 6]:
                for ri in [2, 3]:
                    COMBOS.append({**BASE, "init_pct": init, "trail_pct": trail, "lookback": lb, "top_n": top, "rotation_interval": ri})

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
        annual = max_dd = sharpe = calmar = None
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
        return {**combo, "annual": annual, "max_dd": max_dd, "sharpe": sharpe, "calmar": calmar}
    except:
        return {**combo, "annual": None, "max_dd": None}

def main():
    print(f"═══ 第3轮优化: {len(COMBOS)} 组 ═══")
    results = []
    
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, c): i for i, c in enumerate(COMBOS)}
        for f in as_completed(futures):
            r = f.result()
            idx = futures[f]
            if r["annual"] is not None:
                print(f"[{idx+1}/{len(COMBOS)}] 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% | init={r.get('init_pct',0.5)} trail={r.get('trail_pct',0.07)} lb={r.get('lookback',20)} top={r.get('top_n',5)} ri={r.get('rotation_interval',3)} sl={r.get('stop_loss',0.08)} tmode={r.get('trail','e1')}")
            else:
                print(f"[{idx+1}/{len(COMBOS)}] FAILED")
            results.append(r)
    
    valid = [r for r in results if r["annual"] is not None and r["annual"] > 0]
    valid.sort(key=lambda x: x["annual"], reverse=True)
    
    print(f"\n{'='*80}")
    print("  TOP 15 按年化收益排序")
    print(f"{'='*80}")
    for i, r in enumerate(valid[:15]):
        print(f"  #{i+1}: 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} 夏普={r.get('sharpe',0):.2f}")
        print(f"       init={r.get('init_pct',0.5)} trail={r.get('trail_pct',0.07)} lb={r.get('lookback',20)} top={r.get('top_n',5)} ri={r.get('rotation_interval',3)} sl={r.get('stop_loss',0.08)} tmode={r.get('trail','e1')} rsi={r.get('rsi_entry_max','def')} cd={r.get('confirm_days','def')}")

    with open("/Users/lujie/Documents/code/quant/grid_results_v4.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
