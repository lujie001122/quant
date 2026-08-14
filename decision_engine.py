#!/usr/bin/env python3
"""
decision_engine.py — 决策引擎

接收 signal_generator 的所有 Signal，按置信度加权投票，
调 signal_cleaner 清洗 AI 信号，调 money_manager 计算数量，
调 risk_manager 风控检查，输出最终交易指令 OrderIntent。

用法:
  from decision_engine import DecisionEngine, OrderIntent
  engine = DecisionEngine()
  intents = engine.process(result, positions, realtime, portfolio_data)

Phase 4: 从 signal_generator.py 的 execute_signals 提取决策逻辑，
         形成独立的决策层，为回测和实盘提供统一接口。
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from money_manager import MoneyManager
from risk_manager import RiskManager
from signal_cleaner import clean_signals
from position_info import PositionInfo


# ═══════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════

@dataclass
class OrderIntent:
    """交易意图 — 决策引擎输出，订单管理器输入"""
    code: str
    action: str                     # '买入', '卖出'
    trade_type: str                 # 'buy', 'sell', 't0', 'liquidate', 'reduce'
    direction: str                  # '买入', '卖出'
    price: float
    shares: int
    pair_price: float = 0.0        # 做T配对挂单价
    reason: str = ""
    confidence: float = 1.0        # 0-1 置信度
    source: str = "strategy"       # 'strategy', 'ai', 'grid', 't0'
    signal_price: float = 0.0     # 原始信号价
    live_price: float = 0.0       # 实时价
    atr_5min: float = 0.0         # 5分钟ATR（做T用）
    fund: float = 0.0             # 分配资金
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict:
        return asdict(self)

    def is_buy(self) -> bool:
        return self.direction == '买入'

    def is_sell(self) -> bool:
        return self.direction == '卖出'

    def is_t0(self) -> bool:
        return self.trade_type == 't0'


# ═══════════════════════════════════════════════════════
# 决策引擎
# ═══════════════════════════════════════════════════════

class DecisionEngine:
    """决策引擎 — 信号→清洗→资金计算→风控→OrderIntent

    管线:
      1. 信号过滤 (只保留有 trade_type 的信号)
      2. 置信度加权投票 (AI 信号 vs 策略信号)
      3. AI 信号清洗 (signal_cleaner)
      4. 资金管理 (money_manager 计算 shares)
      5. 风险管理 (risk_manager 仓位/日亏损检查)
      6. 输出 OrderIntent 列表
    """

    def __init__(self, config: Optional[Dict] = None):
        self.mm = MoneyManager()
        self.rm = RiskManager()
        self.config = config or {}

    # ══════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════

    def process(
        self,
        result: Dict,
        positions: Optional[Dict[str, PositionInfo]] = None,
        realtime: Optional[Dict] = None,
        portfolio_data: Optional[Dict] = None,
        mode: Optional[str] = None,
    ) -> List[OrderIntent]:
        """处理 signal_generator 的输出，生成 OrderIntent 列表

        参数:
          result: generate_signals() 返回的 dict
          positions: {code: PositionInfo, ...} (可选)
          realtime: {code: {price, ...}, ...} (可选)
          portfolio_data: portfolio.json 数据 (可选)
          mode: 'buy_sell', 't0', 或 None(全部)

        返回:
          List[OrderIntent]: 待执行的交易意图列表
        """
        if result is None:
            return []

        signals = result.get("signals", {})
        if not signals:
            return []

        # ── Step 1: AI 信号清洗 ──
        cleaned_result = clean_signals(result, portfolio_data)

        # ── Step 2: 提取 actionable 信号 ──
        actionable = self._extract_actionable(cleaned_result, mode)
        if not actionable:
            return []

        # ── Step 3: 逐个信号生成 OrderIntent ──
        intents = []
        for code, sig in actionable:
            intent = self._signal_to_intent(code, sig, positions, realtime)
            if intent is None:
                continue

            # ── Step 4: 风控检查 ──
            if not self._risk_check(intent, positions, realtime):
                continue

            intents.append(intent)

        # ── Step 5: 按优先级排序 ──
        intents = self._prioritize(intents)

        return intents

    # ══════════════════════════════════════════════════
    # 信号提取
    # ══════════════════════════════════════════════════

    def _extract_actionable(
        self, result: Dict, mode: Optional[str] = None
    ) -> List[Tuple[str, Dict]]:
        """从 result 中提取可执行的信号

        mode: None=全部, 'buy_sell'=买卖+做T, 't0'=只做T
        """
        signals = result.get("signals", {})
        BUY_SELL_TYPES = {"buy", "sell", "liquidate", "reduce"}
        T0_TYPES = {"t0"}

        actionable = []
        for code, sig in signals.items():
            trade_type = sig.get("trade_type")
            if not trade_type:
                continue

            # 模式过滤
            if mode == "buy_sell":
                if trade_type not in BUY_SELL_TYPES and trade_type not in T0_TYPES:
                    continue
            elif mode == "t0":
                if trade_type not in T0_TYPES:
                    continue

            actionable.append((code, sig))

        return actionable

    # ══════════════════════════════════════════════════
    # 信号→OrderIntent
    # ══════════════════════════════════════════════════

    def _signal_to_intent(
        self,
        code: str,
        sig: Dict,
        positions: Optional[Dict[str, PositionInfo]] = None,
        realtime: Optional[Dict] = None,
    ) -> Optional[OrderIntent]:
        """将单个信号转换为 OrderIntent"""
        trade_type = sig["trade_type"]
        action = sig.get("action", "")
        reason = sig.get("reason", "")
        t0_pair = sig.get("t0_pair")

        # ── 解析价格 ──
        signal_price = self._parse_price(sig.get("price", "0"))
        live_price = self._get_live_price(code, realtime)
        price = self._calculate_limit_price(trade_type, action, signal_price, live_price)

        if price <= 0:
            return None

        # ── 确定方向 ──
        direction = self._get_direction(trade_type, action)

        # ── 计算数量 ──
        shares = self._calculate_shares(code, sig, trade_type, action, price, t0_pair, positions, realtime)
        if shares < 100:
            return None

        # ── 做T配对价 ──
        pair_price = 0.0
        if trade_type == "t0" and t0_pair:
            pair_price = t0_pair.get("pair_price", 0.0)
            # 用实时 ATR 重算配对价
            sig_atr = sig.get("atr_5min") or sig.get("atr", 0)
            try:
                sig_atr = float(sig_atr)
            except (ValueError, TypeError):
                sig_atr = 0.0
            if sig_atr > 0:
                if direction == '买入':
                    pair_price = round(price + sig_atr * 1.1, 3)
                else:
                    pair_price = round(price - sig_atr * 1.1, 3)

        # ── 确定来源 ──
        source = self._get_source(trade_type, sig)

        # ── 置信度 ──
        confidence = self._get_confidence(sig, source)

        return OrderIntent(
            code=code,
            action=action,
            trade_type=trade_type,
            direction=direction,
            price=price,
            shares=shares,
            pair_price=pair_price,
            reason=reason,
            confidence=confidence,
            source=source,
            signal_price=signal_price,
            live_price=live_price,
            atr_5min=float(sig.get("atr_5min", 0) or 0),
            fund=self._get_fund(code),
        )

    # ══════════════════════════════════════════════════
    # 辅助方法
    # ══════════════════════════════════════════════════

    @staticmethod
    def _parse_price(price_str) -> float:
        try:
            return float(price_str)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _get_live_price(code: str, realtime: Optional[Dict]) -> float:
        if realtime is None:
            return 0.0
        live = realtime.get(code, {})
        return live.get("price", 0.0) if live else 0.0

    @staticmethod
    def _calculate_limit_price(
        trade_type: str, action: str, signal_price: float, live_price: float
    ) -> float:
        """计算下单限价（买入取低，卖出取高）"""
        if live_price <= 0:
            return signal_price

        is_buy = (
            trade_type == "buy"
            or (trade_type == "t0" and "买入" in action)
        )
        is_sell = (
            trade_type in ("sell", "liquidate", "reduce")
            or (trade_type == "t0" and "卖出" in action)
        )

        if is_buy:
            return min(signal_price, live_price) if signal_price > 0 else live_price
        elif is_sell:
            return max(signal_price, live_price) if signal_price > 0 else live_price
        return signal_price if signal_price > 0 else live_price

    @staticmethod
    def _get_direction(trade_type: str, action: str) -> str:
        """获取交易方向（中文）"""
        if trade_type in ("buy",):
            return "买入"
        elif trade_type in ("sell", "liquidate", "reduce"):
            return "卖出"
        elif trade_type == "t0":
            return "买入" if "买入" in action else "卖出"
        return "卖出"

    def _calculate_shares(
        self,
        code: str,
        sig: Dict,
        trade_type: str,
        action: str,
        price: float,
        t0_pair: Optional[Dict],
        positions: Optional[Dict[str, PositionInfo]],
        realtime: Optional[Dict],
    ) -> int:
        """计算交易股数"""
        # 做T
        if trade_type == "t0":
            return self._calc_t0_shares(code, sig, t0_pair, positions)

        # 买入
        if trade_type == "buy":
            return self._calc_buy_shares(code, sig, price)

        # 卖出/清仓/减仓
        if trade_type in ("sell", "liquidate", "reduce"):
            return self._calc_sell_shares(code, sig, trade_type, positions)

        return 0

    def _calc_t0_shares(
        self, code: str, sig: Dict, t0_pair: Optional[Dict],
        positions: Optional[Dict[str, PositionInfo]]
    ) -> int:
        """计算做T股数"""
        if t0_pair:
            return t0_pair.get("shares", 0)

        # 降级：从持仓算30%
        if positions and code in positions:
            pos = positions[code]
            return self.mm.calc_t0_shares(pos, ratio=0.30)
        return 0

    def _calc_buy_shares(self, code: str, sig: Dict, price: float) -> int:
        """计算买入股数"""
        ratio = self._parse_position_ratio(sig.get("position_ratio", ""))
        fund = self._get_fund(code)
        return self.mm.calc_position_size(fund, ratio, price)

    def _calc_sell_shares(
        self, code: str, sig: Dict, trade_type: str,
        positions: Optional[Dict[str, PositionInfo]]
    ) -> int:
        """计算卖出股数"""
        if positions is None or code not in positions:
            return 0
        pos = positions[code]
        current_shares = pos.shares

        if trade_type == "liquidate":
            return current_shares

        ratio = self._parse_position_ratio(sig.get("position_ratio", ""))
        if ratio > 0:
            shares = int(current_shares * ratio / 100) * 100
        else:
            shares = int(current_shares * 0.30 / 100) * 100

        return min(shares, current_shares) if shares >= 100 else current_shares

    @staticmethod
    def _parse_position_ratio(ratio_str: str) -> float:
        """解析持仓比例字符串 → 浮点数"""
        if not ratio_str:
            return 0.0
        try:
            ratio_str = ratio_str.replace("%", "").strip()
            return float(ratio_str) / 100.0
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _get_fund(code: str) -> float:
        """获取标的分配资金"""
        from market_data import ETFS
        return ETFS.get(code, {}).get("fund", 44000)

    @staticmethod
    def _get_source(trade_type: str, sig: Dict) -> str:
        """确定信号来源"""
        if trade_type == "t0":
            return "t0"
        if sig.get("grid_signal") and sig["grid_signal"] != "无":
            return "grid"
        if sig.get("stop_signal") and "未触发" not in sig["stop_signal"]:
            return "stop_loss"
        return "strategy"

    @staticmethod
    def _get_confidence(sig: Dict, source: str) -> float:
        """获取信号置信度"""
        if source == "ai":
            return sig.get("confidence", 0.7)
        if source == "stop_loss":
            return 1.0  # 止损信号最高置信度
        if source == "t0":
            t0_signal = sig.get("t0_signal", "")
            if "极端" in t0_signal:
                return 1.0
            return 0.8
        return 0.9  # 策略信号默认置信度

    # ══════════════════════════════════════════════════
    # 风控检查
    # ══════════════════════════════════════════════════

    def _risk_check(
        self,
        intent: OrderIntent,
        positions: Optional[Dict[str, PositionInfo]],
        realtime: Optional[Dict],
    ) -> bool:
        """风控检查 — 返回 True 表示通过"""
        # ── 仓位上限检查（买入信号） ──
        if intent.is_buy() and positions and intent.code in positions:
            pos = positions[intent.code]
            if pos.has_position:
                capped, ratio = self.rm.is_position_capped(pos, intent.price)
                if capped:
                    print(f"  [RISK] {intent.code} 仓位{ratio*100:.0f}%已达上限，跳过买入")
                    return False

        # ── 日亏损限额检查 ──
        if positions and realtime:
            is_hit, loss_pct, reason = self.rm.is_daily_loss_limit_hit(
                positions, realtime, datetime.now().strftime("%Y-%m-%d")
            )
            if is_hit:
                print(f"  [RISK] {reason}，跳过所有交易")
                return False

        return True

    # ══════════════════════════════════════════════════
    # 优先级排序
    # ══════════════════════════════════════════════════

    @staticmethod
    def _prioritize(intents: List[OrderIntent]) -> List[OrderIntent]:
        """按优先级排序: 止损清仓 > 减仓 > 做T卖出 > 做T买入 > 卖出 > 买入"""
        priority_map = {
            "liquidate": 0,
            "reduce": 1,
            "t0": 2,       # 卖出优先于买入在下面处理
            "sell": 4,
            "buy": 5,
        }

        def sort_key(i: OrderIntent):
            base = priority_map.get(i.trade_type, 9)
            # 做T内部：卖出优先
            if i.trade_type == "t0":
                base = 2 if i.is_sell() else 3
            # 置信度高的优先
            return (base, -i.confidence)

        return sorted(intents, key=sort_key)


# ═══════════════════════════════════════════════════════
# 模块级快捷函数
# ═══════════════════════════════════════════════════════

_engine = DecisionEngine()


def process_signals(result, positions=None, realtime=None, portfolio_data=None, mode=None):
    """快捷函数: 处理信号生成 OrderIntent 列表"""
    return _engine.process(result, positions, realtime, portfolio_data, mode)


def signal_to_intent(code, sig, positions=None, realtime=None):
    """快捷函数: 单个信号 → OrderIntent"""
    return _engine._signal_to_intent(code, sig, positions, realtime)