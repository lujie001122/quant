#!/usr/bin/env python3
"""
order_manager.py — 订单管理器

接收 OrderIntent，生成订单，维护订单状态生命周期。
提供 submit_order / cancel_order / query_order / check_timeout 接口。

用法:
  from order_manager import OrderManager, Order
  om = OrderManager()
  order = om.submit_order(intent)
  om.cancel_order(order.order_id)
  orders = om.query_orders(code="159516")
  t0_orders = om.query_orders_by_account("t0")

Phase 4: 从 state_center.py 的订单管理逻辑提取，形成独立订单管理层。
         与现有 orders/ 目录文件格式兼容。
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from decision_engine import OrderIntent


# ═══════════════════════════════════════════════════════
# 数据类
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


# ═══════════════════════════════════════════════════════
# 订单管理器
# ═══════════════════════════════════════════════════════

class OrderManager:
    """订单管理器 — 订单生命周期管理

    职责:
      1. 生成订单 (submit_order)
      2. 撤单 (cancel_order)
      3. 查询 (query_order / query_orders)
      4. 超时自动撤单 (check_timeout)
      5. 持久化到 orders/ 目录
    """

    # 默认超时时间（秒）
    DEFAULT_TIMEOUT_SECONDS = 300  # 5分钟

    def __init__(
        self,
        orders_dir: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        if orders_dir is None:
            orders_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "orders"
            )
        self.orders_dir = orders_dir
        self.intent_dir = os.path.join(orders_dir, "intent")
        self.timeout_seconds = timeout_seconds

        # 内存订单缓存
        self._orders: Dict[str, Order] = {}

        # 确保目录存在
        os.makedirs(self.orders_dir, exist_ok=True)
        os.makedirs(self.intent_dir, exist_ok=True)

        # 加载已有订单
        self._load_existing_orders()

    # ══════════════════════════════════════════════════
    # 订单生成
    # ══════════════════════════════════════════════════

    def submit_order(self, intent: OrderIntent, broker_order_id: str = "") -> Order:
        """从 OrderIntent 生成订单并持久化

        参数:
          intent: 决策引擎输出的交易意图
          broker_order_id: 券商返回的订单ID（可选）

        返回:
          Order: 生成的订单对象
        """
        order_id = self._generate_order_id(intent.code, intent.trade_type, intent.direction)

        # 构造 action 字符串
        action = self._intent_to_action(intent.trade_type, intent.direction)

        # 设置超时时间
        expires_at = (
            datetime.now() + timedelta(seconds=self.timeout_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")

        # 根据 intent 的 trade_type 自动设置子账户
        account = "t0" if (intent.trade_type == "t0" or "t0" in action) else "main"

        order = Order(
            order_id=order_id,
            code=intent.code,
            action=action,
            direction=intent.direction,
            shares=intent.shares,
            price=intent.price,
            pair_price=intent.pair_price,
            reason=intent.reason,
            source=intent.source,
            confidence=intent.confidence,
            broker_order_id=broker_order_id,
            expires_at=expires_at,
            account=account,
        )

        # 保存到内存和磁盘
        self._orders[order_id] = order
        self._persist_order(order)

        return order

    @staticmethod
    def _intent_to_action(trade_type: str, direction: str) -> str:
        """trade_type → action 字符串"""
        mapping = {
            ("buy", "买入"): "buy",
            ("sell", "卖出"): "sell",
            ("liquidate", "卖出"): "sell",
            ("reduce", "卖出"): "sell",
            ("t0", "买入"): "t0_buy",
            ("t0", "卖出"): "t0_sell",
        }
        return mapping.get((trade_type, direction), "sell")

    def _generate_order_id(self, code: str, trade_type: str, direction: str) -> str:
        """生成唯一订单ID"""
        today = datetime.now().strftime("%Y%m%d")
        ts = int(time.time() * 1000)
        dir_short = "B" if direction == "买入" else "S"
        return f"{today}_{code}_{dir_short}_{trade_type}_{ts}"

    # ══════════════════════════════════════════════════
    # 订单状态管理
    # ══════════════════════════════════════════════════

    def update_order_status(
        self,
        order_id: str,
        status: str,
        filled_shares: int = 0,
        avg_fill_price: float = 0.0,
        broker_order_id: str = "",
        error_message: str = "",
    ) -> Optional[Order]:
        """更新订单状态"""
        order = self._orders.get(order_id)
        if order is None:
            # 尝试从磁盘加载
            order = self._load_order(order_id)
            if order is None:
                return None

        order.status = status
        if filled_shares > 0:
            order.filled_shares = filled_shares
        if avg_fill_price > 0:
            order.avg_fill_price = avg_fill_price
        if broker_order_id:
            order.broker_order_id = broker_order_id
        if error_message:
            order.error_message = error_message
        order.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._orders[order_id] = order
        self._persist_order(order)

        return order

    def mark_filled(
        self, order_id: str, filled_shares: int = 0, avg_fill_price: float = 0.0
    ) -> Optional[Order]:
        """标记订单为已成交"""
        return self.update_order_status(
            order_id,
            status="filled",
            filled_shares=filled_shares,
            avg_fill_price=avg_fill_price,
        )

    def mark_failed(self, order_id: str, error_message: str = "") -> Optional[Order]:
        """标记订单为失败"""
        return self.update_order_status(
            order_id, status="failed", error_message=error_message
        )

    def mark_revoked(self, order_id: str) -> Optional[Order]:
        """标记订单为已撤销"""
        return self.update_order_status(order_id, status="revoked")

    def mark_expired(self, order_id: str) -> Optional[Order]:
        """标记订单为已过期"""
        return self.update_order_status(order_id, status="expired")

    # ══════════════════════════════════════════════════
    # 撤单
    # ══════════════════════════════════════════════════

    def cancel_order(self, order_id: str) -> bool:
        """撤单 — 更新本地状态为 revoked"""
        order = self._orders.get(order_id)
        if order is None:
            return False

        if order.is_done():
            return False  # 已终态，不能撤

        self.mark_revoked(order_id)
        return True

    def cancel_all_pending(self) -> List[str]:
        """撤销所有 pending 订单"""
        cancelled = []
        for order_id, order in list(self._orders.items()):
            if order.status == "pending":
                if self.cancel_order(order_id):
                    cancelled.append(order_id)
        return cancelled

    # ══════════════════════════════════════════════════
    # 超时检查
    # ══════════════════════════════════════════════════

    def check_timeout(self) -> List[Order]:
        """检查超时订单，自动标记为 expired

        返回:
          List[Order]: 超时订单列表
        """
        now = datetime.now()
        expired_orders = []

        for order_id, order in list(self._orders.items()):
            if not order.is_active():
                continue
            if not order.expires_at:
                continue

            try:
                expires = datetime.strptime(order.expires_at, "%Y-%m-%d %H:%M:%S")
                if now >= expires:
                    self.mark_expired(order_id)
                    expired_orders.append(order)
                    print(f"  [ORDER_TIMEOUT] {order_id} 超时自动撤单")
            except ValueError:
                pass

        return expired_orders

    # ══════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════

    def query_order(self, order_id: str) -> Optional[Order]:
        """查询单个订单"""
        if order_id in self._orders:
            return self._orders[order_id]
        return self._load_order(order_id)

    def query_orders(
        self,
        code: Optional[str] = None,
        status: Optional[str] = None,
        date: Optional[str] = None,
    ) -> List[Order]:
        """查询订单（支持按代码/状态/日期过滤）"""
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        results = []
        for order_id, order in self._orders.items():
            if code and order.code != code:
                continue
            if status and order.status != status:
                continue
            # order_id 格式: YYYYMMDD_code_...
            if date and not order_id.startswith(date):
                continue
            results.append(order)

        return sorted(results, key=lambda o: o.created_at)

    def get_active_orders(self) -> List[Order]:
        """获取所有活跃（未终态）订单"""
        return [o for o in self._orders.values() if o.is_active()]

    def get_filled_codes(self) -> set:
        """获取已成交/已挂单 (code, action, direction) 三元组，用于去重"""
        filled = set()
        for order in self._orders.values():
            if order.status in ("filled", "pending", "failed"):
                filled.add((order.code, order.action, order.direction))
        return filled

    def query_orders_by_account(self, account: str) -> List[Order]:
        """按子账户过滤订单

        参数:
          account: "main" 或 "t0"

        返回:
          List[Order]: 属于该子账户的订单列表
        """
        return sorted(
            [o for o in self._orders.values() if o.account == account],
            key=lambda o: o.created_at,
        )

    # ══════════════════════════════════════════════════
    # intent 文件管理
    # ══════════════════════════════════════════════════

    def write_intent(self, code: str, action: str, direction: str, mode: str = "") -> str:
        """写 intent 文件防止跨 cron 重复"""
        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"{today}_{code}_{action}.json"
        filepath = os.path.join(self.intent_dir, filename)

        intent_data = {
            "date": today,
            "code": code,
            "action": action,
            "direction": direction,
            "mode": mode,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        with open(filepath, "w") as f:
            json.dump(intent_data, f, ensure_ascii=False, indent=2)

        return filepath

    def load_intent_files(self) -> Dict[str, Dict]:
        """加载所有 intent 文件"""
        intents = {}
        if not os.path.exists(self.intent_dir):
            return intents

        for fname in os.listdir(self.intent_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(self.intent_dir, fname)
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                intents[fname] = data
            except Exception:
                pass

        return intents

    def cleanup_intent_files(self) -> int:
        """清理当天 intent 文件（收盘后调用）"""
        today = datetime.now().strftime("%Y-%m-%d")
        cleaned = 0
        if not os.path.exists(self.intent_dir):
            return cleaned

        for fname in os.listdir(self.intent_dir):
            if fname.startswith(today) and fname.endswith(".json"):
                fpath = os.path.join(self.intent_dir, fname)
                try:
                    os.remove(fpath)
                    cleaned += 1
                except OSError:
                    pass

        return cleaned

    def cleanup_old_intent_files(self, keep_days: int = 3) -> int:
        """清理N天前的 intent 文件"""
        cutoff = datetime.now() - timedelta(days=keep_days)
        cleaned = 0
        if not os.path.exists(self.intent_dir):
            return cleaned

        for fname in os.listdir(self.intent_dir):
            if not fname.endswith(".json"):
                continue
            try:
                date_str = fname[:10]  # YYYY-MM-DD
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    fpath = os.path.join(self.intent_dir, fname)
                    os.remove(fpath)
                    cleaned += 1
            except (ValueError, OSError):
                pass

        return cleaned

    # ══════════════════════════════════════════════════
    # 持久化
    # ══════════════════════════════════════════════════

    def _order_filepath(self, order: Order) -> str:
        """生成订单文件路径 — Q6: 文件名含 order_id 避免覆盖"""
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(
            self.orders_dir, f"{today}_{order.code}_{order.action}_{order.order_id}.json"
        )

    def _persist_order(self, order: Order) -> None:
        """持久化订单到文件"""
        filepath = self._order_filepath(order)
        try:
            with open(filepath, "w") as f:
                json.dump(order.to_dict(), f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"  [ORDER_MGR] ⚠️ 保存订单失败: {e}")

    def _load_order(self, order_id: str) -> Optional[Order]:
        """从磁盘加载单个订单"""
        # 遍历 orders 目录查找匹配的订单
        today = order_id[:8]
        for fname in os.listdir(self.orders_dir):
            if not fname.startswith(today) or not fname.endswith(".json"):
                continue
            fpath = os.path.join(self.orders_dir, fname)
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                if data.get("order_id") == order_id:
                    return Order(**data)
            except Exception:
                pass
        return None

    def _load_existing_orders(self) -> None:
        """加载 orders/ 目录中已有的订单文件"""
        if not os.path.exists(self.orders_dir):
            return

        for fname in os.listdir(self.orders_dir):
            if not fname.endswith(".json") or fname.startswith("intent"):
                continue
            fpath = os.path.join(self.orders_dir, fname)
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                order_id = data.get("order_id")
                if not order_id:
                    continue
                # 跳过 intent 目录
                if "intent" in fpath:
                    continue
                order = Order(**data)
                self._orders[order_id] = order
            except Exception:
                pass

    def get_orders_count(self) -> int:
        """获取订单总数"""
        return len(self._orders)


# ═══════════════════════════════════════════════════════
# 模块级快捷函数（向后兼容 state_center 调用）
# ═══════════════════════════════════════════════════════

_DEFAULT_OM = None


def _get_om() -> OrderManager:
    global _DEFAULT_OM
    if _DEFAULT_OM is None:
        _DEFAULT_OM = OrderManager()
    return _DEFAULT_OM


def load_today_orders(date_str: Optional[str] = None):
    """向后兼容: 加载今日订单（返回 {order_id: raw_dict}）"""
    om = _get_om()
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    orders = om.query_orders(date=date_str)
    return {o.order_id: o.to_dict() for o in orders}


def load_intent_files():
    """向后兼容: 加载 intent 文件"""
    return _get_om().load_intent_files()


def write_intent(code: str, action: str, direction: str, mode: str = ""):
    """向后兼容: 写 intent 文件"""
    return _get_om().write_intent(code, action, direction, mode)


def cleanup_intent_files():
    """向后兼容: 清理 intent 文件"""
    return _get_om().cleanup_intent_files()


def cleanup_old_intent_files(keep_days: int = 3):
    """向后兼容: 清理旧 intent 文件"""
    return _get_om().cleanup_old_intent_files(keep_days)


def check_pending_orders(mode: Optional[str] = None, sync_entrust: bool = False):
    """向后兼容: 检查 pending 订单

    返回: (pending_map: Dict, filled_codes: set)
    """
    om = _get_om()
    pending = {}
    filled = set()

    for order in om._orders.values():
        if order.status == "pending":
            pending[order.order_id] = order.to_dict()
        if order.status in ("filled", "pending", "failed"):
            filled.add((order.code, order.action, order.direction))

    return pending, filled