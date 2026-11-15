#!/usr/bin/env python3
"""移动止盈唯一真相源 —— backtest_bt.py 与实盘 signal_generator.py 共用。

口径（与 bt_ext._update_trail_state 逐行等价，回测结果不变）:
  · 浮盈 ≥ activation 记激活；浮盈 ≥ lockin 记锁盈，锁盈后止损线 = 峰值 × (1 - gap)
  · gap: e1 → ATR>5% 用 7% / ≥3% 用 5% / 否则 3%
         atr → max(atr_mult × ATR, 2%)，上限 30%
         fixed → trail_pct
  · lockin 当日先挂 成本×(1+lockin_floor)，之后由峰值回撤线接管
所有 ATR 参数均为小数口径（0.03 = 3%）。
"""
import os
import yaml

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
with open(_CFG_PATH) as _f:
    _CFG = yaml.safe_load(_f) or {}

TRAILING = _CFG.get('trailing', {}) or {}
PROFIT_TAKE = _CFG.get('profit_take', {}) or {}
TRAIL_PCT = float(_CFG.get('trail_pct', 0.12))
LOCKIN_FLOOR = float(TRAILING.get('lockin_floor', 0.05))
_MIN_GAP, _MAX_GAP = 0.02, 0.30


def mode():
    return str(TRAILING.get('mode', 'e1')).lower()


def params(atr_pct=None, mode_override=None):
    """→ (activation, lockin, gap)"""
    m = str(mode_override or mode()).lower()
    if m == 'fixed':
        return TRAIL_PCT + 0.01, TRAIL_PCT + 0.08, TRAIL_PCT
    _v2 = bool((_CFG.get('strategy_v2') or {}).get('enabled', False))
    activation = float(PROFIT_TAKE.get('e1_activation_v2' if _v2 else 'e1_activation', 0.035))
    lockin = float(PROFIT_TAKE.get('e1_lockin', 0.07))
    a = float(atr_pct or 0.0)
    if m == 'atr':
        gap = min(max(float(TRAILING.get('atr_mult', 2.0)) * a, _MIN_GAP), _MAX_GAP)
    else:  # e1
        gap = 0.07 if a > 0.05 else (0.05 if a >= 0.03 else 0.03)
    return activation, lockin, gap


def update(avg, peak, price, atr_pct=None, lockin_hit=False, mode_override=None):
    """→ (trail_line, activation_hit, lockin_hit, should_exit)

    与回测 _update_trail_state 完全同序：锁盈当日先挂成本×1.05，再被峰值回撤线覆盖。
    """
    if not avg or avg <= 0:
        return 0.0, False, lockin_hit, False
    pp = (price - avg) / avg
    act, lock, gap = params(atr_pct, mode_override)
    act_hit = pp >= act
    line = 0.0
    if pp >= lock:
        lockin_hit = True
        line = avg * (1 + LOCKIN_FLOOR)
    if lockin_hit and peak and peak > 0:
        line = peak * (1 - gap)
    return line, act_hit, lockin_hit, bool(line > 0 and price <= line)


def label(atr_pct=None, mode_override=None):
    m = str(mode_override or mode()).lower()
    _, _, gap = params(atr_pct, mode_override)
    if m == 'atr':
        return f"ATR{float(TRAILING.get('atr_mult', 2.0)):.1f}x({gap*100:.1f}%)"
    if m == 'e1':
        return f"{gap*100:.0f}%"
    return f"{gap*100:.0f}%"


def describe():
    """诊断用: 当前生效口径"""
    m = mode()
    if m == 'atr':
        return f"ATRx{float(TRAILING.get('atr_mult', 2.0)):.1f}"
    return m


def describe_long():
    """人读口径（报告/推送用，避免硬编码描述与实际配置漂移）"""
    m = mode()
    act, lock, _ = params(0.0)
    if m == 'atr':
        return (f"移动止盈(浮盈{act*100:.1f}%激活, {lock*100:.0f}%锁盈后"
                f"峰值回撤ATRx{float(TRAILING.get('atr_mult', 2.0)):.1f})")
    if m == 'e1':
        return (f"移动止盈(浮盈{act*100:.1f}%激活, {lock*100:.0f}%锁盈后"
                f"峰值回撤3%/5%/7%按ATR分档)")
    return f"移动止盈(固定{float(TRAIL_PCT)*100:.0f}%峰值回撤)"
