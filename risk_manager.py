#!/usr/bin/env python3
"""
风险管理器 — 止损检查、仓位上限检查、日亏损限额检查

从 strategy.py 和 signal_generator.py 提取风险控制逻辑:
  - 止损检查 (strategy.evaluate_stop 中的止损逻辑)
  - 仓位上限检查 (50% 单只上限)
  - 日亏损限额检查 (daily drawdown limit)

职责:
  1. 止损信号评估 (是否触发止损/止盈/减仓)
  2. 仓位上限检查 (当前仓位是否超过50%)
  3. 日亏损限额检查 (当日亏损是否超过限额)

用法:
  from risk_manager import RiskManager
  rm = RiskManager()
  stop_actions = rm.check_stop_loss(pos, tech, price, today_str)
  if rm.is_position_capped(pos, price, TOTAL_FUND):
      print("仓位已满")
  if rm.is_daily_loss_limit_hit(positions, price, today_str):
      print("日亏损限额已到")
"""

from typing import Tuple

# 从 position_info 导入统一常量
from position_info import (
    TOTAL_FUND, MAX_POSITION_RATIO,
)

# 从 config.yaml 读取日亏损限额
import yaml as _yaml, os as _os
_config_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'config.yaml')
try:
    with open(_config_path, 'r') as _f:
        _cfg = _yaml.safe_load(_f) or {}
    _daily_loss = _cfg.get('stop_loss', {}).get('daily_loss_limit_pct', 0.20)
except Exception:
    _daily_loss = 0.20

# strategy_v2: 止盈激活与 tier1 止损参数
_v2_cfg = _cfg.get('strategy_v2', {})
_V2_ENABLED = bool(_v2_cfg.get('enabled', False))
_V2_E1_ACTIVATION = float(_v2_cfg.get('e1_activation_v2', 0.05))
_V2_TIER1_PCT = float(_v2_cfg.get('tier1_pct_v2', 0.06))

from datetime import datetime as _dt, timedelta as _td

# ═══════════════════════════════════════════════════════════════
# 实盘风控闸门（只被 executor 实盘下单路径调用；回测不经过这里）
#   闸门1 日亏限额  闸门2 连亏熔断  闸门3 单票仓位上限
#   状态落盘 logs/risk_state.json（原子写），拦截写 logs/alerts/alert_YYYY-MM-DD.log
# ═══════════════════════════════════════════════════════════════

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
RISK_STATE_PATH = _os.path.join(_ROOT, 'logs', 'risk_state.json')
RISK_ALERT_DIR = _os.path.join(_ROOT, 'logs', 'alerts')

# 阈值全部来自 config.yaml risk 段；缺省值保守但不激进
_RISK_DEFAULTS = {
    'daily_loss_limit': 0.03,
    'consecutive_loss_count': 4,
    'cooldown_days': 3,
    'single_position_cap': 0.50,
}


def single_position_cap(mode=None):
    """单票仓位上限按策略模式取值（全系统唯一取值点）。

      chase   : 单吊 N=1，单只目标就是 100% → 读 chase.max_single_pct（config 1.0）
      flow    : 20 只等权 5%/只 → 读 risk.single_position_cap（0.50）
      pyramid : 自带权重体系 → 读 risk.single_position_cap（0.50）
    mode=None 时取 config.yaml strategy.mode。
    """
    r = (_cfg.get('risk', {}) or {})
    risk_cap = float(r.get('single_position_cap', _RISK_DEFAULTS['single_position_cap']))
    m = mode if mode is not None else ((_cfg.get('strategy', {}) or {}).get('mode') or '')
    if str(m).strip().lower() == 'chase':
        c = (_cfg.get('chase', {}) or {}).get('max_single_pct')
        if c is not None:
            return float(c)
    return risk_cap


def risk_config():
    """读取 config.yaml risk 段阈值（运行期读取，便于测试与调参）"""
    r = (_cfg.get('risk', {}) or {})
    return {
        'daily_loss_limit': float(r.get('daily_loss_limit', _RISK_DEFAULTS['daily_loss_limit'])),
        'consecutive_loss_count': int(r.get('consecutive_loss_count', _RISK_DEFAULTS['consecutive_loss_count'])),
        'cooldown_days': int(r.get('cooldown_days', _RISK_DEFAULTS['cooldown_days'])),
        'single_position_cap': single_position_cap(),
    }


def _today_str():
    return _dt.now().strftime('%Y-%m-%d')


def _empty_risk_state(today_str):
    return {
        'date': today_str,
        'daily_realized_pnl': 0.0,
        'daily_float_pnl': 0.0,
        'daily_pnl': 0.0,
        'daily_pnl_pct': 0.0,
        'total_asset': 0.0,
        'consecutive_losses': 0,
        'loss_streak_started': '',
        'circuit_breaker_until': '',
        'circuit_breaker_reason': '',
        'trades': [],
        'updated_at': '',
    }


