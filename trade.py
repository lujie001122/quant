#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口（EvolvingSim 官方接口重构版）

用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入
  python3 trade.py sell CODE SHARES PRICE        # 卖出
  python3 trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]  # 做T买入配对：买入+高位卖出挂单
  python3 trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE] # 做T卖出配对：卖出+低位买入挂单

铁律:
  1. 只用 EvolvingSim 公开接口: getHoldingShares/getAccountInfo/buy/sell/getEntrust/revokeEntrust
  2. 数量必须是 100 整数倍，且不低于 5000 股
  3. 调 EvolvingSim 前先 open -a 同花顺 + sleep 1，调完后 sleep 2
  4. 不用子进程 subprocess 包装 EvolvingSim
  5. 不调底层私有函数（issuingEntrust 等）
  6. AI Agent 只能通过 python3 trade.py 执行买卖，不得直接调 EvolvingSim
"""
import sys
import os
import json
import time
import subprocess
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PF_PATH = os.path.join(SCRIPT_DIR, 'portfolio.json')

from evolving.evolving import EvolvingSim
# state_center 统一提供状态读写接口
from state_center import get_code_map

import yaml as _yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
try:
    with open(_CONFIG_PATH) as _f:
        _CONF = _yaml.safe_load(_f) or {}
except Exception:
    _CONF = {}


CODE_MAP = get_code_map()  # {code: {"name": str, "sid": str}}


# ─── EvolvingSim 调用 ──────────────────────────────────────────────────

def _call_evolving(method_name, *args, e=None):
    """直接调用 EvolvingSim 方法。可复用传入的 EvolvingSim 实例。"""
    _e = e or EvolvingSim()
    try:
        method = getattr(_e, method_name)
        return method(*args)
    except Exception as ex:
        print(f"  ⚠️ EvolvingSim.{method_name} 异常: {ex}")
        return None
    finally:
        if e is None:
            time.sleep(2)


# ─── 数量校验 ──────────────────────────────────────────────────────────

def _validate_shares(shares):
    """校验数量是 100 的整数倍，且不低于最小股数(从config读取)。不足自动修正。"""
    _min_shares = _CONF.get("trade", {}).get("min_shares", 5000)
    if shares < _min_shares:
        print(f"⚠️ 数量 {shares} 低于 {_min_shares} 股下限，自动修正为 {_min_shares}")
        shares = _min_shares
    if shares % 100 != 0:
        print(f"❌ 数量 {shares} 不是 100 的整数倍")
        sys.exit(1)
    return shares


# ─── sync ──────────────────────────────────────────────────────────────

def sync():
    """同步持仓到 portfolio.json"""
    h = _call_evolving('getHoldingShares')
    acct = _call_evolving('getAccountInfo')

    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
    else:
        pf = {'positions': {}, 'account': {}}

    code_map = get_code_map()

    if isinstance(h, dict) and h.get('status'):
        for row in h.get('data', []):
            if not row or len(row) < 12:
                continue
            code = row[0]
            try:
                shares_val = int(row[6])
            except (ValueError, TypeError):
                shares_val = 0
            try:
                cost = float(row[10])
            except (ValueError, TypeError):
                cost = 0.0
            if shares_val > 0:
                cur_price = float(row[2])
                name = code_map.get(code, {}).get('name') or row[1]
                # 从 _signal_state 读取 base_price，回退到 avg_cost
                signal_state = pf.get('_signal_state', {}).get(code, {})
                base_price = signal_state.get('base_price') or cost
                pf['positions'][code] = {
                    'name': name,
                    'shares': shares_val,
                    'avg_cost': cost,
                    'current_price': cur_price,
                    'market_value': float(row[11]),
                    'pnl': float(row[3]),  # 仅展示用
                    'pnl_pct': round(float(row[3]) / (cost * shares_val) * 100, 2) if cost > 0 else 0,  # 仅展示用
                    'base_price': base_price,
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


# ─── 连续竞价判断 ──────────────────────────────────────────────────────

def _is_continuous_auction():
    """连续竞价时段: 9:30-11:30, 13:00-15:00"""
    now = time.localtime()
    t = now.tm_hour * 60 + now.tm_min
    return (570 <= t <= 690) or (780 <= t <= 900)


# ─── 统一买卖做T入口 ───────────────────────────────────────────────────

def _unified_trade(action, code, shares, price, pair_price=None):
    """统一买卖做T流程。"""
    shares = _validate_shares(shares)
    is_t0 = action in ('t0_buy', 't0_sell')
    amount = shares * price

    # ETF名称
    name = CODE_MAP.get(code, {}).get('name', code)
    if not name or name == code:
        try:
            with open(PF_PATH) as f:
                pf = json.load(f)
            name = pf.get('positions', {}).get(code, {}).get('name', code)
        except Exception:
            pass

    e = EvolvingSim()
    # v2: 统一使用 state_center 的订单管理接口
    from state_center import sync_entrust_to_orders, write_intent, write_intent_failed, write_order
    sync_entrust_to_orders(e=e)
    direction = '买入' if 'buy' in action else '卖出'
    write_intent(code, action, direction)

    if is_t0 and not _is_continuous_auction():
        print(f"❌ 非连续竞价时段，不执行挂单")
        write_intent_failed(code, action, shares, price, reason='非连续竞价时段')
        return False

    if action in ('buy', 't0_buy'):
        method, pair_method, pair_action = 'buy', 'sell', 't0_sell'
    else:
        method, pair_method, pair_action = 'sell', 'buy', 't0_buy'

    # 下单
    time.sleep(2)
    result = _call_evolving(method, code, shares, price, e=e)

    if is_t0:
        spread = (pair_price - price) / price * 100 if action == 't0_buy' else (price - pair_price) / price * 100
        print(f"🔁 做T{'买入' if 'buy' in action else '卖出'} {name}({code}) {shares:,}股 @{price} → 挂{'卖' if 'buy' in action else '买'}@{pair_price}  价差{spread:+.1f}%  金额¥{amount:,.0f}")
    elif action == 'buy':
        print(f"📊 买入 {name}({code}) {shares:,}股 @{price}  金额¥{amount:,.0f}")
    else:
        print(f"📊 卖出 {name}({code}) {shares:,}股 @{price}  金额¥{amount:,.0f}")

    if result is None:
        try:
            from evolving import ascmds
            raw = os.popen(ascmds.asissuingEntrustSim + ' ' + method + ' stock ' + code + ' ' + str(price) + ' ' + str(shares)).read().strip()
            if raw and 'Begin failed' in raw:
                print(f"  🔄 Begin failed，重启同花顺...")
                os.system("pkill -9 -x 同花顺")
                time.sleep(3)
                os.system("open -a /Applications/同花顺.app")
                time.sleep(5)
        except Exception:
            pass

    # Bug6: 双重写订单修复 — 统一由 state_center.write_order 管理
    write_order(code, action, shares, price, 'pending')

    if is_t0 and pair_price is not None:
        time.sleep(5)
        _call_evolving(pair_method, code, shares, pair_price, e=e)
        write_order(code, pair_action, shares, pair_price, 'pending')

    return True


# ─── buy / sell / t0_buy / t0_sell（缩为2-3行） ────────────────────────

def do_buy(code, shares, price):
    return _unified_trade('buy', code, shares, price)


def do_sell(code, shares, price):
    return _unified_trade('sell', code, shares, price)


def do_t0_buy(code, shares, price, pair_price=None):
    if pair_price is None:
        pair_price = round(price * 1.02, 3)
    return _unified_trade('t0_buy', code, shares, price, pair_price)


def do_t0_sell(code, shares, price, pair_price=None):
    if pair_price is None:
        pair_price = round(price * 0.98, 3)
    return _unified_trade('t0_sell', code, shares, price, pair_price)


# ─── main ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'sync':
        sync()

    elif cmd == 'buy':
        if len(sys.argv) != 5:
            print("用法: trade.py buy CODE SHARES PRICE")
            sys.exit(1)
        do_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == 'sell':
        if len(sys.argv) != 5:
            print("用法: trade.py sell CODE SHARES PRICE")
            sys.exit(1)
        do_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == 't0_buy':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        do_t0_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)

    elif cmd == 't0_sell':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        do_t0_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)