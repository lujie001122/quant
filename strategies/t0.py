#!/usr/bin/env python3
"""
T0Strategy — 做T配对策略

从 strategy.py 拆分:
  - evaluate_t0: 做T评分+配对
  - evaluate_t0_execute: 从评分结果解析执行动作
  - 做T评分依赖 rsi_macd.py 中的 t0_buy_score / t0_sell_score
"""

from strategies.base import BaseStrategy
from strategies.rsi_macd import t0_buy_score, t0_sell_score, is_auction_time
from position_info import TOTAL_FUND
from position_info import MAX_POSITION_RATIO
from money_manager import calc_t0_pair_price

import os, json, yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
with open(_CONFIG_PATH) as _f:
    _CONF = yaml.safe_load(_f)

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHASE_STATE_PATH = os.path.join(_ROOT_DIR, 'orders', 't0_chase_state.json')

from datetime import datetime
import logging
log = logging.getLogger('t0')


# ── 追价状态文件 I/O ────────────────────────────────────────────────────

def _load_chase_state() -> dict:
    """从 orders/t0_chase_state.json 加载追价状态。"""
    try:
        if os.path.exists(_CHASE_STATE_PATH):
            with open(_CHASE_STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as ex:
        log.warning(f"[T0] 加载追价状态失败: {ex}")
    return {}


def _save_chase_state(state: dict) -> None:
    """持久化追价状态到 orders/t0_chase_state.json。"""
    try:
        os.makedirs(os.path.dirname(_CHASE_STATE_PATH), exist_ok=True)
        with open(_CHASE_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except Exception as ex:
        log.warning(f"[T0] 保存追价状态失败: {ex}")


def _in_t0_window(now):
    """做T开仓时间窗口: 9:35-10:30, 13:30-14:20"""
    t = now.hour * 60 + now.minute
    win1 = 9 * 60 + 35 <= t <= 10 * 60 + 30
    win2 = 13 * 60 + 30 <= t <= 14 * 60 + 20
    return win1 or win2


# ── 持久化辅助：把文件状态加载成一个有 t0_chase_state 属性的 namespace ──

class _ChaseHolder:
    """轻量 namespace，让 check_t0_pair_chase 能用 pos.t0_chase_state。"""
    def __init__(self, state_dict: dict):
        self.t0_chase_state = state_dict


def load_chase_holder() -> '_ChaseHolder':
    """从文件加载追价状态，返回 _ChaseHolder。"""
    return _ChaseHolder(_load_chase_state())


def save_chase_holder(holder: '_ChaseHolder') -> None:
    """把 _ChaseHolder 中的 t0_chase_state 持久化到文件。"""
    # datetime 不能直接 JSON 序列化，转为 isoformat 字符串
    state = {}
    for pid, st in holder.t0_chase_state.items():
        entry = dict(st)
        if isinstance(entry.get('last_chase_time'), datetime):
            entry['last_chase_time'] = entry['last_chase_time'].isoformat()
        if isinstance(entry.get('created_at'), datetime):
            entry['created_at'] = entry['created_at'].isoformat()
        state[pid] = entry
    _save_chase_state(state)


def record_t0_pair_state(holder: '_ChaseHolder', pair_id: str, code: str,
                          main_action: str, pair_action: str, shares: int,
                          main_contract: str, pair_contract: str) -> None:
    """配对单下发后记录两条腿到 t0_chase_state（写入点①）。

    main_action : '买入' | '卖出'   （主动腿，已下发）
    pair_action : '卖出' | '买入'   （配对腿，已下发）
    main_contract / pair_contract : 从 getEntrust 捞到的真实合同号
    """
    holder.t0_chase_state[pair_id] = {
        'code': code,
        'leg1_action': main_action,
        'leg1_contract': main_contract,
        'leg2_action': pair_action,
        'leg2_contract': pair_contract,
        'pending_shares': shares,
        'filled_leg': None,         # 由 detect_single_leg 更新
        'pending_action': None,     # 由 detect_single_leg 更新
        'pending_contract': None,   # 由 detect_single_leg 更新
        'chase_count': 0,
        'last_chase_time': None,
        'created_at': datetime.now().isoformat(),
    }
    log.info(f"[T0] 记录配对单 {pair_id} {code}: "
             f"{main_action}(contract={main_contract}) / "
             f"{pair_action}(contract={pair_contract}) {shares}股")


def detect_single_leg(holder: '_ChaseHolder', now: datetime) -> None:
    """每个周期检查追价状态（写入点②）。

    数据源：
      源1 orders/*.json  — 本地记录的两腿 status
      源2 getHoldingShares — THS 真实持仓 vs portfolio.json 基准持仓
      源3 getEntrust('today', False) — 当日全部委托的成交备注

    三源任一显示某腿已成交则判为 filled。
    - 一腿 filled 另一腿 pending → 更新 filled_leg/pending_action/pending_contract
    - 两腿均 filled → 从 t0_chase_state 删除该 pair（清理状态）
    - 两腿均 pending / 接口失败 → 不动
    """
    from state_center import load_today_orders
    import json as _json

    today_orders = load_today_orders()     # {fname: order_dict}
    today_str = now.strftime('%Y%m%d')

    # 读 portfolio.json 基准持仓（源2 基准）
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _pf_path = os.path.join(_root, 'portfolio.json')
    _baseline: dict = {}
    try:
        with open(_pf_path, encoding='utf-8') as _f:
            _pf = _json.load(_f)
        for _code, _ss in _pf.get('_signal_state', {}).items():
            _baseline[_code] = int(_ss.get('shares', 0))
    except Exception:
        pass

    # 尝试调用 THS 接口（一次即可，结果复用）
    _ths_holding: dict = {}    # {code: real_shares}
    _ths_entrusts: dict = {}   # {code: [row, ...]}
    try:
        from evolving.evolving import EvolvingSim
        import time as _time
        _e = EvolvingSim()
        _h = _e.getHoldingShares()
        _time.sleep(1)
        if isinstance(_h, dict) and _h.get('status'):
            for _row in _h.get('data', []) or []:
                if _row:
                    _ths_holding[str(_row[0])] = int(float(_row[6]))
        _ent = _e.getEntrust('today', False)
        _time.sleep(1)
        if isinstance(_ent, dict) and _ent.get('status'):
            for _row in _ent.get('data', []) or []:
                if _row and len(_row) >= 11:
                    _c = str(_row[2])
                    _ths_entrusts.setdefault(_c, []).append(_row)
    except Exception as _ex:
        log.warning(f"[T0] detect_single_leg THS 接口调用失败: {_ex}")

    def _order_file_filled(code, direction):
        """源1：orders/*.json 中是否存在已成交的腿。"""
        for _fname, _ord in today_orders.items():
            if _ord.get('code') != code:
                continue
            if direction == '买入' and '买' not in _ord.get('direction', ''):
                continue
            if direction == '卖出' and '卖' not in _ord.get('direction', ''):
                continue
            if _ord.get('status') == 'filled':
                return True
        return False

    def _order_file_contract(code, direction):
        """从 orders/*.json 获取该腿的合同号（最新一条）。"""
        for _fname in sorted(today_orders.keys(), reverse=True):
            _ord = today_orders[_fname]
            if _ord.get('code') != code:
                continue
            if direction == '买入' and '买' not in _ord.get('direction', ''):
                continue
            if direction == '卖出' and '卖' not in _ord.get('direction', ''):
                continue
            c = _ord.get('contract', 'pending')
            if c and c != 'pending':
                return c
        return None

    def _holding_filled(code, direction, shares):
        """源2：真实持仓 vs 基准持仓。"""
        baseline = _baseline.get(code, 0)
        real = _ths_holding.get(code, -1)
        if real < 0:
            return False
        if direction == '买入':
            return (real - baseline) >= shares
        else:
            return (baseline - real) >= shares

    def _entrust_filled(code, direction, shares):
        """源3：当日委托回报中成交备注。"""
        rows = _ths_entrusts.get(code, [])
        for _row in rows:
            _op = str(_row[4])
            _remark = str(_row[5])
            _qty_str = str(_row[6])
            if direction == '买入' and '买' not in _op:
                continue
            if direction == '卖出' and '卖' not in _op:
                continue
            _filled = (('已成' in _remark or '全部' in _remark)
                       and '未成' not in _remark and '撤' not in _remark)
            if not _filled:
                continue
            try:
                if int(float(_qty_str)) >= shares:
                    return True
            except ValueError:
                pass
        return False

    to_delete = []
    for pair_id, st in list(holder.t0_chase_state.items()):
        code = st.get('code', '')
        leg1_action = st.get('leg1_action', '')
        leg2_action = st.get('leg2_action', '')
        leg1_contract = st.get('leg1_contract', 'pending')
        leg2_contract = st.get('leg2_contract', 'pending')
        shares = int(st.get('pending_shares', 0))

        # 三源判断两腿成交状态
        leg1_filled = (
            _order_file_filled(code, leg1_action) or
            _holding_filled(code, leg1_action, shares) or
            _entrust_filled(code, leg1_action, shares)
        )
        leg2_filled = (
            _order_file_filled(code, leg2_action) or
            _holding_filled(code, leg2_action, shares) or
            _entrust_filled(code, leg2_action, shares)
        )

        log.info(f"[T0] detect {pair_id} {code}: "
                 f"{leg1_action}={'filled' if leg1_filled else 'pending'} "
                 f"{leg2_action}={'filled' if leg2_filled else 'pending'}")

        if leg1_filled and leg2_filled:
            log.info(f"[T0] {pair_id} {code} 两腿均成交，清理追价状态")
            to_delete.append(pair_id)
            continue

        if leg1_filled and not leg2_filled:
            # 尝试从最新 orders/*.json 获取 leg2 合同号（如 trade.py 已写入）
            fresh_contract = _order_file_contract(code, leg2_action) or leg2_contract
            st['filled_leg'] = leg1_action
            st['pending_action'] = leg2_action
            st['pending_contract'] = fresh_contract
            log.info(f"[T0] {pair_id} {code} 单腿: "
                     f"已成={leg1_action}, 待追={leg2_action} contract={fresh_contract}")
            continue

        if leg2_filled and not leg1_filled:
            fresh_contract = _order_file_contract(code, leg1_action) or leg1_contract
            st['filled_leg'] = leg2_action
            st['pending_action'] = leg1_action
            st['pending_contract'] = fresh_contract
            log.info(f"[T0] {pair_id} {code} 单腿: "
                     f"已成={leg2_action}, 待追={leg1_action} contract={fresh_contract}")
            continue

        # 两腿均 pending，不处理
        log.info(f"[T0] {pair_id} {code} 两腿均未成交，跳过")

    for pid in to_delete:
        del holder.t0_chase_state[pid]


def _fetch_new_contract_after_chase(code: str, direction: str, price: float) -> str:
    """追价重挂后从 getEntrust 捞新合同号（写入点③）。找不到返回空串。"""
    try:
        from evolving.evolving import EvolvingSim
        import time as _time
        _e = EvolvingSim()
        _time.sleep(2)
        ent = _e.getEntrust('today', False)
        _time.sleep(1)
    except Exception as ex:
        log.warning(f"[T0] _fetch_new_contract_after_chase getEntrust 失败: {ex}")
        return ''
    if not (isinstance(ent, dict) and ent.get('status') and ent.get('data')):
        return ''
    for row in reversed(ent['data']):
        if not row or len(row) < 11:
            continue
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
        if abs(row_price - price) < 0.002:
            contract = str(row[10]).strip()
            if contract and contract != '0':
                return contract
    return ''


class T0Strategy(BaseStrategy):
    """做T配对策略 — 评估做T信号并生成配对挂单"""

    def evaluate_t0(self, t, pos, price, today_str, atr_pct):
        """评估做T信号

        返回:
          dict with keys: t0_signal, buy_score, sell_score, trade_type, action, position_ratio, reason, t0_pair
        """
        rsi_5min = t.get("rsi_5min")
        macd_5min_status = t.get("macd_5min_status")
        vol_ratio_5min = t.get("vol_ratio_5min")
        ma20_5min_slope = t.get("ma20_5min_slope")

        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_hours = market_open <= now <= market_close
        in_auction = is_auction_time(now)

        if not is_market_hours or rsi_5min is None or in_auction:
            buy_score = 0
            sell_score = 0
        else:
            buy_score = t0_buy_score(rsi_5min, macd_5min_status, vol_ratio_5min)
            sell_score = t0_sell_score(rsi_5min, macd_5min_status, vol_ratio_5min)

            if ma20_5min_slope is not None:
                if ma20_5min_slope > _CONF.get("t0", {}).get("ma20_slope_sell_cap", 0.003):
                    sell_score = 0
                elif ma20_5min_slope < _CONF.get("t0", {}).get("ma20_slope_buy_cap", -0.005):
                    buy_score = 0

        t0_signal = "无"
        if not is_market_hours:
            t0_signal = "无(非交易时段)"
        elif in_auction:
            t0_signal = "无(集合竞价时段)"

        result = {
            "t0_signal": t0_signal,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "trade_type": None,
            "action": None,
            "position_ratio": None,
            "reason": None,
            "t0_pair": None,
        }

        if not pos.has_position:
            if buy_score >= 2 or sell_score >= 2:
                result["t0_signal"] = "有做T5m信号但无底仓"
            return result

        if buy_score >= 2:
            if pos.can_t0_today(today_str, atr_pct):
                t0_signal = f"买入做T5m({buy_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
            else:
                t0_signal = f"买入做T5m({buy_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
            result["t0_signal"] = t0_signal
        elif sell_score >= 2:
            if pos.can_t0_today(today_str, atr_pct):
                t0_signal = f"卖出做T5m({sell_score}项共振,RSI5m={rsi_5min},MACD5m={macd_5min_status},量比5m={vol_ratio_5min})"
            else:
                t0_signal = f"卖出做T5m({sell_score}项信号,今日做T次数已用尽({pos._get_daily_log(today_str).get('t0_count', 0)}/{pos.max_daily_t0(atr_pct)}))"
            result["t0_signal"] = t0_signal

        return result

    def evaluate_t0_execute(self, result, t, pos, price, today_str, atr_pct, record_t0=True):
        """从 evaluate_t0 结果中解析出具体的执行动作

        返回: (action, position_ratio, trade_type, reason, t0_pair) 或 None
        """
        buy_score = result["buy_score"]
        sell_score = result["sell_score"]
        t0_signal = result["t0_signal"]

        if ((buy_score >= 2 or sell_score >= 2) and pos.has_position
              and pos.can_t0_today(today_str, atr_pct)):

            # ── 时间窗口限制: 仅 9:35-10:30 和 13:30-14:20 允许开新做T ──
            now = datetime.now()
            if not _in_t0_window(now):
                log.info(f"[T0] 窗口外跳过 {now.strftime('%H:%M')}")
                return None

            if "买入" in t0_signal:
                if record_t0:
                    pos.record_t0(today_str)
                ratio = _CONF.get("t0", {}).get("t0_buy_ratio", 0.30)
                atr_5m = t.get("atr_5min")
                t0_pair = None
                if atr_5m and price > 0:
                    pair_shares = max(int(pos.shares * ratio / 100) * 100, _CONF.get("trade", {}).get("min_shares", 5000))
                    pair_price = calc_t0_pair_price(price, pair_shares, True, atr_5min=atr_5m)  # Bug D: 传入ATR
                    if pair_shares >= 100 and pair_price > 0:
                        spread = abs(pair_price - price) * pair_shares
                        buy_amount = price * pair_shares
                        min_abs = _CONF.get("t0", {}).get("min_spread_abs", 120)
                        min_pct = _CONF.get("t0", {}).get("min_spread_pct", 0.0035)
                        min_required = max(min_abs, min_pct * buy_amount)
                        spread_pct = spread / buy_amount if buy_amount > 0 else 0
                        log.info(f"[T0] 买入做T 价差={spread:.2f}元 占买入金额={spread_pct*100:.3f}% "
                                 f"门槛=max({min_abs}元,{min_pct*100:.2f}%×{buy_amount:.0f}元)={min_required:.2f}元")
                        if spread >= min_required:
                            t0_pair = {
                                "trade_type": "t0_pair",
                                "action": "卖出(配对挂单)",
                                "price": price,
                                "pair_price": pair_price,
                                "shares": pair_shares,
                                "spread": round(spread, 2),
                                "reason": f"做T买入后高位卖出: 动态价差门槛={min_required:.2f}元"
                                          f"(=max({min_abs}元,{min_pct*100:.2f}%×{buy_amount:.0f}元)), 挂单价={pair_price:.3f}",
                            }
                        else:
                            log.info(f"[T0] 价差不足, 买入做T配对单不下发(t0_pair=None)")
                return ("买入", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

            elif "卖出" in t0_signal:
                if record_t0:
                    pos.record_t0(today_str)
                ratio = _CONF.get("t0", {}).get("t0_sell_ratio", _CONF.get("signal", {}).get("t0_sell_half_ratio", 0.1))
                atr_5m = t.get("atr_5min")
                t0_pair = None
                if atr_5m and price > 0:
                    pair_shares = max(int(pos.shares * ratio / 100) * 100, _CONF.get("trade", {}).get("min_shares", 5000))
                    pair_price = calc_t0_pair_price(price, pair_shares, False, atr_5min=atr_5m)  # Bug D: 传入ATR
                    if pair_shares >= 100 and pair_price > 0:
                        spread = abs(price - pair_price) * pair_shares
                        sell_amount = price * pair_shares
                        min_abs = _CONF.get("t0", {}).get("min_spread_abs", 120)
                        min_pct = _CONF.get("t0", {}).get("min_spread_pct", 0.0035)
                        min_required = max(min_abs, min_pct * sell_amount)
                        spread_pct = spread / sell_amount if sell_amount > 0 else 0
                        log.info(f"[T0] 卖出做T 价差={spread:.2f}元 占金额={spread_pct*100:.3f}% "
                                 f"门槛=max({min_abs}元,{min_pct*100:.2f}%×{sell_amount:.0f}元)={min_required:.2f}元")
                        if spread >= min_required:
                            t0_pair = {
                                "trade_type": "t0_pair",
                                "action": "买入(配对挂单)",
                                "price": price,
                                "pair_price": pair_price,
                                "shares": pair_shares,
                                "spread": round(spread, 2),
                                "reason": f"做T卖出后低位接回: 动态价差门槛={min_required:.2f}元"
                                          f"(=max({min_abs}元,{min_pct*100:.2f}%×{sell_amount:.0f}元)), 挂单价={pair_price:.3f}",
                            }
                        else:
                            log.info(f"[T0] 价差不足, 卖出做T配对单不下发(t0_pair=None)")
                return ("卖出", f"{ratio*100:.0f}%", "t0", t0_signal + "(量比)", t0_pair)

        return None

    def check_t0_pair_chase(self, pos, now, quote_fn, cancel_fn, place_fn, t0_close_fn=None):
        """单腿成交后追价

        每 5 分钟检查一次，最多追 3 次，14:45 前清不掉调 t0_close 平仓路径。

        参数:
          pos:         PositionInfo 实例，pos.t0_chase_state 存追价状态
          now:         datetime 当前时间（便于测试注入）
          quote_fn:    quote_fn(code) -> {'bid1': float, 'ask1': float}
          cancel_fn:   cancel_fn(contract) -> bool  撤单
          place_fn:    place_fn(action, code, shares, price) -> bool  下单(action='买入'/'卖出')
          t0_close_fn: 14:45仍未清干净时的平仓回调，None则仅打印[ALERT]

        t0_chase_state 每条结构:
          {
            'code': str,
            'filled_leg': '买入' | '卖出',    # 已成交腿
            'pending_action': '买入' | '卖出', # 未成交腿方向
            'pending_shares': int,
            'pending_contract': str | None,
            'chase_count': int,               # 已追价次数
            'last_chase_time': datetime | None,
          }

        返回: list of (pair_id, result_tag) 本次执行记录
        """
        CHASE_INTERVAL = 300   # 5分钟(秒)
        MAX_CHASE = 3
        DEADLINE = now.replace(hour=14, minute=45, second=0, microsecond=0)

        actions = []
        for pair_id, st in list(pos.t0_chase_state.items()):
            code            = st.get('code', '')
            pending_action  = st.get('pending_action', '')   # '买入' or '卖出'
            shares          = st.get('pending_shares', 0)
            contract        = st.get('pending_contract')
            chase_count     = st.get('chase_count', 0)
            last_time       = st.get('last_chase_time')

            # ── 14:45 截止: 清不掉调 t0_close ──────────────────────────
            if now >= DEADLINE:
                log.warning(f"[ALERT] [T0] {pair_id} {code} 14:45截止仍有单腿未成交, "
                            f"调用t0_close平仓路径 pending={pending_action} {shares}股")
                if t0_close_fn is not None:
                    try:
                        t0_close_fn()
                    except Exception as ex:
                        log.error(f"[T0] t0_close_fn调用异常: {ex}")
                actions.append((pair_id, "deadline_alert"))
                continue

            # ── 每 5 分钟一次 ───────────────────────────────────────────
            if last_time is not None:
                # 从文件加载时 last_chase_time 是 isoformat 字符串，需解析
                if isinstance(last_time, str):
                    try:
                        last_time = datetime.fromisoformat(last_time)
                    except ValueError:
                        last_time = None
                if last_time is not None:
                    elapsed = (now - last_time).total_seconds()
                    if elapsed < CHASE_INTERVAL:
                        continue

            # ── 最多追 3 次 ─────────────────────────────────────────────
            if chase_count >= MAX_CHASE:
                log.info(f"[T0] {pair_id} {code} 追价已达上限{MAX_CHASE}次, 等待14:45截止清仓")
                continue

            # ── 取对手价: 买入腿用卖一(ask1), 卖出腿用买一(bid1) ────────
            q = quote_fn(code) if callable(quote_fn) else None
            if not q:
                log.warning(f"[T0] {pair_id} {code} 行情获取失败, 跳过本次追价")
                actions.append((pair_id, "quote_fail"))
                continue
            if pending_action == '买入':
                new_price = float(q.get('ask1') or 0)
            else:
                new_price = float(q.get('bid1') or 0)
            if new_price <= 0:
                log.warning(f"[T0] {pair_id} {code} 对手价无效({pending_action} ask1/bid1={new_price}), 跳过")
                actions.append((pair_id, "price_invalid"))
                continue

            # ── 撤未成交腿 ──────────────────────────────────────────────
            revoke_ok = True
            if contract:
                revoke_ok = cancel_fn(contract) if callable(cancel_fn) else True
            if not revoke_ok:
                log.warning(f"[T0] {pair_id} {code} 撤单失败(contract={contract}), 跳过追价")
                actions.append((pair_id, "cancel_fail"))
                continue

            # ── 按对手价重挂 ────────────────────────────────────────────
            ok = place_fn(pending_action, code, shares, new_price) if callable(place_fn) else True
            n = chase_count + 1
            log.info(f"[T0] {pair_id} {code} 追价第{n}/{MAX_CHASE}次: "
                     f"{pending_action} {shares}股 @{new_price:.3f}(对手价) "
                     f"{'成功' if ok else '失败'}")

            st['chase_count'] = n
            st['last_chase_time'] = now

            # ── place_fn 成功后回写新合同号，确保下次撤单有效 ────────
            if ok:
                new_contract = _fetch_new_contract_after_chase(code, pending_action, new_price)
                if new_contract and new_contract != 'pending':
                    st['pending_contract'] = new_contract
                    log.info(f"[T0] {pair_id} {code} 回写新合同号={new_contract}")

            actions.append((pair_id, f"chase_{n}"))

        return actions