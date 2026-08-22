#!/usr/bin/env python3
"""批量跑回测，逐个区间，不设超时"""
import yaml, subprocess, sys

conf_path = 'config.yaml'
with open(conf_path) as f:
    conf = yaml.safe_load(f)
orig_start = conf['backtest']['trade_start']
orig_end = conf['backtest']['trade_end']

# 从短到长跑
periods = [
    ('3个月', '2026-05-22', '2026-08-22'),
    ('6个月', '2026-02-22', '2026-08-22'),
    ('9个月', '2025-11-22', '2026-08-22'),
    ('1年', '2025-08-22', '2026-08-22'),
]

results = []
for label, start, end in periods:
    conf['backtest']['trade_start'] = start
    conf['backtest']['trade_end'] = end
    with open(conf_path, 'w') as f:
        yaml.dump(conf, f, allow_unicode=True, default_flow_style=False)

    print(f'\n>>> 回测: {label} ({start} ~ {end}) ...', flush=True)
    result = subprocess.run(
        [sys.executable, 'backtest_bt.py'],
        capture_output=True, text=True
    )
    output = result.stdout
    metrics = {}
    for line in output.split('\n'):
        for k in ['初始资金','最终资金','总收益','年化收益','最大回撤','夏普比率','交易笔数','胜率','盈亏比','Calmar']:
            if k in line:
                metrics[k.strip()] = line.strip()
    results.append((label, metrics))
    print(f'  完成 (exit={result.returncode})', flush=True)
    if result.returncode != 0:
        print(f'  stderr: {result.stderr[-500:]}', flush=True)

# 恢复
conf['backtest']['trade_start'] = orig_start
conf['backtest']['trade_end'] = orig_end
with open(conf_path, 'w') as f:
    yaml.dump(conf, f, allow_unicode=True, default_flow_style=False)

print('\n' + '='*70)
print('  回测结果汇总')
print('='*70)
for label, m in results:
    print(f'\n  {label}:')
    for k, v in m.items():
        print(f'    {v}')
print(f'\n✅ config.yaml 已恢复')
