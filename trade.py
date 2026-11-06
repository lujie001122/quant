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


def _fetch_contract(e, code, direction, price, shares):
    """下单后从 getEntrust 捞真实合同号。
    getEntrust row: [0]日期 [1]时间 [2]代码 [3]名称 [4]操作 [5]备注 [6]委托量
                    [7]撤销量 [8]委托价 [9]成交价 [10]合同编号
    匹配条件: code + 方向 + 价格 + 委托量，跳过已被 orders/*.json 占用的合同号，
    按委托时间倒序取第一笔符合条件的。
    找不到返回 'pending'。
    """
    # 读取已被占用的合同号
    used_contracts = set()
    orders_dir = os.path.join(SCRIPT_DIR, 'orders')
    if os.path.isdir(orders_dir):
        for fname in os.listdir(orders_dir):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(orders_dir, fname)) as _f:
                    _od = json.load(_f)
                _c = str(_od.get('contract', '')).strip()
                if _c and _c not in ('0', 'pending', ''):
                    used_contracts.add(_c)
            except Exception:
                pass

    try:
        time.sleep(2)
        ent = e.getEntrust('today', False)
    except Exception as ex:
        print(f"  ⚠️ _fetch_contract getEntrust 异常: {ex}")
        return 'pending'
    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        return 'pending'

    # 按 row[1](委托时间) 倒序排列后逐条匹配
    rows = ent['data']
    try:
        sorted_rows = sorted(
            [r for r in rows if r and len(r) >= 11],
            key=lambda r: str(r[1]),
            reverse=True,
        )
    except Exception:
        sorted_rows = list(reversed([r for r in rows if r and len(r) >= 11]))

    for row in sorted_rows:
        if str(row[2]) != code:
            continue
        row_op = str(row[4])
        if direction == '买入' and '买' not in row_op:
            continue
        if direction == '卖出' and '卖' not in row_op:
            continue
        try:
            row_price = float(row[8])
        except (ValueError, TypeError):
            continue
        if abs(row_price - price) >= 0.002:
            continue
        # 新增：委托量匹配
        try:
            row_shares = int(row[6])
        except (ValueError, TypeError):
            continue
        if row_shares != shares:
            continue
        contract = str(row[10]).strip()
        if not contract or contract == '0':
            continue
        # 跳过已被其他订单占用的合同号
        if contract in used_contracts:
            print(f"  ⏭️ {code} {direction} 合同号={contract} 已被占用，继续查找")
            continue
        print(f"  📋 {code} {direction} {shares}股 合同号={contract}")
        return contract
    print(f"  ⚠️ {code} {direction} @{price} {shares}股 未在getEntrust中找到合同号，contract=pending")
    return 'pending'


def is_filled_status(status_text: str) -> bool:
    """判断委托状态是否为已全部成交。
    规则：
      "全部成交" / "已成交"  → True
      "未成交"              → False（挂单待成交）
      "已撤" / "废单"       → False
      "部分成交"            → False（部分成交不等于全部成交）
      其余含"成交"的文本    → False（保守处理）
      空字符串              → False
    """
    s = str(status_text).strip()
    if not s:
        return False
    if '未成交' in s:
        return False
    if '已撤' in s or '废单' in s:
        return False
    if '全部成交' in s or '已成交' in s:
        return True
    # 部分成交、其余含"成交"字样 → False
    return False


