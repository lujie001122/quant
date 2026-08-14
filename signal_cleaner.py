#!/usr/bin/env python3
"""
signal_cleaner.py — AI信号清洗

对信号生成后的结果做智能清洗，检查:
  1. 近期卖出降权 — 刚卖出(3日内)的标的不应立刻买入，防止追涨杀跌
  2. 收益率虚高降权 — 部分卖出后券商重算avg_cost导致收益率虚高，避免基于虚高收益率的误判
  3. 信号合理性检查 — 融合 sell_signal_filter 的校验逻辑

用法:
  from signal_cleaner import clean_signals
  cleaned = clean_signals(result, portfolio_data)
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════

# 近期卖出冷静期（天）：刚卖出后短时间内不买入
SELL_COOLDOWN_DAYS = 3

# 收益率虚高阈值（%）：券商上报的收益率超过entry_cost基准收益率此比例时视为虚高
YIELD_INFLATION_THRESHOLD = 0.15  # 15%

# 信号方向映射
BUY_DIRECTIONS = {"buy", "t0"}
SELL_DIRECTIONS = {"sell", "liquidate", "reduce", "t0"}


# ═══════════════════════════════════════════════════════
# 核心清洗函数
# ═══════════════════════════════════════════════════════

def clean_signals(
    result: Dict,
    portfolio_data: Optional[Dict] = None,
    today: Optional[str] = None,
) -> Dict:
    """
    对 generate_signals 的输出结果做智能清洗。
    返回清洗后的 result（原地修改 + 返回引用）。

    参数:
      result: generate_signals() 的返回值，含 signals dict
      portfolio_data: portfolio.json 数据（可选，不传则自动加载）
      today: 日期字符串 %Y-%m-%d（可选，不传则用今天）

    返回:
      清洗后的 result（tags 字段会标注清洗操作）
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    if portfolio_data is None:
        from state_center import load_portfolio
        portfolio_data = load_portfolio()

    signals = result.get("signals", {})
    if not signals:
        result.setdefault("_cleaning_tags", []).append("无信号")
        return result

    positions = portfolio_data.get("positions", {})
    signal_state = portfolio_data.get("_signal_state", {})

    cleaning_tags = []
    cleaned_count = 0

    for code, sig in signals.items():
        tags = []

        # ═══ 清洗1: 近期卖出降权 ═══
        tag = _check_recent_sell(code, sig, positions, signal_state, today)
        if tag:
            tags.append(tag)
            cleaned_count += 1

        # ═══ 清洗2: 收益率虚高降权 ═══
        tag = _check_yield_inflation(code, sig, positions, signal_state)
        if tag:
            tags.append(tag)
            cleaned_count += 1

        # ═══ 清洗3: 信号合理性检查（买入信号但无足够资金） ═══
        tag = _check_buy_without_cash(code, sig, portfolio_data)
        if tag:
            tags.append(tag)
            cleaned_count += 1

        # ═══ 清洗4: 建仓信号但近期清仓过（冷静期检查） ═══
        tag = _check_liqudation_cooldown(code, sig, signal_state, today)
        if tag:
            tags.append(tag)
            cleaned_count += 1

        if tags:
            sig["_cleaning"] = tags
            # 如果有阻断性标签，修改信号action
            for tag in tags:
                if tag.get("action") == "downgrade":
                    original_action = sig.get("action", "")
                    sig["action"] = f"持有(清洗:{tag['reason'][:30]})"
                    sig["trade_type"] = None
                    sig["reason"] = f"[清洗] {tag['reason']}"
                    break

    if cleaned_count > 0:
        cleaning_tags.append(f"信号清洗: {cleaned_count} 条被调整")
        result.setdefault("_cleaning_tags", []).extend(cleaning_tags)

    return result


# ═══════════════════════════════════════════════════════
# 清洗规则实现
# ═══════════════════════════════════════════════════════

