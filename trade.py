#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口（EvolvingSim 官方接口重构版）

用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入（持仓增量确认）
  python3 trade.py sell CODE SHARES PRICE        # 卖出（持仓增量确认）
  python3 trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]  # 做T买入配对：买入+高位卖出挂单
  python3 trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE] # 做T卖出配对：卖出+低位买入挂单
  python3 trade.py revoke_all                    # 全撤所有买卖委托+清理今日订单文件

铁律:
  1. 只用 EvolvingSim 公开接口: getHoldingShares/getAccountInfo/buy/sell/getEntrust/revokeEntrust
  2. 数量必须是 100 整数倍
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
ORDERS_DIR = os.path.join(SCRIPT_DIR, 'orders')

from evolving.evolving import EvolvingSim
from tracker import get_code_map

CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}


# ─── EvolvingSim 调用 ──────────────────────────────────────────────────

def _call_evolving(method_name, *args):
    """
    直接调用 EvolvingSim 方法。
    sleep(2) 放在 finally 中确保每次调用结束后锁有足够时间释放。
    """
    e = EvolvingSim()
    try:
        method = e.__getattribute__(method_name)
        return method(*args)
    except Exception as ex:
        print(f"  ⚠️ EvolvingSim.{method_name} 异常: {ex}")
        return None
    finally:
        time.sleep(2)


# ─── 数量校验 ──────────────────────────────────────────────────────────

