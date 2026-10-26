"""测试 _zero_share_count 逻辑（复刻 trade.py sync() 的 elif 分支，不依赖同花顺API）"""
import json, os, copy

PF_PATH = '/tmp/test_portfolio_zero.json'

def base_pf():
    return {
        'positions': {
            '159001': {'name': '深证ETF', 'shares': 1000, 'avg_cost': 1.0,
                        'current_price': 1.1, 'market_value': 1100, 'pnl': 100, 'pnl_pct': 10, 'base_price': 1.0},
        },
        'account': {},
    }

def apply_zero(pf, code, sold_volumes=None):
    """模拟 sync() 遇到 shares_val==0 时的处理逻辑"""
    sold_volumes = sold_volumes or {}
    old = pf['positions'][code].get('shares', 0)
    zero_counts = pf.setdefault('_zero_share_count', {})
    if old > 0 and sold_volumes.get(code, 0) >= old:
        del pf['positions'][code]
        zero_counts.pop(code, None)
        return ('deleted_immediate', f"当日委托证明已清仓，卖出量{sold_volumes.get(code,0)}>=原持仓{old}，立即删除")
    else:
        n = zero_counts.get(code, 0) + 1
        zero_counts[code] = n
        if n >= 3:
            del pf['positions'][code]
            zero_counts.pop(code, None)
            return ('deleted_count3', f"[SYNC] {code} 连续3次返回0股，判定已清仓，删除持仓")
        else:
            return ('kept', f"[SYNC] {code} 返回0股，第 {n} 次，保留原持仓 {old}股")

def apply_normal(pf, code, shares_val):
    """模拟 sync() 遇到 shares>0 时，清零计数"""
    zero_counts = pf.setdefault('_zero_share_count', {})
    zero_counts.pop(code, None)
    pf['positions'][code] = {'name': 'test', 'shares': shares_val, 'avg_cost': 1.0,
                             'current_price': 1.1, 'market_value': shares_val*1.1, 'pnl': 0, 'pnl_pct': 0, 'base_price': 1.0}

def persist(pf):
    with open(PF_PATH, 'w') as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)

def load():
    with open(PF_PATH) as f:
        return json.load(f)

def fresh():
    if os.path.exists(PF_PATH):
        os.remove(PF_PATH)
    pf = base_pf()
    persist(pf)
    return pf

print("=" * 60)
print("测试一: 连续 3 次 0 股 → 第3次删除")
print("=" * 60)
pf = fresh()
for i in range(1, 5):
    pf = load()
    res, msg = apply_zero(pf, '159001')
    persist(pf)
    print(f"  第{i}次: {res} | {msg}")
    if res.startswith('deleted'):
        print(f"  >>> 持仓已删除，positions={list(load()['positions'].keys())}")
        break

print()
print("=" * 60)
print("测试二: 中间夹一次正常值 → 计数归零")
print("=" * 60)
pf = fresh()
seq = ['zero', 'zero', 'normal', 'zero', 'zero', 'zero']
for i, action in enumerate(seq, 1):
    pf = load()
    if action == 'zero':
        res, msg = apply_zero(pf, '159001')
        persist(pf)
        print(f"  第{i}步(zero):  {res:16s} | {msg}")
    else:
        apply_normal(pf, '159001', 800)
        persist(pf)
        zc = load().get('_zero_share_count', {}).get('159001', '无')
        print(f"  第{i}步(normal): 返回800股，计数清零 (_zero_share_count={zc})")

print()
print("=" * 60)
print("测试三: 当日委托证明清仓 → 立即删除")
print("=" * 60)
pf = fresh()
pf = load()
res, msg = apply_zero(pf, '159001', sold_volumes={'159001': 1000})
persist(pf)
print(f"  sold=1000>=old=1000: {res} | {msg}")
print(f"  >>> positions={list(load()['positions'].keys())}, _zero_share_count={load().get('_zero_share_count', {})}")

# 清理
if os.path.exists(PF_PATH):
    os.remove(PF_PATH)
print()
print("三段测试完成。")
