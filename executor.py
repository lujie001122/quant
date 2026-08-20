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


# ═══════════════════════════════════════════════════════
# 信号执行器 (P2-9: 从 signal_generator.py 迁移)
# ═══════════════════════════════════════════════════════

class SignalExecutor:
    """信号执行器 — 遍历信号并执行交易。

    从 signal_generator.py 的 execute_signals 函数迁移而来。
    与 executor.py 的 Broker 层分离: Broker 负责单笔订单执行，
    SignalExecutor 负责信号解析、去重、重试、意图文件等完整流程。

    P2-9 迁移: 原来 signal_generator.py:586-1005 (~420行)
    """

    def __init__(self):
        """延迟导入避免循环依赖"""
        self._imports_loaded = False

    def _ensure_imports(self):
        """加载 signal_generator 依赖"""
        if self._imports_loaded:
            return
        import subprocess as _subprocess

        from market_data import fetch_realtime_quotes, ETFS
        from position_info import PositionInfo, TOTAL_FUND
        from strategies.rsi_macd import _parse_position_ratio, _get_position_shares
        from state_center import (
            get_position_shares,
            check_pending_orders,
            trade_type_to_mode,
            get_signal_direction,
            signal_direction,
            detect_error_type,
            # build_retry_cmd,  # [注释] retry_cmd 不再使用
            cleanup_intent_files,
            cleanup_old_intent_files,
        )

        self._subprocess = _subprocess
        self._fetch_realtime_quotes = fetch_realtime_quotes
        self._ETFS = ETFS
        self._PositionInfo = PositionInfo
        self._TOTAL_FUND = TOTAL_FUND
        self._parse_position_ratio = _parse_position_ratio
        self._get_position_shares = _get_position_shares
        self._check_pending_orders = check_pending_orders
        self._trade_type_to_mode = trade_type_to_mode
        self._get_signal_direction = get_signal_direction
        self._signal_direction = signal_direction
        self._detect_error_type = detect_error_type
        # self._build_retry_cmd = build_retry_cmd  # [注释] retry_cmd 不再使用
        self._cleanup_intent_files = cleanup_intent_files
        self._cleanup_old_intent_files = cleanup_old_intent_files
        self._imports_loaded = True

    def _write_intent_for_sg(self, code, mode, direction):
        """写 intent 文件（兜底，trade.py 内部已写）"""
        if mode == 't0':
            action = 't0_buy' if direction == '买入' else 't0_sell'
        else:
            action = 'buy' if direction == '买入' else 'sell'
        from state_center import write_intent
        write_intent(code, action, direction, mode=mode)

    def execute_signals(self, result, mode=None):
        """遍历信号，调用 trade.py 执行交易（含异常处理+状态持久化+告警推送）

        mode: None=全部, 'buy_sell'=买卖信号+做T信号都执行(互不干扰), 't0'=只执行做T

        --execute 下单逻辑（v3.1.2）：
        - 买入限价 = min(信号价, 当前实时价)，确保 ≤ 卖一价能挂进去
        - 卖出限价 = max(信号价, 当前实时价)，确保 ≥ 买一价能挂进去
        - 做T配对价 = 当前实时价 ± ATR×1.1（重新计算，不跟信号价比较）
        """
        self._ensure_imports()

        if result is None:
            print("[EXECUTE] 无信号结果，跳过执行")
            return

        signals = result.get("signals", {})
        if not signals:
            print("[EXECUTE] 无信号数据，跳过执行")
            return

        if mode:
            print(f"[EXECUTE] 模式过滤: {mode}")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        trade_script = os.path.join(script_dir, "trade.py")
        today_str = datetime.now().strftime("%Y-%m-%d")

        # ═══ 获取当前实时价 ═══
        live_quotes = {}
        try:
            live_quotes = self._fetch_realtime_quotes()
            print(f"[EXECUTE] 获取实时行情: {len(live_quotes)} 只ETF")
        except Exception as e:
            print(f"[EXECUTE] ⚠️ 获取实时行情失败: {e}，将使用信号价作为兜底")

        # 按模式过滤信号
        BUY_SELL_TYPES = {"buy", "sell", "liquidate", "reduce"}
        T0_TYPES = {"t0"}

        actionable = []
        for code, sig in signals.items():
            trade_type = sig.get("trade_type")
            action = sig.get("action", "")
            # 兜底：从 t0_signal 字段推导 trade_type 和 action
            t0_sig = sig.get("t0_signal", "")
            if t0_sig and t0_sig not in ("无", "无(非交易时段)", "无(集合竞价时段)"):
                if "买入" in t0_sig or "卖出" in t0_sig:
                    if trade_type is None or trade_type == "buy":
                        trade_type = "t0"
                        if "买入" in t0_sig:
                            action = "买入"
                        elif "卖出" in t0_sig:
                            action = "卖出"
                        sig["trade_type"] = trade_type
                        sig["action"] = action
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

        if not actionable:
            print("[EXECUTE] 无交易信号，跳过执行")
            return

        print(f"[EXECUTE] 共 {len(actionable)} 个交易信号，开始执行...")
        print("=" * 60)

        # ═══ 全撤 ═══
        print("[EXECUTE] 全撤所有未成交委托...")
        try:
            revoke_proc = self._subprocess.run(
                [sys.executable or "python3", trade_script, "revoke_all"],
                capture_output=True, text=True, timeout=30, cwd=script_dir
            )
            if revoke_proc.returncode != 0:
                print(f"  ⚠️ revoke_all 返回码非零({revoke_proc.returncode})，继续执行...")
                if revoke_proc.stdout:
                    print(f"  {revoke_proc.stdout.strip()}")
                if revoke_proc.stderr:
                    print(f"  [stderr] {revoke_proc.stderr.strip()}")
            else:
                if revoke_proc.stdout:
                    for line in revoke_proc.stdout.strip().split("\n"):
                        print(f"  {line}")
        except self._subprocess.TimeoutExpired:
            print(f"  ⚠️ revoke_all 超时，继续执行...")
        except Exception as e:
            print(f"  ⚠️ revoke_all 异常: {e}，继续执行...")

        # ═══ 撤单后重新收集 filled_codes ═══
        pending_map, filled_codes = self._check_pending_orders(mode, sync_entrust=False)
        if filled_codes:
            print(f"[EXECUTE] 去重: {len(filled_codes)} 笔已成交/已挂单，跳过重复信号")
        if pending_map:
            print(f"[EXECUTE] 当前 pending 挂单: {len(pending_map)} 笔")

        success_count = 0
        fail_count = 0
        skip_count = 0

        for i, (code, sig) in enumerate(actionable):
            trade_type = sig["trade_type"]
            action = sig.get("action", "")

            # 去重检查
            sig_mode = self._trade_type_to_mode(trade_type)
            sig_direction = self._get_signal_direction(trade_type, action)
            if (code, sig_mode, sig_direction) in filled_codes:
                print(f"  ⏭️ {code} mode={sig_mode} dir={sig_direction} 今日已成交，跳过")
                skip_count += 1
                continue

            # 获取当前实时价
            signal_price_str = sig.get("price", "0")
            try:
                signal_price = float(signal_price_str)
            except (ValueError, TypeError):
                signal_price = 0.0

            live = live_quotes.get(code, {})
            live_price = live.get("price", 0.0) if live else 0.0

            # 下单限价方向修正
            is_buy_direction = (
                trade_type == "buy"
                or (trade_type == "t0" and "买入" in action)
            )
            is_sell_direction = (
                trade_type in ("sell", "liquidate", "reduce")
                or (trade_type == "t0" and "卖出" in action)
            )

            if live_price > 0:
                if is_buy_direction:
                    if signal_price > 0 and live_price < signal_price:
                        price = live_price
                        print(f"[EXECUTE] {code} 买入限价修正: 信号价={signal_price:.3f} > 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                    else:
                        price = signal_price if signal_price > 0 else live_price
                        print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 买入限价={price:.3f}")
                elif is_sell_direction:
                    if signal_price > 0 and live_price > signal_price:
                        price = live_price
                        print(f"[EXECUTE] {code} 卖出限价修正: 信号价={signal_price:.3f} < 当前价={live_price:.3f}, 使用当前价={price:.3f}")
                    else:
                        price = signal_price if signal_price > 0 else live_price
                        print(f"[EXECUTE] {code} 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 卖出限价={price:.3f}")
                else:
                    price = signal_price if signal_price > 0 else live_price
                    print(f"[EXECUTE] {code} 未知方向(trade_type={trade_type}), 信号价={signal_price:.3f}, 当前价={live_price:.3f}, 兜底限价={price:.3f}")
            else:
                price = signal_price
                print(f"[EXECUTE] {code} 实时价获取失败，兜底用信号价={signal_price:.3f}")

            if price <= 0:
                print(f"[EXECUTE] ⚠️ {code} 价格无效(信号={signal_price_str}, 实时={live_price})，跳过")
                fail_count += 1
                continue

            t0_pair = sig.get("t0_pair")

            # ═══ 信号解析 + 构建命令 ═══
            cmd = None
            label = None
            shares = 0

            # ── 做T信号 ──
            if trade_type == "t0" and t0_pair:
                pair_shares = t0_pair.get("shares", 0)
                sig_atr = sig.get("atr_5min") or sig.get("atr", 0)
                try:
                    sig_atr = float(sig_atr)
                except (ValueError, TypeError):
                    sig_atr = 0.0
                old_pair_price = t0_pair.get("pair_price", 0)

                # P2-10: 使用 money_manager 统一计算做T配对价（固定+230价差）
                from money_manager import calc_t0_pair_price
                if "买入" in action:
                    pair_price = calc_t0_pair_price(price, pair_shares, True) if pair_shares >= 100 else old_pair_price
                elif "卖出" in action:
                    pair_price = calc_t0_pair_price(price, pair_shares, False) if pair_shares >= 100 else old_pair_price
                else:
                    pair_price = old_pair_price

                if pair_shares >= 100:
                    print(f"[EXECUTE] {code} 做T配对价: 固定价差=230元, 当前价={price:.3f}, 股数={pair_shares}, 配对价={pair_price:.3f}")
                shares = pair_shares

                if pair_shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 做T股数不足({pair_shares})，跳过")
                    fail_count += 1
                    continue

                if "买入" in action:
                    cmd = ["python3", trade_script, "t0_buy",
                           code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                    label = f"做T买入 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
                elif "卖出" in action:
                    cmd = ["python3", trade_script, "t0_sell",
                           code, str(pair_shares), f"{price:.3f}", f"{pair_price:.3f}"]
                    label = f"做T卖出 {code} {pair_shares}股 @{price:.3f} (配对@{pair_price:.3f})"
                else:
                    print(f"[EXECUTE] ⚠️ {code} 做T方向未知(action={action})，跳过")
                    fail_count += 1
                    continue

            # ── 做T信号(无配对挂单，降级为普通买卖) ──
            elif trade_type == "t0" and not t0_pair:
                current_shares = self._get_position_shares(code)
                if current_shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 做T但无持仓({current_shares}股)，跳过")
                    fail_count += 1
                    continue
                ratio = 0.30
                shares = int(current_shares * ratio / 100) * 100
                if shares < 100:
                    shares = 100
                if "买入" in action:
                    cmd = ["python3", trade_script, "buy", code, str(shares), f"{price:.3f}"]
                    label = f"做T买入(无配对) {code} {shares}股 @{price:.3f}"
                elif "卖出" in action:
                    cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
                    label = f"做T卖出(无配对) {code} {shares}股 @{price:.3f}"
                else:
                    print(f"[EXECUTE] ⚠️ {code} 做T方向未知(action={action})，跳过")
                    fail_count += 1
                    continue

            # ── 普通买入 ──
            elif trade_type == "buy":
                ratio = self._parse_position_ratio(sig.get("position_ratio", ""))
                fund = self._ETFS.get(code, {}).get("fund", 44000)
                shares = int(fund * ratio / price / 100) * 100
                if shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 买入股数不足(fund={fund}, ratio={ratio:.0%}, shares={shares})，跳过")
                    fail_count += 1
                    continue
                cmd = ["python3", trade_script, "buy", code, str(shares), f"{price:.3f}"]
                label = f"买入 {code} {shares}股 @{price:.3f}"

            # ── 普通卖出 ──
            elif trade_type == "sell":
                current_shares = self._get_position_shares(code)
                ratio = self._parse_position_ratio(sig.get("position_ratio", ""))
                if ratio > 0:
                    shares = int(current_shares * ratio / 100) * 100
                else:
                    shares = current_shares
                if shares < 100 or shares > current_shares:
                    shares = current_shares
                if shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 卖出股数不足(current={current_shares}, shares={shares})，跳过")
                    fail_count += 1
                    continue
                cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
                label = f"卖出 {code} {shares}股 @{price:.3f}"

            # ── 清仓 ──
            elif trade_type == "liquidate":
                current_shares = self._get_position_shares(code)
                shares = current_shares
                if current_shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 清仓但无持仓({current_shares}股)，跳过")
                    fail_count += 1
                    continue
                cmd = ["python3", trade_script, "sell", code, str(current_shares), f"{price:.3f}"]
                label = f"清仓 {code} {current_shares}股 @{price:.3f}"

            # ── 减仓 ──
            elif trade_type == "reduce":
                current_shares = self._get_position_shares(code)
                ratio = self._parse_position_ratio(sig.get("position_ratio", ""))
                if ratio > 0:
                    shares = int(current_shares * ratio / 100) * 100
                else:
                    shares = int(current_shares * 0.30 / 100) * 100
                if shares < 100 or shares > current_shares:
                    shares = min(current_shares, max(shares, 100))
                if shares < 100 or current_shares < 100:
                    print(f"[EXECUTE] ⚠️ {code} 减仓股数不足(current={current_shares}, shares={shares})，跳过")
                    fail_count += 1
                    continue
                cmd = ["python3", trade_script, "sell", code, str(shares), f"{price:.3f}"]
                label = f"减仓 {code} {shares}股 @{price:.3f}"

            else:
                print(f"[EXECUTE] ⚠️ {code} 未知交易类型({trade_type})，跳过")
                fail_count += 1
                continue

            # ═══ 执行命令（含重试） ═══
            direction = self._signal_direction(sig)
            print(f"\n[{i+1}/{len(actionable)}] {label}")
            print(f"  → {' '.join(cmd)}")

            # [注释] retry_cmd 不再使用
            # retry_cmd = self._build_retry_cmd(trade_script, code, trade_type, sig, price, shares)
            max_retries = 3
            success = False
            final_error = None
            final_error_type = None
            before_shares = 0
            after_shares = 0

            os.system("open -a 同花顺")
            os.system("cliclick c:960,50")

            for attempt in range(1, max_retries + 1):
                try:
                    proc = self._subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=120, cwd=script_dir
                    )
                    stdout = proc.stdout or ""
                    stderr_out = proc.stderr or ""

                    err_type, err_detail = self._detect_error_type(stdout, stderr_out, proc.returncode)

                    if err_type is None and proc.returncode == 0:
                        if stdout:
                            for line in stdout.strip().split("\n"):
                                if "持仓从" in line and "→" in line:
                                    try:
                                        parts = line.split("持仓从")[1].split("→")
                                        before_shares = int(parts[0].strip())
                                        after_shares = int(parts[1].split("股")[0].strip())
                                    except (ValueError, IndexError):
                                        pass
                                    break
                            print(stdout.strip())
                        if stderr_out:
                            print(f"  [stderr] {stderr_out.strip()}")
                        print(f"  ✅ {code} {direction} {shares}股@{price:.3f} → 持仓{before_shares}→{after_shares}股")
                        success_count += 1
                        success = True

                        # ═══ 添加到 filled_codes ═══
                        filled_codes.add((code, sig_mode, sig_direction))

                        # ═══ 同时写 intent 记录 ═══
                        self._write_intent_for_sg(code, sig_mode, sig_direction)
                        break

                    elif err_type == "tonghuashun_disconnect":
                        if attempt < max_retries:
                            print(f"  ⚠️ 同花顺断连，激活窗口后重试 ({attempt}/{max_retries})...")
                            os.system("osascript -e 'tell application \"同花顺\" to activate'")
                            time.sleep(2)
                        else:
                            if stdout:
                                print(stdout.strip())
                            if stderr_out:
                                print(f"  [stderr] {stderr_out.strip()}")
                            alert_msg = f"[ALERT] ❌ {code} {direction}失败: 同花顺断连(3次重试全失败)。"
                            # [注释] retry_cmd 不再使用
                            print(alert_msg, file=sys.stderr)
                            final_error = err_detail
                            final_error_type = err_type

                    elif err_type == "not_filled":
                        if stdout:
                            print(stdout.strip())
                        if stderr_out:
                            print(f"  [stderr] {stderr_out.strip()}")
                        alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单未成交(增量确认失败)。"
                        # [注释] retry_cmd 不再使用
                        print(alert_msg, file=sys.stderr)
                        final_error = err_detail
                        final_error_type = err_type
                        break

                    else:
                        if stdout:
                            print(stdout.strip())
                        if stderr_out:
                            print(f"  [stderr] {stderr_out.strip()}")
                        print(f"  ❌ 失败 (exit={proc.returncode})")
                        final_error = err_detail or f"退出码={proc.returncode}"
                        final_error_type = err_type or "other"
                        break

                except self._subprocess.TimeoutExpired:
                    if attempt < max_retries:
                        print(f"  ⚠️ 超时，重试 ({attempt}/{max_retries})...")
                    else:
                        alert_msg = f"[ALERT] ❌ {code} {direction}失败: 下单超时(120s×3)。"
                        # [注释] retry_cmd 不再使用
                        print(alert_msg, file=sys.stderr)
                        final_error = "超时"
                        final_error_type = "timeout"

                except Exception as e:
                    print(f"  ❌ 异常: {e}")
                    final_error = str(e)
                    final_error_type = "exception"
                    break

            if not success:
                fail_count += 1

            if i < len(actionable) - 1:
                time.sleep(3)

        print("\n" + "=" * 60)
        print(f"[EXECUTE] 执行完成: 成功 {success_count}, 失败 {fail_count}, 跳过 {skip_count}")

        # ═══ 收盘后清理 intent 文件 ═══
        self._cleanup_intent_files()
        self._cleanup_old_intent_files()


# 模块级 SignalExecutor 实例
_executor = SignalExecutor()


def execute_signals(result, mode=None):
    """遍历信号执行交易（快捷函数，向后兼容 signal_generator.execute_signals）"""
    return _executor.execute_signals(result, mode=mode)