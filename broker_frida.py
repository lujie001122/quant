#!/usr/bin/env python3
"""
broker_frida.py — 进程内数据通道 broker（同花顺 App 内部数据对象，零点击）

相比 EvolvingSim（AppleScript 模拟点击界面）：
  - 数据源是 App 内存里的持仓/委托/成交对象，不依赖界面停在哪个页、不依赖屏幕锁
  - 通过 ths_cli.py --engine frida 子进程取数，JSON 结构化返回
  - 失败时抛 ThsCliError，由调用方决定是否回退旧通道；绝不返回假数据

命令与字段来源（2026-11-12 实测）：
  positions  3 行：代码/名称/数量/可用/成本/现价/市值/市场
  orders     委托行（含合同号 2135/方向/状态/委托量/委托价/成交量/成交价）
  trades     成交明细（含合同号/成交价/成交量/成交金额）
  cancel <合同号> --real   真撤单，撤后 3s 从缓存复查状态
  account    资金：frida readAccountDirect（UserManager→activeUser→zichanData）
             失败才回退旧通道 ths_cli.py --engine apple account（见 get_account）

接口对齐：
  SimBroker.get_positions()  → {code: {shares, avg_cost, total_cost, current_price}}
  SimBroker.get_account()    → {total_asset, cash, market_value}
  RealBroker.get_positions() → {code: {name, shares, avg_cost, current_price}}
  ThsBroker.get_positions()  = 二者超集，另加 available/frozen/market_value/pnl/pnl_pct
  trade.py sync 直接消费这份结构，字段名与 portfolio.json positions 一致。

下单（buy/sell）本轮未迁移：ThsBroker.execute() 原样委托 RealBroker（EvolvingSim）。
"""

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

from executor import BaseBroker, ExecutionResult, Order

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(SCRIPT_DIR, "ths_cli.py")
VENV_PY = os.path.join(SCRIPT_DIR, ".venv/bin/python3")

# 读接口超时：缓存空时 ths_cli 会自动 prime（可能点一次面板），给足时间
READ_TIMEOUT = 120
# 资金：frida account 是纯 getter（快）；回退旧通道 EvolvingSim 时较慢
ACCT_TIMEOUT = 120


class ThsCliError(RuntimeError):
    """ths_cli 调用失败/返回非 JSON/业务 ok=false。"""


def _parse_json_output(out: str) -> Dict:
    """解析 ths_cli 的 stdout。

    cancel 命令在 --json 下仍会先打印几行人读日志（ths_cli 现有行为），
    真正的 JSON 在最后；因此纯 JSON 解析失败时从后往前找 JSON 起始行。
    """
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pass
    idx = out.find("\n{")
    while idx != -1:
        try:
            return json.loads(out[idx + 1:])
        except json.JSONDecodeError:
            idx = out.find("\n{", idx + 2)
    raise json.JSONDecodeError("no json object", out, 0)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


