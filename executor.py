#!/usr/bin/env python3
"""
executor.py — 交易执行器

提供统一的交易执行接口，支持回测和实盘两种模式。

架构:
  BaseBroker (抽象类)
  ├── SimBroker (回测用 — 模拟成交)
  └── RealBroker (实盘用 — 封装 EvolvingSim)

通过 config.yaml 的 broker_type 选择:
  broker_type: "sim"  → SimBroker
  broker_type: "real" → RealBroker

用法:
  from executor import get_broker
  broker = get_broker()  # 自动根据 config 选择
  result = broker.execute(order)

Phase 4: 从 trade.py 提取抽象层，为回测和实盘提供统一接口。
"""

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from order_manager import Order


# ═══════════════════════════════════════════════════════
# 执行结果
# ═══════════════════════════════════════════════════════

class ExecutionResult:
    """执行结果"""

    def __init__(
        self,
        success: bool,
        order: Order,
        broker_order_id: str = "",
        filled_shares: int = 0,
        avg_fill_price: float = 0.0,
        error_message: str = "",
        error_type: Optional[str] = None,
    ):
        self.success = success
        self.order = order
        self.broker_order_id = broker_order_id
        self.filled_shares = filled_shares if filled_shares > 0 else order.shares
        self.avg_fill_price = avg_fill_price if avg_fill_price > 0 else order.price
        self.error_message = error_message
        self.error_type = error_type  # 'timeout', 'disconnect', 'not_filled', 'other'

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "order_id": self.order.order_id,
            "code": self.order.code,
            "action": self.order.action,
            "shares": self.filled_shares,
            "price": self.avg_fill_price,
            "broker_order_id": self.broker_order_id,
            "error_message": self.error_message,
            "error_type": self.error_type,
        }


# ═══════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════

