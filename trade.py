#!/usr/bin/env python3
"""
固化的下单工具 — 所有买卖操作统一接口

数据通道:
  - sync 读持仓/委托/成交/资金: 优先 frida 进程内接口 (ths_cli --engine frida)，
    失败才回退 EvolvingSim
  - 下单 buy/sell/t0_*: 优先 frida 接口（ths.place → App 自己的 sendWeituoRequest），
    失败才回退 EvolvingSim 界面自动化；两条路径都打印实际通道

用法:
  python3 trade.py sync                          # 同步持仓到 portfolio.json
  python3 trade.py buy CODE SHARES PRICE         # 买入
  python3 trade.py sell CODE SHARES PRICE        # 卖出
  python3 trade.py t0_buy CODE SHARES PRICE [PAIR_PRICE]  # 做T买入配对：买入+高位卖出挂单
  python3 trade.py t0_sell CODE SHARES PRICE [PAIR_PRICE] # 做T卖出配对：卖出+低位买入挂单

铁律:
  1. 下单先走 frida 接口（不点界面、锁屏可用）；只有在 frida 未把单发出去时才回退
     EvolvingSim.buy/sell，已发出的单禁止回退（防重复下单）
  2. 数量必须是 100 整数倍，且不低于 config.yaml trade.min_shares
  3. 回退 EvolvingSim 时才需要 open -a 同花顺 + sleep 1，调完后 sleep 2
  4. 不用子进程 subprocess 包装 EvolvingSim（frida 读通道经 ths_cli 子进程）
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
# state_center 统一提供状态读写接口 + 结构化错误码（与 executor 的跨进程契约）
from state_center import (
    get_code_map, emit_errcode,
    EXIT_THS_DISCONNECT, EXIT_ORDER_FAILED, EXIT_PAIR_LEG_FAILED,
)

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
    """⚠️ 委托核验器（本文件版本，仅下单主流程 trade.py 使用）。

    【两份实现不一致，主路径用 executor 版本 — 不要合并】
    另一份：executor.py `SignalExecutor._verify_order_via_entrust`（executor 执行
    trade.py 子进程后的独立核验，是实际下单链路上的主路径）。两者口径差异：
      1) 本版额外查"委托日期[0]必须是今天"；executor 版不查日期，只靠 since 时间窗。
      2) 本版额外排除 orders/*.json 已用合同号；executor 版不排除（会把旧合同号算命中）。
      3) 本版固定 8 次轮询；executor 版有情况b/c 区分 + 额外 10 次(接口抖动)轮询。
      4) 本版只返回 (ok, contract, detail)；executor 版返回
         (status, detail, {'filled','status'})，能区分"没下出去"与"接口抖动未知"。
      5) 本版没有"状态已撤/废单"过滤；executor 版同样没有（两版都靠 is_filled_status 判成交）。
    合并会同时改动下单链路与子进程核验语义，风险高，本次只做标注。

    下单后强制核验：查当日委托表，匹配 代码+方向 且拿到合同号才算成功。
    不允许用返回码为0当作成交依据。接口查不到视为未知，不得当作成功。

    轮询策略：最多8次，每次间隔3秒，匹配即返回成功；8次都未匹配则判失败。
    接口返回异常(unknown err)时同样重试，8次后判失败。

    匹配规则：
      - 代码 + 方向 必须完全匹配
      - since: 下单时刻(HH:MM:SS)，委托时间早于该时刻的记录一律跳过（防误匹配同日旧单）
      - 数量容差：只接受数量相等或差异不超过100股的记录；差异超过100股不算匹配
      - 委托表时间在第1列（格式 HH:MM:SS）
      - 委托日期[0]必须是今天（YYYYMMDD），排除昨日及更早的委托
      - 合同号[10]必须不在 orders/ 历史文件已记录的合同号集合中
      - 状态[5]只接受"未成交"或"全部成交"/"已成交"，"已撤"/"废单"直接跳过
      - 状态含"成交"字样的记录优先匹配并在日志中标注
      - 只有时间窗口内完全找不到满足数量容差的记录才返回失败

    返回:
      (True, contract, detail)   核验通过，contract为合同号
      (False, '', detail)         核验失败
    """
    from datetime import datetime as _dt
    today_str = _dt.now().strftime('%Y%m%d')

    # 读取 orders/ 目录下所有已记录的历史合同号（复用与 _fetch_contract 相同的读取方式）
    used_contracts = set()
    orders_dir = os.path.join(SCRIPT_DIR, 'orders')
    if os.path.isdir(orders_dir):
        for fname in os.listdir(orders_dir):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(orders_dir, fname)
            if os.path.isdir(fpath):
                continue
            try:
                with open(fpath) as _f:
                    _od = json.load(_f)
                _c = str(_od.get('contract', '')).strip()
                if _c and _c not in ('0', 'pending', ''):
                    used_contracts.add(_c)
            except Exception:
                pass

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
            # 硬性过滤1：委托日期必须是今天
            row_date = str(row[0]).strip().replace('-', '')
            if row_date != today_str:
                continue
            # 硬性过滤2：合同号不能是历史已记录的合同号
            if contract in used_contracts:
                continue
            # 硬性过滤3：状态必须是"未成交"或"全部成交"/"已成交"，排除"已撤"/"废单"
            row_status = str(row[5]).strip() if len(row) > 5 else ''
            if '已撤' in row_status or '废单' in row_status:
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


def _positions_from_holding_rows(h):
    """旧通道 getHoldingShares 行 → 统一持仓结构（字段与 frida 通道对齐）。

    row: [0]代码 [1]名称 [2]现价 [3]盈亏额 [6]数量 [8]可用 [9]冻结 [10]成本 [11]市值
    数量为 0 的行也保留，交给 sync 做清仓判定（与旧行为一致）。
    """
    out = {}
    for row in (h or {}).get('data', []) or []:
        if not row or len(row) < 12:
            continue
        code = row[0]
        try:
            shares_val = int(row[6])
        except (ValueError, TypeError):
            shares_val = 0
        try:
            available_val = int(row[8])
        except (ValueError, TypeError):
            available_val = 0
        try:
            frozen_val = int(row[9])
        except (ValueError, TypeError):
            frozen_val = 0
        try:
            cost = float(row[10])
        except (ValueError, TypeError):
            cost = 0.0
        try:
            cur_price = float(row[2])
        except (ValueError, TypeError):
            cur_price = 0.0
        try:
            market_value = float(row[11])
        except (ValueError, TypeError):
            market_value = 0.0
        try:
            pnl = float(row[3])
        except (ValueError, TypeError):
            pnl = 0.0
        total_cost = cost * shares_val
        out[code] = {
            'name': row[1],
            'shares': shares_val,
            'available': available_val,
            'frozen': frozen_val,
            'avg_cost': cost,
            'current_price': cur_price,
            'market_value': market_value,
            'pnl': pnl,
            'pnl_pct': round(pnl / total_cost * 100, 2) if total_cost > 0 else 0,
            'source': 'legacy',
        }
    return out


def _sold_volumes_from_orders(orders):
    """frida orders → 当日各代码卖出委托量 {code: vol}（委托量，不是成交量）。"""
    sold = {}
    for o in orders or []:
        if o.get('direction') != '卖出':
            continue
        code = str(o.get('code', '')).strip()
        if not code:
            continue
        sold[code] = sold.get(code, 0) + int(o.get('shares') or 0)
    return sold


def _sold_volumes_from_entrust(ent):
    """旧通道 getEntrust 行 → 当日各代码卖出委托量 {code: vol}。"""
    sold = {}
    if not (isinstance(ent, dict) and ent.get('status')):
        return sold
    for er in ent.get('data', []) or []:
        if er and len(er) > 6 and str(er[4]).strip() == '卖出':
            try:
                ec = str(er[2]).strip()
                sold[ec] = sold.get(ec, 0) + int(er[6])
            except (ValueError, TypeError):
                pass
    return sold


def _apply_zero_position(pf, code, sold_volumes):
    """持仓表返回 0 股/缺席时的清仓判定（原 sync 逻辑，抽成函数复用）。

    - 当日卖出委托量 >= 原持仓 → 立即删（有委托证据）
    - 否则连续 3 次返回 0 股才删（处理隔日清仓/数据异常）
    """
    if code not in pf.get('positions', {}):
        return
    old = pf['positions'][code].get('shares', 0)
    zero_counts = pf.setdefault('_zero_share_count', {})
    if old > 0 and (sold_volumes or {}).get(code, 0) >= old:
        del pf['positions'][code]
        zero_counts.pop(code, None)
        from state_center import reset_signal_state_on_liquidate
        reset_signal_state_on_liquidate(pf, code, time.strftime('%Y-%m-%d'))
        return
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


def sync():
    """同步持仓到 portfolio.json（持有 sync 执行锁，覆盖整个读-改-写过程）。

    与 10:00 信号并发时：抢不到锁就跳过本次（退出码 0），绝不两进程同时改同一文件。
    """
    from core.exec_lock import ExecLock
    lock = ExecLock('sync')
    if not lock.acquire():
        print("[SYNC] 未抢到 sync 锁（另一进程正在同步持仓），本次跳过，退出码 0")
        return
    try:
        _sync_locked()
    finally:
        lock.release()


def _sync_locked():
    """真正执行持仓同步（调用方已持 ExecLock('sync')）。

    数据通道（每段都打印实际来源）:
      - 持仓/委托/成交/资金: frida 接口（ths_cli --engine frida）→ 失败才回退 EvolvingSim
    """
    if os.path.exists(PF_PATH):
        with open(PF_PATH) as f:
            pf = json.load(f)
    else:
        pf = {'positions': {}, 'account': {}}

    code_map = get_code_map()

    try:
        from executor import get_data_broker
        broker = get_data_broker()
    except Exception as ex:
        broker = None
        print(f"  ⚠️ 载入 frida 通道失败: {ex} → 全部回退旧通道")

    # ── 1) 持仓 ────────────────────────────────────────────────────
    positions = None
    pos_src = None
    if broker is not None:
        try:
            positions = broker.get_positions()
            pos_src = 'frida'
            print(f"  [通道] 持仓: frida 接口 (ths_cli positions)  {len(positions)} 只")
        except Exception as ex:
            print(f"  ⚠️ frida 持仓失败: {ex} → 回退旧通道 getHoldingShares")
    if positions is None:
        print("  ⏳ 旧通道等待同花顺 API 就绪 15s...")
        time.sleep(15)
        h = _call_evolving('getHoldingShares')
        if isinstance(h, dict) and h.get('status'):
            positions = _positions_from_holding_rows(h)
            pos_src = 'legacy'
            print(f"  [通道] 持仓: 旧通道 EvolvingSim.getHoldingShares  {len(positions)} 只")
        else:
            print("  🚨 sync: frida 与旧通道均未取到持仓，跳过本次 sync（不写盘）")
            return

    # ── 2) 委托 ────────────────────────────────────────────────────
    orders = None
    ord_src = None
    sold_volumes = {}
    if broker is not None:
        try:
            orders = broker.get_orders()
            ord_src = 'frida'
            sold_volumes = _sold_volumes_from_orders(orders)
            print(f"  [通道] 委托: frida 接口 (ths_cli orders)  {len(orders)} 行")
        except Exception as ex:
            print(f"  ⚠️ frida 委托失败: {ex} → 回退旧通道 getEntrust")
    if orders is None:
        ent = _call_evolving('getEntrust', 'today', False)
        if isinstance(ent, dict) and ent.get('status'):
            ord_src = 'legacy'
            sold_volumes = _sold_volumes_from_entrust(ent)
            print(f"  [通道] 委托: 旧通道 EvolvingSim.getEntrust  {len(ent.get('data') or [])} 行")
        else:
            ord_src = 'unavailable'
            print("  ⚠️ 委托两通道均失败，清仓核对退化为连续3次计数")

    # ── 3) 成交（sync 不写成交，只做通道读数校验）──────────────────
    trd_src = 'skipped'
    if broker is not None:
        try:
            trades = broker.get_trades()
            trd_src = 'frida'
            print(f"  [通道] 成交: frida 接口 (ths_cli trades)  {len(trades)} 笔")
        except Exception as ex:
            trd_src = 'unavailable'
            print(f"  ⚠️ frida 成交失败: {ex}（sync 不依赖成交，继续）")

    # ── 4) 资金（优先 frida account → 失败才回退旧通道）─────────────
    acct_data = None
    acct_src = None
    if broker is not None:
        try:
            a = broker.get_account()
            acct_data = {
                '总资产': a['total_asset'],
                '可用金额': a['cash'],
                '总市值': a['market_value'],
            }
            acct_src = a.get('source') or 'frida 接口'
            print(f"  [通道] 资金: {acct_src}")
        except Exception as ex:
            print(f"  ⚠️ broker 取资金失败: {ex} → 回退直接调 getAccountInfo")
    if acct_data is None:
        acct = _call_evolving('getAccountInfo')
        if isinstance(acct, dict) and acct.get('status'):
            d = acct.get('data') or {}
            try:
                acct_data = {
                    '总资产': float(d['总资产']),
                    '可用金额': float(d['可用金额']),
                    '总市值': float(d['总市值']),
                }
                acct_src = '旧通道 EvolvingSim.getAccountInfo'
                print(f"  [通道] 资金: {acct_src}")
            except (KeyError, TypeError, ValueError) as ex:
                print(f"  ⚠️ 旧通道账户字段异常: {ex}")
        else:
            print("  ⚠️ 资金两通道均失败，保留 portfolio.json 上次账户数据")

    # ── 持仓写盘 ───────────────────────────────────────────────────
    if not positions:
        print("  ⚠️ sync: 持仓数据为空，跳过持仓同步（保留原持仓）")
    else:
        for code, d in positions.items():
            shares_val = int(d.get('shares') or 0)
            if shares_val > 0:
                cost = float(d.get('avg_cost') or 0)
                # 优先从 etf_pool 取完整名称（API返回的名称经常截断）
                name = code_map.get(code, {}).get('name') or d.get('name') or code
                # 从 _signal_state 读取 base_price，回退到 avg_cost
                signal_state = pf.get('_signal_state', {}).get(code, {})
                base_price = signal_state.get('base_price') or cost
                pf['positions'][code] = {
                    'name': name,
                    'shares': shares_val,                   # 总持股（实际数量，含冻结）
                    'available': int(d.get('available') or 0),  # 可用股数（做T/卖出时参考）
                    'frozen': int(d.get('frozen') or 0),        # 冻结股数（已挂卖单等）
                    'avg_cost': cost,
                    'current_price': float(d.get('current_price') or 0),
                    'market_value': float(d.get('market_value') or 0),
                    'pnl': float(d.get('pnl') or 0),       # 仅展示用
                    'pnl_pct': float(d.get('pnl_pct') or 0),  # 仅展示用
                    'base_price': base_price,
                }
                pf.get('_zero_share_count', {}).pop(code, None)
            else:
                _apply_zero_position(pf, code, sold_volumes)
        # frida 持仓表不返回已清仓标的 → 缺席的原有持仓按 0 股走同一套清仓判定
        if pos_src == 'frida':
            for code in list(pf.get('positions', {}).keys()):
                if code not in positions:
                    _apply_zero_position(pf, code, sold_volumes)

    # ── 账户写盘（校验通过才写）────────────────────────────────────
    if acct_data is not None:
        ok, reason = _validate_account_data(acct_data, pf)
        if ok:
            pf['account'] = {
                'total_asset': float(acct_data['总资产']),
                'cash': float(acct_data['可用金额']),
                'market_value': float(acct_data['总市值']),
            }
        else:
            print(f"  🚨 sync 账户校验失败: {reason}")
            print("  ⚠️ 保留上次账户数据，持仓照常同步")
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

    from core.atomic_writer import atomic_write_json
    if not atomic_write_json(PF_PATH, pf):
        print("  🚨 sync: portfolio.json 原子写入失败")

    # 开盘第一次 sync 落一份当日基线快照（t0_close 用真实持仓 vs 基线判定单边敞口）
    try:
        from core.daily_baseline import snapshot_if_missing
        if snapshot_if_missing(pf.get('positions', {})):
            print(f"  📌 已记录当日基线快照: {len(pf.get('positions', {}))} 只")
    except Exception as _bl_ex:
        print(f"  ⚠️ 基线快照写入失败: {_bl_ex}")

    total_asset = pf.get('account', {}).get('total_asset', 0)
    print(f"✅ 同步: {len(pf['positions'])}只 | 总资产{total_asset:.0f} | "
          f"来源: 持仓={pos_src} 委托={ord_src} 成交={trd_src} 资金={acct_src or '保留上次'}")


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

_TICK = 0.001  # ETF 最小价位


def _fetch_prev_close(code):
    """取昨收价（用于算当日涨跌停）。取不到返回 0。"""
    try:
        from market_data import fetch_realtime_quotes
        q = fetch_realtime_quotes([code])
        return float((q.get(code) or {}).get("prev_close") or 0)
    except Exception as ex:
        print(f"  ⚠️ 取昨收失败({code}): {ex}")
        return 0.0


def _t0_pair_price_in_limit(code, shares, price, is_buy_t0, pair_price):
    """做T配对价最后一道防线：必须落在当日涨跌幅区间且方向正确。

    返回 (ok, pair_price, note)。ok=False 表示这笔做T不能下（连1个价位都凑不出）。
    """
    from money_manager import day_price_limits
    limit_down, limit_up = day_price_limits(code, _fetch_prev_close(code), price)
    if limit_up <= 0:
        return False, 0.0, "拿不到昨收，无法确认当日涨跌停价"
    # 方向必须正确：配对卖 > 主腿买价，配对买 < 主腿卖价
    if (is_buy_t0 and float(pair_price) <= price) or (not is_buy_t0 and float(pair_price) >= price):
        return False, 0.0, (f"配对价方向错误(主腿{price:.3f}, 配对{pair_price:.3f})，"
                            f"这笔做T没有价差，不做")
    if is_buy_t0:
        lo, hi = round(price + _TICK, 3), limit_up
    else:
        lo, hi = limit_down, round(price - _TICK, 3)
    if hi < lo:
        return False, 0.0, (f"当日涨跌幅内无合法配对价(现价{price:.3f}, "
                            f"跌停{limit_down:.3f}, 涨停{limit_up:.3f})")
    px = round(min(max(float(pair_price), lo), hi), 3)
    actual_yuan = abs(px - price) * shares
    if actual_yuan < _TICK * shares - 1e-9:
        return False, 0.0, (f"界内最大价差{abs((hi if is_buy_t0 else lo) - price) * shares:.0f}元 "
                            f"< 1个价位的最小价差{_TICK * shares:.0f}元")
    note = ""
    if abs(px - float(pair_price)) > 1e-9:
        note = (f"配对价被界限制约：目标价差{abs(float(pair_price) - price) * shares:.0f}元 → "
                f"实际可挂{actual_yuan:.0f}元（配对价{px:.3f}, 跌停{limit_down:.3f}, 涨停{limit_up:.3f}）")
    return True, px, note


def _t0_suggest_price(code, is_buy, fallback):
    """单边未配对时的补挂建议价：买入参考卖一，卖出参考买一。"""
    try:
        from market_data import fetch_realtime_quotes
        q = (fetch_realtime_quotes([code]) or {}).get(code, {})
        best = float((q.get("ask1") if is_buy else q.get("bid1")) or 0)
        last = float(q.get("price") or 0)
        for v in (best, last, float(fallback or 0)):
            if v and v > 0:
                return round(v, 3)
    except Exception:
        pass
    return round(float(fallback or 0), 3)


def _alert_t0_single_leg(code, name, action, shares, price, pair_price, detail, pair_is_buy):
    """主腿已发出、配对腿没挂上 → 单边暴露告警（打印 + 落 logs/alerts）。"""
    suggest = _t0_suggest_price(code, pair_is_buy, pair_price or price)
    side = "卖出" if action == 't0_sell' else "买入"
    pair_side = "买入" if pair_is_buy else "卖出"
    msg = (f"{name}({code}) 做T{side} {shares:,}股 @{price:.3f} 已发出，"
           f"配对{pair_side} @{pair_price:.3f} 挂不上｜原因: {detail}｜"
           f"建议补挂价 {suggest:.3f}｜请立即人工补挂，否则 14:50 尾盘平仓兜底")
    print("")
    print("  " + "!" * 70)
    print(f"  ⚠️ T0 单边未配对 — {name}({code})")
    print(f"  主腿: 做T{side} {shares:,}股 @{price:.3f}（已发出，合同号见上）")
    print(f"  配对腿: {pair_side} {shares:,}股 @{pair_price:.3f} 未挂上")
    print(f"  原因: {detail}")
    print(f"  ➡️ 建议补挂价: {suggest:.3f}（{pair_side}），或 14:50 尾盘平仓兜底")
    print("  " + "!" * 70)
    print("")
    try:
        from core.alert import get_alert_manager
        get_alert_manager().critical("T0 单边未配对", msg, dedup_key=f"t0_single_leg:{code}")
    except Exception as ex:
        print(f"  ⚠️ 告警写入失败: {ex}")


def _place_order(method, code, shares, price, e=None):
    """买/卖：先走 frida 接口（App 自己的下单方法），失败回退 EvolvingSim 界面自动化。

    返回 (ok, contract, detail, exitcode)。ok=True 表示委托表里确认到了这笔单，
    contract 交给 write_order 落盘；exitcode 是给上游的结构化失败语义
    （state_center.EXIT_*）：None=成功，EXIT_THS_DISCONNECT=真断连，
    EXIT_ORDER_FAILED=主腿未确认（禁止重下）。
    """
    try:
        from broker_frida import ThsBroker
        r = ThsBroker().place(method, code, shares, price)
        print(f"  [通道] 下单: frida 接口（{r.get('via')}）  合同号={r['contract']}  状态={r['status']}")
        return True, r["contract"], f"frida 委托表 合同号={r['contract']} 状态={r['status']}", None
    except Exception as ex:
        # 单已发给柜台、只是委托表没确认 → 不许回退，否则同一笔下两次
        if '已发送' in str(ex) or '未拿到合同号' in str(ex):
            print(f"  ⚠️ frida 已发送但委托表未确认: {ex}")
            return False, None, f"frida 已发送未确认，禁止回退重下，请人工核对委托表: {ex}", EXIT_ORDER_FAILED
        print(f"  ⚠️ frida 接口下单失败: {ex}")

    print(f"  [通道] 下单: 旧通道 EvolvingSim.{method}（界面自动化）")
    since = time.strftime("%H:%M:%S")
    time.sleep(2)
    result = _call_evolving(method, code, shares, price, e=e)
    print(f"  📤 EvolvingSim.{method} 原始返回: {result}")
    if result is None:
        print(f"  ⚠️ EvolvingSim 返回 None，重启同花顺后重试一次")
        try:
            os.system("pkill -9 -x 同花顺")
            time.sleep(3)
            os.system("open -a /Applications/同花顺.app")
            time.sleep(5)
        except Exception:
            pass
        since = time.strftime("%H:%M:%S")
        result = _call_evolving(method, code, shares, price, e=e)
        print(f"  📤 重试后 EvolvingSim.{method} 原始返回: {result}")
        if result is None:
            # 旧通道两次都没有任何响应 → 真断连（没有任何委托报出），允许上游重启重试
            emit_errcode("THS_DISCONNECT", f"{method} {code} {shares}股 @{price}")
            return False, None, "旧通道两次无响应(断连)，未报出任何委托", EXIT_THS_DISCONNECT
    elif isinstance(result, tuple) and len(result) > 0 and not result[0]:
        return False, None, f"同花顺拒单（EvolvingSim 返回 {result}）", EXIT_ORDER_FAILED

    direction = '买入' if 'buy' in method else '卖出'
    ok, contract, detail = _verify_order_in_entrust(e, code, direction, shares, price, since=since)
    return ok, contract, detail, (None if ok else EXIT_ORDER_FAILED)


def _unified_trade(action, code, shares, price, pair_price=None):
    """统一买卖做T流程。"""
    shares = _validate_shares(shares)
    is_t0 = action in ('t0_buy', 't0_sell')
    amount = shares * price

    if is_t0:
        # 配对价必须落在当日涨跌幅区间内：越界按界内降级，连1个价位都凑不出就整笔不做。
        # 放在最前面判定，取消时不留 intent/订单痕迹，不影响后续重试。
        ok_price, pair_price, note = _t0_pair_price_in_limit(
            code, shares, price, action == 't0_buy', float(pair_price or 0))
        if not ok_price:
            print(f"⛔ {code} 做T取消，不下任何单: {note}")
            return False
        if note:
            print(f"  ⚠️ {note}")

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
    # 下单前先落盘 intent（status=pending）：即使订单文件还没写，别的进程也能靠它去重
    write_intent(code, action, direction, shares=shares, price=price, status='pending')

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

    # 下单意图日志
    if is_t0:
        spread = (pair_price - price) / price * 100 if action == 't0_buy' else (price - pair_price) / price * 100
        print(f"🔁 做T{'买入' if 'buy' in action else '卖出'} {name}({code}) {shares:,}股 @{price} → 挂{'卖' if 'buy' in action else '买'}@{pair_price}  价差{spread:+.1f}%  金额¥{amount:,.0f}")
    elif action == 'buy':
        print(f"📊 买入 {name}({code}) {shares:,}股 @{price}  金额¥{amount:,.0f}")
    else:
        print(f"📊 卖出 {name}({code}) {shares:,}股 @{price}  金额¥{amount:,.0f}")

    ok_main, contract_main, detail_main, code_main = _place_order(method, code, shares, price, e)
    if not ok_main:
        from state_center import write_intent_failed as _wif
        _wif(code, action, shares, price, reason='委托表未确认')
        print(f"  ❌ 下单失败: {detail_main}")
        sys.exit(code_main or EXIT_ORDER_FAILED)
    print(f"  ✅ 核验通过: {detail_main}")
    write_order(code, action, shares, price, contract_main, status='pending', baseline_shares=baseline_shares)
    # 主腿已报出：intent 标记 submitted（订单文件已落盘，去重以订单文件为准）
    write_intent(code, action, direction, shares=shares, price=price,
                 status='submitted', contract=contract_main)

    # T0 配对单
    if is_t0 and pair_price is not None:
        time.sleep(2)
        ok_pair, contract_pair, detail_pair, _code_pair = _place_order(pair_method, code, shares, pair_price, e)
        if not ok_pair:
            # 主腿已发出、配对腿没挂上 → 立刻告警并给补挂价，别静默留单边
            _alert_t0_single_leg(code, name, action, shares, price, pair_price,
                                 detail_pair, pair_is_buy=(pair_method == 'buy'))
            print(f"  ❌ 配对单下单失败: {detail_pair}")
            # 主腿已发出 → 结构化标记"配对腿失败"，上游禁止重跑（否则主腿下两次）
            emit_errcode("PAIR_LEG_FAILED", f"{code} 主腿已发出，配对腿未挂上")
            sys.exit(EXIT_PAIR_LEG_FAILED)
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