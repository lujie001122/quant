#!/usr/bin/env python3
"""
sell_signal_filter.py — 卖出信号合理性验证

对每个卖出信号做多重检查，确保不会出现以下不合理情况:
  1. 无持仓时发出卖出信号
  2. 卖出数量超过实际持仓
  3. 成本价异常 (成本为0、价格低于成本但卖出等)
  4. 同日重复卖出同一标的
  5. 卖出后持仓变为负数
  6. 信号价与市价偏差过大(>5%)
  7. 硬止损/清仓信号但卖出比例不足100%

从 signal_generator.py 和 backtest_bt.py 提取的信号过滤逻辑。
"""

from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════
# 信号过滤
# ═══════════════════════════════════════════════════════

def validate_sell_signal(
    code: str,
    signal: Dict,
    positions: Dict[str, Dict],
    live_prices: Dict[str, float],
    sold_today: Optional[set] = None,
    price_deviation_limit: float = 0.05,
) -> Tuple[bool, str]:
    """
    验证卖出信号是否合理。

    参数:
      code: ETF代码
      signal: 信号字典 (含 action, trade_type, position_ratio, price 等)
      positions: 持仓字典 {code: {shares, avg_cost, ...}}
      live_prices: 实时价格 {code: price}
      sold_today: 今日已卖出标的集合
      price_deviation_limit: 信号价与市价偏差上限 (默认5%)

    返回:
      (is_valid, reason)
      is_valid: True 表示信号合理可执行
      reason: 如果不合理，给出原因
    """
    trade_type = signal.get("trade_type", "")
    action = signal.get("action", "")

    # 只过滤卖出类信号
    if trade_type not in ("sell", "liquidate", "reduce") and "卖出" not in action:
        return True, "非卖出信号,跳过过滤"

    pos = positions.get(code, {})
    shares = pos.get("shares", 0)
    avg_cost = pos.get("avg_cost", 0)
    live_price = live_prices.get(code, 0)

    # ═══ 检查1: 无持仓 → 不允许卖出 ═══
    if shares <= 0:
        # 清仓信号在无持仓时允许通过(标记已卖完)
        if trade_type == "liquidate":
            return True, "清仓信号(无持仓,视为已完成)"
        return False, f"{code} 无持仓,不允许卖出({action})"

    # ═══ 检查2: 成本价异常 ═══
    if avg_cost <= 0:
        return False, f"{code} 成本价异常({avg_cost}),不允许卖出"

    if live_price <= 0:
        return False, f"{code} 实时价格异常({live_price}),不允许卖出"

    # ═══ 检查3: 信号价与市价偏差过大 ═══
    signal_price_str = signal.get("price", "0")
    try:
        signal_price = float(signal_price_str)
    except (ValueError, TypeError):
        signal_price = live_price

    if signal_price > 0 and live_price > 0:
        deviation = abs(signal_price - live_price) / live_price
        if deviation > price_deviation_limit:
            return False, (
                f"{code} 信号价{signal_price:.3f}与市价{live_price:.3f}偏差{deviation*100:.1f}%"
                f"超过{price_deviation_limit*100:.0f}%上限,疑似异常"
            )

    # ═══ 检查4: 清仓信号但卖出比例不足100% ═══
    if trade_type == "liquidate":
        position_ratio = signal.get("position_ratio", "")
        # 清仓信号必须卖全部
        if "100%" not in str(position_ratio):
            return False, f"{code} 清仓信号但卖出比例不是100%: {position_ratio}"

    # ═══ 检查5: 同日重复卖出 ═══
    if sold_today and code in sold_today:
        return False, f"{code} 今日已卖出,不重复卖出"

    # ═══ 检查6: 卖出数量合理性 ═══
    ratio = _parse_sell_ratio(signal)
    sell_shares = int(shares * ratio)

    if sell_shares < 100:
        return False, f"{code} 卖出股数不足({sell_shares}股,最低100)"

    if sell_shares > shares:
        return False, f"{code} 卖出股数{sell_shares} > 持仓{shares}股"

    # ═══ 检查7: 做T卖出但价格低于成本 ═══
    if trade_type == "t0" and live_price < avg_cost:
        return False, f"{code} 做T卖出价{live_price:.3f}低于成本{avg_cost:.3f},不执行"

    return True, "通过"