def load_risk_state(today_str=None):
    """读取风控状态（文件损坏/缺失时自动从 .bak 恢复，仍无则返回空状态）"""
    from core.atomic_writer import atomic_read_json
    st = atomic_read_json(RISK_STATE_PATH, default=None)
    if not isinstance(st, dict):
        st = {}
    base = _empty_risk_state(today_str or _today_str())
    for k, v in base.items():
        st.setdefault(k, v)
    if not isinstance(st.get('trades'), list):
        st['trades'] = []
    return st


def save_risk_state(state):
    """原子写风控状态（临时文件 + fsync + rename，并发写不会出现半截 JSON）"""
    from core.atomic_writer import atomic_write_json
    state['updated_at'] = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
    return atomic_write_json(RISK_STATE_PATH, state)


def _roll_risk_day(state, today_str):
    """日切：重置当日盈亏/当日逐笔，保留连亏计数与熔断截止日"""
    if state.get('date') == today_str:
        return False
    state['date'] = today_str
    for k in ('daily_realized_pnl', 'daily_float_pnl', 'daily_pnl', 'daily_pnl_pct'):
        state[k] = 0.0
    state['trades'] = []
    return True


def _portfolio_positions():
    try:
        from state_center import load_portfolio
        return ((load_portfolio() or {}).get('positions') or {})
    except Exception:
        return {}


def _portfolio_total_asset():
    try:
        from state_center import load_portfolio
        ta = float(((load_portfolio() or {}).get('account') or {}).get('total_asset') or 0)
        if ta > 0:
            return ta
    except Exception:
        pass
    return float(TOTAL_FUND)


