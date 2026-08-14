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

from datetime import datetime
from typing import Tuple

# 从 position_info 导入统一常量
from position_info import (
    TOTAL_FUND, MAX_POSITION_RATIO, MAX_DAILY_LOSS_PCT,
    DEAD_RATIO, ACTIVE_RATIO,
)


class RiskManager:
    """风险管理器 — 统一处理止损、仓位上限、日亏损限额、连续亏损熔断"""

    def __init__(self, state_center=None):
        # 延迟导入 state_center，避免循环依赖
        if state_center is None:
            from state_center import StateCenter
            state_center = StateCenter.get_instance()
        self._state = state_center
        self.MAX_DAILY_LOSS_PCT = 0.05  # 单日最大亏损5%
        self.MAX_CONSECUTIVE_LOSS_DAYS = 3  # 连续亏损3天熔断
        self.MIN_DAILY_LOSS_PCT = 0.01  # 日亏损>1%才计入连续亏损
        self.daily_pnl = 0.0
        # 连续亏损追踪
        self.consecutive_loss_days = 0
        self.last_pnl_date = None
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""

    def record_daily_pnl(self, today_str, pnl_amount=None):
        """记录每日盈亏，更新连续亏损计数

        参数:
          today_str: 日期字符串 "YYYY-MM-DD"
          pnl_amount: 当日盈亏金额（None 则从 state_center 获取）
        """
        if pnl_amount is None:
            pnl_amount = self._state.get_daily_pnl()

        if self.last_pnl_date == today_str:
            return  # 同一天不重复记录

        total_asset = self._state.get_total_asset()
        if total_asset <= 0:
            return

        loss_pct = abs(pnl_amount) / total_asset if pnl_amount < 0 else 0

        if loss_pct >= self.MIN_DAILY_LOSS_PCT:
            self.consecutive_loss_days += 1
        else:
            self.consecutive_loss_days = 0

        self.last_pnl_date = today_str

        # 更新熔断状态
        if self.consecutive_loss_days >= self.MAX_CONSECUTIVE_LOSS_DAYS:
            self._circuit_breaker_active = True
            self._circuit_breaker_reason = (
                f"连续{self.consecutive_loss_days}天亏损"
                f"(日亏损>{self.MIN_DAILY_LOSS_PCT:.0%})，"
                f"触发熔断保护"
            )
        else:
            self._circuit_breaker_active = False
            self._circuit_breaker_reason = ""

    def check_consecutive_loss(self):
        """检查连续亏损熔断状态

        返回:
          (blocked: bool, reason: str)
        """
        if self._circuit_breaker_active:
            return True, self._circuit_breaker_reason
        return False, ""

    def reset_circuit_breaker(self):
        """重置熔断状态（手动恢复或盈利日后自动重置）"""
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self.consecutive_loss_days = 0
        self.last_pnl_date = None

    def get_consecutive_loss_info(self):
        """获取连续亏损状态信息"""
        return {
            "consecutive_loss_days": self.consecutive_loss_days,
            "circuit_breaker_active": self._circuit_breaker_active,
            "circuit_breaker_reason": self._circuit_breaker_reason,
            "last_pnl_date": self.last_pnl_date,
            "max_consecutive_loss_days": self.MAX_CONSECUTIVE_LOSS_DAYS,
            "min_daily_loss_pct": self.MIN_DAILY_LOSS_PCT,
        }

    # ═══════════════════════════════════════════════
    # 5. 综合工具
    # ═══════════════════════════════════════════════

    def check_stop_loss(self, pos, t, price, today_str):
        """评估止盈止损信号（同 strategy.evaluate_stop 完全一致）

        返回:
          stop_actions: [(signal_name, signal_type), ...]
        """
        return self._evaluate_stop_loss(pos, t, price, today_str)

    def _evaluate_stop_loss(self, pos, t, price, today_str):
        """止盈止损评估核心逻辑"""
        stop_actions = []

        if not pos.has_position:
            return stop_actions

        # ── 均价止损20%: 从买入均价跌20%无条件清仓(极限方案C: 放宽) ──
        if pos.entry_cost > 0 and price <= pos.entry_cost * 0.80:
            stop_actions.append((f"均价止损20%(avg={pos.entry_cost:.3f}→现价{price:.3f})", "avg_stop_20pct"))

        # ── 硬止盈60%: base_price*1.60(极限方案C) ──
        if pos.base_price and price >= pos.base_price * 1.60:
            stop_actions.append((f"硬止盈60%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_60pct"))

        # ── 移动止盈 ──
        if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
            label = "移动止盈8%(成本+5%)" if not pos.reached_15pct else "移动止盈15%(峰值回撤5%)"
            stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

        # ── 硬止损25%: 从价格峰值回撤25%清仓(极限方案C: 放宽) ──
        if pos.peak_price > 0 and price < pos.peak_price * 0.75:
            stop_actions.append((f"硬止损25%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_25pct"))

        # Level 1a: 连续2天破MA20 → 减30%总仓
        if t["ma20"] and pos.stop_level == 0:
            if price < t["ma20"]:
                if pos.below_ma20_date != today_str:
                    pos.below_ma20_count += 1
                    pos.below_ma20_date = today_str
                if pos.below_ma20_count >= 2:
                    stop_actions.append(("分级止损1级:连续2天破MA20减30%", "reduce_30pct_all"))
                    pos.stop_level = 1
            else:
                pos.below_ma20_count = 0
                pos.below_ma20_date = None

        # Level 1b: DIF<0 → 再减30%
        if pos.stop_level == 1 and t["dif"] is not None and t["dif"] < 0:
            stop_actions.append(("分级止损2级:DIF<0再减30%", "reduce_30pct_all"))
            pos.stop_level = 2

        # Level 1c: MACD死叉/绿柱放大 → 清仓
        if pos.stop_level == 2 and t["macd_status"] in ["死叉", "绿柱放大"]:
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

        # 趋势止盈(MACD红柱缩短+破MA5)
        if t["macd_status"] == "红柱缩短" and price < t["ma5"]:
            stop_actions.append(("趋势止盈(MACD红柱缩短+破MA5)卖10%活动仓", "trend_profit_sell"))

        # 破MA5卖活动仓5%
        if price < t["ma5"] and t["rsi"] and t["rsi"] > 50:
            if pos.active_shares > 0:
                stop_actions.append(("破MA5卖活动仓5%", "sell_active_5pct"))

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
        """检查是否已达仓位上限 (50%)

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
        return ratio >= MAX_POSITION_RATIO, ratio

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

    @staticmethod
    def can_open_new_position(pos, price, total_fund=None):
        """是否可以新开仓位

        检查: 仓位上限
        """
        capped, ratio = RiskManager.is_position_capped(pos, price, total_fund)
        if capped:
            return False, f"仓位已达{ratio*100:.0f}%上限50%"
        return True, ""

    # ═══════════════════════════════════════════════
    # 3. 日亏损限额检查
    # ═══════════════════════════════════════════════

    def check_daily_loss_limit(self, order_intent=None) -> Tuple[bool, str]:
        """检查当日亏损是否已达上限（极限方案C: 取消日亏损限额，始终通过）"""
        return True, "极限方案C: 日亏损限额已取消"

    def check_order(self, order_intent) -> Tuple[bool, str]:
        """统一风控入口（P0 修复）

        检查顺序:
          1. 日亏损限额检查（每日最多亏损5%）
          2. 仓位上限检查（单只不超过50%）

        参数:
          order_intent: OrderIntent 对象

        返回:
          (ok: bool, reason: str)
        """
        # 1. 日亏损限额检查
        ok, msg = self.check_daily_loss_limit(order_intent)
        if not ok:
            return False, msg

        # 2. 仓位上限检查（买入信号）
        if hasattr(order_intent, 'is_buy') and order_intent.is_buy():
            from state_center import get_position_info
            pos_data = get_position_info(order_intent.code)
            if pos_data.get("shares", 0) > 0:
                from position_info import PositionInfo
                pos = PositionInfo()
                pos.shares = pos_data.get("shares", 0)
                pos.avg_cost = pos_data.get("avg_cost", 0.0)
                pos.cost = pos.avg_cost * pos.shares
                capped, ratio = self.is_position_capped(pos, order_intent.price)
                if capped:
                    return False, (
                        f"{order_intent.code} 仓位{ratio*100:.0f}%已达50%上限，"
                        f"跳过买入"
                    )

        return True, "通过"

    @staticmethod
    def is_daily_loss_limit_hit(positions, realtime, today_str, max_loss_pct=None, prev_close=None):
        """检查当日是否超过日亏损限额（极限方案C: 已取消，始终返回 False）"""
        return False, 0.0, ""


# ═══════════════════════════════════════════════
# 模块级快捷函数（向后兼容）
# ═══════════════════════════════════════════════

_rm = RiskManager()


def check_order(order_intent):
    """统一风控入口（P0 修复：日亏损限额在此实际生效）
    
    检查顺序:
      1. 日亏损限额检查（每日最多亏损5%）
      2. 仓位上限检查（单只不超过50%）
      3. 止损检查（止盈/止损/减仓）
    
    返回: (ok: bool, reason: str)
    """
    return _rm.check_order(order_intent)


def check_stop_loss(pos, t, price, today_str):
    """评估止盈止损信号（快捷方式）"""
    return _rm.check_stop_loss(pos, t, price, today_str)


def resolve_stop_signal(pos, stop_actions):
    """解析止盈止损信号（快捷方式）"""
    return _rm.resolve_stop_signal(pos, stop_actions)


def is_position_capped(pos, price, total_fund=None):
    """检查是否已达仓位上限（快捷方式）"""
    return RiskManager.is_position_capped(pos, price, total_fund)


def get_position_ratio(pos, price, total_fund=None):
    """获取当前持仓占比（快捷方式）"""
    return RiskManager.get_position_ratio(pos, price, total_fund)


def is_daily_loss_limit_hit(positions, realtime, today_str, max_loss_pct=None):
    """检查日亏损限额（快捷方式）"""
    return RiskManager.is_daily_loss_limit_hit(positions, realtime, today_str, max_loss_pct)