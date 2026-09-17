#!/usr/bin/env python3
"""
core/repositories.py — 数据访问层（Repository 模式）

每张表一个 Repository 类，封装查询和写入，不写原生 SQL。
所有方法为 @staticmethod，直接调用:

    from core.repositories import TradeRepo
    TradeRepo.insert({...})
    TradeRepo.list_by_code('515880')

去重约定:
  - EntrustRepo.insert_ignore / DealRepo.insert_ignore 用
    INSERT IGNORE（stmt.prefix_with('IGNORE')），靠 contract_no / deal_no 唯一键
  - trades 表由 trade_recorder 逐笔写入，不做去重（同一次成交只 record 一次）

会话管理: 全部通过 core.db.get_session() contextmanager，自动 commit/rollback。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Optional

from sqlalchemy import delete, func, insert, select, update

from core.db import get_session
from core.models import (
    DailyEquity,
    Deal,
    Entrust,
    PositionState,
    ReconcileLog,
    SyncLog,
    Trade,
)


# ══════════════════════════════════════════════
# 1. TradeRepo — 核心 Repository
# ══════════════════════════════════════════════

class TradeRepo:
    """trades 表 — 交易记录主表（含信号溯源）"""

    @staticmethod
    def insert(data: dict) -> int:
        """插入一笔交易，返回 id

        data 字段与 core/models.Trade 一一对应（trade_date/trade_time/code/
        direction/shares 必填，其余可选）。
        """
        with get_session() as s:
            obj = Trade(**data)
            s.add(obj)
            s.flush()
            return obj.id

    @staticmethod
    def list_by_date(d: date) -> List[Trade]:
        """按日期查询当日所有交易"""
        with get_session() as s:
            stmt = select(Trade).where(Trade.trade_date == d).order_by(Trade.trade_time, Trade.id)
            return list(s.scalars(stmt))

    @staticmethod
    def list_by_code(code: str, start: date = None, end: date = None) -> List[Trade]:
        """★核心：按标的查询所有交易（用于持仓明细追溯）

        参数:
          code: ETF代码
          start/end: 可选日期区间（含两端）
        """
        with get_session() as s:
            stmt = select(Trade).where(Trade.code == code)
            if start is not None:
                stmt = stmt.where(Trade.trade_date >= start)
            if end is not None:
                stmt = stmt.where(Trade.trade_date <= end)
            stmt = stmt.order_by(Trade.trade_date, Trade.trade_time, Trade.id)
            return list(s.scalars(stmt))

    @staticmethod
    def stats_by_category(d: date) -> List[dict]:
        """当日按交易分类统计（normal/t0/grid/sell_core）

        返回: [{category, count, buy_count, sell_count, amount, pnl}]
        """
        with get_session() as s:
            stmt = (
                select(
                    Trade.trade_category.label('category'),
                    func.count(Trade.id).label('count'),
                    func.sum(
                        func.if_(Trade.direction == 'buy', 1, 0)
                    ).label('buy_count'),
                    func.sum(
                        func.if_(Trade.direction == 'sell', 1, 0)
                    ).label('sell_count'),
                    func.coalesce(func.sum(Trade.amount), 0).label('amount'),
                    func.coalesce(func.sum(Trade.pnl), 0).label('pnl'),
                )
                .where(Trade.trade_date == d)
                .group_by(Trade.trade_category)
            )
            rows = s.execute(stmt).all()
            return [
                {
                    'category': r.category or 'normal',
                    'count': int(r.count),
                    'buy_count': int(r.buy_count or 0),
                    'sell_count': int(r.sell_count or 0),
                    'amount': float(r.amount or 0),
                    'pnl': float(r.pnl or 0),
                }
                for r in rows
            ]

    @staticmethod
    def monthly_overall(year_month: str) -> dict:
        """月度汇总：总交易、胜率、盈亏比、平均盈亏

        参数:
          year_month: 'YYYY-MM'
        返回: {total_trades, sell_trades, win_count, loss_count, win_rate,
               avg_win, avg_loss, profit_factor, total_pnl, total_fee, net_pnl}
        """
        try:
            y, m = year_month.split('-')
            start = date(int(y), int(m), 1)
            end = date(int(y) + 1, 1, 1) if int(m) == 12 else date(int(y), int(m) + 1, 1)
        except (ValueError, IndexError):
            return {}

        with get_session() as s:
            total = s.scalar(
                select(func.count(Trade.id)).where(
                    Trade.trade_date >= start, Trade.trade_date < end))
            sells = list(s.scalars(
                select(Trade).where(
                    Trade.trade_date >= start, Trade.trade_date < end,
                    Trade.direction == 'sell')))
            fee = s.scalar(
                select(func.coalesce(func.sum(Trade.fee), 0)).where(
                    Trade.trade_date >= start, Trade.trade_date < end))

        pnls = [float(t.pnl or 0) for t in sells]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        profit_factor = abs(avg_win / avg_loss) if avg_loss else (float('inf') if avg_win else 0.0)

        return {
            'month': year_month,
            'total_trades': int(total or 0),
            'sell_trades': len(sells),
            'win_count': len(wins),
            'loss_count': len(losses),
            'win_rate': round(len(wins) / len(pnls), 4) if pnls else 0.0,
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 4) if profit_factor != float('inf') else 'inf',
            'total_pnl': round(total_pnl, 2),
            'total_fee': float(fee or 0),
            'net_pnl': round(total_pnl - float(fee or 0), 2),
        }

    @staticmethod
    def stats_by_code(start: date, end: date) -> List[dict]:
        """分ETF统计（区间内）

        返回: [{code, name, trades, buys, sells, total_buy_amount,
               total_sell_amount, pnl, win_count, loss_count, win_rate}]
        """
        with get_session() as s:
            stmt = (
                select(
                    Trade.code,
                    func.max(Trade.name).label('name'),
                    func.count(Trade.id).label('trades'),
                    func.sum(func.if_(Trade.direction == 'buy', 1, 0)).label('buys'),
                    func.sum(func.if_(Trade.direction == 'sell', 1, 0)).label('sells'),
                    func.coalesce(func.sum(
                        func.if_(Trade.direction == 'buy', Trade.amount, 0)), 0).label('buy_amount'),
                    func.coalesce(func.sum(
                        func.if_(Trade.direction == 'sell', Trade.amount, 0)), 0).label('sell_amount'),
                    func.coalesce(func.sum(Trade.pnl), 0).label('pnl'),
                    func.sum(func.if_(Trade.direction == 'sell',
                                      func.if_(Trade.pnl > 0, 1, 0), 0)).label('win_count'),
                    func.sum(func.if_(Trade.direction == 'sell',
                                      func.if_(Trade.pnl < 0, 1, 0), 0)).label('loss_count'),
                )
                .where(Trade.trade_date >= start, Trade.trade_date <= end)
                .group_by(Trade.code)
                .order_by(func.coalesce(func.sum(Trade.pnl), 0))
            )
            rows = s.execute(stmt).all()
            result = []
            for r in rows:
                n_decided = int(r.win_count or 0) + int(r.loss_count or 0)
                result.append({
                    'code': r.code,
                    'name': r.name or '',
                    'trades': int(r.trades),
                    'buys': int(r.buys or 0),
                    'sells': int(r.sells or 0),
                    'total_buy_amount': float(r.buy_amount or 0),
                    'total_sell_amount': float(r.sell_amount or 0),
                    'pnl': round(float(r.pnl or 0), 2),
                    'win_count': int(r.win_count or 0),
                    'loss_count': int(r.loss_count or 0),
                    'win_rate': round(int(r.win_count or 0) / n_decided, 4) if n_decided else 0.0,
                })
            return result

    @staticmethod
    def stats_by_signal(start: date, end: date) -> List[dict]:
        """按信号来源统计胜率（统计卖出交易的盈亏）

        返回: [{signal_source, sell_count, win_count, loss_count,
               win_rate, total_pnl, avg_pnl}]
        """
        with get_session() as s:
            stmt = (
                select(
                    Trade.signal_source,
                    func.count(Trade.id).label('sell_count'),
                    func.sum(func.if_(Trade.pnl > 0, 1, 0)).label('win_count'),
                    func.sum(func.if_(Trade.pnl < 0, 1, 0)).label('loss_count'),
                    func.coalesce(func.sum(Trade.pnl), 0).label('total_pnl'),
                    func.coalesce(func.avg(Trade.pnl), 0).label('avg_pnl'),
                )
                .where(Trade.trade_date >= start, Trade.trade_date <= end,
                       Trade.direction == 'sell')
                .group_by(Trade.signal_source)
            )
            rows = s.execute(stmt).all()
            result = []
            for r in rows:
                n = int(r.win_count or 0) + int(r.loss_count or 0)
                result.append({
                    'signal_source': r.signal_source or '未标记',
                    'sell_count': int(r.sell_count),
                    'win_count': int(r.win_count or 0),
                    'loss_count': int(r.loss_count or 0),
                    'win_rate': round(int(r.win_count or 0) / n, 4) if n else 0.0,
                    'total_pnl': round(float(r.total_pnl or 0), 2),
                    'avg_pnl': round(float(r.avg_pnl or 0), 2),
                })
            result.sort(key=lambda x: x['total_pnl'], reverse=True)
            return result


# ══════════════════════════════════════════════
# 2. EntrustRepo — 委托表
# ══════════════════════════════════════════════

class EntrustRepo:
    """entrusts 表 — 同花顺委托"""

    @staticmethod
    def insert_ignore(data: dict) -> int:
        """INSERT IGNORE 插入（contract_no 重复则跳过），返回插入行数"""
        with get_session() as s:
            stmt = insert(Entrust).values(**data).prefix_with('IGNORE')
            result = s.execute(stmt)
            return result.rowcount

    @staticmethod
    def get_by_contract(contract_no: str) -> Optional[Entrust]:
        """按合同编号查询"""
        with get_session() as s:
            return s.scalar(select(Entrust).where(Entrust.contract_no == contract_no))

    @staticmethod
    def list_by_date(d: date) -> List[Entrust]:
        """按委托日期查询"""
        with get_session() as s:
            stmt = select(Entrust).where(
                func.date(Entrust.order_time) == d).order_by(Entrust.order_time)
            return list(s.scalars(stmt))

    @staticmethod
    def update_fill_info(contract_no: str, filled_shares: int,
                         avg_fill_price: Any, total_fee: Any = None,
                         status: str = None) -> int:
        """回填成交信息（filled_shares / avg_fill_price / fee / status）"""
        values: dict = {
            'filled_shares': filled_shares,
            'avg_fill_price': avg_fill_price,
        }
        if total_fee is not None:
            values['total_fee'] = total_fee
        if status is not None:
            values['status'] = status
        with get_session() as s:
            stmt = update(Entrust).where(Entrust.contract_no == contract_no).values(**values)
            result = s.execute(stmt)
            return result.rowcount


# ══════════════════════════════════════════════
# 3. DealRepo — 成交明细表
# ══════════════════════════════════════════════

class DealRepo:
    """deals 表 — 同花顺成交明细"""

    @staticmethod
    def insert_ignore(data: dict) -> int:
        """INSERT IGNORE 插入（deal_no 重复则跳过），返回插入行数"""
        with get_session() as s:
            stmt = insert(Deal).values(**data).prefix_with('IGNORE')
            result = s.execute(stmt)
            return result.rowcount

    @staticmethod
    def list_by_contract(contract_no: str) -> List[Deal]:
        """某合同编号下的所有成交（一笔委托多笔成交）"""
        with get_session() as s:
            stmt = select(Deal).where(Deal.contract_no == contract_no).order_by(Deal.deal_time)
            return list(s.scalars(stmt))

    @staticmethod
    def list_by_date(d: date) -> List[Deal]:
        """按成交日期查询"""
        with get_session() as s:
            stmt = select(Deal).where(func.date(Deal.deal_time) == d).order_by(Deal.deal_time)
            return list(s.scalars(stmt))

    @staticmethod
    def aggregate_by_contract(contract_no: str) -> Optional[dict]:
        """按合同编号聚合: 总成交股数 / 加权均价 / 总手续费"""
        with get_session() as s:
            stmt = (
                select(
                    func.coalesce(func.sum(Deal.deal_shares), 0).label('total_shares'),
                    func.coalesce(func.sum(Deal.deal_amount), 0).label('total_amount'),
                    func.coalesce(func.sum(Deal.fee), 0).label('total_fee'),
                    func.count(Deal.id).label('deal_count'),
                )
                .where(Deal.contract_no == contract_no)
            )
            r = s.execute(stmt).one()
            total_shares = int(r.total_shares or 0)
            total_amount = float(r.total_amount or 0)
            return {
                'contract_no': contract_no,
                'deal_count': int(r.deal_count),
                'total_shares': total_shares,
                'total_amount': round(total_amount, 2),
                'avg_fill_price': round(total_amount / total_shares, 4) if total_shares else None,
                'total_fee': float(r.total_fee or 0),
            }


# ══════════════════════════════════════════════
# 4. PositionStateRepo — 持仓状态表
# ══════════════════════════════════════════════

class PositionStateRepo:
    """position_state 表 — portfolio.json _signal_state 的镜像"""

    @staticmethod
    def upsert(data: dict) -> None:
        """插入或更新（按 code 主键 upsert）"""
        code = data.get('code')
        if not code:
            raise ValueError("upsert 缺少 code")
        with get_session() as s:
            obj = s.get(PositionState, code)
            if obj is None:
                s.add(PositionState(**data))
            else:
                for k, v in data.items():
                    if k != 'code':
                        setattr(obj, k, v)

    @staticmethod
    def get(code: str) -> Optional[PositionState]:
        """查询单个持仓状态"""
        with get_session() as s:
            return s.get(PositionState, code)

    @staticmethod
    def list_active() -> List[PositionState]:
        """列出所有 shares>0 的活跃持仓"""
        with get_session() as s:
            stmt = select(PositionState).where(PositionState.shares > 0)
            return list(s.scalars(stmt))

    @staticmethod
    def reset(code: str = None) -> int:
        """清空持仓状态（code=None 清空全部），返回删除行数。慎用。"""
        with get_session() as s:
            stmt = delete(PositionState)
            if code:
                stmt = stmt.where(PositionState.code == code)
            result = s.execute(stmt)
            return result.rowcount


# ══════════════════════════════════════════════
# 5. DailyEquityRepo — 每日净值表
# ══════════════════════════════════════════════

class DailyEquityRepo:
    """daily_equity 表 — 资金曲线"""

    @staticmethod
    def upsert(data: dict) -> None:
        """插入或更新（按 trade_date 主键 upsert）"""
        d = data.get('trade_date')
        if not d:
            raise ValueError("upsert 缺少 trade_date")
        if isinstance(d, str):
            data = dict(data)
            data['trade_date'] = date.fromisoformat(d)
        with get_session() as s:
            obj = s.get(DailyEquity, data['trade_date'])
            if obj is None:
                s.add(DailyEquity(**data))
            else:
                for k, v in data.items():
                    if k != 'trade_date':
                        setattr(obj, k, v)

    @staticmethod
    def range(start: date = None, end: date = None) -> List[DailyEquity]:
        """查询区间净值（升序）"""
        with get_session() as s:
            stmt = select(DailyEquity)
            if start is not None:
                stmt = stmt.where(DailyEquity.trade_date >= start)
            if end is not None:
                stmt = stmt.where(DailyEquity.trade_date <= end)
            stmt = stmt.order_by(DailyEquity.trade_date)
            return list(s.scalars(stmt))


# ══════════════════════════════════════════════
# 6. ReconcileLogRepo — 对账记录表
# ══════════════════════════════════════════════

class ReconcileLogRepo:
    """reconcile_log 表"""

    @staticmethod
    def insert(data: dict) -> int:
        """插入一条对账记录，返回 id"""
        with get_session() as s:
            obj = ReconcileLog(**data)
            s.add(obj)
            s.flush()
            return obj.id

    @staticmethod
    def list_by_date(d: date) -> List[ReconcileLog]:
        """查询某日对账记录"""
        with get_session() as s:
            stmt = select(ReconcileLog).where(ReconcileLog.reconcile_date == d)
            return list(s.scalars(stmt))


# ══════════════════════════════════════════════
# 7. SyncLogRepo — 同步日志表
# ══════════════════════════════════════════════

class SyncLogRepo:
    """sync_log 表"""

    @staticmethod
    def insert(data: dict) -> int:
        """插入同步日志，返回 id（开始时调用）"""
        with get_session() as s:
            obj = SyncLog(**data)
            s.add(obj)
            s.flush()
            return obj.id

    @staticmethod
    def update(log_id: int, data: dict) -> int:
        """更新同步日志（结束时回写 inserted/failed/finished_at）"""
        with get_session() as s:
            obj = s.get(SyncLog, log_id)
            if obj is None:
                return 0
            for k, v in data.items():
                setattr(obj, k, v)
            return 1