def calc_floating_pnl(quotes=None):
    """当日浮动盈亏 = Σ 持仓股数 ×（现价 − 昨收）；无昨收数据的标的不计入。"""
    total = 0.0
    for code, pos in _portfolio_positions().items():
        try:
            shares = int(pos.get('shares', 0) or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0:
            continue
        q = (quotes or {}).get(code) or {}
        try:
            price = float(q.get('price') or 0)
            prev = float(q.get('prev_close') or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and prev > 0:
            total += (price - prev) * shares
    return round(total, 2)


def get_daily_loss_info(today_str=None, quotes=None, total_asset=None):
    """闸门1 数据：当日已实现 + 浮动盈亏、占总资产比例、限额"""
    today_str = today_str or _today_str()
    cfg = risk_config()
    st = load_risk_state(today_str)
    rolled = _roll_risk_day(st, today_str)
    realized = float(st.get('daily_realized_pnl', 0) or 0)
    if quotes:
        floating = calc_floating_pnl(quotes)
    else:
        floating = float(st.get('daily_float_pnl', 0) or 0)
    ta = float(total_asset or 0) or _portfolio_total_asset()
    daily_pnl = round(realized + floating, 2)
    pct = daily_pnl / ta if ta > 0 else 0.0
    if rolled:
        st['daily_pnl'] = daily_pnl
        st['daily_pnl_pct'] = round(pct, 6)
        st['daily_float_pnl'] = floating
        st['total_asset'] = round(ta, 2)
        save_risk_state(st)
    return {
        'ok': pct > -cfg['daily_loss_limit'],
        'daily_pnl': daily_pnl,
        'realized': realized,
        'floating': floating,
        'pct': pct,
        'limit': cfg['daily_loss_limit'],
        'total_asset': ta,
        'state': st,
    }


def check_daily_loss_limit(today_str=None, quotes=None, total_asset=None):
    """闸门1 日亏限额：当日亏损达到阈值 → 禁止新开仓。

    返回 (ok: bool, reason: str)；reason 带具体数值。
    """
    info = get_daily_loss_info(today_str, quotes=quotes, total_asset=total_asset)
    if not info['ok']:
        return False, (
            f"当日亏损 {info['pct']*100:.1f}% 已触达日亏限额 -{info['limit']*100:.1f}%"
            f"（已实现 {info['realized']:+.0f}元 + 浮动 {info['floating']:+.0f}元）")
    return True, (
        f"当日盈亏 {info['pct']*100:+.2f}%（已实现 {info['realized']:+.0f} + "
        f"浮动 {info['floating']:+.0f} = {info['daily_pnl']:+.0f}元），"
        f"限额 -{info['limit']*100:.1f}%")


def get_consecutive_loss_info(today_str=None):
    """闸门2 数据：连亏笔数、熔断截止日、阈值；冷却到期自动解锁并清零连亏"""
    today_str = today_str or _today_str()
    cfg = risk_config()
    st = load_risk_state(today_str)
    until = str(st.get('circuit_breaker_until') or '')
    if until and today_str > until:
        # 冷却到期 → 复位，重新计数
        st['circuit_breaker_until'] = ''
        st['circuit_breaker_reason'] = ''
        st['consecutive_losses'] = 0
        st['loss_streak_started'] = ''
        save_risk_state(st)
        until = ''
    streaks = int(st.get('consecutive_losses', 0) or 0)
    active = bool(until) and today_str <= until
    return {
        'consecutive_losses': streaks,
        'consecutive_loss_count': cfg['consecutive_loss_count'],
        'cooldown_days': cfg['cooldown_days'],
        'circuit_breaker_active': active,
        'circuit_breaker_until': until,
        'circuit_breaker_reason': str(st.get('circuit_breaker_reason') or ''),
        'loss_streak_started': str(st.get('loss_streak_started') or ''),
        'last_pnl_date': str(st.get('date') or ''),
    }


def check_consecutive_loss(today_str=None):
    """闸门2 连亏熔断：熔断期内禁止新开仓。

    返回 (blocked: bool, reason: str)；reason 带具体数值。
    """
    info = get_consecutive_loss_info(today_str)
    n = info['consecutive_losses']
    threshold = info['consecutive_loss_count']
    if info['circuit_breaker_active']:
        return True, f"熔断中（连亏 {n} 笔，冷却至 {info['circuit_breaker_until']}）"
    if n > 0:
        return False, f"连亏 {n}/{threshold} 笔，未触发熔断"
    return False, f"连亏 0/{threshold} 笔，无冷却"


def reset_circuit_breaker(today_str=None):
    """手动/自动解除熔断：清空连亏计数与冷却截止日"""
    today_str = today_str or _today_str()
    st = load_risk_state(today_str)
    st['consecutive_losses'] = 0
    st['loss_streak_started'] = ''
    st['circuit_breaker_until'] = ''
    st['circuit_breaker_reason'] = ''
    save_risk_state(st)
    return get_consecutive_loss_info(today_str)


def check_single_position_cap(code, price, shares, total_asset=None, quotes=None,
                              allow_compress=True, mode=None):
    """闸门3 单票仓位上限：目标市值（当前+本次买入）/ 总资产 > 上限。

    上限按策略模式取值（chase=chase.max_single_pct / 其余=risk.single_position_cap）。
    超限时优先压缩到上限（压到不足 100 股则拒绝），返回 dict：
      {ok, shares, cur_ratio, target_ratio, cap, compressed, reason}
    """
    cap = single_position_cap(mode)
    ta = float(total_asset or 0) or _portfolio_total_asset()
    pos = _portfolio_positions().get(code, {}) or {}
    try:
        cur_shares = int(pos.get('shares', 0) or 0)
    except (TypeError, ValueError):
        cur_shares = 0
    px = float(((quotes or {}).get(code) or {}).get('price') or 0) or float(price or 0)
    new_shares = int(shares or 0)
    if ta <= 0 or px <= 0:
        return {'ok': True, 'shares': new_shares, 'cur_ratio': 0.0, 'target_ratio': 0.0,
                'cap': cap, 'compressed': False, 'reason': '缺总资产/价格，跳过单票上限校验'}

    from core.position_manager import PositionManager
    pm = PositionManager(total_fund=ta)
    pm.max_position_ratio = cap   # 阈值按模式取值：chase→chase.max_single_pct，其余→risk.single_position_cap
    cur_mv = cur_shares * px
    cur_ratio = pm.position_value_ratio(code, {code: {'market_value': cur_mv}})
    room = pm.available_position_ratio(code, {code: {'market_value': cur_mv}})
    target_ratio = (cur_mv + new_shares * px) / ta

    if target_ratio <= cap:
        return {'ok': True, 'shares': new_shares, 'cur_ratio': cur_ratio,
                'target_ratio': target_ratio, 'cap': cap, 'compressed': False,
                'reason': (f"目标占比 {target_ratio*100:.1f}% ≤ 上限 {cap*100:.0f}%"
                           f"（当前 {cur_ratio*100:.1f}%，剩余额度 {room*100:.1f}%）")}

    if allow_compress:
        max_by_cap = int(max(cap * ta - cur_mv, 0.0) / px / 100) * 100
        if max_by_cap >= 100:
            new_ratio = (cur_mv + max_by_cap * px) / ta
            return {'ok': True, 'shares': max_by_cap, 'cur_ratio': cur_ratio,
                    'target_ratio': target_ratio, 'cap': cap, 'compressed': True,
                    'reason': (f"单票占比 {target_ratio*100:.0f}% > 上限 {cap*100:.0f}%，"
                               f"压缩股数 {new_shares}→{max_by_cap}（占比降至 {new_ratio*100:.1f}%）")}

    return {'ok': False, 'shares': 0, 'cur_ratio': cur_ratio, 'target_ratio': target_ratio,
            'cap': cap, 'compressed': False,
            'reason': f"单票占比 {target_ratio*100:.0f}% > 上限 {cap*100:.0f}%"}


def _write_risk_alert(line, today_str=None):
    """拦截记录落 alerts 日志（logs/alerts/alert_YYYY-MM-DD.log）"""
    today_str = today_str or _today_str()
    try:
        _os.makedirs(RISK_ALERT_DIR, exist_ok=True)
        path = _os.path.join(RISK_ALERT_DIR, f'alert_{today_str}.log')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f"[{_dt.now().strftime('%H:%M:%S')}] [RISK] {line}\n")
    except Exception as ex:
        print(f"[RISK] ⚠️ 告警日志写入失败: {ex}")


def _risk_block(line, today_str=None):
    print(f"[RISK] {line}")
    _write_risk_alert(line, today_str)
    return {'ok': False, 'shares': 0, 'reason': line}


def pre_trade_gate(code, price, shares, trade_type='buy', total_asset=None,
                   quotes=None, today_str=None, mode=None):
    """实盘下单前统一风控闸门（executor 唯一调用点）。

    只拦买入/加仓（trade_type=buy 或做T买入腿）；卖出/减仓/止损/做T平仓由调用方免检。
    按 a 日亏限额 → b 连亏熔断 → c 单票上限 顺序校验，每道打印结论（含数值）。
    mode: 当前策略模式（chase/flow/pyramid），决定 c 的单票上限；None 时取 config strategy.mode。

    返回 {'ok': bool, 'shares': int, 'reason': str}（压缩时 shares 为压缩后股数）
    """
    today_str = today_str or _today_str()
    shares = int(shares or 0)
    allow_compress = (trade_type == 'buy')   # 做T买入腿不改股数（配对单已定）

    # a. 日亏限额
    d_ok, d_reason = check_daily_loss_limit(today_str, quotes=quotes, total_asset=total_asset)
    if not d_ok:
        return _risk_block(f"拒绝开仓 {code}：{d_reason}", today_str)
    print(f"[RISK] PASS 日亏限额 {code}：{d_reason}")

    # b. 连亏熔断
    c_blocked, c_reason = check_consecutive_loss(today_str)
    if c_blocked:
        return _risk_block(f"{c_reason}，拒绝开仓 {code}", today_str)
    print(f"[RISK] PASS 连亏熔断 {code}：{c_reason}")

    # c. 单票仓位上限（上限按 mode 取值）
    cap = check_single_position_cap(code, price, shares, total_asset=total_asset,
                                    quotes=quotes, allow_compress=allow_compress, mode=mode)
    if not cap['ok']:
        return _risk_block(f"拒绝开仓 {code}：{cap['reason']}", today_str)
    if cap['compressed']:
        print(f"[RISK] 压缩开仓 {code}：{cap['reason']}")
    else:
        print(f"[RISK] PASS 单票上限 {code}：{cap['reason']}")

    return {'ok': True, 'shares': int(cap['shares']), 'reason': '通过'}


def record_daily_pnl(today_str=None, pnl_amount=None, trade=None, quotes=None,
                     total_asset=None):
    """成交后记账：当日盈亏 + 逐笔结果；日切自动重置当日字段。

    trade: {'code','direction','shares','price','pnl'}，pnl 为已实现盈亏（无成本基准时 None）。
    连亏计数：pnl<0 记 1 笔亏损，pnl>=0 清零；达到 consecutive_loss_count 笔 → 熔断 cooldown_days 天。
    返回写入后的状态 dict。
    """
    today_str = today_str or _today_str()
    cfg = risk_config()
    st = load_risk_state(today_str)
    _roll_risk_day(st, today_str)

    if trade:
        pnl = trade.get('pnl')
        try:
            pnl = round(float(pnl), 2) if pnl is not None else None
        except (TypeError, ValueError):
            pnl = None
        st['trades'].append({
            'time': _dt.now().strftime('%H:%M:%S'),
            'code': trade.get('code', ''),
            'direction': trade.get('direction', ''),
            'shares': int(trade.get('shares', 0) or 0),
            'price': float(trade.get('price', 0) or 0),
            'pnl': pnl,
            'result': 'unknown' if pnl is None else ('loss' if pnl < 0 else 'win'),
        })
        st['trades'] = st['trades'][-200:]
        if pnl is not None:
            st['daily_realized_pnl'] = round(float(st.get('daily_realized_pnl', 0) or 0) + pnl, 2)
            if pnl < 0:
                if int(st.get('consecutive_losses', 0) or 0) == 0:
                    st['loss_streak_started'] = today_str
                st['consecutive_losses'] = int(st.get('consecutive_losses', 0) or 0) + 1
            else:
                st['consecutive_losses'] = 0
                st['loss_streak_started'] = ''
            if int(st['consecutive_losses']) >= cfg['consecutive_loss_count']:
                until = (_dt.strptime(today_str, '%Y-%m-%d')
                         + _td(days=cfg['cooldown_days'])).strftime('%Y-%m-%d')
                st['circuit_breaker_until'] = until
                st['circuit_breaker_reason'] = f"连亏 {st['consecutive_losses']} 笔"

    if pnl_amount is not None:
        st['daily_pnl'] = round(float(pnl_amount), 2)
        st['daily_float_pnl'] = round(st['daily_pnl'] - float(st.get('daily_realized_pnl', 0) or 0), 2)
    else:
        floating = calc_floating_pnl(quotes) if quotes else float(st.get('daily_float_pnl', 0) or 0)
        st['daily_float_pnl'] = floating
        st['daily_pnl'] = round(float(st.get('daily_realized_pnl', 0) or 0) + floating, 2)

    ta = float(total_asset or 0) or _portfolio_total_asset()
    st['total_asset'] = round(ta, 2)
    st['daily_pnl_pct'] = round(st['daily_pnl'] / ta, 6) if ta > 0 else 0.0
    save_risk_state(st)
    return st


def record_trade_result(code, direction, shares, price, today_str=None, quotes=None,
                        total_asset=None, trade_type=''):
    """成交后记账统一入口：卖出/减仓/止损按 entry_avg_cost 算本笔已实现盈亏。

    direction: '买入' / '卖出'（也接受 buy/sell）
    买入笔 pnl=None（不产生已实现盈亏，也不影响连亏计数）。
    """
    shares = int(shares or 0)
    price = float(price or 0)
    is_sell = str(direction) in ('卖出', 'sell', 'SELL', 'Sell')
    pnl = None
    entry_cost = 0.0
    if is_sell and shares > 0 and price > 0:
        try:
            from state_center import load_portfolio
            pf = load_portfolio() or {}
            sig = (pf.get('_signal_state') or {}).get(code) or {}
            entry_cost = float(sig.get('entry_avg_cost') or 0)
            if entry_cost <= 0:
                entry_cost = float(((pf.get('positions') or {}).get(code) or {}).get('avg_cost') or 0)
        except Exception:
            entry_cost = 0.0
        if entry_cost > 0:
            pnl = round((price - entry_cost) * shares, 2)
    return record_daily_pnl(
        today_str, trade={'code': code, 'direction': str(direction), 'shares': shares,
                          'price': price, 'pnl': pnl},
        quotes=quotes, total_asset=total_asset)


class RiskManager:
    """风险管理器 — 统一处理止损、仓位上限、日亏损限额、连续亏损熔断"""

    def __init__(self, state_center=None, enhanced_trend=False):
        # 延迟导入 state_center，避免循环依赖
        if state_center is None:
            from state_center import StateCenter
            state_center = StateCenter.get_instance()
        self._state = state_center
        self.MAX_DAILY_LOSS_PCT = _daily_loss  # O2: 从config.yaml读取
        self.MAX_CONSECUTIVE_LOSS_DAYS = _cfg.get("risk", {}).get("max_consecutive_loss_days", 3)  # 从config读取
        self.MIN_DAILY_LOSS_PCT = _cfg.get("risk", {}).get("min_daily_loss_pct", 0.01)  # 从config读取
        self.daily_pnl = 0.0
        # 连续亏损追踪
        self.consecutive_loss_days = 0
        self.last_pnl_date = None
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        # 增强趋势止盈模式
        self.enhanced_trend = enhanced_trend

    def record_daily_pnl(self, today_str, pnl_amount=None, trade=None, quotes=None,
                         total_asset=None):
        """记录当日盈亏 + 逐笔成交结果（落盘 logs/risk_state.json，原子写）"""
        st = record_daily_pnl(today_str, pnl_amount=pnl_amount, trade=trade,
                              quotes=quotes, total_asset=total_asset)
        self.daily_pnl = float(st.get('daily_pnl', 0) or 0)
        self.consecutive_loss_days = int(st.get('consecutive_losses', 0) or 0)
        self.last_pnl_date = st.get('date')
        self._circuit_breaker_active = bool(st.get('circuit_breaker_until'))
        self._circuit_breaker_reason = str(st.get('circuit_breaker_reason') or '')
        return st

    def check_consecutive_loss(self, today_str=None):
        """检查连续亏损熔断状态（读落盘状态，冷却到期自动解锁）

        返回:
          (blocked: bool, reason: str)
        """
        blocked, reason = check_consecutive_loss(today_str)
        self._circuit_breaker_active = blocked
        self._circuit_breaker_reason = reason
        return blocked, reason

    def reset_circuit_breaker(self):
        """重置熔断状态（手动恢复；盈利笔也会自动清零）"""
        info = reset_circuit_breaker()
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self.consecutive_loss_days = 0
        self.last_pnl_date = None
        return info

    def get_consecutive_loss_info(self, today_str=None):
        """获取连续亏损状态信息"""
        return get_consecutive_loss_info(today_str)

    # ═══════════════════════════════════════════════
    # 5. 综合工具
    # ═══════════════════════════════════════════════

    def check_stop_loss(self, pos, t, price, today_str, enhanced_trend=None):
        """评估止盈止损信号（同 strategy.evaluate_stop 完全一致）

        参数:
          enhanced_trend: 覆盖实例的 enhanced_trend 设置（默认使用 self.enhanced_trend）

        返回:
          stop_actions: [(signal_name, signal_type), ...]
        """
        if enhanced_trend is None:
            enhanced_trend = self.enhanced_trend
        return self._evaluate_stop_loss(pos, t, price, today_str, enhanced_trend)

    def _evaluate_stop_loss(self, pos, t, price, today_str, enhanced_trend=False):
        # P0-6: 已统一止损逻辑 — rsi_macd.py 的 evaluate_stop 和 resolve_stop_signal
        # 现在委托给 risk_manager，单一来源。risk_manager 是唯一止损逻辑真相来源。
        """止盈止损评估核心逻辑"""
        stop_actions = []

        if not pos.has_position:
            return stop_actions

        # ── 阶梯止盈 + 盈利保护(增量: 只加不改现有止损) ──
        if pos.entry_cost > 0:
            profit_pct = (price - pos.entry_cost) / pos.entry_cost

            # 盈利保护: 浮盈>=3%时止损线上移到成本×1.005(保本+0.5%)
            profit_protect_cfg = _cfg.get("profit_protect", {})
            activate_pct = profit_protect_cfg.get("activate_pct", 0.03)
            stop_margin = profit_protect_cfg.get("stop_margin", 0.005)
            if profit_pct >= activate_pct:
                protect_stop = pos.entry_cost * (1 + stop_margin)
                pos.trailing_stop_price = max(pos.trailing_stop_price, protect_stop)

            # 阶梯止盈: 三级检查，只卖活动仓
            ladder_cfg = _cfg.get("ladder_profit", {})
            if ladder_cfg.get('enabled', False):
                l1_pct = ladder_cfg.get("level1_pct", 0.03)
                l1_sell = ladder_cfg.get("level1_sell", 0.10)
                l2_pct = ladder_cfg.get("level2_pct", 0.05)
                l2_sell = ladder_cfg.get("level2_sell", 0.15)
                l3_pct = ladder_cfg.get("level3_pct", 0.08)
                l3_sell = ladder_cfg.get("level3_sell", 0.20)

                if profit_pct >= l1_pct and not pos.reached_3pct:
                    pos.reached_3pct = True
                    stop_actions.append((f"阶梯止盈{int(l1_pct*100)}%卖活动仓{int(l1_sell*100)}%", f"sell_active_{int(l1_sell*100)}pct"))

                if profit_pct >= l2_pct and not pos.reached_5pct:
                    pos.reached_5pct = True
                    stop_actions.append((f"阶梯止盈{int(l2_pct*100)}%卖活动仓{int(l2_sell*100)}%", f"sell_active_{int(l2_sell*100)}pct"))

                if profit_pct >= l3_pct and not pos.reached_8pct_ladder:
                    pos.reached_8pct_ladder = True
                    stop_actions.append((f"阶梯止盈{int(l3_pct*100)}%卖活动仓{int(l3_sell*100)}%", f"sell_active_{int(l3_sell*100)}pct"))

        # ── 均价止损分级: 仓位<50%→-25%, 50-80%→-20%, >80%→-15% ──
        # ── 浮盈止损上移: 浮盈>5%→保本线, >10%→成本+5% ──
        if pos.entry_cost > 0:
            pos_ratio = pos.shares * price / TOTAL_FUND if price > 0 else 0
            # strategy_v2: tier1 止损收紧到 tier1_pct_v2（0.06）
            _tier1_pct = _V2_TIER1_PCT if _V2_ENABLED else _cfg.get("stop_loss", {}).get("tier1_pct", 0.15)
            if pos_ratio > _cfg.get("stop_loss", {}).get("tier1_ratio", 0.80):
                stop_pct = _tier1_pct
            elif pos_ratio >= _cfg.get("stop_loss", {}).get("tier2_ratio", 0.50):
                stop_pct = _cfg.get("stop_loss", {}).get("tier2_pct", 0.20)
            else:
                stop_pct = _cfg.get("stop_loss", {}).get("tier3_pct", 0.25)
            # 浮盈止损上移
            float_profit_pct = (price - pos.entry_cost) / pos.entry_cost if pos.entry_cost > 0 else 0
            if float_profit_pct > _cfg.get("stop_loss", {}).get("float_profit_trigger2", 0.10):
                adjusted_stop = pos.entry_cost * (1 + _cfg.get("stop_loss", {}).get("float_profit_stop2", 0.05))  # 浮盈>10%, 止损线=成本+5%
            elif float_profit_pct > _cfg.get("stop_loss", {}).get("float_profit_trigger1", 0.05):
                adjusted_stop = pos.entry_cost * (1 + _cfg.get("stop_loss", {}).get("float_profit_stop1", 0.0))  # 浮盈>5%, 止损线=保本
            else:
                adjusted_stop = pos.entry_cost * (1 - stop_pct)
            if price <= adjusted_stop:
                if float_profit_pct > 0.05:
                    stop_actions.append((f"浮盈止损上移 浮盈{float_profit_pct*100:.1f}%(entry={pos.entry_cost:.3f}→stop={adjusted_stop:.3f})", "breakeven_stop"))
                else:
                    stop_actions.append((f"均价止损{stop_pct*100:.0f}%(仓位{pos_ratio*100:.0f}%,avg={pos.entry_cost:.3f}→现价{price:.3f})", "avg_stop_20pct"))

        # ── 硬止盈60%: base_price*1.60(极限方案C) ──
        if pos.base_price and price >= pos.base_price * _cfg.get("stop_loss", {}).get("hard_take_profit_mult", 1.60):
            stop_actions.append((f"硬止盈{(_cfg.get('stop_loss', {}).get('hard_take_profit_mult', 1.60)-1)*100:.0f}%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_60pct"))

        # ── 移动止盈 ──
        if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
            trail_pct = _cfg.get('trail_pct', 0.12)
            label = f"移动止盈{trail_pct*100:.0f}%(峰值回撤{trail_pct*100:.0f}%)"
            stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

        # ── 硬止损25%: 从价格峰值回撤25%清仓(极限方案C: 放宽) ──
        if pos.peak_price > 0 and price < pos.peak_price * (1 - _cfg.get("stop_loss", {}).get("hard_stop_pct", 0.25)):
            stop_actions.append((f"硬止损{_cfg.get('stop_loss', {}).get('hard_stop_pct', 0.25)*100:.0f}%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_25pct"))

        # Level 1a: 连续2天破MA20 → 减30%总仓
        if t["ma20"] and pos.stop_level == 0:
            if price < t["ma20"]:
                if pos.below_ma20_date != today_str:
                    pos.below_ma20_count += 1
                    pos.below_ma20_date = today_str
                if pos.below_ma20_count >= _cfg.get("stop_loss", {}).get("ma20_break_days", 2):
                    stop_actions.append(("分级止损1级:连续2天破MA20减30%", "reduce_30pct_all"))
                    pos.stop_level = 1
            else:
                pos.below_ma20_count = 0
                pos.below_ma20_date = None

        # Level 1b: DIF<0 → 再减30%
        if pos.stop_level == 1 and t["dif"] is not None and t["dif"] < 0:
            stop_actions.append(("分级止损2级:DIF<0再减30%", "reduce_30pct_all"))
            pos.stop_level = 2

        # Level 1c: MACD死叉/绿柱放大 → 清仓（需额外确认: price<MA20持续3天 或 RSI<35）
        if pos.stop_level == 2 and t["macd_status"] in ["死叉", "绿柱放大"]:
            below_ma20_3days = pos.below_ma20_count >= _cfg.get("stop_loss", {}).get("ma20_break_days", 2) + 1
            rsi_low = t["rsi"] is not None and t["rsi"] < _cfg.get("stop_loss", {}).get("rsi_low_confirm", 35)
            if below_ma20_3days or rsi_low:
                stop_actions.append(("分级止损3级:MACD趋势恶化清仓", "liquidate_trend"))
                pos.stop_level = 3

        # 分级止损恢复
        if pos.stop_level > 0 and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0 and t["macd_status"] in ["红柱放大", "红柱缩短"]:
            pos.stop_level = 0
            pos.below_ma20_count = 0
            pos.below_ma20_date = None

        # 分级止损恢复: stop_level=2 但MACD未恶化且价格恢复
        if pos.stop_level == 2 and t["macd_status"] not in ["死叉", "绿柱放大"] and t["ma20"] and price > t["ma20"] and t["dif"] is not None and t["dif"] > 0:
            pos.stop_level = 0
            pos.below_ma20_count = 0
            pos.below_ma20_date = None

        # 趋势止盈 — 增强版 or 原版
        if enhanced_trend:
            # 增强趋势止盈 — MACD红柱缩短+RSI<60 (放宽触发), 卖20%
            if t["macd_status"] == "红柱缩短" and t["rsi"] is not None and t["rsi"] < 60:
                if pos.can_trend_profit_today(today_str):
                    stop_actions.append(("趋势止盈增强(MACD红柱缩短+RSI<60)卖20%活动仓", "trend_profit_sell_enhanced"))
                    pos.record_trend_profit(today_str)
        else:
            # 原版: MACD红柱缩短+破MA5+破MA10 — 卖10%
            if t["macd_status"] == "红柱缩短" and price < t["ma5"] and t.get("ma10") and price < t["ma10"]:
                if pos.can_trend_profit_today(today_str):
                    stop_actions.append(("趋势止盈(MACD红柱缩短+破MA5)卖10%活动仓", "trend_profit_sell"))
                    pos.record_trend_profit(today_str)

        # 破MA5卖活动仓5% — 带冷却机制(保留：贡献显著)
        if price < t["ma5"] and t["rsi"] and t["rsi"] > 50:
            if pos.active_shares > 0 and pos.can_ma5_sell_today(today_str):
                stop_actions.append(("破MA5卖活动仓5%", "sell_active_5pct"))
                pos.record_ma5_sell(today_str)

        return stop_actions

    def resolve_stop_signal(self, pos, stop_actions):
        """从 stop_actions 解析最终止盈止损信号文本"""
        if not stop_actions:
            return "未触发" if pos.has_position else "未触发(无持仓)"

        # 优先级: 清仓 > 减仓 > 卖活动仓
        for sig_name, sig_type in stop_actions:
            if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_60pct",
                            "avg_stop_20pct", "hard_stop_25pct", "breakeven_stop"):
                return sig_name
        for sig_name, sig_type in stop_actions:
            if sig_type in ("reduce_30pct_all",):
                return sig_name
        return stop_actions[0][0]

    # ══════════════════════════════════════════════
    # 2. 仓位上限检查
    # ══════════════════════════════════════════════

    @staticmethod
    def is_position_capped(pos, price, total_fund=None):
        """检查是否已达单票仓位上限（按 strategy.mode 取值：chase 100%，其余 50%）

        参数:
          pos: PositionInfo 实例
          price: 当前价格
          total_fund: 总资金 (默认 TOTAL_FUND)
        返回:
          (is_capped: bool, ratio: float)
        """
        if total_fund is None:
            total_fund = TOTAL_FUND
        if not pos.has_position or total_fund <= 0:
            return False, 0.0
        ratio = pos.shares * price / total_fund
        return ratio >= single_position_cap(), ratio

    @staticmethod
    def get_position_ratio(pos, price, total_fund=None):
        """获取当前持仓占比

        返回:
          float: 0-1 之间的占比
        """
        if total_fund is None:
            total_fund = TOTAL_FUND
        if not pos.has_position or total_fund <= 0:
            return 0.0
        return pos.shares * price / total_fund

    # ═══════════════════════════════════════════════
    # 3. 日亏损限额检查
    # ═══════════════════════════════════════════════

    def check_daily_loss_limit(self, order_intent=None, today_str=None, quotes=None,
                               total_asset=None) -> Tuple[bool, str]:
        """闸门1 日亏限额：当日已实现+浮动亏损达到 config risk.daily_loss_limit → 禁止新开仓"""
        return check_daily_loss_limit(today_str, quotes=quotes, total_asset=total_asset)

    def check_single_position_cap(self, code, price, shares, total_asset=None, quotes=None,
                                  allow_compress=True, mode=None):
        """闸门3 单票仓位上限（chase → chase.max_single_pct，其余 → risk.single_position_cap）"""
        return check_single_position_cap(code, price, shares, total_asset=total_asset,
                                         quotes=quotes, allow_compress=allow_compress, mode=mode)

    def pre_trade_gate(self, code, price, shares, trade_type='buy', total_asset=None,
                       quotes=None, today_str=None, mode=None):
        """三道闸门统一入口（买入/加仓前调用）"""
        return pre_trade_gate(code, price, shares, trade_type=trade_type,
                              total_asset=total_asset, quotes=quotes, today_str=today_str,
                              mode=mode)

    def check_order(self, order_intent) -> Tuple[bool, str]:
        """统一风控入口：卖出类免检，买入类走 a日亏 → b连亏 → c单票上限

        参数:
          order_intent: 订单意图（字典或对象，含 code/price/shares/direction 等字段）

        返回:
          (ok: bool, reason: str)
        """
        if hasattr(order_intent, 'is_sell') and order_intent.is_sell():
            return True, "免检（卖出/减仓/止损/做T平仓不增加风险敞口）"
        code = getattr(order_intent, 'code', '')
        price = float(getattr(order_intent, 'price', 0) or 0)
        shares = int(getattr(order_intent, 'shares', 0) or 0)
        res = pre_trade_gate(code, price, shares, trade_type='buy')
        return res['ok'], res['reason']


# ═══════════════════════════════════════════════
# 模块级快捷函数（向后兼容）
# ═══════════════════════════════════════════════

# 单例实例（不再区分 enhanced/非 enhanced，通过参数传递）
_rm = RiskManager()


def check_stop_loss(pos, t, price, today_str, enhanced_trend=False):
    """评估止盈止损信号（快捷方式）
    
    参数:
      enhanced_trend: 启用增强趋势止盈（通过参数传递，避免实例状态污染）
    """
    return _rm.check_stop_loss(pos, t, price, today_str, enhanced_trend=enhanced_trend)

def resolve_stop_signal(pos, stop_actions, enhanced_trend=False):
    """解析止盈止损信号（快捷方式）"""
    return _rm.resolve_stop_signal(pos, stop_actions)


def is_position_capped(pos, price, total_fund=None):
    """检查是否已达仓位上限（快捷方式）"""
    return RiskManager.is_position_capped(pos, price, total_fund)


def get_position_ratio(pos, price, total_fund=None):
    """获取当前持仓占比（快捷方式）"""
    return RiskManager.get_position_ratio(pos, price, total_fund)