def _check_recent_sell(
    code: str,
    sig: Dict,
    positions: Dict,
    signal_state: Dict,
    today: str,
) -> Optional[Dict]:
    """
    检查1: 近期卖出降权。
    如果标的在 SELL_COOLDOWN_DAYS 天内被卖出过，且当前信号是买入方向，
    则降权（降低置信度）。
    """
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")

    # 非买入信号不处理
    is_buy = (trade_type == "buy" or (trade_type == "t0" and "买入" in action))
    if not is_buy:
        return None

    # 检查 signal_state 中的最近卖出记录
    state = signal_state.get(code, {})
    trades = state.get("trades", []) if isinstance(state.get("trades"), list) else []
    # 也检查 position 中的 trade 记录
    pos = positions.get(code, {})
    pos_trades = pos.get("trades", []) if isinstance(pos.get("trades"), list) else []

    all_trades = trades + pos_trades

    recent_sell_dates = []
    for t in all_trades:
        t_date = t.get("date", "")
        t_action = t.get("action", "")
        t_type = t.get("trade_type", "")
        t_reason = t.get("reason", "")
        if t_date and (t_action in ("sell", "liquidate", "reduce") or "卖出" in str(t_action)):
            try:
                td = datetime.strptime(t_date, "%Y-%m-%d").date()
                today_d = datetime.strptime(today, "%Y-%m-%d").date()
                days_ago = (today_d - td).days
                if 0 <= days_ago <= SELL_COOLDOWN_DAYS:
                    recent_sell_dates.append((t_date, days_ago, t_reason))
            except (ValueError, TypeError):
                continue

    if recent_sell_dates:
        most_recent = min(recent_sell_dates, key=lambda x: x[1])
        sell_reason = most_recent[2] if len(most_recent) > 2 else ""

        # 区分卖出类型：止盈卖出后买入不降权，止损卖出后买入降权
        if "take_profit" in sell_reason or "止盈" in sell_reason or "趋势止盈" in sell_reason:
            return None  # 止盈卖出后的买入信号不降权，允许二次入场

        return {
            "rule": "recent_sell",
            "action": "downgrade",
            "reason": f"近期卖出({most_recent[0]}, {most_recent[1]}天前, 原因:{sell_reason}), "
                      f"冷静期内({SELL_COOLDOWN_DAYS}天), 暂不买入",
            "detail": {"recent_sells": [(s[0], s[1]) for s in recent_sell_dates]},
        }

    return None


def _check_yield_inflation(
    code: str,
    sig: Dict,
    positions: Dict,
    signal_state: Dict,
) -> Optional[Dict]:
    """
    检查2: 收益率虚高降权。
    部分卖出后券商重算 avg_cost 可能导致收益率虚高。
    通过比较 entry_avg_cost（建仓原始价）和当前 avg_cost 判断。
    """
    pos = positions.get(code, {})
    state = signal_state.get(code, {})

    shares = pos.get("shares", 0) or state.get("shares", 0)
    avg_cost = pos.get("avg_cost", 0) or state.get("avg_cost", 0)
    entry_avg_cost = state.get("entry_avg_cost", 0)

    if shares <= 0 or avg_cost <= 0 or entry_avg_cost <= 0:
        return None

    # 如果 entry_avg_cost 和 avg_cost 偏差过大 → 说明发生过部分卖出，成本被污染
    if entry_avg_cost > 0 and avg_cost > 0:
        cost_deviation = abs(avg_cost - entry_avg_cost) / entry_avg_cost
        if cost_deviation > YIELD_INFLATION_THRESHOLD:
            return {
                "rule": "yield_inflation",
                "action": "warn",
                "reason": f"收益率可能虚高: entry_avg_cost={entry_avg_cost:.3f}, "
                          f"券商avg_cost={avg_cost:.3f}, 偏差{cost_deviation*100:.1f}%",
                "detail": {
                    "entry_avg_cost": entry_avg_cost,
                    "avg_cost": avg_cost,
                    "deviation": cost_deviation,
                },
            }

    return None


