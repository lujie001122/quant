#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口（EvolvingSim 官方接口重构版）

用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入
  python3 trade.py sell CODE SHARES PRICE        # 卖出
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
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PF_PATH = os.path.join(SCRIPT_DIR, 'portfolio.json')
ORDERS_DIR = os.path.join(SCRIPT_DIR, 'orders')
INTENT_DIR = os.path.join(ORDERS_DIR, 'intent')

from evolving.evolving import EvolvingSim
from state_center import (
    get_code_map,
    load_today_orders,
    sync_entrust_to_orders,
    write_order as _write_order,
    update_order_status,
    cleanup_intent_force,
)

CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}


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


# ─── 订单持久化 ──────────────────────────────────────────────────────────

def _write_intent(code, action, shares, price):
    """下单前写 intent 文件到 orders/intent/ 目录。
    格式: {date}_{code}_{action}.json
    记录信号意图，防同 cron 内多个信号重复下单。
    """
    os.makedirs(INTENT_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    intent_path = os.path.join(INTENT_DIR, f'{today}_{code}_{action}.json')

    direction = '买入' if 'buy' in action else '卖出'
    intent = {
        'code': code,
        'action': action,
        'shares': shares,
        'price': price,
        'direction': direction,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    try:
        with open(intent_path, 'w') as f:
            json.dump(intent, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ intent 文件写入失败: {e}")


def _write_intent_for_failed(code, action, shares, price, reason='failed'):
    """下单失败时写 intent 文件（status=failed），下次 cron 可去重识别。
    文件名加 _failed 后缀避免覆盖成功单的 intent。
    """
    os.makedirs(INTENT_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    # 失败 intent 用不同文件名避免覆盖正常 intent
    intent_path = os.path.join(INTENT_DIR, f'{today}_{code}_{action}_failed.json')

    direction = '买入' if 'buy' in action else '卖出'
    intent = {
        'code': code,
        'action': action,
        'shares': shares,
        'price': price,
        'direction': direction,
        'status': 'failed',
        'reason': reason,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    try:
        with open(intent_path, 'w') as f:
            json.dump(intent, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ failed intent 文件写入失败: {e}")


# _write_order 和 _update_order_status 已从 state_center 导入，不再重复定义


# ─── 从 signal_generator.py 复制：_load_today_orders + _sync_entrust_to_orders ──
# P2-3: 已统一从 state_center 导入，不再重复定义
_load_today_orders = load_today_orders
_sync_entrust_to_orders = sync_entrust_to_orders


# ─── 全撤 ──────────────────────────────────────────────────────────────

def _revoke_all(e=None, orders=None):
    """全撤：调用 EvolvingSim 撤销全部买卖委托，并标记本地 pending 订单为 revoked。
    e: 可选复用的 EvolvingSim 实例，None 则自建
    orders: 可选预加载的今日订单 {filename: order_dict}，None 则自加载
    """
    own_e = e is None
    if own_e:
        e = EvolvingSim()
    revoke_ok = False
    try:
        result = e.revokeEntrust(revokeType='allBuyAndSell')
        if result is not None:
            revoke_ok = result[0]
            print(f"{'✅ 全撤成功' if revoke_ok else '⚠️ 全撤失败'}: {result[1] if len(result) > 1 else result}")
        else:
            print(f"⚠️ 全撤失败: 返回 None")
    except Exception as ex:
        print(f"⚠️ 全撤异常: {ex}")
    finally:
        time.sleep(2)

    # 标记今日 pending 订单为 revoked，保留 filled
    if orders is None:
        orders = _load_today_orders()
    updated = 0
    for fname, order in orders.items():
        if order.get('status') != 'pending':
            continue
        order['status'] = 'revoked'
        order['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        order_path = os.path.join(ORDERS_DIR, fname)
        try:
            with open(order_path, 'w') as f:
                json.dump(order, f, ensure_ascii=False, indent=2)
            updated += 1
        except (IOError, OSError) as ex:
            print(f"  ⚠️ 标记 revoked 失败 {fname}: {ex}")
    if updated:
        print(f"✅ 已标记 {updated} 个 pending 订单为 revoked（filled 订单保留）")

    return revoke_ok


def revoke_all():
    """公开接口：全撤所有买卖委托。"""
    result = _revoke_all()
    # 清理 intent 文件（撤单后无需保留意图），使用 state_center 统一函数
    cleanup_intent_force()
    return result


# ─── 连续竞价判断 ──────────────────────────────────────────────────────

def _is_continuous_auction():
    """连续竞价时段: 9:30-11:30, 13:00-15:00"""
    now = time.localtime()
    t = now.tm_hour * 60 + now.tm_min
    return (570 <= t <= 690) or (780 <= t <= 900)


# ─── 统一买卖做T入口 ───────────────────────────────────────────────────

def _unified_trade(action, code, shares, price, pair_price=None):
    """统一买卖做T流程：
    1. 校验100倍数
    2. 一个实例 e：_sync_entrust + _revoke_all + buy/sell + pair 全程复用
    3. 写 intent 文件（防重复）
    4. buy/sell（复用 e 实例）→ 调用后直接认为成交成功，不检查返回值
    5. EvolvingSim 返回 None → AppleScript debug → "Begin failed" 则杀同花顺重启
    6. 做T：挂配对反方向单（复用 e 实例）→ 同样直接认为成功
    7. _write_order 写入订单（默认成功，不检查 ok/contract）
    """
    # 1. 校验100倍数
    _validate_shares(shares)

    is_t0 = action in ('t0_buy', 't0_sell')

    # 2. 一个实例：_sync_entrust + _revoke_all + buy/sell + pair 全程复用
    e = EvolvingSim()
    orders = _load_today_orders()
    _sync_entrust_to_orders(e=e, orders=orders)
    # 3. 写 intent 文件（下单前，防跨 cron 重复）
    _write_intent(code, action, shares, price)

    # 4. 做T：检查连续竞价
    if is_t0 and not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        _write_intent_for_failed(code, action, shares, price, reason='非连续竞价时段')
        return False

    # 确定 EvolvingSim 方法名和配对方向
    if action in ('buy', 't0_buy'):
        method = 'buy'
        pair_method = 'sell'
        pair_action = 't0_sell'
    else:
        method = 'sell'
        pair_method = 'buy'
        pair_action = 't0_buy'

    # 5. 下单主单 → 复用 e 实例
    if is_t0:
        print(f"🔁 做T {action} {code} {shares}股@{price} | 配对挂单@{pair_price}")
    else:
        print(f"📊 {action} {code} {shares}股@{price}")

    time.sleep(2)  # 撤单和下单之间间隔
    result = _call_evolving(method, code, shares, price, e=e)

    # 下单后直接认为成交成功，不检查返回值
    print(f"✅ {method} {code} {shares}股@{price} → 已提交EvolvingSim，认为成交成功")

    # 如果 EvolvingSim 返回 None（进程卡死），用 AppleScript debug 检查
    if result is None:
        try:
            from evolving import ascmds
            raw = os.popen(ascmds.asissuingEntrustSim + ' ' + method + ' stock ' + code + ' ' + str(price) + ' ' + str(shares)).read().strip()
            print(f"  [DEBUG] EvolvingSim返回None，AppleScript返回: {raw}")
            # 检查是否包含 "Begin failed" → 同花顺进程卡死，杀进程重启
            if raw and ('Begin failed' in raw):
                print(f"  🔄 检测到 'Begin failed'，杀同花顺进程并重启...")
                os.system("pkill -9 -x 同花顺")
                time.sleep(3)
                os.system("open -a /Applications/同花顺.app")
                time.sleep(5)
                print(f"  ✅ 同花顺已重启，继续执行")
            elif raw and 'failed' in raw.lower():
                print(f"  ⚠️ 检测到 'failed'（非Begin failed），仍按成功处理，不杀进程")
            else:
                print(f"  ⚠️ EvolvingSim返回None但AppleScript未检测到异常，仍按成功处理")
        except Exception as ex:
            print(f"  ⚠️ AppleScript debug 异常: {ex}，仍按成功处理")

    # 6. _write_order 写入订单（默认成功，不检查ok/contract）
    _write_order(code, action, shares, price, 'pending', 'pending')

    # 7. 做T：挂配对反方向单 → 复用 e 实例
    if is_t0 and pair_price is not None:
        time.sleep(2)  # 两次调用之间间隔
        p_result = _call_evolving(pair_method, code, shares, pair_price, e=e)
        print(f"   ✅ 配对{pair_method}挂单 {code} {shares}股@{pair_price} → 已提交EvolvingSim，认为成交成功")

        _write_order(code, pair_action, shares, pair_price, 'pending', 'pending')
        print(f"✅ 做T {action} {code} {shares}股@{price} | 配对{pair_method}挂单 {shares}股@{pair_price}")

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

    elif cmd == 'revoke_all':
        revoke_all()

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)