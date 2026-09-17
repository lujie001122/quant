#!/usr/bin/env python3
"""
tests/seed_data.py — 测试数据生成

用 ORM 写入模拟数据（515880 通信ETF 的完整买入/卖出链路，
复刻"106600股是怎么买进来的"场景），可直接跑通 position_cost_trace。

⚠️ 会清空并重写 quant 库的 trades / position_state / daily_equity /
   entrusts / deals 表（仅测试库场景使用）。

用法:
  source .venv/bin/activate
  python3 tests/seed_data.py
"""

import os
import sys
from datetime import date, datetime, time
from decimal import Decimal

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.db import get_session, init_db
from core.models import DailyEquity, Deal, Entrust, PositionState, Trade
from core.repositories import (
    DealRepo,
    EntrustRepo,
    PositionStateRepo,
    TradeRepo,
)

# ══════════════════════════════════════════════
# 清空（幂等: 重复跑不累积）
# ══════════════════════════════════════════════

def clear_tables():
    """清空测试涉及的表"""
    with get_session() as s:
        for model in (Trade, PositionState, DailyEquity, Entrust, Deal):
            s.query(model).delete()
    print("✅ 已清空 trades / position_state / daily_equity / entrusts / deals")


# ══════════════════════════════════════════════
# 模拟数据: 515880 通信ETF — 106600股买入链路
# ══════════════════════════════════════════════

def seed_515880():
    """515880 通信ETF: 3笔买入共106600股 + 1笔卖出 + 2笔做T"""
    # ── 买入明细（对应文档示例）──
    buys = [
        # (date, shares, price, signal_source, contract_no, signal_date, category)
        (date(2026, 9, 10), 50000, '0.665', 'RSI抄底',   '20260910001', date(2026, 9, 10), 'normal'),
        (date(2026, 9, 12), 30000, '0.652', '确认加仓',  '20260912003', date(2026, 9, 12), 'normal'),
        (date(2026, 9, 14), 26600, '0.648', '逆势补仓',  '20260914005', date(2026, 9, 14), 'normal'),
    ]
    for d, shares, price, sig, cno, sig_d, cat in buys:
        TradeRepo.insert({
            'trade_date': d, 'trade_time': time(10, 30, 0),
            'code': '515880', 'name': '通信ETF',
            'direction': 'buy', 'shares': shares,
            'price': Decimal(price),
            'amount': (Decimal(price) * shares).quantize(Decimal('0.01')),
            'fee': Decimal('5.00'),
            'contract_no': cno,
            'signal_date': sig_d, 'signal_source': sig,
            'strategy_name': 'ETF轮动',
            'trade_category': cat, 'account': 'main',
            'reason': f'{sig}触发买入',
        })

    # ── 卖出: 趋势止盈 20000股, +2.25% ──
    TradeRepo.insert({
        'trade_date': date(2026, 9, 16), 'trade_time': time(14, 5, 0),
        'code': '515880', 'name': '通信ETF',
        'direction': 'sell', 'shares': 20000,
        'price': Decimal('0.672'),
        'amount': Decimal('13440.00'),
        'fee': Decimal('5.00'),
        'pnl': Decimal('300.00'), 'pnl_pct': Decimal('0.0225'),
        'contract_no': '20260916002',
        'signal_date': date(2026, 9, 16), 'signal_source': '趋势止盈',
        'strategy_name': 'ETF轮动',
        'entry_cost': Decimal('0.665'), 'hold_days': 6,
        'trade_category': 'normal', 'account': 'main',
        'reason': '涨幅达标趋势止盈',
    })

    # ── 做T: 9/17 低买高卖各21300股 ──
    TradeRepo.insert({
        'trade_date': date(2026, 9, 17), 'trade_time': time(11, 11, 58),
        'code': '515880', 'name': '通信ETF',
        'direction': 'buy', 'shares': 21300,
        'price': Decimal('0.688'), 'amount': Decimal('14654.40'),
        'fee': Decimal('0'), 'contract_no': '20260917010',
        'signal_source': '做T-低吸', 'trade_category': 't0',
        'account': 'main', 'reason': '做T买入',
    })
    TradeRepo.insert({
        'trade_date': date(2026, 9, 17), 'trade_time': time(13, 42, 11),
        'code': '515880', 'name': '通信ETF',
        'direction': 'sell', 'shares': 21300,
        'price': Decimal('0.695'), 'amount': Decimal('14803.50'),
        'fee': Decimal('0'), 'pnl': Decimal('149.10'), 'pnl_pct': Decimal('0.0102'),
        'contract_no': '20260917011', 'pair_deal_no': '20260917010',
        'pair_pnl': Decimal('149.10'),
        'signal_source': '做T-高抛', 'trade_category': 't0',
        'account': 'main', 'reason': '做T卖出',
    })

    # ── 持仓状态（portfolio _signal_state 镜像）──
    PositionStateRepo.upsert({
        'code': '515880', 'name': '通信ETF',
        'shares': 106600, 'core_shares': 106600,
        'avg_cost': Decimal('0.657'), 'entry_avg_cost': Decimal('0.668'),
        'base_price': Decimal('0.657'), 'peak_price': Decimal('0.689'),
        'trailing_stop_price': Decimal('0.60632'),
        'stop_level': 0, 'build_phase': 2,
        'reached_2pct': True, 'reached_4pct': True, 'reached_6pct': True,
        'grid_frozen': False, 'empty_days': 0,
        'prev_macd_status': '红柱放大', 'prev_rsi': Decimal('55.41'),
        'first_buy_date': date(2026, 9, 10),
        'liquidate_dates': [],
    })
    print("✅ 515880 通信ETF: 4买2卖 + position_state")