def _verify_order_in_entrust(e, code, direction, shares, price=None, since=None):
    """下单后强制核验：查当日委托表，匹配 代码+方向 且拿到合同号才算成功。
    不允许用返回码为0当作成交依据。接口查不到视为未知，不得当作成功。

    轮询策略：最多8次，每次间隔3秒，匹配即返回成功；8次都未匹配则判失败。
    接口返回异常(unknown err)时同样重试，8次后判失败。

    匹配规则：
      - 代码 + 方向 必须完全匹配
      - since: 下单时刻(HH:MM:SS)，委托时间早于该时刻的记录一律跳过（防误匹配同日旧单）
      - 数量容差：只接受数量相等或差异不超过100股的记录；差异超过100股不算匹配
      - 委托表时间在第1列（格式 HH:MM:SS）
      - 状态含"成交"字样的记录优先匹配并在日志中标注
      - 只有时间窗口内完全找不到满足数量容差的记录才返回失败

    返回:
      (True, contract, detail)   核验通过，contract为合同号
      (False, '', detail)         核验失败
    """
    MAX_TRIES = 8
    last_ent = None
    last_rows = []
    last_reason = ''

    for attempt in range(1, MAX_TRIES + 1):
        time.sleep(3)
        try:
            ent = e.getEntrust('today', False)
        except Exception as ex:
            last_reason = f"getEntrust 调用异常(未知): {ex}"
            print(f"  第{attempt}次查委托表，接口异常: {ex}，未匹配。")
            last_ent = None
            continue

        # 接口异常：status不为真 或 无data → 重试
        if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
            last_reason = f"getEntrust 返回异常(非有效数据): {ent}"
            print(f"  第{attempt}次查委托表，返回非有效数据，未匹配。")
            last_ent = ent
            continue

        rows = ent['data']
        last_ent = ent
        last_rows = [str(r) for r in rows if r and len(r) >= 11]
        print(f"  第{attempt}次查委托表，共{len(last_rows)}条记录。")

        # 匹配：代码+方向+时间窗口+数量容差（≤100股）
        # 优先匹配含"成交"状态的记录
        best_row = None
        best_filled = False
        for row in rows:
            if not row or len(row) < 11:
                continue
            if str(row[2]).strip() != code:
                continue
            row_op = str(row[4]).strip()
            if direction == '买入' and '买' not in row_op:
                continue
            if direction == '卖出' and '卖' not in row_op:
                continue
            contract = str(row[10]).strip()
            if not contract or contract == '0':
                continue
            # 时间窗口：委托时间 >= since
            if since:
                row_time = str(row[1]).strip()
                if row_time < since:
                    continue
            # 数量容差：差异不超过100股
            try:
                row_shares = int(row[6])
            except (ValueError, TypeError):
                continue
            if abs(row_shares - shares) > 100:
                continue
            # 检查状态是否为全部成交
            row_status = str(row[5]).strip() if len(row) > 5 else ''
            is_filled = is_filled_status(row_status)
            if best_row is None or (is_filled and not best_filled):
                best_row = row
                best_filled = is_filled
        if best_row is not None:
            try:
                matched_shares = int(best_row[6])
            except (ValueError, TypeError):
                matched_shares = -1
            contract = str(best_row[10]).strip()
            filled_tag = '(已成交)' if best_filled else ''
            shares_tag = '' if matched_shares == shares else f'(委托量={matched_shares}≠信号量={shares},差异{abs(matched_shares-shares)}股)'
            since_tag = f' since>={since}' if since else ''
            print(f"  第{attempt}次即匹配{filled_tag}: {code} {direction} 合同号={contract}{shares_tag}{since_tag}")
            return True, contract, f"匹配成功: {code} {direction} 合同号={contract}{filled_tag}{shares_tag}{since_tag}"

        last_reason = f"委托表共{len(last_rows)}条，时间窗口内未找到 {code} {direction} 数量容差≤100股的有效记录"

    # 8次都未匹配 — 失败
    detail = (
        f"【核验失败】{MAX_TRIES}次轮询均未匹配: 代码={code} 方向={direction} since={since}\n"
        f"  原始返回: {last_ent}\n"
        f"  最后一次委托表条数: {len(last_rows)}条\n"
        f"  最后一次判定依据: {last_reason}\n"
        f"  委托表最后一次全部记录:\n" + "\n".join(last_rows[:30])
        + ("\n  ..." if len(last_rows) > 30 else "")
        + "\n  ⚠️ 可能已下单但接口未刷新，请人工核对同花顺委托表"
    )
    return False, '', detail


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