class ThsBroker(BaseBroker):
    """进程内接口 broker：读持仓/委托/成交 + 撤单走 frida；资金走旧通道。"""

    def __init__(self, timeout: int = READ_TIMEOUT):
        self.timeout = timeout
        self._py = VENV_PY if os.path.isfile(VENV_PY) else sys.executable

    # ── ths_cli 子进程 ────────────────────────────────────────────────

    def _cli(self, engine: str, cmd: str, *extra, timeout: int = None) -> Dict:
        """跑 ths_cli 并解析 JSON。stderr 的进度日志不进 JSON。"""
        argv = [self._py, CLI_PATH, "--json", "--engine", engine, cmd, *extra]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout or self.timeout, cwd=SCRIPT_DIR,
            )
        except subprocess.TimeoutExpired:
            raise ThsCliError(f"ths_cli {engine} {cmd} 超时({timeout or self.timeout}s)")
        except OSError as ex:
            raise ThsCliError(f"ths_cli 启动失败: {ex}")

        out = (proc.stdout or "").strip()
        if not out:
            err = (proc.stderr or "").strip().splitlines()
            raise ThsCliError(
                f"ths_cli {engine} {cmd} 无输出(exit={proc.returncode}): "
                f"{err[-1] if err else 'empty'}"
            )
        try:
            return _parse_json_output(out)
        except json.JSONDecodeError:
            raise ThsCliError(
                f"ths_cli {engine} {cmd} 非 JSON 输出(exit={proc.returncode}): {out[:200]}"
            )

    def _frida(self, cmd: str, *extra, timeout: int = None) -> Dict:
        """frida 命令：要求 ok=true，否则抛错。"""
        data = self._cli("frida", cmd, *extra, timeout=timeout)
        if not data.get("ok"):
            raise ThsCliError(
                f"frida {cmd} 失败: {data.get('error') or data.get('note') or data}"
            )
        return data

    # ── 原始数据（带 source，便于汇报数据来源）──────────────────────

    def _raw(self, cmd: str) -> Dict:
        """读缓存类命令的公共处理：prime 失败伪装成 0 行的情况要当失败抛错。"""
        data = self._frida(cmd)
        note = str(data.get("note") or "")
        if not data.get("count") and "after prime" in note:
            raise ThsCliError(f"frida {cmd} prime 后仍为空: {note}")
        data.setdefault("source", "frida")
        return data

    def positions_raw(self) -> Dict:
        return self._raw("positions")

    def orders_raw(self) -> Dict:
        return self._raw("orders")

    def trades_raw(self) -> Dict:
        return self._raw("trades")

    # ── BaseBroker 读接口 ────────────────────────────────────────────

    def get_positions(self) -> Dict[str, Dict]:
        """持仓：来源 frida positions（App 内部持仓表）。

        字段与 portfolio.json positions 对齐：
          name/shares/available/frozen/avg_cost/total_cost/current_price/
          market_value/pnl/pnl_pct/source
        frozen = 数量 - 可用（接口无独立冻结字段）。
        接口只返回持仓表里的行；已清仓标的不会出现（sync 按缺席处理）。
        """
        data = self.positions_raw()
        # 同花顺接口名称会截断（如"消费ETF汇"），按项目约定优先用 etf_pool 完整名
        try:
            from state_center import get_code_map
            code_map = get_code_map()
        except Exception:
            code_map = {}
        positions: Dict[str, Dict] = {}
        for row in data.get("rows") or []:
            code = str(row.get("代码", "")).strip()
            if not code:
                continue
            shares = _to_int(row.get("数量"))
            if shares <= 0:
                continue
            available = _to_int(row.get("可用"), shares)
            cost = _to_float(row.get("成本"))
            price = _to_float(row.get("现价"))
            mv = _to_float(row.get("市值"))
            if mv <= 0 and price > 0:
                mv = shares * price
            total_cost = cost * shares
            pnl = mv - total_cost if cost > 0 else 0.0
            name = (code_map.get(code, {}).get("name")
                    or str(row.get("名称", "")).strip())
            positions[code] = {
                "name": name,
                "shares": shares,
                "available": available,
                "frozen": max(shares - available, 0),
                "avg_cost": cost,
                "total_cost": total_cost,
                "current_price": price,
                "market_value": mv,
                "pnl": pnl,
                "pnl_pct": round(pnl / total_cost * 100, 2) if total_cost > 0 else 0.0,
                "source": "frida",
            }
        return positions

    def get_orders(self) -> List[Dict]:
        """委托列表：来源 frida orders（含合同号 2135）。

        委托表没有成交量列，成交量由状态推（与 ths_cli 一致）：
          全部成交 → 委托量；未成交/撤单 → 0；部分成交 → 0（无法确定）
        """
        data = self.orders_raw()
        orders: List[Dict] = []
        for row in data.get("rows") or []:
            code = str(row.get("代码", "")).strip()
            if not code:
                continue
            status = str(row.get("状态", "")).strip()
            shares = _to_int(row.get("委托量"))
            filled = _to_int(row.get("成交量")) if row.get("成交量", "") != "" else 0
            orders.append({
                "contract": str(row.get("合同号", "")).strip(),
                "code": code,
                "name": str(row.get("名称", "")).strip(),
                "direction": str(row.get("方向", "")).strip(),   # 买入/卖出
                "status": status,
                "shares": shares,
                "price": _to_float(row.get("委托价")),
                "filled_shares": filled,
                "avg_fill_price": _to_float(row.get("成交价")),
                "cancel_shares": _to_int(row.get("撤销量")),
                "market": str(row.get("市场", "")).strip(),
                "account": str(row.get("股东账户", "")).strip(),
                "date": str(row.get("日期", "")).strip(),
                "time": str(row.get("时间", "")).strip(),
                "source": "frida",
            })
        return orders

    def get_trades(self) -> List[Dict]:
        """成交明细：来源 frida trades（含合同号/成交价/成交量/成交金额）。"""
        data = self.trades_raw()
        trades: List[Dict] = []
        for row in data.get("rows") or []:
            code = str(row.get("代码", "")).strip()
            if not code:
                continue
            trades.append({
                "contract": str(row.get("合同号", "")).strip(),
                "code": code,
                "name": str(row.get("名称", "")).strip(),
                "direction": str(row.get("方向", "")).strip(),
                "price": _to_float(row.get("成交价")),
                "shares": _to_int(row.get("成交量")),
                "amount": _to_float(row.get("成交金额")),
                "time": str(row.get("时间", "")).strip(),
                "source": "frida",
            })
        return trades

    def get_execution_history(self) -> list:
        """委托历史：来源 frida orders（SimBroker 同名接口的实盘版）。

        每条含合同号/方向/状态/委托量/成交量，供对账与追价使用。
        """
        return self.get_orders()

    def get_account(self) -> Dict:
        """账户资金：优先 frida（ths_cli --engine frida account）。

        字段名对齐 SimBroker.get_account()：total_asset / cash / market_value
        （cash = 可用金额，market_value = 总市值）。frida 通道失败才回退旧通道
        EvolvingSim.getAccountInfo；两路都失败即抛错，由调用方（trade.py sync）
        保留上次落盘账户数据，绝不用 0 覆盖。
        """
        try:
            d = self._frida("account", timeout=ACCT_TIMEOUT)
            total = _to_float(d.get("总资产"))
            cash = _to_float(d.get("可用金额"))
            mv = _to_float(d.get("总市值"))
            if total <= 0:
                raise ThsCliError(f"frida account 总资产={total} 异常")
            return {
                "total_asset": total,
                "cash": cash,
                "market_value": mv,
                "source": "frida 接口",
            }
        except ThsCliError as ex:
            print(f"  ⚠️ frida 资金失败: {ex} → 回退旧通道 getAccountInfo")

        data = self._cli("apple", "account", timeout=ACCT_TIMEOUT)
        if not data.get("status"):
            raise ThsCliError(f"旧通道 account 无有效返回: {data}")
        d = data.get("data") or {}
        if not isinstance(d, dict) or "总资产" not in d:
            raise ThsCliError(f"旧通道 account 字段异常: {d}")
        total = _to_float(d.get("总资产"))
        cash = _to_float(d.get("可用金额"))
        mv = _to_float(d.get("总市值"))
        if total <= 0:
            raise ThsCliError(f"旧通道 account 总资产={total} 异常")
        return {
            "total_asset": total,
            "cash": cash,
            "market_value": mv,
            "source": "旧通道 EvolvingSim.getAccountInfo",
        }

    # ── 撤单 ─────────────────────────────────────────────────────────

    def cancel_contract(self, contract: str, real: bool = True) -> Dict:
        """按合同号撤单：frida cancel <合同号> [--real]。

        real=True 才真撤；撤后 ths_cli 等 3s 从委托缓存复查状态。
        未确认撤单成功 → 抛 ThsCliError（不谎报）。
        """
        contract = str(contract or "").strip()
        if not contract or contract == "pending":
            raise ThsCliError(f"合同号无效: {contract!r}")
        if not real:
            return self._frida("cancel", contract, timeout=120)
        data = self._frida("cancel", contract, "--real", timeout=180)
        new_status = str(data.get("new_status") or "")
        if not data.get("confirmed_cancelled") and "撤" not in new_status and "废" not in new_status:
            raise ThsCliError(
                f"撤单未确认: 合同号={contract} 状态 {data.get('old_status')} → {new_status or '(无)'}"
            )
        return data

    def cancel(self, order: Order) -> bool:
        """撤单（BaseBroker 接口）。失败返回 False，由调用方决定是否回退旧通道。"""
        try:
            self.cancel_contract(order.broker_order_id, real=True)
            return True
        except ThsCliError as ex:
            print(f"  ⚠️ frida 撤单失败: {ex}")
            return False

    def cancel_all(self) -> bool:
        """全撤：遍历委托列表逐笔撤（frida 无批量撤单命令）。"""
        try:
            orders = self.get_orders()
        except ThsCliError as ex:
            print(f"  ⚠️ frida 取委托失败: {ex}")
            return False
        ok = True
        for o in orders:
            status = o.get("status", "")
            if "成交" in status or "撤" in status:
                continue
            try:
                self.cancel_contract(o["contract"], real=True)
            except ThsCliError as ex:
                print(f"  ⚠️ 撤单失败 合同号={o.get('contract')}: {ex}")
                ok = False
        return ok

    # ── 查询 ─────────────────────────────────────────────────────────

    def query_contract(self, contract: str) -> Optional[Dict]:
        """按合同号查委托，返回 get_orders() 同结构；找不到返回 None。"""
        contract = str(contract or "").strip()
        for o in self.get_orders():
            if o["contract"] == contract:
                return o
        return None

    def query(self, order: Order) -> Optional[Dict]:
        """查订单状态：先按合同号，再按 代码+方向+委托量 匹配。"""
        try:
            orders = self.get_orders()
        except ThsCliError:
            return None
        direction = "买入" if order.is_buy() else "卖出"
        for o in orders:
            if order.broker_order_id and o["contract"] == str(order.broker_order_id):
                return {"status": o["status"], "filled_shares": o["filled_shares"],
                        "avg_fill_price": o["avg_fill_price"], "contract": o["contract"]}
        for o in orders:
            if (o["code"] == order.code and o["direction"] == direction
                    and o["shares"] == order.shares):
                return {"status": o["status"], "filled_shares": o["filled_shares"],
                        "avg_fill_price": o["avg_fill_price"], "contract": o["contract"]}
        return None

    # ── 其余 BaseBroker 接口 ─────────────────────────────────────────

    def execute(self, order: Order) -> ExecutionResult:
        """下单本轮未迁移，原样走 RealBroker（EvolvingSim → trade.py 同一路径）。"""
        from executor import RealBroker
        return RealBroker().execute(order)

    def is_connected(self) -> bool:
        """桥进程 + 模拟仓登录态。"""
        try:
            data = self._frida("status", timeout=30)
            return bool(data.get("ok")) and bool(data.get("active_is_moni"))
        except ThsCliError:
            return False


_broker: Optional[ThsBroker] = None


def get_ths_broker() -> ThsBroker:
    """进程内单例。"""
    global _broker
    if _broker is None:
        _broker = ThsBroker()
    return _broker