# ═══════════════════════════════════════════════════════
# 批量信号过滤 + 冲突检测
# ═══════════════════════════════════════════════════════

def filter_sell_signals(
    signals: Dict[str, Dict],
    positions: Dict[str, Dict],
    live_prices: Dict[str, float],
    sold_today: Optional[set] = None,
) -> Tuple[Dict[str, Dict], List[Dict]]:
    """
    批量过滤卖出信号，返回过滤后的信号 + 被拒绝的信号列表。

    返回:
      (filtered_signals, rejected_signals)
      filtered_signals: 通过验证的信号 (action 可能被修改为 '持有')
      rejected_signals: [{code, signal, reason}, ...] 被拒绝的信号详情
    """
    if sold_today is None:
        sold_today = set()

    filtered = {}
    rejected = []

    for code, sig in signals.items():
        is_valid, reason = validate_sell_signal(
            code, sig, positions, live_prices, sold_today
        )
        if is_valid:
            filtered[code] = sig
            trade_type = sig.get("trade_type", "")
            action = sig.get("action", "")
            if trade_type in ("sell", "liquidate", "reduce") or "卖出" in action:
                sold_today.add(code)
        else:
            rejected.append({"code": code, "signal": sig, "reason": reason})
            # 拒绝的卖出信号改为持有
            new_sig = dict(sig)
            new_sig["action"] = "持有(拒绝)"
            new_sig["trade_type"] = None
            new_sig["reason"] = f"[卖出过滤] {reason}"
            filtered[code] = new_sig

    return filtered, rejected


def detect_signal_conflicts(signals: Dict[str, Dict]) -> List[Dict]:
    """
    检测信号冲突: 同一标的同一天既有买入又有卖出。

    返回: [{code, conflicts: list, ...}]
    """
    conflicts = []
    seen_buy = {}
    seen_sell = {}

    for i, (code, sig) in enumerate(signals.items()):
        trade_type = sig.get("trade_type", "")
        action = sig.get("action", "")

        if trade_type in ("buy",) or ("买入" in action and trade_type == "t0"):
            seen_buy[code] = i
        elif trade_type in ("sell", "liquidate", "reduce") or ("卖出" in action and trade_type == "t0"):
            seen_sell[code] = i

    for code in seen_buy:
        if code in seen_sell:
            conflicts.append({
                "code": code,
                "buy_index": seen_buy[code],
                "sell_index": seen_sell[code],
                "message": f"{code} 同时存在买入和卖出信号,应优先处理清仓/止损",
            })

    return conflicts


def should_skip_sell_for_liquidation(code: str, all_signals: Dict[str, Dict]) -> bool:
    """
    检查是否应该跳过普通卖出信号(因为存在清仓/减仓信号)。
    清仓信号优先级最高，普通卖出信号应该被覆盖。
    """
    sig = all_signals.get(code, {})
    trade_type = sig.get("trade_type", "")
    if trade_type in ("liquidate",):
        return False  # 清仓信号本身不该被跳过

    # 检查同一 code 是否有其他标记(通过 reason)
    reason = sig.get("reason", "")
    if "清仓" in reason or "liquidate" in reason.lower():
        return True

    return False


# ═══════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════

def _parse_sell_ratio(signal: Dict) -> float:
    """从信号中解析卖出比例(0-1)"""
    ratio_str = signal.get("position_ratio", "")
    if not ratio_str:
        return 0.3  # 默认30%

    # 处理"减30%总仓" / "10%(活动仓)" / "5%(活动仓)" 等格式
    try:
        if "减" in ratio_str and "%" in ratio_str:
            import re
            m = re.search(r'(\d+)%', ratio_str)
            if m:
                return int(m.group(1)) / 100
        if "%" in ratio_str:
            pct_str = ratio_str.split("%")[0]
            # 处理 "30%(RSI抄底)" → "30"
            if "(" in pct_str:
                pct_str = pct_str.split("(")[0]
            return float(pct_str) / 100
    except (ValueError, IndexError):
        pass

    # 按 trade_type 兜底
    trade_type = signal.get("trade_type", "")
    if trade_type == "liquidate":
        return 1.0
    elif trade_type == "reduce":
        return 0.3
    elif trade_type == "sell":
        return 0.05
    return 0.3