def _check_buy_without_cash(
    code: str,
    sig: Dict,
    portfolio_data: Dict,
) -> Optional[Dict]:
    """
    检查3: 买入信号但现金不足。
    如果账户现金少于最小买入金额，阻止买入信号。
    """
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")
    is_buy = (trade_type == "buy" or (trade_type == "t0" and "买入" in action))
    if not is_buy:
        return None

    account = portfolio_data.get("account", {})
    cash = account.get("cash", 0) or 0
    total_asset = account.get("total_asset", 0) or 0

    # 估算最小买入金额（100股 * 价格）
    signal_price = sig.get("price", "0")
    try:
        price = float(signal_price)
    except (ValueError, TypeError):
        price = 0

    min_buy = price * 100 if price > 0 else 0

    if min_buy > 0 and cash < min_buy:
        return {
            "rule": "insufficient_cash",
            "action": "downgrade",
            "reason": f"现金不足: 现金{cash:.0f} < 最小买入{min_buy:.0f}",
            "detail": {"cash": cash, "min_buy": min_buy},
        }

    return None


def _check_liqudation_cooldown(
    code: str,
    sig: Dict,
    signal_state: Dict,
    today: str,
) -> Optional[Dict]:
    """
    检查4: 建仓信号但近期清仓过（冷静期检查）。
    如果 signal_state 中有 liquidate_dates 且最近清仓在冷静期内，阻止建仓。
    """
    trade_type = sig.get("trade_type", "")
    action = sig.get("action", "")
    is_buy = (trade_type == "buy" or (trade_type == "t0" and "买入" in action))
    if not is_buy:
        return None

    state = signal_state.get(code, {})
    liquidate_dates = state.get("liquidate_dates", [])
    if not isinstance(liquidate_dates, list) or not liquidate_dates:
        return None

    # 找最近清仓日期
    try:
        recent_liquidate = max(
            (d for d in liquidate_dates if d),
            key=lambda d: datetime.strptime(d, "%Y-%m-%d").date()
            if isinstance(d, str) and "-" in d else datetime.min
        )
        if not isinstance(recent_liquidate, str) or "-" not in recent_liquidate:
            return None
        ld = datetime.strptime(recent_liquidate, "%Y-%m-%d").date()
        td = datetime.strptime(today, "%Y-%m-%d").date()
        days_since = (td - ld).days

        cooldown = state.get("cooldown_days", 2)  # 默认2天冷静期
        if 0 <= days_since < cooldown:
            return {
                "rule": "liquidation_cooldown",
                "action": "downgrade",
                "reason": f"清仓冷静期: {recent_liquidate}清仓, {days_since}天后冷静期"
                          f"({cooldown}天), 暂不建仓",
                "detail": {"last_liquidate": recent_liquidate, "days_since": days_since},
            }
    except (ValueError, TypeError):
        pass

    return None


# ═══════════════════════════════════════════════════════
# 批量清洗 + 过滤（与 sell_signal_filter 集成）
# ═══════════════════════════════════════════════════════

def apply_cleaning_pipeline(
    result: Dict,
    portfolio_data: Optional[Dict] = None,
) -> Dict:
    """
    完整信号清洗流程:
    1. signal_cleaner 的智能清洗
    2. 可选的 sell_signal_filter 校验（如果可用）

    返回清洗后的 result。
    """
    # 第一步：智能清洗
    result = clean_signals(result, portfolio_data)

    # 第二步：卖出信号过滤（如果可用）
    try:
        from sell_signal_filter import filter_sell_signals
        from state_center import load_portfolio as _load_pf
        pf = portfolio_data or _load_pf()
        positions = pf.get("positions", {})
        # 从 signals 中提取实时价格
        live_prices = {}
        for code, sig in result.get("signals", {}).items():
            try:
                live_prices[code] = float(sig.get("price", 0))
            except (ValueError, TypeError):
                live_prices[code] = 0

        filtered_signals, rejected = filter_sell_signals(
            result.get("signals", {}),
            positions,
            live_prices,
        )
        result["signals"] = filtered_signals
        if rejected:
            result.setdefault("_cleaning_tags", []).append(
                f"sell_signal_filter 拒绝: {len(rejected)} 条"
            )
            result["_rejected_signals"] = rejected
    except ImportError:
        # sell_signal_filter 不可用，跳过
        pass

    return result


# ═══════════════════════════════════════════════════════
# 独立运行
# ═══════════════════════════════════════════════════════

def main():
    """独立运行测试：从 stdin 读取信号结果，输出清洗后的结果"""
    import sys
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        print("请通过 stdin 传入 JSON 格式的信号结果", file=sys.stderr)
        sys.exit(1)

    result = apply_cleaning_pipeline(input_data)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import json
    main()