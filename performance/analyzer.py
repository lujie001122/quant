#!/usr/bin/env python3
"""
performance/analyzer.py — 绩效分析引擎

核心能力:
  1. position_cost_trace(code) ★ 持仓成本追溯:
     某标的的完整买入/卖出明细——哪天买的、什么信号触发、
     每笔成本多少、赚了还是亏了。
  2. daily_report(date)      每日交易明细 + 分类汇总
  3. contract_detail(no)    合同编号聚合（一笔委托的多笔成交）
  4. monthly_summary(month) 月度绩效（胜率/盈亏比/分ETF/分信号）
  5. t0_vs_normal_stats()   做T vs 普通 vs 网格对比
  6. equity_curve()         资金曲线
  7. signal_source_stats()  按信号来源统计胜率

数据来源: MySQL（core.repositories 各 Repo），不读 JSON。
"""

import sys
import os
from datetime import date, datetime
from typing import Optional

# 让 performance/ 下的脚本可直接 python3 performance/xxx.py 运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.repositories import (
    DailyEquityRepo,
    DealRepo,
    EntrustRepo,
    PositionStateRepo,
    TradeRepo,
)


def _parse_date(s: str) -> date:
    """'2026-09-17' -> date"""
    return date.fromisoformat(s)


def _round(v, n=2):
    """None-safe 四舍五入"""
    if v is None:
        return None
    return round(float(v), n)