def _validate_account_data(acct_data, pf_prev):
    """校验 getAccountInfo 返回的账户数据一致性。

    校验规则:
      1. 总资产 <= 0 → 拒绝
      2. 总资产与 (现金 + 市值) 偏差 > 2% → 拒绝
      3. 总资产比上次落盘值低 50% 以上 → 拒绝

    返回:
      (ok: bool, reason: str)
    """
    try:
        total = float(acct_data['总资产'])
        cash  = float(acct_data['可用金额'])
        mv    = float(acct_data['总市值'])
    except (KeyError, TypeError, ValueError) as e:
        return False, f"账户字段解析失败: {e}"

    # 规则1: 总资产不能 <= 0
    if total <= 0:
        return False, f"总资产={total:.0f} 异常(≤0),拒绝写盘"

    # 规则2: 总资产与 (现金+市值) 偏差 > 2%
    computed = cash + mv
    if computed > 0:
        deviation = abs(total - computed) / computed
        if deviation > 0.02:
            return False, (
                f"总资产{total:.0f} 与现金{cash:.0f}+市值{mv:.0f}={computed:.0f} "
                f"偏差{deviation*100:.1f}%>2%,拒绝写盘"
            )

    # 规则3: 总资产比上次落盘值低 50% 以上
    prev_total = pf_prev.get('account', {}).get('total_asset', 0)
    if prev_total > 0 and total < prev_total * 0.50:
        return False, (
            f"总资产{total:.0f} 比上次落盘值{prev_total:.0f} 低"
            f"{(1 - total/prev_total)*100:.0f}%,超过50%阈值,拒绝写盘"
        )

    return True, "ok"


