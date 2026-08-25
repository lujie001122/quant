#!/usr/bin/env python3
"""Run backtest with custom start/end, capture metrics"""
import sys, os, yaml, subprocess, json, re

def run_backtest(start, end, label):
    """Run backtest for a period and return metrics"""
    # Update config
    conf_path = 'config.yaml'
    with open(conf_path) as f:
        conf = yaml.safe_load(f)
    
    # Save original
    orig_start = conf['backtest']['trade_start']
    orig_end = conf['backtest']['trade_end']
    
    conf['backtest']['trade_start'] = start
    conf['backtest']['trade_end'] = end
    with open(conf_path, 'w') as f:
        yaml.dump(conf, f, allow_unicode=True, default_flow_style=False)
    
    print(f"\n>>> Running backtest: {label} ({start} ~ {end})", flush=True)
    result = subprocess.run(
        [sys.executable, 'backtest_bt.py'],
        capture_output=True, text=True, timeout=600
    )
    output = result.stdout
    
    # Parse metrics
    metrics = {}
    patterns = {
        'initial': r'初始资金.*?([\d,]+)',
        'final': r'最终资金.*?([\d,]+)',
        'total_return': r'总收益[：:]\s*([+-]?[\d.]+)%',
        'annual_return': r'年化收益[：:]\s*([+-]?[\d.]+)%',
        'max_drawdown': r'最大回撤[：:]\s*([\d.]+)%',
        'sharpe': r'夏普比率[：:]\s*([\d.]+)',
        'trades': r'交易笔数[：:]\s*(\d+)',
        'win_rate': r'胜率[：:]\s*([\d.]+)%',
        'profit_factor': r'盈亏比[：:]\s*([\d.]+)',
    }
    
    for key, pat in patterns.items():
        m = re.search(pat, output)
        if m:
            metrics[key] = m.group(1)
    
    # Restore config
    conf['backtest']['trade_start'] = orig_start
    conf['backtest']['trade_end'] = orig_end
    with open(conf_path, 'w') as f:
        yaml.dump(conf, f, allow_unicode=True, default_flow_style=False)
    
    print(f"  Metrics: {json.dumps(metrics, ensure_ascii=False)}", flush=True)
    if result.returncode != 0:
        print(f"  stderr: {result.stderr[-500:]}", flush=True)
    
    return metrics, output

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Run both periods
    results = {}
    
    # 2025 full year
    m1, o1 = run_backtest('2025-01-01', '2025-12-31', '2025全年')
    results['2025'] = m1
    
    # 2026 YTD
    m2, o2 = run_backtest('2026-01-01', '2026-08-22', '2026至今')
    results['2026'] = m2
    
    print("\n=== BASELINE RESULTS ===")
    print(json.dumps(results, indent=2, ensure_ascii=False))
