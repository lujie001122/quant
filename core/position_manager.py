#!/usr/bin/env python3
"""
core/position_manager.py — 仓位管理（Kelly公式 + 风险平价 + 资金分配 + can_open检查）

解决问题:
  - 仓位计算分散在 money_manager.py / risk_manager.py / state_center.py
  - 无统一的 can_open 检查（买入前不知道是否允许开仓）
  - 无Kelly公式/风险平价等科学仓位管理
  - 资金分配逻辑不透明

核心功能:
  1. Kelly公式: 根据胜率和赔率计算最优仓位比例
  2. 风险平价: 按波动率倒数分配资金
  3. can_open: 买入前综合检查（仓位上限/日买入次数/冷却期/资金充足）
  4. allocate_fund: 在多只ETF之间分配资金

用法:
  from core.position_manager import PositionManager

  pm = PositionManager(total_fund=220000)

  # Kelly公式
  kelly_ratio = pm.kelly_fraction(win_rate=0.55, win_loss_ratio=1.5)

  # 风险平价
  weights = pm.risk_parity({"159516": 0.015, "510300": 0.020})

  # 买入前检查
  can, reason = pm.can_open(code="159516", current_positions={...}, today_buys=0)

  # 资金分配
  allocation = pm.allocate_fund(codes=["159516", "510300", "588170"])
"""

import math
import os
from typing import Dict, List, Optional, Tuple

import yaml


# ══════════════════════════════════════════════
# 仓位管理器
# ══════════════════════════════════════════════

