#!/usr/bin/env python3
"""Run all 11 optimization steps sequentially and collect results"""
import subprocess, re, json, os, sys

PROJECT = "/Users/lujie/Documents/code/quant"
RESULTS_FILE = f"{PROJECT}/optimization_step_results.json"

def run_bt(args):
    """Run backtest and return metrics dict"""
    cmd = f"cd {PROJECT} && python3 backtest_bt.py {args} 2>&1"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    m = {}
    for line in out.split('\n'):
        line = line.strip()
        if '总收益:' in line and 'total_return' not in m:
            x = re.search(r'([+-]?\d+\.?\d*)%$', line)
            if x: m['total_return'] = float(x.group(1))
        elif '年化收益:' in line:
            x = re.search(r'([+-]?\d+\.?\d*)%$', line)
            if x: m['annual_return'] = float(x.group(1))
        elif '最大回撤:' in line:
            x = re.search(r'(\d+\.?\d*)%$', line)
            if x: m['max_drawdown'] = float(x.group(1))
        elif '夏普比率:' in line:
            x = re.search(r'([+-]?\d+\.?\d*)$', line)
            if x: m['sharpe'] = float(x.group(1))
        elif 'Calmar' in line and '比率' in line:
            x = re.search(r'([+-]?\d+\.?\d*)$', line)
            if x: m['calmar'] = float(x.group(1))
        elif '交易笔数:' in line:
            x = re.search(r'(\d+)$', line)
            if x: m['trades'] = int(x.group(1))
        elif '胜率:' in line and '盈亏比' not in line:
            x = re.search(r'(\d+\.?\d*)%$', line)
            if x: m['win_rate'] = float(x.group(1))
        elif '盈亏比:' in line:
            x = re.search(r'(\d+\.?\d*)$', line)
            if x: m['profit_factor'] = float(x.group(1))
    return m

if __name__ == "__main__":
    step_name = sys.argv[1]
    bt_args = sys.argv[2]
    
    print(f"Running step: {step_name}")
    print(f"Args: {bt_args}")
    m = run_bt(bt_args)
    m['step'] = step_name
    m['args'] = bt_args
    
    # Load existing results
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'r') as f:
            results = json.load(f)
    else:
        results = []
    
    results.append(m)
    
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"Result: 年化={m.get('annual_return','N/A')}% 回撤={m.get('max_drawdown','N/A')}% 夏普={m.get('sharpe','N/A')}")
