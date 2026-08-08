#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口
用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入（持仓增量确认）
  python3 trade.py sell CODE SHARES PRICE        # 卖出（持仓增量确认）
  python3 trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]  # 做T买入配对：买入+高位卖出挂单
  python3 trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE] # 做T卖出配对：卖出+低位买入挂单
  python3 trade.py revoke CODE 买入              # 撤同方向未成交委托
  python3 trade.py revoke CODE 卖出              # 撤同方向未成交委托

铁律:
  1. 下单前查持仓 → 下单 → 下单后查持仓确认增量（不查委托）
  2. 增量 = 目标 → 成功。增量 ≠ 目标 → 报错，不重试
  3. 每次新建 EvolvingSim 实例，不跨操作复用
  4. AI Agent 只能通过 python3 trade.py 执行买卖，不得直接调 EvolvingSim
  5. 买入限价 ≤ 当前价，卖出限价 ≥ 当前价
  6. 做T配对：先撤同方向未成交单，再挂主单+配对单
  7. 仅在连续竞价时段(9:30-11:30,13:00-15:00)执行挂单
  8. 调用 EvolvingSim 前自动清理 /tmp/lock.json
"""
import sys, os, json, time, urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PF_PATH = os.path.join(SCRIPT_DIR, 'portfolio.json')

from evolving.evolving import EvolvingSim
from tracker import get_code_map

CODE_MAP = {code: info["sid"] for code, info in get_code_map().items()}


# ─── 内部工具 ──────────────────────────────────────────────────────────

LOCK_PATH = '/tmp/lock.json'

def _clean_lock():
    """清理 EvolvingSim 的锁文件，避免异常退出后残留导致 15s 阻塞"""
    if os.path.exists(LOCK_PATH):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass

def _get_position(code):
    """返回 (shares, avg_cost) 或 (0, 0)"""
    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
        pos = pf.get('positions', {}).get(code, {})
        return pos.get('shares', 0), pos.get('avg_cost', 0.0)
    return 0, 0.0


def _activate_tonghuashun():
    """激活同花顺窗口，解决 AppleScript 底层间歇断连"""
    os.system("osascript -e 'tell application \"同花顺\" to activate'")


def _retry_get_holding_shares(max_retries=3, delay=2):
    """带重试的 getHoldingShares"""
    for attempt in range(1, max_retries + 1):
        _clean_lock()
        e = EvolvingSim()
        h = e.getHoldingShares()
        if isinstance(h, dict) and h.get('status'):
            return h
        if attempt < max_retries:
            print(f"⚠️ EvolvingSim 连接失败，{delay}秒后重试 ({attempt}/{max_retries})")
            time.sleep(delay)
    raise RuntimeError("❌ 连接同花顺失败，请确认同花顺在模拟交易界面")


def _get_holding_shares(code=None):
    """
    查 EvolvingSim 持仓（带重试+窗口激活）。
    返回 dict {code: shares} 或 (若 code 指定) 单只数量。
    """
    h = _retry_get_holding_shares()
    result = {}
    for row in h.get('data', []):
        if row and len(row) >= 7:
            result[row[0]] = int(row[6])
    if code:
        return result.get(code, 0)
    return result


def _get_price(code):
    """获取当前市价（来自 EvolvingSim 持仓数据）"""
    _clean_lock()
    e = EvolvingSim()
    h = e.getHoldingShares()
    if not (isinstance(h, dict) and h.get('status')):
        return None
    for row in h.get('data', []):
        if row and len(row) >= 3 and row[0] == code:
            return float(row[2])
    return None


# ─── sync ──────────────────────────────────────────────────────────────

def _retry_get_account_info(max_retries=3, delay=2):
    """带重试+窗口激活的 getAccountInfo"""
    for attempt in range(1, max_retries + 1):
        _activate_tonghuashun()
        time.sleep(0.5)
        _clean_lock()
        e = EvolvingSim()
        acct = e.getAccountInfo()
        if isinstance(acct, dict) and acct.get('status'):
            return acct
        if attempt < max_retries:
            print(f"⚠️ getAccountInfo 失败，{delay}秒后重试 ({attempt}/{max_retries})")
            time.sleep(delay)
    print("⚠️ 获取账户信息失败")
    return None


def sync():
    """同步持仓到 portfolio.json（带重试+窗口激活）"""
    h = _retry_get_holding_shares()
    acct = _retry_get_account_info()

    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
    else:
        pf = {'positions': {}, 'account': {}}

    for row in h.get('data', []):
        if not row or len(row) < 12:
            continue
        code = row[0]
        shares_val = int(row[6])
        cost = float(row[10])
        if shares_val > 0:
            cur_price = float(row[2])
            pf['positions'][code] = {
                'name': row[1],
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


# ─── 持仓增量确认下单 ────────────────────────────────────────────────

def _trade_with_confirmation(action, code, shares, price):
    """
    通用下单 + 持仓增量确认。
    action: 'buy' | 'sell'
    返回 (success, before_shares, after_shares)
    """
    # 1. 查当前持仓
    holding_map = _get_holding_shares()
    before = holding_map.get(code, 0)

    # 2. 下单（新实例）
    _clean_lock()
    e = EvolvingSim()
    if action == 'buy':
        ok, contract = e.buy(code, shares, price)
    else:
        ok, contract = e.sell(code, shares, price)

    if not ok:
        print(f"❌ {action} {code} {shares}股@{price} → 下单失败: {contract}")
        return False, before, before

    # 3. 等一等让成交生效
    time.sleep(1.5)

    # 4. 持仓增量确认
    holding_map2 = _get_holding_shares()
    after = holding_map2.get(code, 0)

    if action == 'buy':
        delta = after - before
        expected = shares
    else:
        delta = before - after
        expected = shares

    if delta == expected:
        # 5. 成交确认 → 同步 portfolio
        sync()
        label = '买入' if action == 'buy' else '卖出'
        print(f"✅ {label} {code} {shares}股@{price} → 持仓从{before}→{after}股 合同:{contract}")
        return True, before, after
    else:
        label = '买入' if action == 'buy' else '卖出'
        print(f"❌ {label} {code} {shares}股@{price} → 下单成功但持仓未变（{before}→{after}），"
              f"期望增量{expected}实际增量{delta}，可能未成交")
        return False, before, after


# ─── buy / sell ────────────────────────────────────────────────────────

def do_buy(code, shares, price):
    return _trade_with_confirmation('buy', code, shares, price)


def do_sell(code, shares, price):
    return _trade_with_confirmation('sell', code, shares, price)


# ─── T0 做T配对 ───────────────────────────────────────────────────────

def _is_continuous_auction():
    """连续竞价时段: 9:30-11:30, 13:00-15:00"""
    now = time.localtime()
    t = now.tm_hour * 60 + now.tm_min
    return (570 <= t <= 690) or (780 <= t <= 900)  # 9:30=570, 11:30=690, 13:00=780, 15:00=900


# ─── 5分钟ATR获取（从腾讯接口拉取5分钟K线计算） ──────────────────────

def _calc_atr(klines, period=14):
    """计算ATR（基于K线列表），与 signal_generator 的 calc_atr 一致"""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        k_prev, k_curr = klines[i - 1], klines[i]
        tr = max(k_curr["high"] - k_curr["low"],
                 abs(k_curr["high"] - k_prev["close"]),
                 abs(k_curr["low"] - k_prev["close"]))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)


def _fetch_klines_5min(code):
    """从腾讯接口获取5分钟K线，返回 [{\"time\",\"open\",\"close\",\"high\",\"low\",\"volume\",\"amount\"}, ...]"""
    sid = CODE_MAP.get(code, "")
    if not sid:
        return []

    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sid},m5,,48"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        stock_data = raw.get("data", {})
        qfq_key = None
        for key in (sid, sid.lower(), sid.upper()):
            if key in stock_data:
                qfq_key = key
                break
        if not qfq_key:
            return []

        kline_list = stock_data[qfq_key].get("m5") or stock_data[qfq_key].get("m5", [])
        if not kline_list:
            return []

        klines = []
        for item in kline_list:
            if len(item) < 6:
                continue
            try:
                o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
                v = float(item[5])
                amt = float(item[7]) if len(item) > 7 else 0
                klines.append({
                    "time": item[0],
                    "open": o, "close": c, "high": h, "low": l,
                    "volume": v, "amount": amt,
                })
            except (ValueError, IndexError):
                continue
        return klines
    except Exception as e:
        print(f"[WARN] {code} 5分钟K线获取失败: {e}")
        return []


def _estimate_atr(code, price=None):
    """
    获取5分钟ATR（从腾讯接口拉取5分钟K线计算）。
    如果5分钟数据不可用，降级为价格×2%的粗估。
    """
    if price is None:
        price = _get_price(code) or 0

    klines_5min = _fetch_klines_5min(code)
    if klines_5min:
        atr = _calc_atr(klines_5min, 14)
        if atr is not None:
            return atr

    # 降级：价格×2%粗估
    print(f"[WARN] {code} 5分钟ATR不可用，降级为价格×2%粗估")
    return round(price * 0.02, 3)


def do_t0_buy(code, shares, price, pair_price=None):
    """
    做T买入配对：
      1. 检查连续竞价时段
      2. 撤同方向(买入)未成交委托
      3. 主单：买入 shares@price（限价≤当前价）
      4. 配对：挂卖出单 shares@pair_price（限价≥当前价，ATR×2）
    配对失败不影响主单结果。
    """
    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 撤同方向未成交委托(买入方向)
    do_revoke(code, '买入')

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 买入限价: ≤ 当前价
    buy_limit = min(price, cur_price)
    if buy_limit != price:
        print(f"   ⚠️ 买入限价调整: {price} → {buy_limit} (≤当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        atr = _estimate_atr(code, price)
        pair_price = round(price + atr * 1.1, 3)
    else:
        atr = None

    print(f"🔁 做T买入 {code} {shares}股@{buy_limit} | ATR≈{atr or 'N/A'} | 配对卖出挂单@{pair_price}")

    # 主单
    ok, before, after = _trade_with_confirmation('buy', code, shares, buy_limit)

    if ok:
        # 配对卖出挂单: 限价 ≥ 当前价
        sell_limit = max(pair_price, cur_price)
        if sell_limit != pair_price:
            print(f"   ⚠️ 卖出限价调整: {pair_price} → {sell_limit} (≥当前价{cur_price})")
        _clean_lock()
        e = EvolvingSim()
        p_ok, p_contract = e.sell(code, shares, sell_limit)
        if p_ok:
            print(f"   ✅ 配对卖出挂单 {code} {shares}股@{sell_limit} 合同:{p_contract}")
        else:
            print(f"   ⚠️ 配对卖出挂单失败 {code} {shares}股@{sell_limit}: {p_contract}")

        print(f"✅ 做T买入 {code} {shares}股@{buy_limit} | 配对卖出挂单 {shares}股@{sell_limit}")
    else:
        print(f"❌ 做T买入主单失败，不挂配对单")

    return ok


def do_t0_sell(code, shares, price, pair_price=None):
    """
    做T卖出配对：
      1. 检查连续竞价时段
      2. 撤同方向(卖出)未成交委托
      3. 主单：卖出 shares@price（限价≥当前价）
      4. 配对：挂买入单 shares@pair_price（限价≤当前价，ATR×2）
    配对失败不影响主单结果。
    """
    if not _is_continuous_auction():
        print(f"❌ 非连续竞价时段(9:30-11:30,13:00-15:00)，不执行挂单")
        return False

    # 撤同方向未成交委托(卖出方向)
    do_revoke(code, '卖出')

    # 获取当前市价用于限价检查
    cur_price = _get_price(code) or price

    # 卖出限价: ≥ 当前价
    sell_limit = max(price, cur_price)
    if sell_limit != price:
        print(f"   ⚠️ 卖出限价调整: {price} → {sell_limit} (≥当前价{cur_price})")

    # 配对价格
    if pair_price is None:
        atr = _estimate_atr(code, price)
        pair_price = round(price - atr * 1.1, 3)
    else:
        atr = None

    print(f"🔁 做T卖出 {code} {shares}股@{sell_limit} | ATR≈{atr or 'N/A'} | 配对买入挂单@{pair_price}")

    # 主单
    ok, before, after = _trade_with_confirmation('sell', code, shares, sell_limit)

    if ok:
        # 配对买入挂单: 限价 ≤ 当前价
        buy_limit = min(pair_price, cur_price)
        if buy_limit != pair_price:
            print(f"   ⚠️ 买入限价调整: {pair_price} → {buy_limit} (≤当前价{cur_price})")
        _clean_lock()
        e = EvolvingSim()
        p_ok, p_contract = e.buy(code, shares, buy_limit)
        if p_ok:
            print(f"   ✅ 配对买入挂单 {code} {shares}股@{buy_limit} 合同:{p_contract}")
        else:
            print(f"   ⚠️ 配对买入挂单失败 {code} {shares}股@{buy_limit}: {p_contract}")

        print(f"✅ 做T卖出 {code} {shares}股@{sell_limit} | 配对买入挂单 {shares}股@{buy_limit}")
    else:
        print(f"❌ 做T卖出主单失败，不挂配对单")

    return ok


# ─── revoke ────────────────────────────────────────────────────────────

def do_revoke(code, direction):
    """
    撤同标的同方向未成交委托。
    direction: '买入' | '卖出'

    流程:
      1. 查 getEntrust → 找 code + direction + 未成交
      2. 逐一 revokeEntrust(contractNo)
      3. 撤完确认委托清空
    """
    _clean_lock()
    e = EvolvingSim()

    # 1. 查委托
    ent = e.getEntrust('today', True)
    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        print(f"⚠️ 无待撤委托 {code} {direction}")
        return True

    # 2. 找匹配的未成交委托
    targets = []
    for row in ent['data']:
        if row and len(row) > 10 and row[2] == code and row[4] == direction and row[5] == '未成交':
            contract = row[10]
            if contract.strip():
                targets.append(contract)

    if not targets:
        print(f"⚠️ 无待撤委托 {code} {direction}")
        return True

    # 3. 逐一撤单
    revoked = 0
    for contract in targets:
        # 每次撤单用新实例
        _clean_lock()
        e2 = EvolvingSim()
        try:
            status, info = e2.revokeEntrust(contract)
            if status:
                print(f"   ✅ 撤: {contract}")
                revoked += 1
            else:
                print(f"   ❌ 撤单失败 {contract}: {info}")
        except Exception as ex:
            print(f"   ❌ 撤单异常 {contract}: {ex}")
        time.sleep(0.3)

    print(f"✅ 撤单 {code} {direction}：{revoked}笔已撤")
    return revoked > 0


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

    elif cmd == 'revoke':
        if len(sys.argv) != 4:
            print("用法: trade.py revoke CODE 买入|卖出")
            sys.exit(1)
        do_revoke(sys.argv[2], sys.argv[3])

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)