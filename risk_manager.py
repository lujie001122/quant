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

# 默认参数
TOTAL_FUND = 220000
MAX_POSITION_RATIO = 0.50  # 单只上限50%
MAX_DAILY_LOSS_PCT = 0.05  # 日亏损限额5%
DEAD_RATIO = 0.6
ACTIVE_RATIO = 0.4


class RiskManager:
    """风险管理器 — 统一处理止损、仓位上限、日亏损限额"""

    # ══════════════════════════════════════════════
    # 1. 止损检查
    # ══════════════════════════════════════════════

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

        # ── 保本止盈: 浮盈≥10%激活, 回落到成本+2%清仓 ──
        if pos.entry_cost > 0:
            profit_pct = (price - pos.entry_cost) / pos.entry_cost
            if profit_pct >= 0.10 - 0.0001:
                pos.breakeven_activated = True
            if pos.breakeven_activated and price <= pos.entry_cost * 1.02:
                stop_actions.append((f"保本止盈(avg={pos.entry_cost:.3f}→现价{price:.3f})", "breakeven_stop"))

        # ── 均价止损12%: 从买入均价跌12%无条件清仓 ──
        if pos.entry_cost > 0 and price <= pos.entry_cost * 0.88:
            stop_actions.append((f"均价止损12%(avg={pos.entry_cost:.3f}→现价{price:.3f})", "avg_stop_12pct"))

        # ── 硬止盈30%: base_price*1.30 ──
        if pos.base_price and price >= pos.base_price * 1.30:
            stop_actions.append((f"硬止盈30%(基准{pos.base_price:.3f}→现价{price:.3f})", "liquidate_30pct"))

        # ── 移动止盈 ──
        if pos.trailing_stop_price > 0 and price <= pos.trailing_stop_price:
            label = "移动止盈8%(成本+5%)" if not pos.reached_15pct else "移动止盈15%(峰值回撤5%)"
            stop_actions.append((f"{label}:触线{pos.trailing_stop_price:.3f}", "liquidate_trailing"))

        # ── 硬止损20%: 从价格峰值回撤20%清仓 ──
        if pos.peak_price > 0 and price < pos.peak_price * 0.80:
            stop_actions.append((f"硬止损20%(峰值{pos.peak_price:.3f}→现价{price:.3f})", "hard_stop_20pct"))

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
            if sig_type in ("liquidate_trend", "liquidate_trailing", "liquidate_30pct",
                            "avg_stop_12pct", "hard_stop_20pct", "breakeven_stop"):
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

    # ══════════════════════════════════════════════
    # 3. 日亏损限额检查
    # ══════════════════════════════════════════════

    @staticmethod
    def is_daily_loss_limit_hit(positions, realtime, today_str, max_loss_pct=None):
        """检查当日是否超过日亏损限额

        参数:
          positions: {code: PositionInfo, ...}
          realtime: {code: {price, ...}, ...}
          today_str: "YYYY-MM-DD"
          max_loss_pct: 日亏损限额 (默认5%)
        返回:
          (is_hit: bool, loss_pct: float, reason: str)
        """
        if max_loss_pct is None:
            max_loss_pct = MAX_DAILY_LOSS_PCT

        total_morning_value = 0.0
        total_current_value = 0.0

        for code, pos in positions.items():
            if not pos.has_position:
                continue
            price = realtime.get(code, {}).get("price", 0)
            if price <= 0:
                continue
            total_current_value += pos.shares * price
            # 估算开盘市值: 用持仓成本或昨日收盘
            total_morning_value += pos.shares * pos.avg_cost

        if total_morning_value <= 0:
            return False, 0.0, ""

        loss_pct = (total_morning_value - total_current_value) / total_morning_value
        if loss_pct > max_loss_pct:
            return True, loss_pct * 100, f"日亏损{loss_pct*100:.1f}%超过限额{max_loss_pct*100:.0f}%"

        return False, loss_pct * 100, ""


# ═══════════════════════════════════════════════
# 模块级快捷函数（向后兼容）
# ═══════════════════════════════════════════════

_rm = RiskManager()


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