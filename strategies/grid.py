#!/usr/bin/env python3
"""
GridStrategy — 网格加仓减仓策略

从 strategy.py 拆分:
  - evaluate_grid_signals: 网格触达(含熔断机制)
  - check_grid_reset: 网格基准重置(含冷却期+下行重置)
  - compute_grid_table: 网格表计算

strategy_v2 开关（config.yaml strategy_v2.enabled）:
  - 趋势过滤: 价格 < MA20 且 MA20 斜率为负时禁止网格买入
  - 删除跌破第5格下移基准的重置逻辑，改为冻结交止损
  - 权重递减合计1.0（0.25/0.20/0.20/0.20/0.15）
  - 网格卖出档位为活动仓10%/15%/20%
  - 整体止损 -8%减半 / -12%清仓
"""

from strategies.base import BaseStrategy
from position_info import GRID_BUY_LEVELS, GRID_SELL_LEVELS, INVERTED_WEIGHTS
import math
import os
import yaml as _yaml

# ── 读取 strategy_v2 配置 ──────────────────────────────────────
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.yaml')
try:
    with open(_cfg_path) as _f:
        _FULL_CFG = _yaml.safe_load(_f) or {}
except Exception:
    _FULL_CFG = {}

_V2_CFG = _FULL_CFG.get('strategy_v2', {})
_V2_ENABLED = bool(_V2_CFG.get('enabled', False))

# v2 权重（递减，合计=1.0）
_V2_WEIGHTS = _V2_CFG.get('inverted_weights_v2', [0.25, 0.20, 0.20, 0.20, 0.15])
# v2 网格卖出活动仓比例（3档）
_V2_SELL_RATIOS = _V2_CFG.get('grid_sell_ratios', [0.10, 0.15, 0.20])
# v2 整体止损参数
_V2_OVERALL_STOP = _V2_CFG.get('grid_overall_stop_loss', {})
_V2_HALF_PCT   = float(_V2_OVERALL_STOP.get('half_pct', 0.08))
_V2_CLEAR_PCT  = float(_V2_OVERALL_STOP.get('clear_pct', 0.12))
_V2_OVERALL_STOP_ENABLED = bool(_V2_OVERALL_STOP.get('enabled', True))


# 常量（从 position_info 统一导入，避免跨文件定义不一致）
# GRID_BUY_LEVELS / GRID_SELL_LEVELS / INVERTED_WEIGHTS 已从 position_info 导入


