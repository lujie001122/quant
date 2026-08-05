#!/usr/bin/env python3
"""
EvolvingSim 下单工具 v1.0
用法:
  python3 trade.py sync         同步持仓
  python3 trade.py buy CODE SHARES PRICE    买入
  python3 trade.py sell CODE SHARES PRICE   卖出
  python3 trade.py revoke CODE 买入/卖出    撤单
"""
import sys, os, json, time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PF_PATH = os.path.join(SCRIPT_DIR, 'portfolio.json')

from evolving.evolving import EvolvingSim


def sync():
    e = EvolvingSim()
    h = e.getHoldingShares()
    if not (isinstance(h, dict) and h.get('status')):
        print("❌ 连接失败")
        return
    acct = e.getAccountInfo()

    with open(PF_PATH) as f:
        pf = json.load(f)

    for row in h.get('data', []):
        if not row or len(row) < 12:
            continue
        code = row[0]
        shares_val = int(row[6])
        cost = float(row[10])
        if shares_val > 0:
            pf['positions'][code] = {
                'name': row[1], 'shares': shares_val, 'avg_cost': cost,
                'market_price': float(row[2]), 'market_value': float(row[11]),
                'pnl': float(row[3]),
                'pnl_pct': round(float(row[3]) / (cost * shares_val) * 100, 2) if cost > 0 else 0,
            }
        elif code in pf['positions']:
            del pf['positions'][code]

    if isinstance(acct, dict) and acct.get('status'):
        d = acct['data']
        pf['account'] = {
            'total_asset': float(d['总资产']),
            'cash': float(d['可用金额']),
            'market_value': float(d['总市值']),
        }
    pf['last_updated'] = time.strftime('%Y-%m-%d %H:%M')
    with open(PF_PATH, 'w') as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)
    print(f"✅ 同步: {len(pf['positions'])}只 | 总资产{pf['account']['total_asset']:.0f}")


def revoke_direction(code, direction):
    e = EvolvingSim()
    ent = e.getEntrust()
    if isinstance(ent, dict) and ent.get('data'):
        for row in ent['data']:
            if row and len(row) > 10 and row[2] == code and row[4] == direction and row[5] == '未成交':
                contract = row[10]
                if contract.strip():
                    r = e.revokeEntrust(contract)
                    print(f"  撤: {contract} {'OK' if r[0] else 'FAIL'}")


def do_buy(code, shares, price):
    e = EvolvingSim()
    r = e.buy(code, shares, price)
    if r[0]:
        print(f"✅ 买入 {code} {shares}股@{price} 合同:{r[1]}")
    else:
        print(f"❌ 买入失败: {r}")


def do_sell(code, shares, price):
    e = EvolvingSim()
    r = e.sell(code, shares, price)
    if r[0]:
        print(f"✅ 卖出 {code} {shares}股@{price} 合同:{r[1]}")
    else:
        print(f"❌ 卖出失败: {r}")


def do_revoke(code, direction):
    """注意: revokeEntrust 是 EvolvingSim 库本身的 bug，不可靠。
    建议手动在同花顺撤单，或等未成交自动过期。"""
    e = EvolvingSim()
    ent = e.getEntrust()
    count = 0
    if isinstance(ent, dict) and ent.get('data'):
        for row in ent['data']:
            if row and len(row) > 10 and row[2] == code and row[4] == direction and row[5] == '未成交':
                contract = row[10]
                if contract.strip():
                    try:
                        r = e.revokeEntrust(contract)
                        print(f"{'✅' if r[0] else '❌'} 撤: {contract}")
                        count += 1
                    except Exception as ex:
                        print(f"❌ 撤单失败(库bug): {ex}")
    if count == 0:
        print("无待撤委托或撤单失败")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: trade.py sync|buy|sell|revoke")
        print("  sync")
        print("  buy CODE SHARES PRICE")
        print("  sell CODE SHARES PRICE")
        print("  revoke CODE DIRECTION")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == 'sync':
        sync()
    elif cmd == 'buy':
        do_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
    elif cmd == 'sell':
        do_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
    elif cmd == 'revoke':
        do_revoke(sys.argv[2], sys.argv[3])
    else:
        print(f"未知: {cmd}")