def sync():
    """同步持仓到 portfolio.json"""
    import time
    time.sleep(15)  # 等待同花顺API就绪
    h = _call_evolving('getHoldingShares')
    acct = _call_evolving('getAccountInfo')

    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
    else:
        pf = {'positions': {}, 'account': {}}

    # ── 账户数据一致性校验 ──────────────────────────────────
    if isinstance(acct, dict) and acct.get('status'):
        ok, reason = _validate_account_data(acct['data'], pf)
        if not ok:
            print(f"  🚨 sync 账户校验失败: {reason}")
            print("  ⚠️  portfolio.json 保持原值，未写盘")
            return
    else:
        print("  ⚠️ sync: getAccountInfo 无有效返回，跳过本次 sync")
        return

    code_map = get_code_map()

    if isinstance(h, dict) and h.get('status'):
        data = h.get('data', [])
        if not data:
            print("  ⚠️ sync: getHoldingShares 返回空数据，跳过持仓同步")
        else:
            sold_volumes = None  # 懒加载: 当日各代码卖出委托量 {code: vol}
            for row in data:
                if not row or len(row) < 12:
                    continue
                code = row[0]
                try:
                    shares_val = int(row[6])   # 实际数量（总持股）
                except (ValueError, TypeError):
                    shares_val = 0
                try:
                    available_val = int(row[8])  # 可用余额
                except (ValueError, TypeError):
                    available_val = 0
                try:
                    frozen_val = int(row[9])     # 冻结数量
                except (ValueError, TypeError):
                    frozen_val = 0
                try:
                    cost = float(row[10])
                except (ValueError, TypeError):
                    cost = 0.0
                if shares_val > 0:
                    cur_price = float(row[2])
                    name = row[1]
                    # 优先从 etf_pool 取完整名称（API返回的名称经常截断）
                    pool_name = code_map.get(code, {}).get('name')
                    if pool_name:
                        name = pool_name
                    # 从 _signal_state 读取 base_price，回退到 avg_cost
                    signal_state = pf.get('_signal_state', {}).get(code, {})
                    base_price = signal_state.get('base_price') or cost
                    pf['positions'][code] = {
                        'name': name,
                        'shares': shares_val,      # 总持股（实际数量，含冻结）
                        'available': available_val, # 可用股数（做T/卖出时参考）
                        'frozen': frozen_val,        # 冻结股数（已挂卖单等）
                        'avg_cost': cost,
                        'current_price': cur_price,
                        'market_value': float(row[11]),
                        'pnl': float(row[3]),  # 仅展示用
                        'pnl_pct': round(float(row[3]) / (cost * shares_val) * 100, 2) if cost > 0 else 0,  # 仅展示用
                        'base_price': base_price,
                    }
                    pf.get('_zero_share_count', {}).pop(code, None)
                elif code in pf['positions']:
                    # shares_val==0: 可能是同花顺数据异常(真实持仓被抹掉)，也可能是真清仓
                    # 用 getEntrust('today') 交叉核对: 当日卖出量>=原持仓才算真清仓，否则保留原持仓
                    # 另引入连续0股计数，连续3次则判定已清仓（处理隔日清仓场景）
                    if sold_volumes is None:
                        sold_volumes = {}
                        try:
                            ent = _call_evolving('getEntrust', 'today', False)
                            if isinstance(ent, dict) and ent.get('status'):
                                for er in ent.get('data', []) or []:
                                    if er and len(er) > 6 and str(er[4]).strip() == '卖出':
                                        try:
                                            ec = str(er[2]).strip()
                                            sold_volumes[ec] = sold_volumes.get(ec, 0) + int(er[6])
                                        except (ValueError, TypeError):
                                            pass
                        except Exception as _ex:
                            print(f"  ⚠️ sync: getEntrust 异常: {_ex}")
                    old = pf['positions'][code].get('shares', 0)
                    zero_counts = pf.setdefault('_zero_share_count', {})
                    if old > 0 and sold_volumes.get(code, 0) >= old:
                        # 当日委托证明已清仓，立即删除
                        del pf['positions'][code]
                        zero_counts.pop(code, None)
                        from state_center import reset_signal_state_on_liquidate
                        reset_signal_state_on_liquidate(pf, code, time.strftime('%Y-%m-%d'))
                    else:
                        n = zero_counts.get(code, 0) + 1
                        zero_counts[code] = n
                        if n >= 3:
                            del pf['positions'][code]
                            zero_counts.pop(code, None)
                            print(f"[SYNC] {code} 连续3次返回0股，判定已清仓，删除持仓")
                            from state_center import reset_signal_state_on_liquidate
                            reset_signal_state_on_liquidate(pf, code, time.strftime('%Y-%m-%d'))
                        else:
                            print(f"[SYNC] {code} 返回0股，第 {n} 次，保留原持仓 {old}股")

    # 写入账户数据（已通过一致性校验）
    if isinstance(acct, dict) and acct.get('status'):
        d = acct['data']
        pf['account'] = {
            'total_asset': float(d['总资产']),
            'cash': float(d['可用金额']),
            'market_value': float(d['总市值']),
        }
    # Bug N: 清理 _signal_state 中已清仓且不在ETF池中的标的
    signal_state = pf.get('_signal_state', {})
    if signal_state:
        # 获取当前持仓和ETF池的标的
        active_positions = set(pf.get('positions', {}).keys())
        try:
            pool_path = os.path.join(SCRIPT_DIR, 'etf_pool.json')
            with open(pool_path) as _pf:
                pool_codes = set(json.load(_pf).get('etf_pool', []))
        except Exception:
            pool_codes = set()
        # 保留条件: 有持仓 或 在ETF池中
        cleaned_codes = []
        for code in list(signal_state.keys()):
            if code not in active_positions and code not in pool_codes:
                del signal_state[code]
                cleaned_codes.append(code)
        if cleaned_codes:
            pf['_signal_state'] = signal_state
            print(f"  🧹 清理 _signal_state 中已清仓标的: {cleaned_codes}")

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