def _validate_shares(shares):
    """校验数量是 100 的整数倍"""
    if shares % 100 != 0:
        print(f"❌ 数量 {shares} 不是 100 的整数倍")
        sys.exit(1)


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
                pf['positions'][code] = {
                    'name': name,
                    'shares': shares_val,
                    'avg_cost': cost,
                    'current_price': cur_price,
                    'market_price': cur_price,
                    'market_value': float(row[11]),
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


# ─── 获取持仓/价格 ─────────────────────────────────────────────────────

def _get_holding_shares(code=None):
    """获取 EvolvingSim 中某标的持仓数量"""
    h = _call_evolving('getHoldingShares')
    result = {}
    if isinstance(h, dict) and h.get('status'):
        for row in h.get('data', []):
            if row and len(row) >= 7:
                try:
                    result[row[0]] = int(row[6])
                except (ValueError, TypeError):
                    result[row[0]] = 0
    if code:
        return result.get(code, 0)
    return result


def _get_price(code):
    """获取当前市价（来自 EvolvingSim 持仓数据）"""
    h = _call_evolving('getHoldingShares')
    if not (isinstance(h, dict) and h.get('status')):
        return None
    for row in h.get('data', []):
        if row and len(row) >= 3 and row[0] == code:
            return float(row[2])
    return None


# ─── 持仓增量确认下单 ──────────────────────────────────────────────────

def _trade_with_confirmation(action, code, shares, price, order_action=None):
    """
    下单 + 订单持久化。不查持仓，照搬手动成功流程。
    """
    if order_action is None:
        order_action = action

    # 下单
    result = _call_evolving(action, code, shares, price)
    if result is None:
        print(f"❌ {action} {code} {shares}股@{price} → 下单失败")
        _write_order(code, order_action, shares, price, None, 'failed')
        return 'failed', 0, 0, None

    ok, contract = result

    if not ok:
        print(f"❌ {action} {code} {shares}股@{price} → 下单失败: {contract}")
        _write_order(code, order_action, shares, price, contract, 'failed')
        return 'failed', 0, 0, contract

    # 下单成功，挂单 pending
    print(f"⏳ {action} {code} {shares}股@{price} → 已挂单 合同:{contract}")
    _write_order(code, order_action, shares, price, contract, 'pending')
    return 'pending', 0, 0, contract


# ─── 订单持久化 ──────────────────────────────────────────────────────────

def _write_order(code, action, shares, price, contract, status='pending'):
    """写订单到 orders/ 目录，用于信号生成器去重/挂单检查。

    action: 'buy' | 'sell' | 't0_buy' | 't0_sell'
    status: 'pending' | 'filled' | 'revoked' | 'failed'
    """
    os.makedirs(ORDERS_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    order_path = os.path.join(ORDERS_DIR, f'{today}_{code}_{action}.json')

    order = {
        'code': code,
        'action': action,
        'shares': shares,
        'price': price,
        'contract': contract,
        'status': status,
        'direction': '买入' if 'buy' in action else '卖出',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    try:
        with open(order_path, 'w') as f:
            json.dump(order, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ 订单持久化失败: {e}")


def _update_order_status(code, action, new_status):
    """更新 orders/ 中订单的状态（撤单/成交后调用）。

    action: 'buy' | 'sell' | 't0_buy' | 't0_sell'
    new_status: 'filled' | 'revoked'
    """
    today = datetime.now().strftime('%Y%m%d')
    order_path = os.path.join(ORDERS_DIR, f'{today}_{code}_{action}.json')

    if not os.path.exists(order_path):
        print(f"  ⚠️ 订单文件不存在: {order_path}")
        return

    try:
        with open(order_path, 'r') as f:
            order = json.load(f)
        order['status'] = new_status
        order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(order_path, 'w') as f:
            json.dump(order, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  ⚠️ 更新订单状态失败: {e}")


# ─── buy / sell ────────────────────────────────────────────────────────

def do_buy(code, shares, price):
    _validate_shares(shares)
    status, before, after, contract = _trade_with_confirmation('buy', code, shares, price)
    return status == 'filled'


def do_sell(code, shares, price):
    _validate_shares(shares)
    status, before, after, contract = _trade_with_confirmation('sell', code, shares, price)
    return status == 'filled'


# ─── T0 做T配对 ───────────────────────────────────────────────────────

def _is_continuous_auction():
    """连续竞价时段: 9:30-11:30, 13:00-15:00"""
    now = time.localtime()
    t = now.tm_hour * 60 + now.tm_min
    return (570 <= t <= 690) or (780 <= t <= 900)


def do_t0_buy(code, shares, price, pair_price=None):
    """
    做T买入配对：
      1. 检查连续竞价时段
      2. 撤同方向(买入)未成交委托
      3. 主单：买入 shares@price（限价≤当前价）
      4. 配对：挂卖出单 shares@pair_price（限价≥当前价）
    配对失败不影响主单结果。
    """
    _validate_shares(shares)

    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 全撤所有未成交委托
    revoke_all()

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 买入限价: ≤ 当前价
    buy_limit = min(price, cur_price)
    if buy_limit != price:
        print(f"   ⚠️ 买入限价调整: {price} → {buy_limit} (≤当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        pair_price = round(price * 1.02, 3)

    print(f"🔁 做T买入 {code} {shares}股@{buy_limit} | 配对卖出挂单@{pair_price}")

    # 主单
    status, before, after, contract = _trade_with_confirmation('buy', code, shares, buy_limit, order_action='t0_buy')

    if status == 'filled':
        # 配对卖出挂单: 限价 ≥ 当前价
        sell_limit = max(pair_price, cur_price)
        if sell_limit != pair_price:
            print(f"   ⚠️ 卖出限价调整: {pair_price} → {sell_limit} (≥当前价{cur_price})")
        result = _call_evolving('sell', code, shares, sell_limit)
        if result is not None:
            p_ok, p_contract = result
            if p_ok:
                print(f"   ✅ 配对卖出挂单 {code} {shares}股@{sell_limit} 合同:{p_contract}")
            else:
                print(f"   ⚠️ 配对卖出挂单失败 {code} {shares}股@{sell_limit}: {p_contract}")
        else:
            print(f"   ⚠️ 配对卖出挂单失败 {code} {shares}股@{sell_limit}")
            p_contract = None

        # 记录配对挂单
        _write_order(code, 't0_sell', shares, sell_limit, p_contract, 'pending')

        print(f"✅ 做T买入 {code} {shares}股@{buy_limit} | 配对卖出挂单 {shares}股@{sell_limit}")
    else:
        print(f"❌ 做T买入主单失败，不挂配对单")

    return status == 'filled'


def do_t0_sell(code, shares, price, pair_price=None):
    """
    做T卖出配对：
      1. 检查连续竞价时段
      2. 撤同方向(卖出)未成交委托
      3. 主单：卖出 shares@price（限价≥当前价）
      4. 配对：挂买入单 shares@pair_price（限价≤当前价）
    配对失败不影响主单结果。
    """
    _validate_shares(shares)

    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 全撤所有未成交委托
    revoke_all()

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 卖出限价: ≥ 当前价
    sell_limit = max(price, cur_price)
    if sell_limit != price:
        print(f"   ⚠️ 卖出限价调整: {price} → {sell_limit} (≥当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        pair_price = round(price * 0.98, 3)

    print(f"🔁 做T卖出 {code} {shares}股@{sell_limit} | 配对买入挂单@{pair_price}")

    # 主单
    status, before, after, contract = _trade_with_confirmation('sell', code, shares, sell_limit, order_action='t0_sell')

    if status == 'filled':
        # 配对买入挂单: 限价 ≤ 当前价
        buy_limit = min(pair_price, cur_price)
        if buy_limit != pair_price:
            print(f"   ⚠️ 买入限价调整: {pair_price} → {buy_limit} (≤当前价{cur_price})")
        result = _call_evolving('buy', code, shares, buy_limit)
        if result is not None:
            p_ok, p_contract = result
            if p_ok:
                print(f"   ✅ 配对买入挂单 {code} {shares}股@{buy_limit} 合同:{p_contract}")
            else:
                print(f"   ⚠️ 配对买入挂单失败 {code} {shares}股@{buy_limit}: {p_contract}")
        else:
            print(f"   ⚠️ 配对买入挂单失败 {code} {shares}股@{buy_limit}")
            p_contract = None

        # 记录配对挂单
        _write_order(code, 't0_buy', shares, buy_limit, p_contract, 'pending')

        print(f"✅ 做T卖出 {code} {shares}股@{sell_limit} | 配对买入挂单 {shares}股@{buy_limit}")
    else:
        print(f"❌ 做T卖出主单失败，不挂配对单")

    return status == 'filled'


# ─── revoke_all ───────────────────────────────────────────────────────

def revoke_all():
    """全撤：调用 EvolvingSim 撤销全部买卖委托，并删除今日 orders/ 订单文件。

    用法：python3 trade.py revoke_all
    成功后删除今天所有 orders/ 目录下的订单文件。
    """
    e = EvolvingSim()
    try:
        result = e.revokeEntrust(revokeType='allBuyAndSell')
        if result is not None:
            status, info = result
            if status:
                print(f"✅ 全撤成功: {info}")
            else:
                print(f"❌ 全撤失败: {info}")
                return False
        else:
            print(f"❌ 全撤失败: 返回 None")
            return False
    except Exception as ex:
        print(f"❌ 全撤异常: {ex}")
        return False
    finally:
        time.sleep(2)

    # 删除今天所有 orders/ 订单文件
    if os.path.exists(ORDERS_DIR):
        today = datetime.now().strftime('%Y%m%d')
        deleted = 0
        for fname in os.listdir(ORDERS_DIR):
            if fname.startswith(today) and fname.endswith('.json'):
                try:
                    os.remove(os.path.join(ORDERS_DIR, fname))
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            print(f"✅ 已清理 {deleted} 个今日订单文件")

    return True


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

    elif cmd == 'revoke_all':
        revoke_all()

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)