class GridStrategy(BaseStrategy):
    """网格加仓减仓策略 — 网格触达判定、基准重置、网格表计算"""

    def evaluate_grid_signals(self, price, base_price, spacing, grid_frozen=False,
                               ma20=None, ma20_slope_down=False):
        """网格触达(含熔断机制)

        strategy_v2 额外参数:
          ma20: 当日 MA20 值（用于趋势过滤）
          ma20_slope_down: MA20 斜率为负（下行）
        """
        if base_price is None:
            return "无(未设基准价)", 0
        if grid_frozen:
            return "网格冻结(跌破第5档,等站上MA5解冻)", 0

        weights      = _V2_WEIGHTS     if _V2_ENABLED else INVERTED_WEIGHTS
        sell_ratios  = _V2_SELL_RATIOS if _V2_ENABLED else None

        tolerance = spacing * 0.15

        for i in range(1, GRID_BUY_LEVELS + 1):
            grid_price = base_price * (1 - spacing * i)
            if abs(price - grid_price) <= tolerance * grid_price:
                # strategy_v2: 趋势过滤 —— 价格 < MA20 且 MA20 下行时禁止网格买入
                if _V2_ENABLED and ma20 is not None and price < ma20 and ma20_slope_down:
                    return "无(v2趋势过滤:价格<MA20且MA20下行,仅允许卖出/止损)", 0

                weight = weights[i - 1]
                frozen_note = " [熔断!第5档后冻结网格]" if i == 5 else ""
                return (
                    f"触及第{i}档买入(网格价{grid_price:.3f},"
                    f"仓位{weight*100:.0f}%){frozen_note}"
                ), weight

        for i in range(1, GRID_SELL_LEVELS + 1):
            grid_price = base_price * (1 + spacing * i)
            if abs(price - grid_price) <= tolerance * grid_price:
                if _V2_ENABLED and sell_ratios is not None:
                    idx = min(i - 1, len(sell_ratios) - 1)
                    sell_pct = sell_ratios[idx]
                    return (
                        f"触及第{i}档卖出(网格价{grid_price:.3f},"
                        f"卖活动仓{int(sell_pct*100)}%)"
                    ), sell_pct
                else:
                    return f"触及第{i}档卖出(网格价{grid_price:.3f})", 0.20

        return "无", 0

    def check_grid_reset(self, price, base_price, spacing, date_str, last_reset_date=None):
        """网格基准重置(含冷却期+下行重置)

        strategy_v2: 删除向下重置逻辑（跌破第5格不再下移基准，改为冻结交止损处理）。
                     仅保留向上重置（价格超过上轨 base*(1+spacing*4)）。
        """
        if base_price is None:
            return base_price, False, last_reset_date
        if last_reset_date is not None and date_str == last_reset_date:
            return base_price, False, last_reset_date
        upper_limit = base_price * (1 + spacing * 4)
        if price > upper_limit:
            return round(price, 3), True, date_str
        # v2: 删除向下重置逻辑
        if not _V2_ENABLED:
            lower_limit = base_price * (1 - spacing * 6)
            if price < lower_limit:
                return round(price, 3), True, date_str
        return base_price, False, last_reset_date

    def compute_grid_table(self, base_price, spacing, fund):
        """计算网格表

        strategy_v2: 使用递减权重（合计1.0），股数按目标金额计算后对齐100股且≥5000。
        """
        if base_price is None:
            return None

        weights = _V2_WEIGHTS if _V2_ENABLED else INVERTED_WEIGHTS

        table = {"base": base_price, "spacing": spacing, "buy": [], "sell": [],
                 "strategy_v2": _V2_ENABLED}
        for i in range(1, GRID_BUY_LEVELS + 1):
            p = base_price * (1 - spacing * i)
            w = weights[i - 1]
            per_fund = fund * w
            if math.isnan(p) or p <= 0:
                continue
            shares = int(per_fund / p / 100) * 100
            if shares < 5000:
                shares = 5000
            table["buy"].append({"grid": i, "price": round(p, 3), "shares": shares,
                                  "fund": round(shares * p, 0), "weight": f"{w*100:.0f}%"})
        for i in range(1, GRID_SELL_LEVELS + 1):
            p = base_price * (1 + spacing * i)
            sell_pct = (_V2_SELL_RATIOS[min(i-1, len(_V2_SELL_RATIOS)-1)] if _V2_ENABLED
                        else 0.20)
            table["sell"].append({"grid": i, "price": round(p, 3),
                                   "sell_active_pct": f"{int(sell_pct*100)}%"})
        return table

    def check_grid_overall_stop(self, pos, price, today_str):
        """网格整体止损（strategy_v2 专用）

        规则:
          - 持仓浮亏 ≥ -8%  → 减半，sig_type = grid_overall_half
          - 持仓浮亏 ≥ -12% → 清仓，sig_type = grid_overall_clear
          - 同日已触发则跳过（去重）
          - 同日已触发 tier1/tier2 均价止损则跳过（去重）

        返回:
          [(sig_name, sig_type)] 或 []
        """
        if not _V2_ENABLED or not _V2_OVERALL_STOP_ENABLED:
            return []
        if not pos.has_position or pos.entry_cost <= 0 or price <= 0:
            return []

        float_loss = (price - pos.entry_cost) / pos.entry_cost  # 负数=亏损
        if float_loss >= 0:
            return []  # 未亏损，不触发

        grid_stop_level = getattr(pos, 'grid_stop_level', 0)
        grid_stop_date  = getattr(pos, 'grid_stop_date', None)

        actions = []

        if float_loss <= -_V2_CLEAR_PCT:
            # -12% 清仓
            if grid_stop_level < 2:
                if grid_stop_date == today_str:
                    return []  # 今日已触发，跳过
                pos.grid_stop_level = 2
                pos.grid_stop_date  = today_str
                actions.append((
                    f"网格整体止损-{_V2_CLEAR_PCT*100:.0f}%清仓"
                    f"(浮亏{float_loss*100:.1f}%,成本{pos.entry_cost:.3f}→现价{price:.3f})",
                    "grid_overall_clear"
                ))
        elif float_loss <= -_V2_HALF_PCT:
            # -8% 减半
            if grid_stop_level < 1:
                if grid_stop_date == today_str:
                    return []  # 今日已触发，跳过
                pos.grid_stop_level = 1
                pos.grid_stop_date  = today_str
                actions.append((
                    f"网格整体止损-{_V2_HALF_PCT*100:.0f}%减半仓"
                    f"(浮亏{float_loss*100:.1f}%,成本{pos.entry_cost:.3f}→现价{price:.3f})",
                    "grid_overall_half"
                ))
        return actions


# ═══════════════════════════════════════════════
# 模块级函数（保持向后兼容）
# ═══════════════════════════════════════════════

def evaluate_grid_signals(price, base_price, spacing, grid_frozen=False,
                           ma20=None, ma20_slope_down=False):
    """网格触达(含熔断机制)"""
    return GridStrategy().evaluate_grid_signals(
        price, base_price, spacing, grid_frozen,
        ma20=ma20, ma20_slope_down=ma20_slope_down
    )


def check_grid_reset(price, base_price, spacing, date_str, last_reset_date=None):
    """网格基准重置(含冷却期+下行重置)"""
    return GridStrategy().check_grid_reset(price, base_price, spacing, date_str, last_reset_date)


def compute_grid_table(base_price, spacing, fund):
    """计算网格表"""
    return GridStrategy().compute_grid_table(base_price, spacing, fund)


def check_grid_overall_stop(pos, price, today_str):
    """网格整体止损（strategy_v2 专用）"""
    return GridStrategy().check_grid_overall_stop(pos, price, today_str)


def is_v2_enabled():
    """返回 strategy_v2 是否已启用"""
    return _V2_ENABLED