# ══════════════════════════════════════════════
# 模拟数据: 其他ETF（丰富统计维度）
# ══════════════════════════════════════════════

def seed_others():
    """512400 有色ETF / 159981 能源化工ETF / 159532 中证2000ETF"""
    # 512400 有色ETF: 买入亏损卖出
    TradeRepo.insert({
        'trade_date': date(2026, 9, 11), 'trade_time': time(10, 0, 0),
        'code': '512400', 'name': '有色ETF',
        'direction': 'buy', 'shares': 25700,
        'price': Decimal('1.828'), 'amount': Decimal('46979.60'),
        'fee': Decimal('5.00'), 'contract_no': '20260911001',
        'signal_date': date(2026, 9, 11), 'signal_source': 'RSI抄底',
        'trade_category': 'normal', 'account': 'main',
    })
    TradeRepo.insert({
        'trade_date': date(2026, 9, 15), 'trade_time': time(9, 45, 0),
        'code': '512400', 'name': '有色ETF',
        'direction': 'sell', 'shares': 5000,
        'price': Decimal('1.705'), 'amount': Decimal('8525.00'),
        'fee': Decimal('5.00'), 'pnl': Decimal('-615.00'), 'pnl_pct': Decimal('-0.0672'),
        'contract_no': '20260915001', 'signal_source': '均价止损',
        'entry_cost': Decimal('1.828'), 'hold_days': 4,
        'trade_category': 'normal', 'account': 'main',
    })
    PositionStateRepo.upsert({
        'code': '512400', 'name': '有色ETF', 'shares': 20700,
        'avg_cost': Decimal('1.828'), 'entry_avg_cost': Decimal('1.828'),
        'peak_price': Decimal('1.900'), 'first_buy_date': date(2026, 9, 11),
        'reached_2pct': False, 'reached_4pct': False, 'reached_6pct': False,
    })

    # 159981 能源化工ETF: 网格交易
    TradeRepo.insert({
        'trade_date': date(2026, 9, 12), 'trade_time': time(10, 15, 0),
        'code': '159981', 'name': '能源化工ETF',
        'direction': 'buy', 'shares': 9800,
        'price': Decimal('1.791'), 'amount': Decimal('17551.80'),
        'fee': Decimal('5.00'), 'contract_no': '20260912001',
        'signal_date': date(2026, 9, 12), 'signal_source': '网格买入',
        'trade_category': 'grid', 'account': 'main',
    })
    TradeRepo.insert({
        'trade_date': date(2026, 9, 16), 'trade_time': time(10, 30, 0),
        'code': '159981', 'name': '能源化工ETF',
        'direction': 'sell', 'shares': 9800,
        'price': Decimal('1.815'), 'amount': Decimal('17787.00'),
        'fee': Decimal('5.00'), 'pnl': Decimal('225.20'), 'pnl_pct': Decimal('0.0128'),
        'contract_no': '20260916001', 'signal_source': '网格卖出',
        'entry_cost': Decimal('1.791'), 'hold_days': 4,
        'trade_category': 'grid', 'account': 'main',
    })
    PositionStateRepo.upsert({
        'code': '159981', 'name': '能源化工ETF', 'shares': 15100,
        'avg_cost': Decimal('1.673'), 'entry_avg_cost': Decimal('1.673'),
        'base_price': Decimal('1.771'), 'peak_price': Decimal('1.850'),
        'first_buy_date': date(2026, 9, 10), 'reached_2pct': True,
        'reached_4pct': False, 'reached_6pct': False,
    })
    print("✅ 512400 / 159981: 亏损卖出 + 网格交易")