class BaseBroker(ABC):
    """交易执行器抽象基类"""

    @abstractmethod
    def execute(self, order: Order) -> ExecutionResult:
        """执行订单"""
        ...

    @abstractmethod
    def cancel(self, order: Order) -> bool:
        """撤销订单"""
        ...

    @abstractmethod
    def query(self, order: Order) -> Optional[Dict]:
        """查询订单状态"""
        ...

    @abstractmethod
    def get_positions(self) -> Dict[str, Dict]:
        """获取持仓"""
        ...

    @abstractmethod
    def get_account(self) -> Dict:
        """获取账户信息"""
        ...

    @abstractmethod
    def cancel_all(self) -> bool:
        """全撤所有未成交委托"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        ...


# ═══════════════════════════════════════════════════════
# 回测执行器
# ═══════════════════════════════════════════════════════

class SimBroker(BaseBroker):
    """回测执行器 — 模拟成交

    用于回测环境，不产生真实交易。
    默认以次日开盘价成交（可配置为次日收盘价或当日收盘价）。
    """

    def __init__(
        self,
        initial_cash: float = 220000,
        fee: float = 5.0,
        slippage_pct: float = 0.001,
        fill_mode: str = "next_open",  # 'next_open', 'next_close', 'same_close'
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fee = fee
        self.slippage_pct = slippage_pct
        self.fill_mode = fill_mode

        # 持仓: {code: {shares, avg_cost, ...}}
        self._positions: Dict[str, Dict] = {}

        # 订单历史
        self._orders: Dict[str, Order] = {}
        self._execution_history: list = []

        # 待成交队列
        self._pending: Dict[str, Order] = {}

    def execute(self, order: Order) -> ExecutionResult:
        """模拟执行订单 — 以当前价格成交"""
        fill_price = order.price

        # 计算费用
        cost = abs(order.shares) * fill_price * self.slippage_pct + self.fee

        if order.is_buy():
            # 检查资金
            required = order.shares * fill_price + cost
            if required > self.cash:
                return ExecutionResult(
                    success=False,
                    order=order,
                    error_message=f"资金不足: 需要¥{required:.0f}, 可用¥{self.cash:.0f}",
                    error_type="not_filled",
                )
            self.cash -= required
            self._update_position(order.code, order.shares, fill_price)
        elif order.is_sell():
            # 检查持仓
            pos = self._positions.get(order.code, {})
            if pos.get("shares", 0) < order.shares:
                return ExecutionResult(
                    success=False,
                    order=order,
                    error_message=f"持仓不足: 需要{order.shares}股, 持有{pos.get('shares', 0)}股",
                    error_type="not_filled",
                )
            self.cash += order.shares * fill_price - cost
            self._update_position(order.code, -order.shares, fill_price)

        self._orders[order.order_id] = order
        result = ExecutionResult(
            success=True,
            order=order,
            filled_shares=order.shares,
            avg_fill_price=fill_price,
        )
        self._execution_history.append(result)
        return result

    def _update_position(self, code: str, delta_shares: int, price: float):
        """更新模拟持仓"""
        if code not in self._positions:
            self._positions[code] = {"shares": 0, "avg_cost": 0.0, "total_cost": 0.0}

        pos = self._positions[code]
        old_shares = pos["shares"]
        new_shares = old_shares + delta_shares

        if new_shares <= 0:
            self._positions[code] = {"shares": 0, "avg_cost": 0.0, "total_cost": 0.0}
        elif delta_shares > 0:
            # 买入：更新均价
            new_total_cost = pos["total_cost"] + delta_shares * price
            pos["shares"] = new_shares
            pos["total_cost"] = new_total_cost
            pos["avg_cost"] = new_total_cost / new_shares
        else:
            # 卖出：均价不变
            pos["shares"] = new_shares
            pos["total_cost"] = pos["avg_cost"] * new_shares

    def cancel(self, order: Order) -> bool:
        """撤销订单（回测中直接标记为已撤销）"""
        if order.order_id in self._pending:
            del self._pending[order.order_id]
            return True
        return False

    def query(self, order: Order) -> Optional[Dict]:
        """查询订单状态"""
        if order.order_id in self._orders:
            return {"status": "filled", "filled_shares": order.shares}
        return None

    def get_positions(self) -> Dict[str, Dict]:
        """获取持仓"""
        return dict(self._positions)

    def get_account(self) -> Dict:
        """获取账户信息"""
        mv = sum(
            p["shares"] * p["avg_cost"]
            for p in self._positions.values()
        )
        return {
            "total_asset": self.cash + mv,
            "cash": self.cash,
            "market_value": mv,
        }

    def cancel_all(self) -> bool:
        """全撤"""
        self._pending.clear()
        return True

    def is_connected(self) -> bool:
        return True

    def get_execution_history(self) -> list:
        return self._execution_history


# ═══════════════════════════════════════════════════════
# 实盘执行器
# ═══════════════════════════════════════════════════════

class RealBroker(BaseBroker):
    """实盘执行器 — 封装 EvolvingSim 真实下单

    遵循 trade.py 的铁律:
      1. 只用 EvolvingSim 公开接口
      2. 数量 100 整数倍
      3. 调用前激活同花顺窗口
      4. 不信任返回值，成交确认交给下次同步
    """

    def __init__(self):
        self._evolving = None
        self._evolving_module = None

    def _get_evolving(self):
        """懒加载 EvolvingSim"""
        if self._evolving_module is None:
            try:
                from evolving.evolving import EvolvingSim
                self._evolving_module = EvolvingSim
            except ImportError:
                raise RuntimeError("EvolvingSim 不可用，请确认环境配置")
        return self._evolving_module()

    def _call_evolving(self, method_name: str, *args) -> Any:
        """调用 EvolvingSim 方法"""
        e = self._get_evolving()
        try:
            method = getattr(e, method_name)
            return method(*args)
        except Exception as ex:
            print(f"  ⚠️ EvolvingSim.{method_name} 异常: {ex}")
            return None
        finally:
            time.sleep(2)

    def execute(self, order: Order) -> ExecutionResult:
        """执行真实订单"""
        # 激活同花顺
        self._ensure_tonghuashun_active()

        # 校验数量
        if order.shares % 100 != 0:
            return ExecutionResult(
                success=False,
                order=order,
                error_message=f"数量{order.shares}不是100的倍数",
                error_type="other",
            )

        # 下单
        try:
            if order.action == "buy":
                result = self._call_evolving("buy", order.code, order.shares, order.price)
            elif order.action == "sell":
                result = self._call_evolving("sell", order.code, order.shares, order.price)
            elif order.action == "t0_buy":
                result = self._call_evolving("buy", order.code, order.shares, order.price)
                # 配对卖出挂单
                if order.pair_price > 0:
                    time.sleep(1)
                    self._call_evolving("sell", order.code, order.shares, order.pair_price)
            elif order.action == "t0_sell":
                result = self._call_evolving("sell", order.code, order.shares, order.price)
                # 配对买入挂单
                if order.pair_price > 0:
                    time.sleep(1)
                    self._call_evolving("buy", order.code, order.shares, order.pair_price)
            else:
                return ExecutionResult(
                    success=False,
                    order=order,
                    error_message=f"未知操作: {order.action}",
                    error_type="other",
                )

            # EvolvingSim 返回值不可信，但非 None 通常表示下单请求已发送
            if result is None:
                return ExecutionResult(
                    success=False,
                    order=order,
                    error_message="EvolvingSim 返回 None",
                    error_type="tonghuashun_disconnect",
                )

            return ExecutionResult(
                success=True,
                order=order,
                filled_shares=0,  # 成交确认交给下次同步
                avg_fill_price=0.0,
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                order=order,
                error_message=str(e),
                error_type="exception",
            )

    def cancel(self, order: Order) -> bool:
        """撤单"""
        self._ensure_tonghuashun_active()
        try:
            result = self._call_evolving("revokeEntrust", revokeType="contractNo", contractNo=order.broker_order_id)
            return result is not None
        except Exception:
            return False

    def cancel_all(self) -> bool:
        """全撤所有买卖委托"""
        self._ensure_tonghuashun_active()
        try:
            result = self._call_evolving("revokeEntrust", revokeType="allBuyAndSell")
            return result is not None
        except Exception:
            return False

    def query(self, order: Order) -> Optional[Dict]:
        """查询订单状态（通过 getEntrust）"""
        self._ensure_tonghuashun_active()
        try:
            result = self._call_evolving("getEntrust", "today", False)
            if result and isinstance(result, dict) and result.get("status"):
                for row in result.get("data", []):
                    if not row:
                        continue
                    # 按 code + direction + shares 匹配
                    if len(row) >= 10:
                        entrust_code = str(row[0]) if row[0] else ""
                        entrust_dir = str(row[4]) if len(row) > 4 else ""
                        entrust_shares = int(row[5]) if len(row) > 5 and row[5] else 0
                        entrust_status = str(row[8]) if len(row) > 8 else ""
                        entrust_price = float(row[7]) if len(row) > 7 and row[7] else 0.0

                        if entrust_code == order.code and entrust_shares == order.shares:
                            return {
                                "status": entrust_status,
                                "filled_shares": entrust_shares if "已成" in entrust_status else 0,
                                "avg_fill_price": entrust_price,
                            }
            return None
        except Exception:
            return None

    def get_positions(self) -> Dict[str, Dict]:
        """获取持仓（通过 getHoldingShares）"""
        self._ensure_tonghuashun_active()
        try:
            h = self._call_evolving("getHoldingShares")
            if not h or not isinstance(h, dict) or not h.get("status"):
                return {}

            positions = {}
            from state_center import get_code_map
            code_map = get_code_map()

            for row in h.get("data", []):
                if not row or len(row) < 12:
                    continue
                code = row[0]
                try:
                    shares = int(row[6])
                except (ValueError, TypeError):
                    shares = 0
                try:
                    cost = float(row[10])
                except (ValueError, TypeError):
                    cost = 0.0

                if shares > 0:
                    positions[code] = {
                        "name": code_map.get(code, {}).get("name", ""),
                        "shares": shares,
                        "avg_cost": cost,
                        "current_price": float(row[2]),
                    }
            return positions
        except Exception:
            return {}

    def get_account(self) -> Dict:
        """获取账户信息"""
        self._ensure_tonghuashun_active()
        try:
            acct = self._call_evolving("getAccountInfo")
            if acct and isinstance(acct, dict) and acct.get("status"):
                data = acct.get("data", [])
                if data and len(data) > 0:
                    row = data[0]
                    return {
                        "total_asset": float(row[0]) if len(row) > 0 else 0,
                        "cash": float(row[1]) if len(row) > 1 else 0,
                        "market_value": float(row[2]) if len(row) > 2 else 0,
                    }
            return {"total_asset": 0, "cash": 0, "market_value": 0}
        except Exception:
            return {"total_asset": 0, "cash": 0, "market_value": 0}

    def is_connected(self) -> bool:
        """检查同花顺连接状态"""
        try:
            result = self._call_evolving("getAccountInfo")
            return result is not None and isinstance(result, dict)
        except Exception:
            return False

    @staticmethod
    def _ensure_tonghuashun_active():
        """确保同花顺窗口在前台"""
        try:
            os.system("open -a 同花顺")
            time.sleep(1)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════

def _load_config() -> Dict:
    """加载 config.yaml"""
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    try:
        import yaml
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # 没有 PyYAML，尝试手动解析简单配置
        try:
            with open(config_path, "r") as f:
                content = f.read()
            # 简单的 key: value 解析
            config = {}
            for line in content.split("\n"):
                if ":" in line and not line.strip().startswith("#"):
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    config[key] = val
            return config
        except Exception:
            return {}


def get_broker() -> BaseBroker:
    """工厂函数: 根据 config.yaml 选择 broker

    broker_type: "sim"  → SimBroker (回测)
    broker_type: "real" → RealBroker (实盘)
    默认: SimBroker
    """
    config = _load_config()
    broker_type = config.get("broker_type", "sim")

    if broker_type == "real":
        return RealBroker()
    else:
        init_cash = float(config.get("total_fund", 220000))
        return SimBroker(initial_cash=init_cash)


# ═══════════════════════════════════════════════════════
# 模块级实例
# ═══════════════════════════════════════════════════════

_broker: Optional[BaseBroker] = None


def get_default_broker() -> BaseBroker:
    """获取默认 broker 实例（单例）"""
    global _broker
    if _broker is None:
        _broker = get_broker()
    return _broker


def execute_order(order: Order) -> ExecutionResult:
    """快捷函数: 执行订单"""
    return get_default_broker().execute(order)


def cancel_all_orders() -> bool:
    """快捷函数: 全撤"""
    return get_default_broker().cancel_all()