def _get_baseline_shares(code):
    """读取 T0 配对建立时的基准持仓（_signal_state 优先，回退 positions）。

    用于写入订单文件 baseline_shares 字段，配对建立后永不改写，
    sync 也不覆盖它。t0_close.py 读取时优先用此固定基线。
    """
    try:
        with open(PF_PATH) as f:
            pf = json.load(f)
        s = pf.get('_signal_state', {}).get(code, {})
        if s.get('shares'):
            return int(s['shares'])
        pos = pf.get('positions', {}).get(code, {})
        if pos.get('shares'):
            return int(pos['shares'])
    except Exception:
        pass
    return 0


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

    # T0 配对：在下单前捕获基准持仓（固定基线，sync 后不变）
    baseline_shares = _get_baseline_shares(code) if is_t0 else None

    if is_t0 and not _is_continuous_auction():
        print(f"❌ 非连续竞价时段，不执行挂单")
        write_intent_failed(code, action, shares, price, reason='非连续竞价时段')
        return False

    if action in ('buy', 't0_buy'):
        method, pair_method, pair_action = 'buy', 'sell', 't0_sell'
    else:
        method, pair_method, pair_action = 'sell', 'buy', 't0_buy'

    # 下单前记录当前时刻，核验时只认 >= 该时刻的委托记录（防误匹配同日旧单）
    _order_since = time.strftime("%H:%M:%S")
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

    # 打印原始返回值（issuingEntrust 返回 (status, contractNo)，核验失败时用于诊断）
    print(f"  📤 EvolvingSim.{method} 原始返回: {result}")

    # 返回值解析：None=调用异常; (False,None)=同花顺拒单; (True,contractNo)=已发送
    if result is None:
        print(f"  ⚠️ EvolvingSim 调用异常返回 None，尝试重启同花顺后重试...")
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
        # 重启后重试一次
        time.sleep(2)
        result = _call_evolving(method, code, shares, price, e=e)
        print(f"  📤 重试后 EvolvingSim.{method} 原始返回: {result}")
    elif isinstance(result, tuple) and len(result) > 0 and not result[0]:
        print(f"  ⚠️ EvolvingSim 返回 status=False（同花顺拒单），原始返回: {result}")

    # ═══ 强制核验：查当日委托表，匹配 代码+方向+数量 且拿到合同号，才算成功 ═══
    # 不允许用返回码为0当作成交依据；接口查不到视为未知，不得当作成功
    direction_main = '买入' if 'buy' in action else '卖出'
    ok_main, contract_main, detail_main = _verify_order_in_entrust(e, code, direction_main, shares, price, since=_order_since)
    if not ok_main:
        print(f"  🚨 核验失败: {code} {direction_main} {shares}股 未在委托表确认")
        print(f"     原始返回: {result}")
        print(f"     判定依据: {detail_main}")
        print(f"  ❌ 下单失败，退出(非零退出码)，不扣做T额度、不写成交记录")
        sys.exit(1)
    print(f"  ✅ 核验通过: {detail_main}")
    write_order(code, action, shares, price, contract_main, status='pending', baseline_shares=baseline_shares)

    # T0 配对单
    if is_t0 and pair_price is not None:
        time.sleep(5)
        pair_result = _call_evolving(pair_method, code, shares, pair_price, e=e)
        print(f"  📤 配对单 EvolvingSim.{pair_method} 原始返回: {pair_result}")
        direction_pair = '卖出' if 'buy' in action else '买入'
        ok_pair, contract_pair, detail_pair = _verify_order_in_entrust(e, code, direction_pair, shares, pair_price, since=_order_since)
        if not ok_pair:
            print(f"  🚨 配对单核验失败: {code} {direction_pair} {shares}股 未在委托表确认")
            print(f"     原始返回: {pair_result}")
            print(f"     判定依据: {detail_pair}")
            print(f"  ❌ 配对单下单失败，退出(非零退出码)")
            sys.exit(1)
        print(f"  ✅ 配对单核验通过: {detail_pair}")
        write_order(code, pair_action, shares, pair_price, contract_pair, status='pending', baseline_shares=baseline_shares)

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
        ok = do_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
        if not ok:
            sys.exit(1)

    elif cmd == 'sell':
        if len(sys.argv) != 5:
            print("用法: trade.py sell CODE SHARES PRICE")
            sys.exit(1)
        ok = do_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
        if not ok:
            sys.exit(1)

    elif cmd == 't0_buy':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        ok = do_t0_buy(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)
        if not ok:
            sys.exit(1)

    elif cmd == 't0_sell':
        if len(sys.argv) < 5:
            print("用法: trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE]")
            sys.exit(1)
        pair_p = float(sys.argv[5]) if len(sys.argv) >= 6 else None
        ok = do_t0_sell(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), pair_p)
        if not ok:
            sys.exit(1)

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)