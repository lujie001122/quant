#!/usr/bin/env python3
"""网格搜索: weekly-rotation 模式参数优化"""
import subprocess
import sys
import itertools
import json
import os

# 搜索空间
PARAMS = {
    "rotation_interval": [5, 10, 15, 20],
    "init_pct": [0.30, 0.50, 0.80],
    "trail_pct": [0.07, 0.10, 0.12, 0.15],
    "lookback": [10, 15, 20, 30],
    "top_n": [2, 3, 5],
    "momentum": ["c2", "simple"],
    "trail": ["fixed", "e1", "e2"],
    "stop_loss": [0.05, 0.08, 0.12],
}

# 高优先级组合 (先跑最有希望的)
PRIORITY_COMBOS = [
    # 高仓位 + 快轮动 + 紧止损
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.10, "lookback": 10, "top_n": 3, "momentum": "c2", "trail": "fixed", "stop_loss": 0.08},
    {"rotation_interval": 5, "init_pct": 0.50, "trail_pct": 0.07, "lookback": 15, "top_n": 3, "momentum": "c2", "trail": "e1", "stop_loss": 0.08},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.12, "lookback": 20, "top_n": 2, "momentum": "c2", "trail": "fixed", "stop_loss": 0.05},
    {"rotation_interval": 10, "init_pct": 0.50, "trail_pct": 0.10, "lookback": 10, "top_n": 5, "momentum": "c2", "trail": "e1", "stop_loss": 0.08},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.15, "lookback": 15, "top_n": 2, "momentum": "c2", "trail": "e2", "stop_loss": 0.05},
    {"rotation_interval": 5, "init_pct": 0.50, "trail_pct": 0.10, "lookback": 20, "top_n": 3, "momentum": "c2", "trail": "fixed", "stop_loss": 0.05},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.07, "lookback": 10, "top_n": 5, "momentum": "simple", "trail": "e1", "stop_loss": 0.08},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.10, "lookback": 15, "top_n": 3, "momentum": "c2", "trail": "e2", "stop_loss": 0.08},
    {"rotation_interval": 10, "init_pct": 0.80, "trail_pct": 0.07, "lookback": 10, "top_n": 3, "momentum": "c2", "trail": "e1", "stop_loss": 0.05},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.12, "lookback": 15, "top_n": 5, "momentum": "c2", "trail": "fixed", "stop_loss": 0.12},
    # 更激进
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.10, "lookback": 10, "top_n": 2, "momentum": "c2", "trail": "e1", "stop_loss": 0.05},
    {"rotation_interval": 5, "init_pct": 0.80, "trail_pct": 0.15, "lookback": 10, "top_n": 3, "momentum": "c2", "trail": "e2", "stop_loss": 0.05},
    # 趋势入场
    {"rotation_interval": 5, "init_pct": 0.50, "trail_pct": 0.10, "lookback": 15, "top_n": 3, "momentum": "c2", "trail": "fixed", "stop_loss": 0.08},
]

RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grid_results.json")

def run_backtest(combo, idx, total):
    args = ["python3", "backtest_bt.py", "--concentrated", "--weekly-rotation"]
    for k, v in combo.items():
        if k == "momentum":
            args.append(f"--momentum={v}")
        elif k == "trail":
            args.append(f"--trail={v}")
        else:
            args.append(f"--{k.replace('_', '-')}={v}")

    label = f"[{idx+1}/{total}] " + " ".join(f"{k}={v}" for k, v in combo.items())
    print(f"\n{label}")
    
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=180)
        output = result.stdout + result.stderr
        
        # Parse results
        annual = None
        max_dd = None
        sharpe = None
        calmar = None
        win_rate = None
        trades = None
        pf = None
        
        for line in output.split("\n"):
            if "年化收益:" in line:
                try:
                    val = line.split("年化收益:")[-1].replace("%", "").replace("+", "").strip()
                    annual = float(val)
                except:
                    pass
            elif "最大回撤:" in line:
                try:
                    max_dd = float(line.split(":")[-1].replace("%", "").strip())
                except:
                    pass
            elif "夏普比率:" in line:
                try:
                    sharpe = float(line.split(":")[-1].strip())
                except:
                    pass
            elif "Calmar" in line:
                try:
                    calmar = float(line.split(":")[-1].strip())
                except:
                    pass
            elif "胜率:" in line:
                try:
                    win_rate = float(line.split(":")[-1].replace("%", "").strip())
                except:
                    pass
            elif "交易笔数:" in line:
                try:
                    trades = int(line.split(":")[-1].strip())
                except:
                    pass
            elif "盈亏比:" in line:
                try:
                    pf = float(line.split(":")[-1].strip())
                except:
                    pass
        
        result_entry = {
            "params": combo,
            "annual": annual,
            "max_dd": max_dd,
            "sharpe": sharpe,
            "calmar": calmar,
            "win_rate": win_rate,
            "trades": trades,
            "pf": pf,
        }
        
        status = f"年化={annual}% 回撤={max_dd}% Calmar={calmar} 夏普={sharpe} 胜率={win_rate}% 交易={trades} 盈亏比={pf}" if annual is not None else "解析失败"
        print(f"  → {status}")
        
        return result_entry
        
    except subprocess.TimeoutExpired:
        print(f"  → 超时")
        return {"params": combo, "error": "timeout"}
    except Exception as e:
        print(f"  → 错误: {e}")
        return {"params": combo, "error": str(e)}


def main():
    combos = PRIORITY_COMBOS
    total = len(combos)
    results = []
    
    print(f"═══ 网格搜索: {total} 组参数 ═══")
    
    # Load existing results if any
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            results = json.load(f)
        print(f"已加载 {len(results)} 条已有结果")
    
    for idx, combo in enumerate(combos):
        r = run_backtest(combo, idx, total)
        results.append(r)
        
        # Save incrementally
        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    
    # Summary
    valid = [r for r in results if "annual" in r and r["annual"] is not None]
    if valid:
        valid.sort(key=lambda x: x.get("annual", 0), reverse=True)
        print("\n" + "=" * 80)
        print("  TOP 10 按年化收益排序")
        print("=" * 80)
        for i, r in enumerate(valid[:10]):
            p = r["params"]
            print(f"  #{i+1}: 年化={r['annual']:+.2f}% 回撤={r['max_dd']:.2f}% Calmar={r.get('calmar',0):.2f} | "
                  f"ri={p.get('rotation_interval',5)} init={p.get('init_pct',0.3)} trail={p.get('trail_pct',0.12)} "
                  f"lb={p.get('lookback',20)} top={p.get('top_n',3)} mom={p.get('momentum','c2')} "
                  f"tmode={p.get('trail','fixed')} sl={p.get('stop_loss',0.08)}")


if __name__ == "__main__":
    main()
