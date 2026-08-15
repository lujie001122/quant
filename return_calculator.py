#!/usr/bin/env python3
"""
return_calculator.py — 总资产收益率计算

从 backtest_bt.py / signal_generator.py 提取的
收益率、风险指标计算逻辑。

支持:
  - 总收益率 / 年化收益率
  - 最大回撤 / Calmar比率
  - 夏普比率
  - 胜率 / 盈亏比
  - 基于 portfolio 状态的持仓市值汇总
"""

from datetime import date
from typing import Any, Dict, Optional, Tuple
import math


# ═══════════════════════════════════════════════════════
# 收益率计算（核心红线：所有收益率以总资产为分母）
# ═══════════════════════════════════════════════════════

def calc_total_return(initial_value: float, final_value: float) -> float:
    """
    总收益率 (%)
    """
    if initial_value <= 0:
        return 0.0
    return (final_value - initial_value) / initial_value * 100


def calc_annual_return(initial_value: float, final_value: float,
                       start_date: str, end_date: str) -> float:
    """
    年化收益率 (CAGR, %)
    """
    if initial_value <= 0:
        return 0.0
    d1 = date.fromisoformat(start_date)
    d2 = date.fromisoformat(end_date)
    years = (d2 - d1).days / 365.0
    if years <= 0:
        return 0.0
    return ((final_value / initial_value) ** (1 / years) - 1) * 100


def calc_daily_return(prev_total_asset: float, curr_total_asset: float) -> float:
    """
    日收益率（以总资产为分母，避免卖出后虚高）。
    核心红线：严禁以持仓市值为分母计算收益率。

    参数:
      prev_total_asset: 前一交易日总资产
      curr_total_asset: 当前交易日总资产

    返回:
      日收益率（小数，如 0.01 = 1%）
    """
    if prev_total_asset <= 0:
        return 0.0
    return (curr_total_asset - prev_total_asset) / prev_total_asset


def calc_position_return(position: Dict[str, Any],
                         current_price: float,
                         total_asset: float) -> float:
    """
    单标的真实收益率（含已实现盈亏，以总资产为分母）。
    不同于 profit_pct（基于持仓成本），此函数反映标的对总资产的贡献。

    参数:
      position: 持仓字典 {shares, entry_avg_cost, avg_cost, realized_pnl, ...}
      current_price: 当前市场价格
      total_asset: 总资产（用于计算占比）

    返回:
      该标的占总资产的收益率（小数）
    """
    if total_asset <= 0:
        return 0.0
    shares = position.get("shares", 0)
    entry_cost = position.get("entry_avg_cost", 0) or position.get("avg_cost", 0)
    realized_pnl = position.get("realized_pnl", 0) or 0.0

    if shares > 0 and entry_cost > 0:
        # 未实现盈亏 + 已实现盈亏，除以总资产
        unrealized = (current_price - entry_cost) * shares
        return (unrealized + realized_pnl) / total_asset
    elif realized_pnl != 0:
        return realized_pnl / total_asset
    return 0.0


def calc_total_asset_return(state_center: Any) -> float:
    """
    总资产收益率（基于 state_center 的持仓和账户信息）。

    参数:
      state_center: 状态中心模块（含 portfolio.json 数据）

    返回:
      总资产收益率（小数）
    """
    try:
        pf = state_center.load_portfolio()
        account = pf.get("account", {})
        total_asset = account.get("total_asset", 0)
        initial_capital = account.get("initial_capital", 0)
        if total_asset > 0 and initial_capital > 0:
            return (total_asset - initial_capital) / initial_capital
    except Exception:
        pass
    return 0.0


# ═══════════════════════════════════════════════════════
# 风险指标
# ═══════════════════════════════════════════════════════

def calc_max_drawdown(equity_curve: list) -> float:
    """
    最大回撤 (%)
    equity_curve: [value_1, value_2, ...] 资产净值序列
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_calmar(annual_return: float, max_drawdown: float) -> float:
    """Calmar比率 = 年化收益 / 最大回撤"""
    if max_drawdown <= 0:
        return 0.0
    return annual_return / max_drawdown


def calc_sharpe_ratio(returns: list, risk_free_rate: float = 0.02) -> float:
    """
    年化夏普比率
    returns: 周期收益率序列 (如日收益率)
    默认假设250个交易日
    """
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean_ret = sum(returns) / n
    if n == 1:
        return 0.0
    variance = sum((r - mean_ret) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0
    if std == 0:
        return 0.0
    # 年化
    daily_risk_free = risk_free_rate / 250
    sharpe = (mean_ret - daily_risk_free) / std * math.sqrt(250)
    return sharpe


# ═══════════════════════════════════════════════════════
# 交易统计
# ═══════════════════════════════════════════════════════

def calc_trade_stats(trades: list) -> Dict:
    """
    从交易列表计算统计
    trades: [{"pnl": float, ...}, ...]
    返回: total_trades, won, lost, win_rate, avg_win, avg_loss, profit_factor
    """
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0, "won": 0, "lost": 0,
            "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
        }

    won_trades = [t for t in trades if t.get("pnl", 0) > 0]
    lost_trades = [t for t in trades if t.get("pnl", 0) <= 0]
    won = len(won_trades)
    lost = len(lost_trades)
    win_rate = won / total * 100

    avg_win = sum(t["pnl"] for t in won_trades) / won if won > 0 else 0
    avg_loss = abs(sum(t["pnl"] for t in lost_trades) / lost) if lost > 0 else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    return {
        "total_trades": total,
        "won": won,
        "lost": lost,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
    }


# ═══════════════════════════════════════════════════════
# 持仓市值汇总
# ═══════════════════════════════════════════════════════

def calc_portfolio_value(positions: Dict[str, Dict],
                         live_prices: Dict[str, float],
                         cash: float = 0.0) -> Tuple[float, float]:
    """
    计算组合总资产
    positions: {code: {shares, avg_cost, ...}}
    live_prices: {code: price}
    返回: (market_value, total_asset)
    """
    mv = 0.0
    for code, pos in positions.items():
        if code in live_prices:
            shares = pos.get("shares", 0)
            mv += shares * live_prices[code]
    total_asset = mv + cash
    return mv, total_asset


def calc_position_ratio(code: str, positions: Dict[str, Dict],
                        live_prices: Dict[str, float],
                        total_asset: float) -> float:
    """
    单标的持仓占比 (0-1)
    """
    if total_asset <= 0 or code not in positions or code not in live_prices:
        return 0.0
    shares = positions[code].get("shares", 0)
    return shares * live_prices[code] / total_asset


def calc_daily_returns_from_equity(equity_curve: list) -> list:
    """
    从资产净值序列计算日收益率
    """
    if len(equity_curve) < 2:
        return []
    returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            ret = (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            returns.append(ret)
    return returns