class PositionManager:
    """仓位管理器 — Kelly公式 + 风险平价 + 资金分配 + 开仓检查

    参数:
      total_fund: 总资金
      config_path: config.yaml 路径（空则自动查找）
    """

    def __init__(
        self,
        total_fund: float = 0,
        config_path: str = "",
    ):
        # 加载配置
        if not config_path:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml"
            )

        self._config = self._load_config(config_path)

        # 从配置读取参数
        self.total_fund = total_fund or self._config.get("total_fund", 220000)
        self.max_per_etf = self._config.get("max_per_etf", 220000)
        self.max_position_ratio = self._config.get("entry", {}).get("position_cap", 0.50)
        self.max_daily_buys = self._config.get("max_daily_buys", 1)
        self.max_daily_t0 = self._config.get("max_daily_t0", 3)
        self.cooldown_days = self._config.get("cooldown_days", 2)
        self.dead_ratio = self._config.get("dead_ratio", 0.3)
        self.active_ratio = self._config.get("active_ratio", 0.7)
        self.min_shares = self._config.get("trade", {}).get("min_shares", 5000)
        self.concentrated_mode = self._config.get("concentrated_mode", False)
        self.concentrated_top_n = self._config.get("concentrated_top_n", 3)

    @staticmethod
    def _load_config(config_path: str) -> dict:
        """加载配置文件"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    # ══════════════════════════════════════════════
    # 1. Kelly公式
    # ══════════════════════════════════════════════

    @staticmethod
    def kelly_fraction(
        win_rate: float,
        win_loss_ratio: float,
        fraction: float = 1.0,
    ) -> float:
        """Kelly公式计算最优仓位比例

        Kelly = (p * b - q) / b
          p = 胜率
          q = 1 - p = 败率
          b = 赔率 (平均盈利/平均亏损)

        参数:
          win_rate: 胜率 (0-1)
          win_loss_ratio: 盈亏比 (平均盈利/平均亏损)
          fraction: Kelly分数（通常用半Kelly=0.5降低波动）

        返回:
          float: 最优仓位比例 (0-1)，负数表示不应下注

        示例:
          >>> PositionManager.kelly_fraction(0.55, 1.5)
          0.277...  # 全Kelly
          >>> PositionManager.kelly_fraction(0.55, 1.5, fraction=0.5)
          0.138...  # 半Kelly
        """
        if win_rate <= 0 or win_rate >= 1 or win_loss_ratio <= 0:
            return 0.0

        p = win_rate
        q = 1 - p
        b = win_loss_ratio

        kelly = (p * b - q) / b

        # Kelly可能为负（不该下注）
        kelly = max(kelly, 0.0)

        # 应用分数（半Kelly等）
        kelly *= fraction

        # 不超过100%
        return min(kelly, 1.0)

    @staticmethod
    def kelly_from_history(
        wins: int,
        losses: int,
        avg_win: float,
        avg_loss: float,
        fraction: float = 0.5,
    ) -> float:
        """从历史数据计算Kelly比例

        参数:
          wins: 盈利次数
          losses: 亏损次数
          avg_win: 平均盈利金额
          avg_loss: 平均亏损金额（正数）
          fraction: Kelly分数

        返回:
          float: Kelly比例
        """
        total = wins + losses
        if total == 0 or avg_loss == 0:
            return 0.0

        win_rate = wins / total
        win_loss_ratio = abs(avg_win / avg_loss)
        return PositionManager.kelly_fraction(win_rate, win_loss_ratio, fraction)

    # ══════════════════════════════════════════════
    # 2. 风险平价
    # ══════════════════════════════════════════════

    @staticmethod
    def risk_parity(
        volatilities: Dict[str, float],
        target_vol: float = 0.0,
    ) -> Dict[str, float]:
        """风险平价分配

        按波动率倒数分配权重，使每只ETF的风险贡献相等。

        参数:
          volatilities: {code: 年化波动率} (如 {"159516": 0.15, "510300": 0.20})
          target_vol: 目标组合波动率（0=不加约束，按自然权重归一化）

        返回:
          dict: {code: 权重} (权重之和=1)

        示例:
          >>> PositionManager.risk_parity({"159516": 0.15, "510300": 0.20})
          {"159516": 0.571, "510300": 0.429}
        """
        if not volatilities:
            return {}

        # 波动率倒数
        inv_vols = {}
        for code, vol in volatilities.items():
            if vol > 0:
                inv_vols[code] = 1.0 / vol
            else:
                # 波动率为0的给予等权
                inv_vols[code] = 1.0

        total_inv = sum(inv_vols.values())

        if total_inv <= 0:
            # 全部等权
            n = len(volatilities)
            return {code: 1.0 / n for code in volatilities}

        weights = {code: inv / total_inv for code, inv in inv_vols.items()}

        # 如果指定了目标波动率，缩放权重
        if target_vol > 0:
            current_vol = PositionManager._portfolio_volatility(weights, volatilities)
            if current_vol > 0:
                scale = min(target_vol / current_vol, 1.0)  # 不超过1
                weights = {code: w * scale for code, w in weights.items()}

        return weights

    @staticmethod
    def _portfolio_volatility(
        weights: Dict[str, float],
        volatilities: Dict[str, float],
    ) -> float:
        """计算组合波动率（简化：假设不相关）"""
        variance = sum(
            (weights.get(code, 0) ** 2) * (vol ** 2)
            for code, vol in volatilities.items()
        )
        return math.sqrt(variance) if variance > 0 else 0.0

    # ══════════════════════════════════════════════
    # 3. can_open 开仓检查
    # ══════════════════════════════════════════════

    def can_open(
        self,
        code: str,
        current_positions: Dict[str, dict],
        today_buys: int = 0,
        last_sell_date: Dict[str, str] = None,
        available_cash: float = 0,
        price: float = 0,
        shares: int = 0,
    ) -> Tuple[bool, str]:
        """综合开仓检查

        检查项:
          1. 日买入次数限制
          2. 单只ETF仓位上限
          3. 冷却期检查
          4. 资金充足检查
          5. 已有仓位重复检查

        参数:
          code: ETF代码
          current_positions: 当前持仓 {code: {shares, market_value}}
          today_buys: 今日已买入次数
          last_sell_date: 最后卖出日期 {code: "YYYY-MM-DD"}
          available_cash: 可用资金
          price: 当前价格
          shares: 计划买入股数

        返回:
          (bool, str): (是否允许, 原因说明)
        """
        if last_sell_date is None:
            last_sell_date = {}

        # ── 检查1: 日买入次数限制 ──
        if today_buys >= self.max_daily_buys:
            return False, f"今日买入次数已达上限({self.max_daily_buys}次)"

        # ── 检查2: 冷却期 ──
        if code in last_sell_date:
            from datetime import datetime
            try:
                last_date = datetime.strptime(last_sell_date[code], "%Y-%m-%d")
                days_since = (datetime.now() - last_date).days
                if days_since < self.cooldown_days:
                    return False, f"冷却期内(还剩{self.cooldown_days - days_since}天)"
            except (ValueError, TypeError):
                pass

        # ── 检查3: 单只ETF仓位上限 ──
        if code in current_positions:
            current_value = current_positions[code].get("market_value", 0)
            current_ratio = current_value / self.total_fund if self.total_fund > 0 else 0
            if current_ratio >= self.max_position_ratio:
                return False, f"仓位已达上限({self.max_position_ratio:.0%})"

        # ── 检查4: 资金充足 ──
        if available_cash > 0 and price > 0 and shares > 0:
            required = price * shares
            if required > available_cash:
                return False, f"资金不足(需要{required:.0f}, 可用{available_cash:.0f})"

        # ── 检查5: 单只ETF金额上限 ──
        if code in current_positions:
            current_value = current_positions[code].get("market_value", 0)
            new_value = current_value
            if price > 0 and shares > 0:
                new_value = current_value + price * shares
            if new_value > self.max_per_etf:
                return False, f"超出单只ETF上限({self.max_per_etf:.0f})"

        return True, "通过"

    def can_open_position(
        self,
        code: str,
        state_center=None,
    ) -> Tuple[bool, str]:
        """基于 StateCenter 的开仓检查（便捷方法）

        参数:
          code: ETF代码
          state_center: StateCenter实例（None则自动获取）

        返回:
          (bool, str)
        """
        if state_center is None:
            try:
                from state_center import StateCenter
                state_center = StateCenter.get_instance()
            except ImportError:
                return False, "无法加载StateCenter"

        # 获取当前持仓信息
        current_positions = {}
        for sym, pos_data in state_center.main_account.positions.items():
            current_positions[sym] = {
                "shares": pos_data.get("volume", 0),
                "market_value": pos_data.get("volume", 0) * pos_data.get("avg_cost", 0),
            }

        today_buys = 0  # 可从trade_recorder获取

        return self.can_open(
            code=code,
            current_positions=current_positions,
            today_buys=today_buys,
            available_cash=state_center.main_account.cash,
        )

    # ══════════════════════════════════════════════
    # 4. 资金分配
    # ══════════════════════════════════════════════

    def allocate_fund(
        self,
        codes: List[str],
        volatilities: Optional[Dict[str, float]] = None,
        method: str = "equal",
    ) -> Dict[str, float]:
        """在多只ETF之间分配资金

        参数:
          codes: ETF代码列表
          volatilities: 波动率数据（风险平价模式需要）
          method: 分配方法
            "equal" — 等权分配
            "risk_parity" — 风险平价
            "concentrated" — 集中持仓（前N只占更大权重）

        返回:
          dict: {code: 分配金额}
        """
        if not codes:
            return {}

        active_fund = self.total_fund * self.active_ratio

        if method == "risk_parity" and volatilities:
            weights = self.risk_parity(volatilities)
            return {code: round(active_fund * weights.get(code, 0), 2) for code in codes}

        elif method == "concentrated" and self.concentrated_mode:
            # 集中模式: 前N只占60%，其余均分40%
            top_n = min(self.concentrated_top_n, len(codes))
            top_weight = 0.6 / top_n
            rest_count = len(codes) - top_n
            rest_weight = 0.4 / rest_count if rest_count > 0 else 0

            allocation = {}
            for i, code in enumerate(codes):
                if i < top_n:
                    allocation[code] = round(active_fund * top_weight, 2)
                else:
                    allocation[code] = round(active_fund * rest_weight, 2)
            return allocation

        else:
            # 等权分配
            per_etf = active_fund / len(codes)
            # 不超过单只上限
            per_etf = min(per_etf, self.max_per_etf)
            return {code: round(per_etf, 2) for code in codes}

    def allocate_by_kelly(
        self,
        codes: List[str],
        win_rates: Dict[str, float],
        win_loss_ratios: Dict[str, float],
        kelly_fraction: float = 0.5,
    ) -> Dict[str, float]:
        """基于Kelly公式的资金分配

        参数:
          codes: ETF代码列表
          win_rates: {code: 胜率}
          win_loss_ratios: {code: 盈亏比}
          kelly_fraction: Kelly分数（0.5=半Kelly）

        返回:
          dict: {code: 分配金额}
        """
        active_fund = self.total_fund * self.active_ratio

        # 计算每只ETF的Kelly比例
        kelly_ratios = {}
        for code in codes:
            wr = win_rates.get(code, 0.5)
            wlr = win_loss_ratios.get(code, 1.0)
            kelly_ratios[code] = self.kelly_fraction(wr, wlr, kelly_fraction)

        # 归一化（Kelly比例之和可能超过1）
        total_kelly = sum(kelly_ratios.values())
        if total_kelly > 1.0 and total_kelly > 0:
            kelly_ratios = {code: k / total_kelly for code, k in kelly_ratios.items()}

        allocation = {}
        for code in codes:
            amount = active_fund * kelly_ratios.get(code, 0)
            amount = min(amount, self.max_per_etf)
            allocation[code] = round(amount, 2)

        return allocation

    # ══════════════════════════════════════════════
    # 5. 仓位计算
    # ══════════════════════════════════════════════

    def calc_shares(
        self,
        fund: float,
        ratio: float,
        price: float,
        round_lot: int = 100,
    ) -> int:
        """计算交易股数

        参数:
          fund: 分配资金
          ratio: 仓位比例
          price: 当前价格
          round_lot: 取整单位

        返回:
          int: 交易股数
        """
        if price <= 0 or fund <= 0 or ratio <= 0:
            return 0
        target_value = fund * ratio
        shares = int(target_value / price / round_lot) * round_lot
        return max(shares, self.min_shares)

    def position_value_ratio(
        self,
        code: str,
        current_positions: Dict[str, dict],
    ) -> float:
        """计算当前持仓占总资金比例

        参数:
          code: ETF代码
          current_positions: 当前持仓

        返回:
          float: 比例 (0-1)
        """
        if code not in current_positions or self.total_fund <= 0:
            return 0.0
        market_value = current_positions[code].get("market_value", 0)
        return market_value / self.total_fund

    def available_position_ratio(
        self,
        code: str,
        current_positions: Dict[str, dict],
    ) -> float:
        """计算剩余可开仓比例

        返回:
          float: 剩余可开仓比例 (0-1)
        """
        current = self.position_value_ratio(code, current_positions)
        return max(self.max_position_ratio - current, 0.0)

    # ══════════════════════════════════════════════
    # 6. 统计信息
    # ══════════════════════════════════════════════

    def get_position_summary(self, current_positions: Dict[str, dict]) -> dict:
        """获取持仓汇总

        返回:
          dict: {
            total_codes, total_market_value, total_ratio,
            cash_ratio, max_single_ratio, by_code: {...}
          }
        """
        if not current_positions:
            return {
                "total_codes": 0,
                "total_market_value": 0.0,
                "total_ratio": 0.0,
                "cash_ratio": 1.0,
                "max_single_ratio": 0.0,
                "by_code": {},
            }

        total_value = 0.0
        max_ratio = 0.0
        by_code = {}

        for code, pos in current_positions.items():
            mv = pos.get("market_value", 0)
            ratio = mv / self.total_fund if self.total_fund > 0 else 0
            total_value += mv
            max_ratio = max(max_ratio, ratio)
            by_code[code] = {
                "market_value": round(mv, 2),
                "ratio": round(ratio, 4),
                "shares": pos.get("shares", 0),
            }

        total_ratio = total_value / self.total_fund if self.total_fund > 0 else 0
        cash_ratio = max(1.0 - total_ratio, 0.0)

        return {
            "total_codes": len(current_positions),
            "total_market_value": round(total_value, 2),
            "total_ratio": round(total_ratio, 4),
            "cash_ratio": round(cash_ratio, 4),
            "max_single_ratio": round(max_ratio, 4),
            "by_code": by_code,
        }
