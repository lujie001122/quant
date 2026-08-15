#!/usr/bin/env python3
"""
datatypes.py — 共享数据类型

系统核心数据类统一定义，消除跨模块循环依赖。
P3-2: 从 decision_engine.py（OrderIntent）和 order_manager.py（Order）提取。

用法:
  from datatypes import OrderIntent, Order
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict


# ═══════════════════════════════════════════════════════
# OrderIntent — 交易意图（决策引擎输出）
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
    signal_price: float = 0.0      # 原始信号价
    live_price: float = 0.0        # 实时价
    atr_5min: float = 0.0          # 5分钟ATR（做T用）
    fund: float = 0.0              # 分配资金
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
# Order — 订单状态机（订单管理器管理）
# ═══════════════════════════════════════════════════════

@dataclass
class Order:
    """订单 — 完整的订单状态机"""
    order_id: str
    code: str
    action: str                    # 'buy', 'sell', 't0_buy', 't0_sell'
    direction: str                 # '买入', '卖出'
    shares: int
    price: float
    pair_price: float = 0.0       # 做T配对价
    status: str = "pending"       # pending, filled, partially_filled, revoked, failed, expired
    filled_shares: int = 0
    avg_fill_price: float = 0.0
    reason: str = ""
    source: str = "strategy"
    confidence: float = 1.0
    broker_order_id: str = ""     # 券商返回的订单ID
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    expires_at: str = ""           # 超时时间
    error_message: str = ""
    retry_count: int = 0
    account: str = "main"         # "main" 或 "t0"，逻辑子账户标识

    def to_dict(self) -> Dict:
        return asdict(self)

    def is_active(self) -> bool:
        """是否处于活跃状态（pending/partially_filled）"""
        return self.status in ("pending", "partially_filled")

    def is_done(self) -> bool:
        """是否已终态"""
        return self.status in ("filled", "revoked", "failed", "expired")

    def is_buy(self) -> bool:
        return 'buy' in self.action and 'sell' not in self.action

    def is_sell(self) -> bool:
        return 'sell' in self.action