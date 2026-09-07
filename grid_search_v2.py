#!/usr/bin/env python3
"""并行网格搜索: weekly-rotation 优化"""
import subprocess
import sys
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
import json

# 精简搜索空间: 基于已有最佳 (simple, e1, ri=5, sl=0.08)
COMBOS = []
for init in [0.50, 0.80, 1.00]:
    for trail in [0.05, 0.07, 0.10, 0.12]:
        for lb in [5, 8, 10, 15, 20]:
            for top in [3, 5]:
                COMBOS.append({
                    "init_pct": init, "trail_pct": trail, "lookback": lb, "top_n": top,
                    "momentum": "simple", "trail": "e1", "stop_loss": 0.08, "rotation_interval": 5
                })

# Also try c2 with best params from above
for init in [0.50, 0.80]:
    for trail in [0.07, 0.10, 0.12]:
        for lb in [15, 20]:
            for top in [2, 3]:
                COMBOS.append({
                    "init_pct": init, "trail_pct": trail, "lookback": lb, "top_n": top,
                    "momentum": "c2", "trail": "fixed", "stop_loss": 0.05, "rotation_interval": 5
                })

def run_one(combo):
    args = ["python3", "backtest_bt.py", "--concentrated", "--weekly-rotation"]
    for k, v in combo.items():
        if k == "momentum":
            args.append(f"--momentum={v}")
        elif k == "trail":
            args.append(f"--trail={v}")
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
    print(f"═══ 并行网格搜索: {len(COMBOS)} 组 ═══")
    results = []
    
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, c): i for i, c in enumerate(COMBOS)}
        for f in as_completed(futures):
            r = f.result()
            idx = futures[f]
            if r["annual"] is not None:
                print(f"[{idx+1}/{len(COMBOS)}] 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% | init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} mom={r['momentum']}")
            else:
                print(f"[{idx+1}/{len(COMBOS)}] FAILED | {r}")
            results.append(r)
    
    # Sort by annual return
    valid = [r for r in results if r["annual"] is not None and r["annual"] > 0]
    valid.sort(key=lambda x: x["annual"], reverse=True)
    
    print(f"\n{'='*80}")
    print("  TOP 10 按年化收益排序")
    print(f"{'='*80}")
    for i, r in enumerate(valid[:10]):
        print(f"  #{i+1}: 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} 夏普={r.get('sharpe',0):.2f} 胜率={r.get('win_rate',0):.1f}% 盈亏比={r.get('pf',0):.2f}")
        print(f"       init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} mom={r['momentum']} trail_mode={r['trail']} sl={r['stop_loss']}")

    # Save results
    with open("/Users/lujie/Documents/code/quant/grid_results_v2.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
