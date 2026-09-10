#!/usr/bin/env python3
"""
core/trade_recorder.py — 交易记录 + 对账 + 绩效报告

解决问题:
  - 交易记录散落在 orders/ 目录的JSON文件中，无统一查询接口
  - 无日终对账机制，实际成交与系统状态可能不一致
  - 无绩效统计，盈亏分析依赖手工

核心功能:
  1. record: 记录交易（成交后自动调用）
  2. get_recent_trades: 查询最近交易
  3. daily_summary: 日终汇总
  4. performance_report: 绩效报告（胜率/盈亏比/最大回撤）
  5. reconcile: 对账（系统持仓 vs 实际持仓）

用法:
  from core.trade_recorder import TradeRecorder

  recorder = TradeRecorder()

  # 记录交易
  recorder.record(code="159516", direction="buy", shares=1000, price=0.523, amount=523.0)

  # 日终汇总
  summary = recorder.daily_summary()

  # 绩效报告
  report = recorder.performance_report(days=30)

  # 对账
  diff = recorder.reconcile(system_positions, actual_positions)
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.atomic_writer import atomic_write_json, atomic_read_json


# ══════════════════════════════════════════════
# 交易记录结构
# ══════════════════════════════════════════════

class TradeRecord:
    """一笔交易记录"""

    __slots__ = (
        'timestamp', 'date', 'time', 'code', 'direction',
        'shares', 'price', 'amount', 'fee', 'pnl', 'tag', 'account',
    )

    def __init__(
        self,
        code: str,
        direction: str,
        shares: int,
        price: float,
        amount: float = 0.0,
        fee: float = 0.0,
        pnl: float = 0.0,
        tag: str = "",
        account: str = "main",
        timestamp: Optional[datetime] = None,
    ):
        self.timestamp = timestamp or datetime.now()
        self.date = self.timestamp.strftime("%Y-%m-%d")
        self.time = self.timestamp.strftime("%H:%M:%S")
        self.code = code
        self.direction = direction.lower()  # "buy" / "sell"
        self.shares = shares
        self.price = price
        self.amount = amount if amount > 0 else shares * price
        self.fee = fee
        self.pnl = pnl  # 卖出时记录已实现盈亏
        self.tag = tag  # 标签: "t0" / "grid" / "signal" / "stop_loss" 等
        self.account = account  # "main" / "t0"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "date": self.date,
            "time": self.time,
            "code": self.code,
            "direction": self.direction,
            "shares": self.shares,
            "price": round(self.price, 4),
            "amount": round(self.amount, 2),
            "fee": round(self.fee, 2),
            "pnl": round(self.pnl, 2),
            "tag": self.tag,
            "account": self.account,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        ts = d.get("timestamp", "")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                ts = datetime.now()
        return cls(
            code=d["code"],
            direction=d["direction"],
            shares=d["shares"],
            price=d["price"],
            amount=d.get("amount", 0),
            fee=d.get("fee", 0),
            pnl=d.get("pnl", 0),
            tag=d.get("tag", ""),
            account=d.get("account", "main"),
            timestamp=ts,
        )

    def __repr__(self):
        return f"Trade({self.date} {self.time} {self.direction.upper()} {self.code} {self.shares}@{self.price})"


# ══════════════════════════════════════════════
# 交易记录器
# ══════════════════════════════════════════════

class TradeRecorder:
    """交易记录器 — 统一记录、查询、统计、对账

    参数:
      data_dir: 交易记录存储目录
    """

    def __init__(self, data_dir: str = ""):
        if data_dir:
            self._data_dir = data_dir
        else:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._data_dir = os.path.join(project_root, "data", "trades")

        os.makedirs(self._data_dir, exist_ok=True)

        # 内存缓存（当日交易）
        self._cache: List[TradeRecord] = []
        self._cache_date: str = ""

    # ── 记录 ──

    def record(
        self,
        code: str,
        direction: str,
        shares: int,
        price: float,
        amount: float = 0.0,
        fee: float = 0.0,
        pnl: float = 0.0,
        tag: str = "",
        account: str = "main",
    ) -> TradeRecord:
        """记录一笔交易

        参数:
          code: ETF代码
          direction: "buy" / "sell"
          shares: 股数
          price: 成交价格
          amount: 成交金额
          fee: 手续费
          pnl: 已实现盈亏（卖出时）
          tag: 交易标签
          account: 账户（main/t0）

        返回:
          TradeRecord: 创建的记录
        """
        trade = TradeRecord(
            code=code,
            direction=direction,
            shares=shares,
            price=price,
            amount=amount,
            fee=fee,
            pnl=pnl,
            tag=tag,
            account=account,
        )

        # 更新内存缓存
        today = trade.date
        if today != self._cache_date:
            self._cache = []
            self._cache_date = today
        self._cache.append(trade)

        # 持久化到文件（按日）
        self._append_to_daily_file(trade)

        return trade

    # ── 查询 ──

    def get_recent_trades(
        self,
        count: int = 20,
        code: str = "",
        direction: str = "",
        tag: str = "",
        account: str = "",
        days: int = 0,
    ) -> List[TradeRecord]:
        """查询最近交易

        参数:
          count: 返回数量
          code: 过滤ETF代码
          direction: 过滤方向
          tag: 过滤标签
          account: 过滤账户
          days: 查询最近N天（0=仅当日）

        返回:
          List[TradeRecord]
        """
        trades = self._load_trades(days=days)

        # 过滤
        if code:
            trades = [t for t in trades if t.code == code]
        if direction:
            trades = [t for t in trades if t.direction == direction.lower()]
        if tag:
            trades = [t for t in trades if t.tag == tag]
        if account:
            trades = [t for t in trades if t.account == account]

        # 按时间倒序
        trades.sort(key=lambda t: t.timestamp, reverse=True)
        return trades[:count]

    def get_trades_by_date(self, date_str: str) -> List[TradeRecord]:
        """获取指定日期的交易"""
        return self._load_daily_file(date_str)

    # ── 日终汇总 ──

    def daily_summary(self, date_str: str = "") -> dict:
        """日终汇总

        参数:
          date_str: 日期（默认今天）

        返回:
          dict: {
            date, total_trades, buy_count, sell_count,
            total_buy_amount, total_sell_amount, total_fee,
            realized_pnl, by_code: {code: {buy/sell/amount/pnl}},
            by_tag: {tag: {count/amount/pnl}},
          }
        """
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        trades = self._load_daily_file(date_str)

        summary: Dict[str, Any] = {
            "date": date_str,
            "total_trades": len(trades),
            "buy_count": 0,
            "sell_count": 0,
            "total_buy_amount": 0.0,
            "total_sell_amount": 0.0,
            "total_fee": 0.0,
            "realized_pnl": 0.0,
            "by_code": {},
            "by_tag": {},
        }

        for t in trades:
            is_buy = t.direction == "buy"
            if is_buy:
                summary["buy_count"] += 1
                summary["total_buy_amount"] += t.amount
            else:
                summary["sell_count"] += 1
                summary["total_sell_amount"] += t.amount
                summary["realized_pnl"] += t.pnl

            summary["total_fee"] += t.fee

            # 按代码汇总
            if t.code not in summary["by_code"]:
                summary["by_code"][t.code] = {"buy": 0, "sell": 0, "amount": 0.0, "pnl": 0.0}
            code_stat = summary["by_code"][t.code]
            if is_buy:
                code_stat["buy"] += 1
            else:
                code_stat["sell"] += 1
            code_stat["amount"] += t.amount
            code_stat["pnl"] += t.pnl

            # 按标签汇总
            tag_key = t.tag or "untagged"
            if tag_key not in summary["by_tag"]:
                summary["by_tag"][tag_key] = {"count": 0, "amount": 0.0, "pnl": 0.0}
            summary["by_tag"][tag_key]["count"] += 1
            summary["by_tag"][tag_key]["amount"] += t.amount
            summary["by_tag"][tag_key]["pnl"] += t.pnl

        # 四舍五入
        for key in ["total_buy_amount", "total_sell_amount", "total_fee", "realized_pnl"]:
            summary[key] = round(summary[key], 2)

        return summary

    # ── 绩效报告 ──

    def performance_report(self, days: int = 30) -> dict:
        """绩效报告

        参数:
          days: 统计最近N天

        返回:
          dict: {
            period_days, total_trades, total_pnl, total_fee, net_pnl,
            win_count, loss_count, win_rate,
            avg_win, avg_loss, profit_factor,
            by_code: {code: {trades, pnl, win_rate}},
          }
        """
        trades = self._load_trades(days=days)

        # 只统计有盈亏的卖出交易
        sell_trades = [t for t in trades if t.direction == "sell"]

        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        flat = [t for t in sell_trades if t.pnl == 0]

        total_pnl = sum(t.pnl for t in sell_trades)
        total_fee = sum(t.fee for t in trades)
        net_pnl = total_pnl - total_fee

        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        # 按代码统计
        by_code: Dict[str, dict] = {}
        for t in sell_trades:
            if t.code not in by_code:
                by_code[t.code] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
            by_code[t.code]["trades"] += 1
            by_code[t.code]["pnl"] += t.pnl
            if t.pnl > 0:
                by_code[t.code]["wins"] += 1
            elif t.pnl < 0:
                by_code[t.code]["losses"] += 1

        for code_stat in by_code.values():
            total = code_stat["wins"] + code_stat["losses"]
            code_stat["win_rate"] = round(code_stat["wins"] / total, 4) if total > 0 else 0.0
            code_stat["pnl"] = round(code_stat["pnl"], 2)

        return {
            "period_days": days,
            "total_trades": len(trades),
            "total_sell_trades": len(sell_trades),
            "total_pnl": round(total_pnl, 2),
            "total_fee": round(total_fee, 2),
            "net_pnl": round(net_pnl, 2),
            "win_count": len(wins),
            "loss_count": len(losses),
            "flat_count": len(flat),
            "win_rate": round(len(wins) / len(sell_trades), 4) if sell_trades else 0.0,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 4) if profit_factor != float('inf') else "inf",
            "by_code": by_code,
        }

    # ── 对账 ──

    def reconcile(
        self,
        system_positions: Dict[str, dict],
        actual_positions: Dict[str, dict],
    ) -> dict:
        """对账：比较系统持仓与实际持仓

        参数:
          system_positions: 系统持仓 {code: {shares, avg_cost, market_value}}
          actual_positions: 实际持仓（券商/同花顺） {code: {shares, avg_cost, market_value}}

        返回:
          dict: {
            match: [匹配的代码],
            system_only: [仅系统有的代码],
            actual_only: [仅实际有的代码],
            mismatch: [{code, system_shares, actual_shares, diff}],
            total_diff_value: 总差异金额,
          }
        """
        system_codes = set(system_positions.keys())
        actual_codes = set(actual_positions.keys())

        result: Dict[str, Any] = {
            "match": [],
            "system_only": list(system_codes - actual_codes),
            "actual_only": list(actual_codes - system_codes),
            "mismatch": [],
            "total_diff_value": 0.0,
        }

        for code in system_codes & actual_codes:
            sys_shares = system_positions[code].get("shares", 0)
            act_shares = actual_positions[code].get("shares", 0)

            if sys_shares == act_shares:
                result["match"].append(code)
            else:
                diff = sys_shares - act_shares
                diff_value = abs(diff) * actual_positions[code].get("avg_cost", 0)
                result["mismatch"].append({
                    "code": code,
                    "system_shares": sys_shares,
                    "actual_shares": act_shares,
                    "diff": diff,
                    "diff_value": round(diff_value, 2),
                })
                result["total_diff_value"] += diff_value

        result["total_diff_value"] = round(result["total_diff_value"], 2)
        return result

    # ── 内部方法 ──

    def _daily_file_path(self, date_str: str) -> str:
        """获取每日交易记录文件路径"""
        return os.path.join(self._data_dir, f"trades_{date_str}.json")

    def _append_to_daily_file(self, trade: TradeRecord) -> None:
        """追加交易记录到当日文件"""
        filepath = self._daily_file_path(trade.date)

        # 读取已有记录
        existing = atomic_read_json(filepath, default=[])
        if not isinstance(existing, list):
            existing = []

        existing.append(trade.to_dict())

        # 原子写入
        atomic_write_json(filepath, existing)

    def _load_daily_file(self, date_str: str) -> List[TradeRecord]:
        """加载指定日期的交易记录"""
        # 优先从缓存读取
        if date_str == self._cache_date:
            return list(self._cache)

        filepath = self._daily_file_path(date_str)
        data = atomic_read_json(filepath, default=[])

        if not isinstance(data, list):
            return []

        return [TradeRecord.from_dict(d) for d in data]

    def _load_trades(self, days: int = 0) -> List[TradeRecord]:
        """加载最近N天的交易记录"""
        all_trades: List[TradeRecord] = []

        if days <= 0:
            # 仅当日
            today = datetime.now().strftime("%Y-%m-%d")
            return self._load_daily_file(today)

        for i in range(days):
            date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            all_trades.extend(self._load_daily_file(date_str))

        return all_trades

    # ── 便捷方法 ──

    def today_trades(self) -> List[TradeRecord]:
        """获取今日所有交易"""
        return self.get_recent_trades(count=9999, days=0)

    def today_buy_count(self) -> int:
        """今日买入次数"""
        trades = self.today_trades()
        return sum(1 for t in trades if t.direction == "buy" and t.account == "main")

    def today_t0_count(self) -> int:
        """今日做T次数"""
        trades = self.today_trades()
        return sum(1 for t in trades if t.tag == "t0")

    def today_pnl(self) -> float:
        """今日已实现盈亏"""
        trades = self.today_trades()
        return round(sum(t.pnl for t in trades if t.direction == "sell"), 2)


# ══════════════════════════════════════════════
# 全局单例（可选使用）
# ══════════════════════════════════════════════

_global_recorder: Optional["TradeRecorder"] = None


def get_recorder() -> "TradeRecorder":
    """获取全局交易记录器实例"""
    global _global_recorder
    if _global_recorder is None:
        _global_recorder = TradeRecorder()
    return _global_recorder


def set_recorder(recorder: "TradeRecorder") -> None:
    """设置全局交易记录器实例"""
    global _global_recorder
    _global_recorder = recorder
