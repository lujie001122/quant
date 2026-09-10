#!/usr/bin/env python3
"""
资金管理器 — 仓位计算、分批建仓、动态再平衡

从 strategy.py 和 signal_generator.py 提取仓位计算逻辑:
  - 仓位计算 (signal_generator.execute_signals 的 --execute 下单逻辑)
  - 分批建仓 (strategy.evaluate_entry 的 build_phase + 确认加仓)
  - 动态再平衡 (DEAD_RATIO + ACTIVE_RATIO 的底仓/活动仓分离)

职责:
  1. 仓位规模计算 (根据信号生成实际股数)
  2. 分批建仓调度 (首笔→确认→补仓)
  3. 底仓/活动仓再平衡

用法:
  from money_manager import MoneyManager
  mm = MoneyManager()
  shares = mm.calc_position_size(fund, ratio, price)
  mm.update_build_phase(pos, price, tech)
"""


# 从 position_info 导入统一常量
from position_info import TOTAL_FUND, DEAD_RATIO, ACTIVE_RATIO, MAX_PER_ETF

import os, yaml as _yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
try:
    with open(_CONFIG_PATH) as _f:
        _CONF = _yaml.safe_load(_f) or {}
except Exception:
    _CONF = {}


MIN_SHARES = _CONF.get("trade", {}).get("min_shares", 5000)  # 从config读取，对齐实盘


