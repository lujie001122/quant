#!/usr/bin/env python3
"""Run backtests for 2025 and 2026 and print summary metrics."""
import subprocess
import sys
import re
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

periods = {
    "2025": ("--start=2025-01-01", "--end=2025-12-31"),
    "2026": ("--start=2026-01-01", "--end=2026-07-27"),
}

results = {}
for label, (start, end) in periods.items():
    print(f"\n{'='*60}")
    print(f"  Running {label} backtest: {start} {end}")
    print(f"{'='*60}")
    cmd = [sys.executable, "backtest_bt.py", start, end]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = p.stdout + p.stderr
    
    # Parse metrics
    total_ret = re.search(r'总收益:\s+([+-]?\d+\.?\d*)%', output)
    max_dd = re.search(r'最大回撤:\s+(\d+\.?\d*)%', output)
    sharpe = re.search(r'夏普比率:\s+(\d+\.?\d*)', output)
    annual = re.search(r'年化收益:\s+([+-]?\d+\.?\d*)%', output)
    win_rate = re.search(r'胜率:\s+(\d+\.?\d*)%', output)
    trades = re.search(r'总交易:\s+(\d+)', output)
    
    r = {
        "total_ret": float(total_ret.group(1)) if total_ret else None,
        "max_dd": float(max_dd.group(1)) if max_dd else None,
        "sharpe": float(sharpe.group(1)) if sharpe else None,
        "annual": float(annual.group(1)) if annual else None,
        "win_rate": float(win_rate.group(1)) if win_rate else None,
        "trades": int(trades.group(1)) if trades else None,
    }
    results[label] = r
    print(f"\n  {label}: 收益={r['total_ret']}%, 回撤={r['max_dd']}%, 夏普={r['sharpe']}, 年化={r['annual']}%, 胜率={r['win_rate']}%, 交易={r['trades']}")

# Summary
print(f"\n{'='*60}")
print("  SUMMARY")
print(f"{'='*60}")
baseline = {
    "2025": {"total_ret": 26.39, "max_dd": 9.29, "sharpe": 1.35},
    "2026": {"total_ret": 28.56, "max_dd": 8.24, "sharpe": 2.16},
}
for label, r in results.items():
    bl = baseline[label]
    if r["total_ret"] is not None:
        diff = r["total_ret"] - bl["total_ret"]
        print(f"  {label}: 收益 {r['total_ret']:+.2f}% (基线 {bl['total_ret']:+.2f}%, 变动 {diff:+.2f}%)")
        print(f"         回撤 {r['max_dd']:.2f}% (基线 {bl['max_dd']:.2f}%)")
        print(f"         夏普 {r['sharpe']:.2f} (基线 {bl['sharpe']:.2f})")
        if abs(diff) > 2.0:
            print(f"  ⚠️  {label} 收益变动 {diff:+.2f}% 超过2%阈值!")