# ══════════════════════════════════════════════
# 模拟数据: entrusts + deals（同花顺链路）
# ══════════════════════════════════════════════

def seed_ths():
    """委托+成交（验证合同编号聚合与 INSERT IGNORE 去重）"""
    # 一笔委托拆两笔成交
    EntrustRepo.insert_ignore({
        'contract_no': '20260910001', 'code': '515880',
        'direction': '买入', 'order_price': Decimal('0.665'),
        'order_shares': 50000, 'status': '已成',
        'trade_category': 'normal', 'signal_source': 'RSI抄底',
        'order_time': datetime(2026, 9, 10, 10, 29, 50),
    })
    DealRepo.insert_ignore({
        'deal_no': 'D20260910001A', 'contract_no': '20260910001',
        'code': '515880', 'direction': '买入',
        'deal_price': Decimal('0.665'), 'deal_shares': 30000,
        'deal_amount': Decimal('19950.00'), 'fee': Decimal('2.50'),
        'deal_time': datetime(2026, 9, 10, 10, 30, 0),
    })
    DealRepo.insert_ignore({
        'deal_no': 'D20260910001B', 'contract_no': '20260910001',
        'code': '515880', 'direction': '买入',
        'deal_price': Decimal('0.665'), 'deal_shares': 20000,
        'deal_amount': Decimal('13300.00'), 'fee': Decimal('2.50'),
        'deal_time': datetime(2026, 9, 10, 10, 30, 1),
    })
    # 验证去重: 重复插入同一 deal_no 应被 IGNORE
    n = DealRepo.insert_ignore({
        'deal_no': 'D20260910001A', 'contract_no': '20260910001',
        'code': '515880', 'direction': '买入',
        'deal_price': Decimal('0.665'), 'deal_shares': 30000,
        'deal_amount': Decimal('19950.00'), 'fee': Decimal('2.50'),
        'deal_time': datetime(2026, 9, 10, 10, 30, 0),
    })
    assert n == 0, f"INSERT IGNORE 去重失效! rowcount={n}"
    print("✅ entrusts/deals: 一委托两成交 + INSERT IGNORE 去重验证通过")


# ══════════════════════════════════════════════
# 模拟数据: daily_equity（资金曲线）
# ══════════════════════════════════════════════

def seed_equity():
    """5天净值（总资产 / 当日盈亏 / 收益率 / 回撤）"""
    rows = [
        # (date, total_asset, cash, market_value, daily_pnl, daily_return)
        (date(2026, 9, 11), 216500, 61000, 155500, None, None),
        (date(2026, 9, 12), 217200, 61500, 155700, 700, 0.0032),
        (date(2026, 9, 14), 216800, 61200, 155600, -400, -0.0018),
        (date(2026, 9, 15), 217900, 61800, 156100, 1100, 0.0051),
        (date(2026, 9, 16), 218500, 62200, 156300, 600, 0.0028),
    ]
    first = rows[0][1]
    peak, max_dd = 0.0, 0.0
    for d, ta, cash, mv, dpnl, dret in rows:
        peak = max(peak, ta)
        if peak > 0:
            max_dd = min(max_dd, (ta - peak) / peak)
        DailyEquityRepo_upsert = None  # noqa — 见下方直接调用
        from core.repositories import DailyEquityRepo
        DailyEquityRepo.upsert({
            'trade_date': d, 'total_asset': Decimal(str(ta)),
            'cash': Decimal(str(cash)), 'market_value': Decimal(str(mv)),
            'daily_pnl': Decimal(str(dpnl)) if dpnl is not None else None,
            'daily_return': Decimal(str(dret)) if dret is not None else None,
            'cumulative_return': Decimal(str(round(ta / first - 1, 4))),
            'max_drawdown': Decimal(str(round(max_dd, 4))),
        })
    print("✅ daily_equity: 5天净值曲线")


# ══════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════

def main():
    init_db()          # 幂等建表
    clear_tables()     # 清空旧测试数据
    seed_515880()      # ★核心: 106600股买入链路
    seed_others()      # 统计维度
    seed_ths()         # 同花顺委托/成交
    seed_equity()      # 资金曲线
    print("\n═══════════════════════════════════════════")
    print("  测试数据就绪! 验证命令:")
    print("  python3 performance/report_cli.py position --code=515880")
    print("  python3 performance/report_cli.py daily --date=2026-09-17")
    print("  python3 performance/report_cli.py monthly --month=2026-09")
    print("  python3 tests/test_analyzer.py")
    print("═══════════════════════════════════════════")


if __name__ == '__main__':
    main()