class PerformanceAnalyzer:
    """绩效分析引擎 — 只读查询与聚合，不写业务数据"""

    # ──────────────────────────────────────────
    # ★核心: 持仓成本追溯
    # ──────────────────────────────────────────

    def position_cost_trace(self, code: str) -> dict:
        """持仓成本追溯：某标的的完整买入/卖出明细

        这是本管理端最核心的功能——点开某持仓看到每笔买入:
        哪天买的、什么信号触发、成本多少、赚了还是亏了。

        参数:
          code: ETF代码，如 '515880'

        返回:
          {
            'code': '515880',
            'name': '通信ETF',
            'position': {  # position_state 表镜像（可能为 None）
                'shares': 106600, 'avg_cost': 0.657,
                'entry_avg_cost': 0.668, 'peak_price': 0.689,
                'trailing_stop_price': 0.60632,
                'first_buy_date': '2026-09-10',
                'reached_2pct': True, 'reached_4pct': True, 'reached_6pct': True,
                ...
            },
            'buys': [  # 每笔买入（时间升序）
                {'trade_date': '2026-09-10', 'shares': 50000, 'price': 0.665,
                 'amount': 33250, 'signal_source': 'RSI抄底', 'contract_no': '...'},
                ...
            ],
            'sells': [  # 每笔卖出
                {'trade_date': '2026-09-16', 'shares': 20000, 'price': 0.672,
                 'pnl': 300, 'pnl_pct': 0.0225, 'entry_cost': 0.665,
                 'hold_days': 6, 'signal_source': '趋势止盈', 'contract_no': '...'},
                ...
            ],
            'summary': {
                'total_bought': 106600,     # 累计买入股数
                'total_sold': 20000,        # 累计卖出股数
                'total_cost': 70047,        # 累计买入金额
                'weighted_avg_cost': 0.657, # 买入加权均价
                'total_pnl': 300,           # 累计已实现盈亏
            },
          }
        """
        trades = TradeRepo.list_by_code(code)
        buys = [t for t in trades if t.direction == 'buy']
        sells = [t for t in trades if t.direction == 'sell']
        pos = PositionStateRepo.get(code)

        total_bought = sum(int(b.shares or 0) for b in buys)
        total_cost = sum(float(b.amount or 0) for b in buys)
        weighted_avg = total_cost / total_bought if total_bought else 0.0

        return {
            'code': code,
            'name': (pos.name if pos else '') or (buys[0].name if buys else '') or code,
            'position': self._pos_to_dict(pos) if pos else None,
            'buys': [self._trade_to_dict(t) for t in buys],
            'sells': [self._trade_to_dict(t) for t in sells],
            'summary': {
                'total_bought': total_bought,
                'total_sold': sum(int(s.shares or 0) for s in sells),
                'total_cost': _round(total_cost),
                'weighted_avg_cost': _round(weighted_avg, 4),
                'total_pnl': _round(sum(float(s.pnl or 0) for s in sells)),
                'total_fee': _round(sum(float(t.fee or 0) for t in trades)),
                'buy_count': len(buys),
                'sell_count': len(sells),
            },
        }

    # ──────────────────────────────────────────
    # 每日报告
    # ──────────────────────────────────────────

    def daily_report(self, date_str: str) -> dict:
        """每日交易明细 + 分类汇总

        参数: date_str 'YYYY-MM-DD'
        返回: {date, trades: [...], by_category: [...], totals: {...}}
        """
        d = _parse_date(date_str)
        trades = TradeRepo.list_by_date(d)
        by_category = TradeRepo.stats_by_category(d)

        total_buy_amount = sum(float(t.amount or 0) for t in trades if t.direction == 'buy')
        total_sell_amount = sum(float(t.amount or 0) for t in trades if t.direction == 'sell')
        realized_pnl = sum(float(t.pnl or 0) for t in trades if t.direction == 'sell')
        total_fee = sum(float(t.fee or 0) for t in trades)

        return {
            'date': date_str,
            'trades': [self._trade_to_dict(t) for t in trades],
            'by_category': by_category,
            'totals': {
                'total_trades': len(trades),
                'buy_count': sum(1 for t in trades if t.direction == 'buy'),
                'sell_count': sum(1 for t in trades if t.direction == 'sell'),
                'total_buy_amount': _round(total_buy_amount),
                'total_sell_amount': _round(total_sell_amount),
                'realized_pnl': _round(realized_pnl),
                'total_fee': _round(total_fee),
            },
        }

    # ──────────────────────────────────────────
    # 合同编号聚合
    # ──────────────────────────────────────────

    def contract_detail(self, contract_no: str) -> dict:
        """合同编号聚合：一笔委托的多笔成交

        返回: {entrust: {...}|None, agg: {...}|None, deals: [...]}
        """
        ent = EntrustRepo.get_by_contract(contract_no)
        agg = DealRepo.aggregate_by_contract(contract_no)
        deals = DealRepo.list_by_contract(contract_no)

        return {
            'entrust': self._entrust_to_dict(ent) if ent else None,
            'agg': agg,
            'deals': [self._deal_to_dict(x) for x in deals],
        }

    # ──────────────────────────────────────────
    # 月度绩效
    # ──────────────────────────────────────────

    def monthly_summary(self, year_month: str) -> dict:
        """月度绩效：胜率/盈亏比/分ETF/分信号

        参数: year_month 'YYYY-MM'
        """
        y, m = year_month.split('-')
        start = date(int(y), int(m), 1)
        end = date(int(y) + 1, 1, 1) if int(m) == 12 else date(int(y), int(m) + 1, 1)
        # stats_by_code 区间含两端，用月末
        end_inclusive = date.fromordinal(end.toordinal() - 1)

        return {
            'month': year_month,
            'overall': TradeRepo.monthly_overall(year_month),
            'by_code': TradeRepo.stats_by_code(start, end_inclusive),
            'by_signal': TradeRepo.stats_by_signal(start, end_inclusive),
        }

    # ──────────────────────────────────────────
    # 做T vs 普通 vs 网格
    # ──────────────────────────────────────────

    def t0_vs_normal_stats(self, start: str, end: str) -> dict:
        """做T vs 普通 vs 网格 vs 卖底仓 对比统计

        参数: start/end 'YYYY-MM-DD'（含两端）
        返回: {categories: [{category, trades, sell_count, pnl, pair_pnl, win_rate}], ...}
        """
        s, e = _parse_date(start), _parse_date(end)
        trades = []
        # 复用 TradeRepo 查询（全区间按类别过滤在内存聚合，数据量小）
        from core.db import get_session
        from core.models import Trade
        with get_session() as sess:
            from sqlalchemy import select
            stmt = (select(Trade)
                    .where(Trade.trade_date >= s, Trade.trade_date <= e)
                    .order_by(Trade.trade_date, Trade.id))
            trades = list(sess.scalars(stmt))

        cats = {}
        for t in trades:
            cat = t.trade_category or 'normal'
            c = cats.setdefault(cat, {
                'category': cat, 'trades': 0, 'buy_count': 0, 'sell_count': 0,
                'buy_amount': 0.0, 'sell_amount': 0.0, 'pnl': 0.0,
                'pair_pnl': 0.0, 'wins': 0, 'losses': 0,
            })
            c['trades'] += 1
            if t.direction == 'buy':
                c['buy_count'] += 1
                c['buy_amount'] += float(t.amount or 0)
            else:
                c['sell_count'] += 1
                c['sell_amount'] += float(t.amount or 0)
                c['pnl'] += float(t.pnl or 0)
                if float(t.pnl or 0) > 0:
                    c['wins'] += 1
                elif float(t.pnl or 0) < 0:
                    c['losses'] += 1
            if t.pair_pnl is not None:
                c['pair_pnl'] += float(t.pair_pnl)

        result = []
        for cat in ('normal', 't0', 'grid', 'sell_core'):
            if cat in cats:
                c = cats[cat]
                decided = c['wins'] + c['losses']
                result.append({
                    'category': cat,
                    'trades': c['trades'],
                    'buy_count': c['buy_count'],
                    'sell_count': c['sell_count'],
                    'buy_amount': _round(c['buy_amount']),
                    'sell_amount': _round(c['sell_amount']),
                    'pnl': _round(c['pnl']),
                    'pair_pnl': _round(c['pair_pnl']),
                    'win_rate': _round(c['wins'] / decided, 4) if decided else 0.0,
                })
        return {'start': start, 'end': end, 'categories': result}

    # ──────────────────────────────────────────
    # 资金曲线
    # ──────────────────────────────────────────

    def equity_curve(self, start: str = None, end: str = None) -> dict:
        """资金曲线（daily_equity 表）

        参数: start/end 'YYYY-MM-DD'（可选）
        返回: {dates: [...], total_assets: [...], daily_pnls: [...],
               daily_returns: [...], max_drawdown: x, total_return: x}
        """
        s = _parse_date(start) if start else None
        e = _parse_date(end) if end else None
        rows = DailyEquityRepo.range(s, e)

        dates = [r.trade_date.isoformat() for r in rows]
        assets = [float(r.total_asset or 0) for r in rows]
        pnls = [float(r.daily_pnl or 0) for r in rows]
        returns = [float(r.daily_return or 0) for r in rows]

        max_dd = 0.0
        peak = 0.0
        for a in assets:
            peak = max(peak, a)
            if peak > 0:
                max_dd = min(max_dd, (a - peak) / peak)

        total_return = (assets[-1] / assets[0] - 1) if assets and assets[0] else 0.0

        return {
            'dates': dates,
            'total_assets': [_round(a) for a in assets],
            'daily_pnls': [_round(p) for p in pnls],
            'daily_returns': returns,
            'max_drawdown': _round(max_dd, 4),
            'total_return': _round(total_return, 4),
        }

    # ──────────────────────────────────────────
    # 信号来源胜率
    # ──────────────────────────────────────────

    def signal_source_stats(self, start: str, end: str) -> list:
        """按信号来源统计胜率（卖出交易）

        参数: start/end 'YYYY-MM-DD'（含两端）
        """
        return TradeRepo.stats_by_signal(_parse_date(start), _parse_date(end))

    # ──────────────────────────────────────────
    # 内部: 对象转 dict
    # ──────────────────────────────────────────

    @staticmethod
    def _trade_to_dict(t) -> dict:
        """Trade ORM 对象 -> 展示 dict（日期/数值全部转 Python 原生类型）"""
        return {
            'id': t.id,
            'trade_date': t.trade_date.isoformat() if t.trade_date else None,
            'trade_time': t.trade_time.isoformat() if t.trade_time else None,
            'code': t.code,
            'name': t.name or '',
            'direction': t.direction,
            'shares': int(t.shares or 0),
            'price': _round(t.price, 4),
            'amount': _round(t.amount),
            'fee': _round(t.fee),
            'pnl': _round(t.pnl),
            'pnl_pct': _round(t.pnl_pct, 4),
            'contract_no': t.contract_no,
            'deal_no': t.deal_no,
            'signal_date': t.signal_date.isoformat() if t.signal_date else None,
            'signal_source': t.signal_source or '',
            'strategy_name': t.strategy_name or '',
            'entry_cost': _round(t.entry_cost, 4),
            'hold_days': t.hold_days,
            'trade_category': t.trade_category or 'normal',
            'pair_deal_no': t.pair_deal_no,
            'pair_pnl': _round(t.pair_pnl),
            'account': t.account or 'main',
            'reason': t.reason or '',
        }

    @staticmethod
    def _pos_to_dict(p) -> dict:
        """PositionState ORM 对象 -> 展示 dict"""
        return {
            'code': p.code,
            'name': p.name or '',
            'shares': int(p.shares or 0),
            'core_shares': int(p.core_shares or 0),
            'grid_shares': int(p.grid_shares or 0),
            'active_shares': int(p.active_shares or 0),
            'avg_cost': _round(p.avg_cost, 4),
            'entry_avg_cost': _round(p.entry_avg_cost, 4),
            'base_price': _round(p.base_price, 4),
            'peak_price': _round(p.peak_price, 4),
            'trailing_stop_price': _round(p.trailing_stop_price, 5),
            'stop_level': p.stop_level,
            'build_phase': p.build_phase,
            'reached_2pct': bool(p.reached_2pct),
            'reached_4pct': bool(p.reached_4pct),
            'reached_6pct': bool(p.reached_6pct),
            'grid_frozen': bool(p.grid_frozen),
            'cooldown_until': p.cooldown_until.isoformat() if p.cooldown_until else None,
            'empty_days': p.empty_days,
            'prev_macd_status': p.prev_macd_status or '',
            'prev_rsi': _round(p.prev_rsi),
            'first_buy_date': p.first_buy_date.isoformat() if p.first_buy_date else None,
            'liquidate_dates': p.liquidate_dates or [],
            'updated_at': p.updated_at.isoformat() if p.updated_at else None,
        }

    @staticmethod
    def _entrust_to_dict(e) -> dict:
        """Entrust ORM 对象 -> 展示 dict"""
        return {
            'contract_no': e.contract_no,
            'entrust_no': e.entrust_no,
            'code': e.code,
            'name': e.name or '',
            'direction': e.direction,
            'order_price': _round(e.order_price, 4),
            'order_shares': e.order_shares,
            'filled_shares': e.filled_shares,
            'avg_fill_price': _round(e.avg_fill_price, 4),
            'total_fee': _round(e.total_fee),
            'status': e.status or '',
            'trade_category': e.trade_category or 'normal',
            'signal_source': e.signal_source or '',
            'signal_date': e.signal_date.isoformat() if e.signal_date else None,
            'order_time': e.order_time.isoformat() if e.order_time else None,
        }

    @staticmethod
    def _deal_to_dict(x) -> dict:
        """Deal ORM 对象 -> 展示 dict"""
        return {
            'deal_no': x.deal_no,
            'contract_no': x.contract_no,
            'code': x.code,
            'direction': x.direction,
            'deal_price': _round(x.deal_price, 4),
            'deal_shares': x.deal_shares,
            'deal_amount': _round(x.deal_amount),
            'fee': _round(x.fee),
            'deal_time': x.deal_time.isoformat() if x.deal_time else None,
        }


# 全局单例
_analyzer: Optional[PerformanceAnalyzer] = None


def get_analyzer() -> PerformanceAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = PerformanceAnalyzer()
    return _analyzer


if __name__ == '__main__':
    # 快速自测: python3 performance/analyzer.py 515880
    code = sys.argv[1] if len(sys.argv) > 1 else '515880'
    import json
    print(json.dumps(get_analyzer().position_cost_trace(code),
                     ensure_ascii=False, indent=2))
