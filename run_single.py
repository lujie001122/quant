#!/usr/bin/env python3
"""Run a single backtest and extract key metrics"""
import subprocess, sys, re, json

def run_backtest(args, label, results_file="optimization_results.json"):
    """Run backtest with given args, extract metrics, append to results file"""
    cmd = f"python3 backtest_bt.py {args}"
    print(f"\n{'='*60}")
    print(f"  Running: {label}")
    print(f"  Command: {cmd}")
    print(f"{'='*60}")
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, 
                          cwd="/Users/lujie/Documents/code/quant", timeout=300)
    output = result.stdout + result.stderr
    
    # Extract metrics
    metrics = {}
    for line in output.split('\n'):
        line = line.strip()
        if '总收益:' in line:
            m = re.search(r'([+-]?\d+\.?\d*)%$', line)
            if m: metrics['total_return'] = float(m.group(1))
        elif '年化收益:' in line:
            m = re.search(r'([+-]?\d+\.?\d*)%$', line)
            if m: metrics['annual_return'] = float(m.group(1))
        elif '最大回撤:' in line:
            m = re.search(r'(\d+\.?\d*)%$', line)
            if m: metrics['max_drawdown'] = float(m.group(1))
        elif '夏普比率:' in line:
            m = re.search(r'([+-]?\d+\.?\d*)$', line)
            if m: metrics['sharpe'] = float(m.group(1))
        elif 'Calmar' in line and '比率' in line:
            m = re.search(r'([+-]?\d+\.?\d*)$', line)
            if m: metrics['calmar'] = float(m.group(1))
        elif '交易笔数:' in line:
            m = re.search(r'(\d+)$', line)
            if m: metrics['trades'] = int(m.group(1))
        elif '胜率:' in line and '盈亏比' not in line:
            m = re.search(r'(\d+\.?\d*)%$', line)
            if m: metrics['win_rate'] = float(m.group(1))
        elif '盈亏比:' in line:
            m = re.search(r'(\d+\.?\d*)$', line)
            if m: metrics['profit_factor'] = float(m.group(1))
    
    metrics['step'] = label
    metrics['args'] = args
    
    # Append to results file
    with open(f"/Users/lujie/Documents/code/quant/{results_file}", 'r') as f:
        data = json.load(f)
    data['steps'].append(metrics)
    with open(f"/Users/lujie/Documents/code/quant/{results_file}", 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"\n  → 总收益: {metrics.get('total_return', 'N/A')}%")
    print(f"  → 年化: {metrics.get('annual_return', 'N/A')}%")
    print(f"  → 回撤: {metrics.get('max_drawdown', 'N/A')}%")
    print(f"  → 夏普: {metrics.get('sharpe', 'N/A')}")
    
    return metrics

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python run_single.py <args> <label>")
        sys.exit(1)
    run_backtest(sys.argv[1], sys.argv[2])
