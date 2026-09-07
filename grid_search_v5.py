#!/usr/bin/env python3
"""第4轮优化: 围绕 40% 最优点深度搜索 (精简版)"""
import subprocess
import json
from concurrent.futures import ProcessPoolExecutor, as_completed

# 最优: 年化40.32%, init=0.6 trail=0.07 lb=15 top=6 ri=2 TE simple e1 sl=0.08
# 精简搜索: 每次只微调1-2个参数

COMBOS = []
BASE = {"momentum": "simple", "trail": "e1", "stop_loss": 0.08, "trend_entry": True}

# 方向1: init微调 (固定其他最优)
for init in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
    COMBOS.append({**BASE, "init_pct": init, "trail_pct": 0.07, "lookback": 15, "top_n": 6, "rotation_interval": 2})

# 方向2: trail微调
for trail in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]:
    COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": trail, "lookback": 15, "top_n": 6, "rotation_interval": 2})

# 方向3: lb微调
for lb in [10, 12, 14, 15, 16, 18, 20]:
    COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.07, "lookback": lb, "top_n": 6, "rotation_interval": 2})

# 方向4: top_n微调
for top in [4, 5, 6, 7, 8, 9, 10]:
    COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.07, "lookback": 15, "top_n": top, "rotation_interval": 2})

# 方向5: ri微调
for ri in [1, 2, 3, 4, 5]:
    COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.07, "lookback": 15, "top_n": 6, "rotation_interval": ri})

# 方向6: sl微调
for sl in [0.04, 0.05, 0.06, 0.08, 0.10, 0.12]:
    COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.07, "lookback": 15, "top_n": 6, "rotation_interval": 2, "stop_loss": sl})

# 方向7: 交叉组合 (最有可能突破50%的组合)
# 猜想: 更高仓位 + 更多持仓 + 更快轮动
COMBOS.append({**BASE, "init_pct": 0.70, "trail_pct": 0.05, "lookback": 15, "top_n": 8, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.80, "trail_pct": 0.05, "lookback": 15, "top_n": 8, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.65, "trail_pct": 0.06, "lookback": 15, "top_n": 7, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.60, "trail_pct": 0.05, "lookback": 12, "top_n": 7, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.55, "trail_pct": 0.05, "lookback": 15, "top_n": 7, "rotation_interval": 1})
COMBOS.append({**BASE, "init_pct": 0.70, "trail_pct": 0.07, "lookback": 15, "top_n": 7, "rotation_interval": 1})
COMBOS.append({**BASE, "init_pct": 0.60, "trail_pct": 0.07, "lookback": 15, "top_n": 6, "rotation_interval": 1})
COMBOS.append({**BASE, "init_pct": 0.60, "trail_pct": 0.05, "lookback": 10, "top_n": 6, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.60, "trail_pct": 0.05, "lookback": 10, "top_n": 8, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.55, "trail_pct": 0.07, "lookback": 12, "top_n": 7, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.65, "trail_pct": 0.05, "lookback": 15, "top_n": 8, "rotation_interval": 2})
COMBOS.append({**BASE, "init_pct": 0.65, "trail_pct": 0.07, "lookback": 12, "top_n": 7, "rotation_interval": 2})
# trail=e2 vs e1
COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.07, "lookback": 15, "top_n": 6, "rotation_interval": 2, "trail": "e2"})
COMBOS.append({**BASE, "init_pct": 0.6, "trail_pct": 0.05, "lookback": 15, "top_n": 6, "rotation_interval": 2, "trail": "e2"})

def run_one(combo):
    args = ["python3", "backtest_bt.py", "--concentrated", "--weekly-rotation", "--trend-entry"]
    for k, v in combo.items():
        if k in ("trend_entry", "dynamic_pct", "sector_diversify", "ma60_filter"):
            continue
        elif k == "momentum":
            args.append(f"--momentum={v}")
        elif k == "trail":
            args.append(f"--trail={v}")
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
    total = len(COMBOS)
    print(f"═══ 第4轮优化: {total} 组 ═══")
    results = []
    
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, c): i for i, c in enumerate(COMBOS)}
        for f in as_completed(futures):
            r = f.result()
            idx = futures[f]
            if r["annual"] is not None:
                print(f"[{idx+1}/{total}] 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} | init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} ri={r['rotation_interval']} sl={r.get('stop_loss',0.08)} tmode={r.get('trail','e1')}")
            else:
                print(f"[{idx+1}/{total}] FAILED")
            results.append(r)
    
    valid = [r for r in results if r["annual"] is not None and r["annual"] > 0]
    valid.sort(key=lambda x: x["annual"], reverse=True)
    
    print(f"\n{'='*80}")
    print("  TOP 20 按年化收益排序")
    print(f"{'='*80}")
    for i, r in enumerate(valid[:20]):
        print(f"  #{i+1}: 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} 夏普={r.get('sharpe',0):.2f}")
        print(f"       init={r['init_pct']} trail={r['trail_pct']} lb={r['lookback']} top={r['top_n']} ri={r['rotation_interval']} sl={r.get('stop_loss',0.08)} tmode={r.get('trail','e1')}")

    with open("/Users/lujie/Documents/code/quant/grid_results_v5.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
