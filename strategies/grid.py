#!/usr/bin/env python3
"""
GridStrategy — 网格加仓减仓策略

从 strategy.py 拆分:
  - evaluate_grid_signals: 网格触达(含熔断机制)
  - check_grid_reset: 网格基准重置(含冷却期+下行重置)
  - compute_grid_table: 网格表计算
"""

from strategies.base import BaseStrategy
from position_info import GRID_BUY_LEVELS, GRID_SELL_LEVELS, INVERTED_WEIGHTS


# 常量（从 position_info 统一导入，避免跨文件定义不一致）
# GRID_BUY_LEVELS / GRID_SELL_LEVELS / INVERTED_WEIGHTS 已从 position_info 导入


class GridStrategy(BaseStrategy):
    """网格加仓减仓策略 — 网格触达判定、基准重置、网格表计算"""

    def evaluate_grid_signals(self, price, base_price, spacing, grid_frozen=False):
        """网格触达(含熔断机制)"""
        if base_price is None:
            return "无(未设基准价)", 0
        if grid_frozen:
            return "网格冻结(跌破第5档,等站上MA5解冻)", 0

        tolerance = spacing * 0.15

        for i in range(1, GRID_BUY_LEVELS + 1):
            grid_price = base_price * (1 - spacing * i)
            if abs(price - grid_price) <= tolerance * grid_price:
                weight = INVERTED_WEIGHTS[i - 1]
                frozen_note = " [熔断!第5档后冻结网格]" if i == 5 else ""
                return f"触及第{i}档买入(网格价{grid_price:.3f},仓位{weight*100:.0f}%){frozen_note}", weight

        for i in range(1, GRID_SELL_LEVELS + 1):
            grid_price = base_price * (1 + spacing * i)
            if abs(price - grid_price) <= tolerance * grid_price:
                return f"触及第{i}档卖出(网格价{grid_price:.3f})", 0.20

        return "无", 0

    def check_grid_reset(self, price, base_price, spacing, date_str, last_reset_date=None):
        """网格基准重置(含冷却期+下行重置)"""
        if base_price is None:
            return base_price, False, last_reset_date
        if last_reset_date is not None and date_str == last_reset_date:
            return base_price, False, last_reset_date
        upper_limit = base_price * (1 + spacing * 4)
        if price > upper_limit:
            return round(price, 3), True, date_str
        lower_limit = base_price * (1 - spacing * 6)
        if price < lower_limit:
            return round(price, 3), True, date_str
        return base_price, False, last_reset_date

    def compute_grid_table(self, base_price, spacing, fund):
        """计算网格表"""
        if base_price is None:
            return None
        import math
        table = {"base": base_price, "spacing": spacing, "buy": [], "sell": []}
        for i in range(1, GRID_BUY_LEVELS + 1):
            p = base_price * (1 - spacing * i)
            w = INVERTED_WEIGHTS[i - 1]
            per_fund = fund * w
            if math.isnan(p) or p <= 0:
                continue
            shares = int(per_fund / p / 100) * 100
            table["buy"].append({"grid": i, "price": round(p, 3), "shares": shares,
                                 "fund": round(shares * p, 0), "weight": f"{w*100:.0f}%"})
        for i in range(1, GRID_SELL_LEVELS + 1):
            p = base_price * (1 + spacing * i)
            table["sell"].append({"grid": i, "price": round(p, 3)})
        return table


# ═══════════════════════════════════════════════
# 模块级函数（保持向后兼容）
# ═══════════════════════════════════════════════

def evaluate_grid_signals(price, base_price, spacing, grid_frozen=False):
    """网格触达(含熔断机制)"""
    return GridStrategy().evaluate_grid_signals(price, base_price, spacing, grid_frozen)


def check_grid_reset(price, base_price, spacing, date_str, last_reset_date=None):
    """网格基准重置(含冷却期+下行重置)"""
    return GridStrategy().check_grid_reset(price, base_price, spacing, date_str, last_reset_date)


def compute_grid_table(base_price, spacing, fund):
    """计算网格表"""
    return GridStrategy().compute_grid_table(base_price, spacing, fund)