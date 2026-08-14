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

import math

# 默认参数
TOTAL_FUND = 220000
MAX_PER_ETF = 44000
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4
MIN_SHARES = 100  # 最小交易单位


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
        return max(shares, 0)

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
        return max(shares, min(pos.shares, round_lot)) if shares < round_lot else shares

    @staticmethod
    def calc_t0_shares(pos, ratio=0.30, round_lot=100):
        """计算做T股数 (默认30%持仓)

        参数:
          pos: PositionInfo 实例
          ratio: 做T比例 (默认0.30)
        """
        if not pos.has_position or pos.shares < round_lot:
            return 0
        shares = int(pos.shares * ratio / round_lot) * round_lot
        return shares if shares >= round_lot else min(pos.shares, round_lot)

    @staticmethod
    def calc_position_ratio(pos, price, total_fund=None):
        """计算当前持仓占比 (0-1)"""
        if total_fund is None:
            total_fund = TOTAL_FUND
        if not pos.has_position or total_fund <= 0:
            return 0.0
        return pos.shares * price / total_fund

    # ══════════════════════════════════════════════
    # 2. 分批建仓调度
    # ══════════════════════════════════════════════

    @staticmethod
    def get_initial_entry_ratio(pos):
        """获取首笔建仓比例 (根据通道)"""
        # 6通道统一返回30%，build_phase逻辑由策略层处理
        return 0.30

    @staticmethod
    def get_confirm_entry_ratio(pos):
        """获取确认加仓比例 (分批2=70%)"""
        return 0.70

    @staticmethod
    def get_build_ratio(pos):
        """获取当前建仓阶段应使用的比例

        返回:
          float: 0-1 之间的比例, 或 None 表示不建仓
        """
        if pos.build_phase == 0:
            return 0.30  # 首笔30%
        elif pos.build_phase == 1:
            return 0.70  # 确认70%
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
        """重新计算底仓/活动仓 (DEAD_RATIO = 60% / ACTIVE_RATIO = 40%)"""
        pos.dead_shares = int(pos.shares * DEAD_RATIO)
        pos.active_shares = pos.shares - pos.dead_shares
        if pos.active_shares == 0 and pos.shares > 0:
            pos.active_shares = int(pos.shares * ACTIVE_RATIO)
        return pos.dead_shares, pos.active_shares

    @staticmethod
    def get_available_active_shares(pos):
        """获取可用于卖出的活动仓股数"""
        if not pos.has_position:
            return 0
        pos.rebalance_dead_active(pos)  # 确保是最新的
        return pos.active_shares

    @staticmethod
    def calc_grid_buy_shares(pos, grid_weight, fund, price, round_lot=100):
        """计算网格买入股数 (倒金字塔权重)"""
        if price <= 0 or fund <= 0:
            return 0
        target_value = fund * grid_weight
        shares = int(target_value / price / round_lot) * round_lot
        return max(shares, 0)

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
    def calc_t0_pair_price(current_price, atr_5min, is_buy_t0):
        """计算做T配对挂单价

        参数:
          current_price: 当前价
          atr_5min: 5分钟ATR
          is_buy_t0: True=T0买入(配对卖出), False=T0卖出(配对买入)
        返回:
          float: 配对挂单价, 或 0.0 表示无效
        """
        if atr_5min <= 0 or current_price <= 0:
            return 0.0
        if is_buy_t0:
            # T0买入 → 高位卖出配对 = 当前价 + ATR*1.1
            return round(current_price + atr_5min * 1.1, 3)
        else:
            # T0卖出 → 低位接回配对 = 当前价 - ATR*1.1
            return round(current_price - atr_5min * 1.1, 3)

    @staticmethod
    def is_t0_pair_viable(current_price, pair_price, shares, min_spread=200):
        """检查做T配对挂单是否有经济意义 (价差*量>200元)"""
        spread = abs(pair_price - current_price) * shares
        return spread > min_spread, round(spread, 2)


# ═══════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════

def calc_position_size(fund, ratio, price, round_lot=100):
    return MoneyManager.calc_position_size(fund, ratio, price, round_lot)


def calc_position_size_percent(pos, ratio, round_lot=100):
    return MoneyManager.calc_position_size_percent(pos, ratio, round_lot)


def calc_t0_shares(pos, ratio=0.30, round_lot=100):
    return MoneyManager.calc_t0_shares(pos, ratio, round_lot)


def get_build_ratio(pos):
    return MoneyManager.get_build_ratio(pos)


def rebalance_dead_active(pos):
    return MoneyManager.rebalance_dead_active(pos)


def validate_order_shares(shares, min_shares=100):
    return MoneyManager.validate_order_shares(shares, min_shares)


def calc_limit_price(signal_price, live_price, is_buy):
    return MoneyManager.calc_limit_price(signal_price, live_price, is_buy)


def calc_t0_pair_price(current_price, atr_5min, is_buy_t0):
    return MoneyManager.calc_t0_pair_price(current_price, atr_5min, is_buy_t0)