class MoneyManager:
    """资金管理器 — 仓位计算、分批建仓、动态再平衡"""

    # ══════════════════════════════════════════════
    # 1. 仓位规模计算
    # ══════════════════════════════════════════════

    @staticmethod
    def calc_position_size(fund, ratio, price, round_lot=100):
        """根据资金、比例、价格计算交易股数

        参数:
          fund: 分配资金 (如 44000)
          ratio: 仓位比例 (如 0.30 表示30%)
          price: 当前价格
          round_lot: 取整单位 (默认100)
        返回:
          int: 交易股数 (已取整到100倍数)
        """
        if price <= 0 or fund <= 0 or ratio <= 0:
            return 0
        target_value = fund * ratio
        shares = int(target_value / price / round_lot) * round_lot
        return max(shares, MIN_SHARES)

    @staticmethod
    def calc_position_size_percent(pos, ratio, round_lot=100):
        """根据当前持仓计算N%仓位股数

        参数:
          pos: PositionInfo 实例
          ratio: 比例 (如 0.30 表示30%)
        """
        if not pos.has_position or pos.shares < round_lot:
            return 0
        shares = int(pos.shares * ratio / round_lot) * round_lot
        return max(shares, MIN_SHARES)

    @staticmethod
    def calc_t0_shares(pos, ratio=None, round_lot=100):
        """计算做T股数 (从config读取默认比例)

        参数:
          pos: PositionInfo 实例
          ratio: 做T比例 (默认从config t0.pair_ratio读取)
        """
        if not pos.has_position or pos.shares < round_lot:
            return 0
        if ratio is None:
            ratio = _CONF.get("t0", {}).get("pair_ratio", 0.30)
        shares = int(pos.shares * ratio / round_lot) * round_lot
        return max(shares, MIN_SHARES)

    # ══════════════════════════════════════════════
    # 2. 分批建仓调度
    # ══════════════════════════════════════════════

    @staticmethod
    def get_build_ratio(pos):
        """获取当前建仓阶段应使用的比例

        返回:
          float: 0-1 之间的比例, 或 None 表示不建仓
        """
        if pos.build_phase == 0:
            return 0.30  # 首笔30%
        elif pos.build_phase == 1:
            return 1.00  # 确认100%(极限方案C)
        else:
            return None  # 已完成建仓

    @staticmethod
    def update_build_phase(pos, price, is_confirm=False):
        """更新建仓阶段状态

        - 首笔建仓: pos._enter_position 会自动设置 build_phase=1
        - 确认加仓: 设置 build_phase=2, 重置止盈状态
        """
        if is_confirm:
            pos.build_phase = 2
            pos.base_price = pos.avg_cost
            pos.peak_price = price
            pos.build_first_price = price
            pos.reached_8pct = False
            pos.reached_15pct = False
            pos.trailing_stop_price = 0.0
            pos.add_count += 1

    # ══════════════════════════════════════════════
    # 3. 底仓/活动仓动态再平衡
    # ══════════════════════════════════════════════

    @staticmethod
    def rebalance_dead_active(pos):
        """重新计算底仓/活动仓 (极限方案C: 取消拆分, 全为活动仓)"""
        pos.dead_shares = 0
        pos.active_shares = pos.shares
        return pos.dead_shares, pos.active_shares

    @staticmethod
    def get_available_active_shares(pos):
        """获取可用于卖出的活动仓股数 (极限方案C: 全部可卖)"""
        if not pos.has_position:
            return 0
        return pos.shares

    @staticmethod
    def calc_grid_buy_shares(pos, grid_weight, fund, price, round_lot=100):
        """计算网格买入股数 (倒金字塔权重)"""
        if price <= 0 or fund <= 0:
            return 0
        target_value = fund * grid_weight
        shares = int(target_value / price / round_lot) * round_lot
        return max(shares, MIN_SHARES)  # 最小交易股数(对齐实盘)

    # ══════════════════════════════════════════════
    # 4. 综合工具
    # ══════════════════════════════════════════════

    @staticmethod
    def validate_order_shares(shares, min_shares=100):
        """验证委托股数有效性

        返回:
          (is_valid: bool, corrected_shares: int, reason: str)
        """
        if shares < min_shares:
            return False, 0, f"股数{shares}<{min_shares},不足最小交易单位"
        if shares % min_shares != 0:
            corrected = shares // min_shares * min_shares
            return True, corrected, f"股数{shares}不是{min_shares}的倍数,修正为{corrected}"
        return True, shares, ""

    @staticmethod
    def calc_limit_price(signal_price, live_price, is_buy):
        """计算下单限价 (买入取低, 卖出取高)

        参数:
          signal_price: 信号价
          live_price: 实时价
          is_buy: True=买入, False=卖出
        返回:
          float: 限价
        """
        if is_buy:
            # 买入限价 = min(信号价, 实时价)
            return min(signal_price, live_price) if live_price > 0 else signal_price
        else:
            # 卖出限价 = max(信号价, 实时价)
            return max(signal_price, live_price) if live_price > 0 else signal_price

    @staticmethod
    def calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min=None, pair_atr_multiplier=None):
        """计算做T配对挂单价

        Bug D 修复：优先使用ATR乘数计算价差，否则回退到固定min_spread。

        参数:
          current_price: 当前价
          shares: 配对股数
          is_buy_t0: True=T0买入(配对卖出), False=T0卖出(配对买入)
          atr_5min: 5分钟ATR值（用于动态价差计算）
          pair_atr_multiplier: ATR乘数（从config t0.pair_atr_multiplier读取）

        返回:
          float: 配对挂单价, 或 0.0 表示无效

        公式:
          - 优先ATR动态价差: spread = atr_5min * pair_atr_multiplier
          - 回退固定价差: spread = min_spread (150元)
          - 买入信号: pair_price = (shares*price + spread) / shares
          - 卖出信号: pair_price = (shares*price - spread) / shares
        """
        if shares <= 0 or current_price <= 0:
            return 0.0

        # Bug D: 优先使用ATR动态价差
        spread = _CONF.get("t0", {}).get("min_spread", 150)  # 默认固定价差
        if atr_5min is not None and atr_5min > 0:
            if pair_atr_multiplier is None:
                pair_atr_multiplier = _CONF.get("t0", {}).get("pair_atr_multiplier", 1.1)
            atr_spread = atr_5min * pair_atr_multiplier * shares
            if atr_spread > 0:
                spread = atr_spread  # ATR价差生效

        if is_buy_t0:
            # 做T买入：先买后卖，配对的卖出价需高于买入价
            return round(current_price + spread / shares, 3)
        else:
            # 做T卖出：先卖后买，配对的买入价需低于卖出价
            return round(current_price - spread / shares, 3)



# ═══════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════

def calc_position_size(fund, ratio, price, round_lot=100):
    return MoneyManager.calc_position_size(fund, ratio, price, round_lot)


def calc_position_size_percent(pos, ratio, round_lot=100):
    return MoneyManager.calc_position_size_percent(pos, ratio, round_lot)


def calc_t0_shares(pos, ratio=None, round_lot=100):
    return MoneyManager.calc_t0_shares(pos, ratio, round_lot)


def get_build_ratio(pos):
    return MoneyManager.get_build_ratio(pos)


def rebalance_dead_active(pos):
    return MoneyManager.rebalance_dead_active(pos)


def validate_order_shares(shares, min_shares=100):
    return MoneyManager.validate_order_shares(shares, min_shares)


def calc_limit_price(signal_price, live_price, is_buy):
    return MoneyManager.calc_limit_price(signal_price, live_price, is_buy)


def calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min=None, pair_atr_multiplier=None):
    return MoneyManager.calc_t0_pair_price(current_price, shares, is_buy_t0, atr_5min, pair_atr_multiplier)